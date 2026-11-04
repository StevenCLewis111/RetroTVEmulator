"""Health monitor: watches every thread, the CPU, memory, locks, queues and the app's key jobs, and writes what it sees to app_log.txt so one test log shows where the time went and what got stuck."""

import collections
import dis
import functools
import gc
import logging
import os
import re
import sys
import threading
import time
import traceback

log = logging.getLogger("health_monitor")

try:
    import performance_governor as _governor
except Exception as _governor_import_error:
    _governor = None
    log.warning("[HEALTH] could not import performance_governor, using the fallback values written in health_monitor.py: %s", _governor_import_error)

try:
    import psutil as _psutil
except Exception as _psutil_import_error:
    _psutil = None
    log.warning("[HEALTH] psutil is not available, the CPU and memory lines are left out: %s", _psutil_import_error)

# ============================================================================
# SECTION: Settings
# ============================================================================

# WARNING: the fallback values below must match the HEALTH_ values in performance_governor.py; they are used only when that module fails to import.
_ENABLED = _governor.HEALTH_MONITOR_ENABLED if _governor is not None else True
_SAMPLE_S = (_governor.HEALTH_SAMPLE_MS if _governor is not None else 50) / 1000.0
_REPORT_S = _governor.HEALTH_REPORT_SECONDS if _governor is not None else 60.0
_RING_S = _governor.HEALTH_RING_SECONDS if _governor is not None else 15.0
_EXPLAIN_GAP_MS = _governor.HEALTH_EXPLAIN_GAP_MS if _governor is not None else 100
_EXPLAIN_MAX_PER_MINUTE = _governor.HEALTH_EXPLAIN_MAX_PER_MINUTE if _governor is not None else 20
_STUCK_S = _governor.HEALTH_STUCK_SECONDS if _governor is not None else 10.0
_SCREEN_STUCK_S = _governor.HEALTH_SCREEN_STUCK_SECONDS if _governor is not None else 3.0
_JOB_WAIT_WARN_S = _governor.HEALTH_JOB_WAIT_WARN_SECONDS if _governor is not None else 120.0
_DUMP_MIN_GAP_S = _governor.HEALTH_DUMP_MIN_GAP_SECONDS if _governor is not None else 60.0
_LOCK_HOLD_WARN_S = (_governor.HEALTH_LOCK_HOLD_WARN_MS if _governor is not None else 500) / 1000.0
_PROFILE_TOP = _governor.HEALTH_PROFILE_TOP if _governor is not None else 5
_NEVER_STUCK = tuple(_governor.HEALTH_NEVER_STUCK_THREADS) if _governor is not None else (
    "OsKeyHookPump", "OsKeyHookWatchdog", "retro-tv-listener", "_readerthread", "_monitor", "VlcLogWriter", "SettingsWriter", "HealthMonitor", "HealthReport")
_JOB_STARTS = tuple(_governor.HEALTH_JOB_THREAD_STARTS) if _governor is not None else (
    "content-activate", "vlc-open", "vlc-stop", "pre-show-picbox", "first-scan-picbox", "prime-", "NextItemPrep", "HubRowSync",
    "NewChannelFirstShow", "_probe_and_patch_durations", "compressor-bake")

_APP_MODULES = frozenset((
    "retro_tv_emulator", "__main__", "media", "media_library", "ui", "menu_nav", "game_deck", "libretro_core", "visualizers",
    "performance_governor", "broadcast_clock", "audio_analysis", "health_monitor", "d3d11_gpu_frame", "d3d11_gpu_gate",
    "d3d11_gpu_reference", "server_discovery", "server_sync", "server_service", "auto_updater", "update_helper"))
_WAIT_FILES = frozenset(("threading", "queue", "selectors", "subprocess", "socket", "socketserver", "ssl", "asyncio"))
_WAIT_NAMES = frozenset(("sleep", "wait", "join", "get", "acquire", "accept", "recv", "recv_into", "recvfrom", "select", "readline",
                         "communicate", "tick", "wait_for", "GetMessageW", "GetMessage", "WaitForSingleObject", "WaitForMultipleObjects"))

# ============================================================================
# SECTION: Thread names and code places
# ============================================================================

_GROUP_TARGET = re.compile(r"^Thread-\d+ \((?P<target>[^)]*)\)$")
_GROUP_NUMBER = re.compile(r"[-_]?\d+$")


def thread_group(name):
    """The family a thread belongs to: the target of an unnamed thread, or the name without its trailing number (loud-win_3 is loud-win)."""
    match = _GROUP_TARGET.match(name or "")
    if match:
        return match.group("target")
    return _GROUP_NUMBER.sub("", name or "") or (name or "?")


def thread_groups_text(threads=None):
    """Every alive thread by family with a count, the biggest families first (content-activate x3, loud-win x5)."""
    counts = collections.Counter(thread_group(t.name) for t in (threading.enumerate() if threads is None else threads))
    return ", ".join("%s x%d" % (name, n) if n > 1 else name
                     for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


_code_kind = {}
_line_names = {}


def _code_is_app(code):
    """True when a code object belongs to one of the app's own scripts."""
    known = _code_kind.get(code)
    if known is None:
        stem = os.path.splitext(os.path.basename(code.co_filename or ""))[0]
        known = stem in _APP_MODULES
        _code_kind[code] = known
    return known


def _names_on_line(code, line):
    """The names a line of compiled code looks up (time, sleep, self, wait); read from the code itself, so it works in the built exe without source files."""
    table = _line_names.get(code)
    if table is None:
        table = {}
        try:
            for ins in dis.get_instructions(code):
                positions = getattr(ins, "positions", None)
                line_no = positions.lineno if positions is not None and positions.lineno else ins.starts_line
                if isinstance(line_no, int) and not isinstance(line_no, bool) and ins.opname in ("LOAD_ATTR", "LOAD_METHOD", "LOAD_GLOBAL", "LOAD_NAME"):
                    table.setdefault(line_no, set()).add(str(ins.argval))
        except Exception as error:
            log.warning("[HEALTH] could not read the compiled code of %s: %s", code.co_name, error)
        _line_names[code] = table
    return table.get(line, ())


def _place(frame):
    """One frame as function (file.py:line)."""
    code = frame.f_code
    return "%s (%s:%d)" % (code.co_name, os.path.basename(code.co_filename or "?"), frame.f_lineno)


def where(frame):
    """(place, kind) for a thread's top frame: the place is the innermost frame in the app's own code, or the top frame when none is; the kind is lock for a thread waiting for a watched lock, wait for one waiting on purpose (sleep, wait, join, a queue, an explicit acquire, a socket) and call for one running or blocked in any other call."""
    top = frame
    probe = frame
    while probe is not None and (not _code_is_app(probe.f_code) or probe.f_code is _TAKE_CODE):
        probe = probe.f_back
    place = _place(probe if probe is not None else top)
    if top.f_code is _TAKE_CODE:
        lock = top.f_locals.get("self")
        return "%s, waiting for lock %s" % (place, getattr(lock, "name", "?")), "lock"
    stem = os.path.splitext(os.path.basename(top.f_code.co_filename or ""))[0]
    if stem in _WAIT_FILES or _WAIT_NAMES.intersection(_names_on_line(top.f_code, top.f_lineno)):
        if probe is not None and probe is not top:
            place = "%s, waiting in %s" % (place, top.f_code.co_name)
        return place, "wait"
    return place, "call"


def _stack_text(frame, limit=25):
    """A thread's stack as text, innermost call last."""
    return "".join(traceback.format_stack(frame, limit=limit)).rstrip()

# ============================================================================
# SECTION: Watched locks
# ============================================================================

_LOCK_TYPES = (type(threading.Lock()), type(threading.RLock()))
_watched_locks = []


class WatchedLock:
    """A lock that keeps how long it is held and waited for, by whom and where; it behaves like the lock it wraps."""

    def __init__(self, inner, name):
        self._inner = inner
        self.name = name
        self._reentrant = not isinstance(inner, type(threading.Lock()))
        self._owner = None
        self._depth = 0
        self._since = None
        self._taken_at = None
        self.waiting = 0
        self._reset_stats()

    def _reset_stats(self):
        self.taken = 0
        self.contended = 0
        self.wait_total = 0.0
        self.wait_max = 0.0
        self.hold_max = 0.0
        self.hold_max_at = None
        self.warned_since = None

    def _take(self, blocking, timeout, caller_depth):
        me = threading.get_ident()
        if self._reentrant and self._owner == me:
            got = self._inner.acquire(blocking, timeout)
            if got:
                self._depth += 1
            return got
        got = self._inner.acquire(False)
        if not got:
            if not blocking:
                return False
            started = time.perf_counter()
            self.waiting += 1
            try:
                got = self._inner.acquire(True, timeout)
            finally:
                self.waiting -= 1
            waited = time.perf_counter() - started
            self.contended += 1
            self.wait_total += waited
            if waited > self.wait_max:
                self.wait_max = waited
            if not got:
                return False
        caller = sys._getframe(caller_depth)
        self._owner = me
        self._depth = 1
        self._since = time.perf_counter()
        self._taken_at = _place(caller)
        self.taken += 1
        return True

    def acquire(self, blocking=True, timeout=-1):
        return self._take(blocking, timeout, 2)

    def release(self):
        if self._reentrant and self._owner == threading.get_ident() and self._depth > 1:
            self._depth -= 1
            self._inner.release()
            return
        since, taken_at = self._since, self._taken_at
        self._owner = None
        self._depth = 0
        self._since = None
        self._inner.release()
        if since is not None:
            held = time.perf_counter() - since
            if held > self.hold_max:
                self.hold_max = held
                self.hold_max_at = taken_at

    def __enter__(self):
        return self._take(True, -1, 2)

    def __exit__(self, *exc):
        self.release()
        return False

    def locked(self):
        inner_locked = getattr(self._inner, "locked", None)
        return inner_locked() if inner_locked is not None else self._owner is not None

    def held_now(self):
        """(seconds held so far, owner thread id, where it was taken) for a held lock, or None."""
        since, owner = self._since, self._owner
        if since is None or owner is None:
            return None
        return time.perf_counter() - since, owner, self._taken_at

    def __getattr__(self, name):
        return getattr(self._inner, name)


_TAKE_CODE = WatchedLock._take.__code__


def watch_module_locks(namespace, prefix="", exclude=()):
    """Wraps every module-level Lock and RLock in a namespace (a module's globals) so its holds and waits are reported; returns how many were wrapped."""
    count = 0
    for name, value in list(namespace.items()):
        if name in exclude or not isinstance(value, _LOCK_TYPES):
            continue
        wrapped = WatchedLock(value, prefix + name)
        namespace[name] = wrapped
        _watched_locks.append(wrapped)
        count += 1
    return count


def watch_lock_attr(owner, attr, name):
    """Wraps one lock held in an object's attribute (a hub's or the settings' lock); returns True when it was wrapped."""
    value = getattr(owner, attr, None) if owner is not None else None
    if not isinstance(value, _LOCK_TYPES):
        return False
    wrapped = WatchedLock(value, name)
    setattr(owner, attr, wrapped)
    _watched_locks.append(wrapped)
    return True

# ============================================================================
# SECTION: Timed jobs and notes
# ============================================================================

_work = {}
_work_lock = threading.Lock()
_main_ident = threading.main_thread().ident


def note(name, seconds):
    """Counts one run of a job (or one wait) that took this many seconds."""
    on_screen = threading.get_ident() == _main_ident
    with _work_lock:
        rec = _work.get(name)
        if rec is None:
            rec = _work[name] = [0, 0.0, 0.0, 0, 0.0, 0, collections.Counter()]
        rec[0] += 1
        rec[1] += seconds
        if seconds > rec[2]:
            rec[2] = seconds
        if on_screen:
            rec[3] += 1
            rec[4] += seconds
        rec[6][thread_group(threading.current_thread().name)] += 1


def _note_error(name):
    with _work_lock:
        rec = _work.get(name)
        if rec is not None:
            rec[5] += 1


def timed(name, fn):
    """fn wrapped so every call is counted under name with its time, the screen thread's share and the threads that called it."""
    @functools.wraps(fn)
    def _timed(*args, **kwargs):
        started = time.perf_counter()
        failed = True
        try:
            result = fn(*args, **kwargs)
            failed = False
            return result
        finally:
            note(name, time.perf_counter() - started)
            if failed:
                _note_error(name)
    _timed.__health_wrapped__ = fn
    return _timed


def time_functions(namespace, names, prefix=""):
    """Wraps the named functions of a namespace (a module's globals) with timed(); a name that is not there writes one line; returns how many were wrapped."""
    count = 0
    for name in names:
        fn = namespace.get(name)
        if not callable(fn):
            log.warning("[HEALTH] %s%s is not in that script, it is not timed", prefix, name)
            continue
        if getattr(fn, "__health_wrapped__", None) is not None:
            continue
        namespace[name] = timed(prefix + name, fn)
        count += 1
    return count

# ============================================================================
# SECTION: Watched values and containers
# ============================================================================

_values = []
_containers = []
_last_sizes = {}
_failed_reads = set()


def watch_value(name, read):
    """Adds a number or short text (a queue length, a load's age) to every report's queue line."""
    _values.append((name, read))


def watch_containers(namespace, prefix=""):
    """Adds every module-level dict, list, set and deque of a namespace to the memory line, so the biggest and the fastest-growing show."""
    _containers.append((namespace, prefix))


def _read(name, read):
    try:
        return read()
    except Exception as error:
        if name not in _failed_reads:
            _failed_reads.add(name)
            log.warning("[HEALTH] could not read %s for the report (left out from now on): %s", name, error)
        return None

# ============================================================================
# SECTION: Garbage collection timing
# ============================================================================

_gc_started = [None]
_gc_events = collections.deque(maxlen=200)
_gc_interval = [0, 0.0, 0.0, None]


def _gc_callback(phase, info):
    if phase == "start":
        _gc_started[0] = time.perf_counter()
        return
    started, _gc_started[0] = _gc_started[0], None
    if started is None:
        return
    took = time.perf_counter() - started
    _gc_events.append((time.monotonic(), took, info.get("generation"), threading.current_thread().name))
    _gc_interval[0] += 1
    _gc_interval[1] += took
    if took > _gc_interval[2]:
        _gc_interval[2] = took
        _gc_interval[3] = info.get("generation")

# ============================================================================
# SECTION: The monitor
# ============================================================================


class _Monitor:
    """The HealthMonitor thread: looks at every thread every sample, explains screen stalls, reports stuck threads and locks, and writes the report block."""

    def __init__(self):
        self.ident = None
        self.seen = {}
        self.names = {}
        self.natives = {}
        self.ring = collections.deque()
        self.profile = collections.Counter()
        self.profile_total = collections.Counter()
        self.gap_requests = collections.deque()
        self.explained_at = collections.deque()
        self.last_dump = -1e9
        self.last_report = time.monotonic()
        self.last_names = 0.0
        self.last_slow_checks = 0.0
        self.stuck_reports = 0
        self.worst_gap = (0, "")
        self.peak_threads = 0
        self.peak_rss = 0
        self.report_threads = None
        self.report_thread = None
        self.cpu_last = None
        self.thread_cpu_last = {}
        self.process = _psutil.Process() if _psutil is not None else None
        self.rss_last = None

    def refresh_names(self):
        threads = threading.enumerate()
        self.names = {t.ident: t.name for t in threads}
        self.natives = {t.ident: getattr(t, "native_id", None) for t in threads}

    def sample(self, now, keep=True):
        """One look at every thread; returns the frames only when keep is true, otherwise none are kept."""
        frames = sys._current_frames()
        running = []
        screen = None
        for ident, frame in frames.items():
            if ident == self.ident:
                continue
            key = (id(frame), frame.f_lasti)
            seen = self.seen.get(ident)
            if seen is None or seen[0] != key:
                place, kind = where(frame)
                seen = self.seen[ident] = [key, now, False, place, kind, None]
            place, kind = seen[3], seen[4]
            if ident == _main_ident:
                screen = (place, kind)
                self.profile[("screen", place)] += 1
                self.profile_total["screen"] += 1
            elif kind == "call":
                group = thread_group(self.names.get(ident, "?"))
                running.append((group, place))
                self.profile[(group, place)] += 1
                self.profile_total[group] += 1
        self.ring.append((now, screen, running))
        while self.ring and now - self.ring[0][0] > _RING_S:
            self.ring.popleft()
        if not keep:
            frames.clear()
            return None
        return frames

    def thread_cpu(self):
        """Seconds of CPU each thread has used, by thread id, or None without psutil."""
        if self.process is None:
            return None
        try:
            per_native = {t.id: t.user_time + t.system_time for t in self.process.threads()}
        except Exception as error:
            log.warning("[HEALTH] reading each thread's CPU use failed: %s", error)
            return None
        return {ident: per_native.get(native) for ident, native in self.natives.items()}

    def slow_checks(self, now, frames):
        """Once a second: stuck threads, long-held locks, ended threads forgotten."""
        for ident in list(self.seen):
            if ident not in frames:
                del self.seen[ident]
        cpu = None
        for ident, frame in frames.items():
            seen = self.seen.get(ident)
            if ident == self.ident or seen is None or seen[2]:
                continue
            still = now - seen[1]
            name = self.names.get(ident, "?")
            if ident == _main_ident:
                if still >= _SCREEN_STUCK_S:
                    seen[2] = True
                    self.report_stuck(name, frame, still, "the screen thread")
                continue
            kind = seen[4]
            limit = _JOB_WAIT_WARN_S if kind == "wait" else _STUCK_S
            if still < limit or any(name.startswith(n) or thread_group(name) == n for n in _NEVER_STUCK):
                continue
            if kind == "wait" and not name.startswith(_JOB_STARTS):
                continue
            if kind == "call":
                # WARNING: a thread working inside one long call (decoding, a database read) sits on one line too; only a thread whose CPU time has stopped rising is stuck.
                if cpu is None:
                    cpu = self.thread_cpu() or {}
                used = cpu.get(ident)
                if used is not None:
                    if seen[5] is None:
                        seen[5] = used
                        continue
                    if used - seen[5] > 0.02:
                        seen[5] = used
                        seen[1] = now
                        continue
            seen[2] = True
            self.report_stuck(name, frame, still, {"call": "a thread", "lock": "a thread waiting for a lock", "wait": "a job thread waiting"}[kind])
        for lock in _watched_locks:
            held = lock.held_now()
            if held is None or held[0] < _LOCK_HOLD_WARN_S or lock.warned_since == lock._since:
                continue
            lock.warned_since = lock._since
            owner_frame = frames.get(held[1])
            log.warning("[HEALTH LOCK] %s held %.1fs so far by %s, taken at %s, now at %s; %d thread(s) waiting for it",
                        lock.name, held[0], self.names.get(held[1], "?"), held[2],
                        where(owner_frame)[0] if owner_frame is not None else "?", lock.waiting)

    def report_stuck(self, name, frame, still, what):
        self.stuck_reports += 1
        log.warning("[HEALTH STUCK] %s, %s, has not moved for %.0fs. Where it is:\n%s", what, name, still, _stack_text(frame))
        self.dump_all("%s %s has not moved for %.0fs" % (what, name, still))

    def dump_all(self, reason):
        now = time.monotonic()
        if now - self.last_dump < _DUMP_MIN_GAP_S:
            return
        self.last_dump = now
        parts = []
        for ident, frame in sys._current_frames().items():
            if ident == self.ident:
                continue
            parts.append("--- %s:\n%s" % (self.names.get(ident, "?"), _stack_text(frame, limit=12)))
        log.warning("[HEALTH DUMP] %s; every thread's stack follows:\n%s", reason, "\n".join(parts))

    def explain(self, end, gap_ms, label):
        if gap_ms > self.worst_gap[0]:
            self.worst_gap = (gap_ms, label)
        while self.explained_at and end - self.explained_at[0] > 60.0:
            self.explained_at.popleft()
        if len(self.explained_at) >= _EXPLAIN_MAX_PER_MINUTE:
            return
        self.explained_at.append(end)
        start = end - gap_ms / 1000.0
        looks = [s for s in self.ring if start - _SAMPLE_S <= s[0] <= end]
        screen = collections.Counter(s[1][0] for s in looks if s[1] is not None)
        others = collections.Counter("%s in %s" % item for s in looks for item in s[2])
        gcs = [e for e in _gc_events if start <= e[0] <= end + 0.05 and e[1] >= 0.01]
        screen_text = ", ".join("%s %d" % kv for kv in screen.most_common(3)) or "no looks taken"
        others_text = ", ".join("%s %d" % kv for kv in others.most_common(_PROFILE_TOP)) or "none"
        gc_text = ("; garbage collection ran %s" % ", ".join("%.0f ms (generation %s, on %s)" % (e[1] * 1000, e[2], e[3]) for e in gcs)) if gcs else ""
        expected = max(1, int(gap_ms / 1000.0 / _SAMPLE_S))
        few_text = ("; only %d of about %d looks could be taken, so one thread held Python without a break for most of the gap (one long step, most likely the place named first)" % (len(looks), expected)) if len(looks) * 3 < expected else ""
        log.info("[HEALTH STALL] %d ms screen gap%s: of %d looks the screen thread was in %s; other threads running app code then: %s%s%s",
                 gap_ms, (" (%s)" % label) if label else "", len(looks), screen_text, others_text, gc_text, few_text)

    def take_snapshot(self, now=None):
        """What the report needs from the looks, taken on the monitor thread so the looks can go on while the report is written."""
        now = time.monotonic() if now is None else now
        span = max(1e-6, now - self.last_report)
        self.last_report = now
        threads = threading.enumerate()
        self.peak_threads = max(self.peak_threads, len(threads))
        idents = {t.ident for t in threads}
        started = ended = 0
        if self.report_threads is not None:
            started = len(idents - self.report_threads)
            ended = len(self.report_threads - idents)
        self.report_threads = idents
        jobs = sorted(((now - self.seen[t.ident][1], t.name) for t in threads
                       if t.name.startswith(_JOB_STARTS) and t.ident in self.seen), reverse=True)[:5]
        profile, totals = self.profile, self.profile_total
        self.profile, self.profile_total = collections.Counter(), collections.Counter()
        return {"span": span, "threads": threads, "started": started, "ended": ended, "jobs": jobs, "profile": profile, "totals": totals}

    def report(self, final_reason=None, snap=None):
        snap = self.take_snapshot() if snap is None else snap
        span, threads, jobs = snap["span"], snap["threads"], snap["jobs"]
        log.info("[HEALTH THREADS] %d alive (%d started, %d ended in %.0fs): %s%s",
                 len(threads), snap["started"], snap["ended"], span, thread_groups_text(threads),
                 ("; job threads on one line longest: " + ", ".join("%s %.0fs" % (n, s) for s, n in jobs)) if jobs else "")
        self.report_cpu(span, threads)
        self.report_profile(snap["profile"], snap["totals"])
        self.report_memory(span)
        self.report_locks()
        self.report_work()
        self.report_values()
        if final_reason is not None:
            log.info("[HEALTH SUMMARY] %s: longest screen gap %d ms%s, %d stuck report(s), most threads at once %d, most memory %s",
                     final_reason, self.worst_gap[0], (" (%s)" % self.worst_gap[1]) if self.worst_gap[1] else "",
                     self.stuck_reports, self.peak_threads,
                     ("%.0f MB" % (self.peak_rss / 1048576.0)) if self.peak_rss else "unknown")

    def report_cpu(self, span, threads):
        if self.process is None:
            return
        try:
            times = self.process.cpu_times()
            used = times.user + times.system
            per_thread = {t.id: t.user_time + t.system_time for t in self.process.threads()}
            system = _psutil.cpu_percent(None)
        except Exception as error:
            log.warning("[HEALTH] reading the CPU use failed: %s", error)
            return
        native = {getattr(t, "native_id", None): t.name for t in threads}
        by_group = collections.Counter()
        for tid, total in per_thread.items():
            before = self.thread_cpu_last.get(tid)
            if before is not None and total >= before:
                by_group[thread_group(native.get(tid, "not a Python thread (VLC, SDL, drivers)"))] += total - before
        self.thread_cpu_last = per_thread
        if self.cpu_last is not None:
            app = (used - self.cpu_last) / span * 100.0
            top = ", ".join("%s %.0f%%" % (g, s / span * 100.0) for g, s in by_group.most_common(8) if s / span >= 0.005)
            log.info("[HEALTH CPU] the app used %.0f%% of one core (%d cores), the whole PC %.0f%%; by thread: %s",
                     app, _psutil.cpu_count() or 0, system, top or "nothing measurable")
        self.cpu_last = used

    def report_profile(self, profile, totals):
        if not totals:
            return
        parts = []
        for group, total in totals.most_common(6):
            places = sorted(((n, place) for (g, place), n in profile.items() if g == group), reverse=True)[:_PROFILE_TOP]
            parts.append("%s (%d looks): %s" % (group, total, ", ".join("%s %.0f%%" % (place, n * 100.0 / total) for n, place in places)))
        log.info("[HEALTH PROFILE] where the time went, by the looks taken while each thread was running: %s", " | ".join(parts))

    def report_memory(self, span):
        rss_text = "unknown"
        if self.process is not None:
            try:
                rss = self.process.memory_info().rss
                self.peak_rss = max(self.peak_rss, rss)
                change = "" if self.rss_last is None else " (%+.0f MB in %.0fs)" % ((rss - self.rss_last) / 1048576.0, span)
                self.rss_last = rss
                rss_text = "%.0f MB%s, most %.0f MB" % (rss / 1048576.0, change, self.peak_rss / 1048576.0)
            except Exception as error:
                log.warning("[HEALTH] reading the memory use failed: %s", error)
        sizes = {}
        for namespace, prefix in _containers:
            for name, value in list(namespace.items()):
                if isinstance(value, (dict, list, set, collections.deque)) and not name.startswith("__"):
                    try:
                        sizes[prefix + name] = len(value)
                    except Exception as error:
                        log.warning("[HEALTH] could not count %s%s: %s", prefix, name, error)
        biggest = sorted(sizes.items(), key=lambda kv: -kv[1])[:8]
        grew = sorted(((n - _last_sizes.get(k, n), k) for k, n in sizes.items() if n - _last_sizes.get(k, n) > 0), reverse=True)[:6]
        _last_sizes.clear()
        _last_sizes.update(sizes)
        gc_runs, gc_total, gc_max, gc_gen = _gc_interval
        _gc_interval[:] = [0, 0.0, 0.0, None]
        log.info("[HEALTH MEMORY] the app holds %s; garbage collection ran %d time(s), %.0f ms in all, longest %.0f ms (generation %s); biggest stores: %s; grew most: %s",
                 rss_text, gc_runs, gc_total * 1000, gc_max * 1000, gc_gen,
                 ", ".join("%s %d" % kv for kv in biggest) or "none",
                 ", ".join("%s +%d" % (k, d) for d, k in grew) or "nothing grew")

    def report_locks(self):
        busy = [lock for lock in _watched_locks if lock.contended or lock.hold_max >= 0.05]
        busy.sort(key=lambda lock: -(lock.wait_max + lock.hold_max))
        if busy:
            log.info("[HEALTH LOCKS] %s", "; ".join(
                "%s taken %d, waited for %d time(s) (%.0f ms in all, longest %.0f ms), longest hold %.0f ms at %s"
                % (lock.name, lock.taken, lock.contended, lock.wait_total * 1000, lock.wait_max * 1000,
                   lock.hold_max * 1000, lock.hold_max_at or "?") for lock in busy[:10]))
        for lock in _watched_locks:
            lock._reset_stats()

    def report_work(self):
        with _work_lock:
            rows = sorted(_work.items(), key=lambda kv: -kv[1][1])
            _work.clear()
        if rows:
            log.info("[HEALTH WORK] %s", "; ".join(
                "%s %d run(s) %.2fs in all, longest %.0f ms%s%s, by %s"
                % (name, rec[0], rec[1], rec[2] * 1000,
                   (", screen thread %d run(s) %.2fs" % (rec[3], rec[4])) if rec[3] else "",
                   (", %d failed" % rec[5]) if rec[5] else "",
                   ", ".join("%s %d" % kv for kv in rec[6].most_common(3))) for name, rec in rows[:16]))

    def report_values(self):
        parts = []
        for name, read in _values:
            value = _read(name, read)
            if value is not None:
                parts.append("%s %s" % (name, value))
        if parts:
            log.info("[HEALTH QUEUES] %s", ", ".join(parts))

    def run(self):
        self.ident = threading.get_ident()
        self.refresh_names()
        while True:
            failed = False
            try:
                time.sleep(_SAMPLE_S)
                now = time.monotonic()
                if now - self.last_names >= 1.0:
                    self.refresh_names()
                    self.last_names = now
                checks_due = now - self.last_slow_checks >= 1.0
                # WARNING: a thread frame kept after the look keeps every local of its function alive, including a pixel array that locks the screen surface; frames are kept only for the once-a-second checks and dropped in the finally.
                frames = self.sample(now, keep=checks_due)
                try:
                    while self.gap_requests:
                        end, gap_ms, label = self.gap_requests.popleft()
                        self.explain(end, gap_ms, label)
                    if checks_due:
                        self.last_slow_checks = now
                        self.slow_checks(now, frames)
                finally:
                    frames = None
                if now - self.last_report >= _REPORT_S and (self.report_thread is None or not self.report_thread.is_alive()):
                    # WARNING: the report reads every thread's CPU time and every store's size; written on its own thread so the looks (and the stall explanations) never pause for it.
                    snap = self.take_snapshot(now)
                    self.report_thread = threading.Thread(target=self.report, kwargs={"snap": snap}, daemon=True, name="HealthReport")
                    self.report_thread.start()
            except Exception as error:
                log.warning("[HEALTH] the monitor pass failed (tried again next pass): %s", error)
                failed = True
            if failed:
                time.sleep(1.0)

# ============================================================================
# SECTION: Public calls
# ============================================================================

_monitor = None


def start():
    """Starts the HealthMonitor thread once; returns True when it runs."""
    global _monitor
    if not _ENABLED:
        log.info("[HEALTH] the health report is off (HEALTH_MONITOR_ENABLED in performance_governor.py)")
        return False
    if _monitor is not None:
        return True
    _monitor = _Monitor()
    if _gc_callback not in gc.callbacks:
        gc.callbacks.append(_gc_callback)
    threading.Thread(target=_monitor.run, daemon=True, name="HealthMonitor").start()
    log.info("[HEALTH START] looking at every thread every %.0f ms, a report every %.0fs; screen gaps of %d ms or more are explained; CPU and memory lines %s",
             _SAMPLE_S * 1000, _REPORT_S, _EXPLAIN_GAP_MS, "on" if _psutil is not None else "off (no psutil)")
    return True


def explain_gap(gap_ms, label=""):
    """Asks for one screen gap to be explained (what the screen thread and every other thread were doing in it); gaps under HEALTH_EXPLAIN_GAP_MS are ignored. Cheap enough to call every frame."""
    if _monitor is None or gap_ms < _EXPLAIN_GAP_MS:
        return
    _monitor.gap_requests.append((time.monotonic(), int(gap_ms), label))


def final_report(reason):
    """Writes the report block now and the whole-run summary; called when the app quits."""
    if _monitor is None:
        return
    try:
        _monitor.report(final_reason=reason)
    except Exception as error:
        log.warning("[HEALTH] the final report failed: %s", error)
