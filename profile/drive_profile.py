"""Remembers how each media drive or share answers (first-byte time and read speed), so priming and keep-awake can size their work to the drive."""
import json
import os
import threading
import time

PROFILE_FILE_NAME = "drive_profiles.json"
# WARNING: a first read slower than this is a drive spinning up or a share reconnecting, not how the drive normally answers, so it is never remembered.
SPIN_UP_MS = 2000.0
_SMOOTHING = 0.5


class DriveProfiles:
    """Per drive root: {"first_byte_ms", "mb_s", "measured_at"}, kept in one small JSON file."""

    def __init__(self, path, clock=time.time):
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._data = {}
        self._load()

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._data = {str(k): dict(v) for k, v in loaded.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self._data = {}

    def _save(self):
        tmp = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self._path)
        except OSError:
            pass

    def get(self, root):
        with self._lock:
            return dict(self._data.get(str(root), {}))

    def note_first_byte(self, root, ms):
        """Remembers how long the first read of an awake drive took; returns False when the sample was a spin-up and was ignored."""
        if ms is None or ms < 0 or ms > SPIN_UP_MS:
            return False
        with self._lock:
            rec = self._data.setdefault(str(root), {})
            old = rec.get("first_byte_ms")
            rec["first_byte_ms"] = round(float(ms) if old is None else old + (float(ms) - old) * _SMOOTHING, 1)
            self._save()
        return True

    def note_throughput(self, root, mb_s):
        """Remembers the read speed measured on a real file of the drive."""
        if mb_s is None or mb_s <= 0:
            return
        with self._lock:
            rec = self._data.setdefault(str(root), {})
            rec["mb_s"] = round(float(mb_s), 1)
            rec["measured_at"] = self._clock()
            self._save()

    def throughput_due(self, root, max_age_s):
        """True when the drive has no speed measurement or the one it has is older than max_age_s."""
        at = self.get(root).get("measured_at")
        return at is None or self._clock() - float(at) > float(max_age_s)

    def workers_for(self, root, default, fast_workers, slow_workers, fast_mb_s, slow_mb_s, fast_ms, slow_ms):
        """How many files may be read at once from this drive: more for a fast one, fewer for a slow one, the default when it has not been measured."""
        rec = self.get(root)
        mb_s, first_ms = rec.get("mb_s"), rec.get("first_byte_ms")
        if mb_s is None:
            return int(default)
        if mb_s <= slow_mb_s or (first_ms is not None and first_ms >= slow_ms):
            return int(slow_workers)
        if mb_s >= fast_mb_s and (first_ms is None or first_ms <= fast_ms):
            return int(fast_workers)
        return int(default)

    def workers_for_roots(self, roots, **limits):
        """The smallest worker count among the drives involved, so one slow drive is never swamped; the default when there are none."""
        roots = [r for r in roots if r]
        if not roots:
            return int(limits["default"])
        return min(self.workers_for(r, **limits) for r in roots)


class DriveReaders:
    """One long-lived reader thread per drive for the keep-awake rounds of the program and the server service: a round hands each drive its read, and a drive whose last read has not come back is skipped (reported as "busy") instead of getting another thread, so a hung drive never piles threads up."""

    def __init__(self, name="DriveReader"):
        self._name = str(name)
        self._lock = threading.Lock()
        self._workers = {}

    def _worker(self, root):
        state = self._workers[root]
        while True:
            state["wake"].wait()
            state["wake"].clear()
            job = state["job"]
            try:
                state["result"] = bool(job())
            except Exception:
                state["result"] = False
            with self._lock:
                state["busy"] = False
                state["job"] = None
            state["done"].set()

    def run_round(self, jobs, timeout_s, clock=time.monotonic):
        """jobs = {drive root: function that reads it and returns True when it answered}; waits at most timeout_s and returns {root: True, False (no answer in time) or "busy" (its last read is still running, so it was skipped)}."""
        handed = []
        results = {}
        with self._lock:
            for root, job in jobs.items():
                state = self._workers.get(root)
                if state is None:
                    state = {"wake": threading.Event(), "done": threading.Event(), "busy": False, "job": None, "result": False}
                    self._workers[root] = state
                    threading.Thread(target=self._worker, args=(root,), daemon=True, name="%s-%s" % (self._name, root)).start()
                if state["busy"]:
                    results[root] = "busy"
                    continue
                state["busy"] = True
                state["job"] = job
                state["done"].clear()
                state["wake"].set()
                handed.append(root)
        deadline = clock() + float(timeout_s)
        for root in handed:
            state = self._workers[root]
            state["done"].wait(max(0.0, deadline - clock()))
            results[root] = state["result"] if state["done"].is_set() else False
        return results

    def busy_roots(self):
        """The drives whose last read has not come back yet."""
        with self._lock:
            return sorted(root for root, state in self._workers.items() if state["busy"])


# WARNING: Windows CreateFile flags and codes used by read_uncached; NO_BUFFERING makes the read come from the drive and not from the system's file cache, and needs a sector-aligned offset, length and buffer.
_GENERIC_READ = 0x80000000
_SHARE_READ_WRITE = 0x00000003
_OPEN_EXISTING = 3
_FLAG_NO_BUFFERING = 0x20000000
_ALIGN = 4096
_CHUNK = 1048576


def read_uncached(path, offset, nbytes):
    """Reads nbytes from path at offset straight from the drive (Windows, no system cache) and returns how many bytes came back; raises when it cannot, so the caller can read normally."""
    if os.name != "nt":
        raise OSError("uncached reads are Windows only")
    import ctypes
    import mmap
    from ctypes import wintypes
    offset -= offset % _ALIGN
    nbytes -= nbytes % _ALIGN
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD]
    kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = wintypes.HANDLE(-1).value
    handle = kernel32.CreateFileW(path, _GENERIC_READ, _SHARE_READ_WRITE, None, _OPEN_EXISTING, _FLAG_NO_BUFFERING, None)
    if handle in (None, invalid):
        raise OSError("could not open %s unbuffered (error %d)" % (path, ctypes.get_last_error()))
    buf = mmap.mmap(-1, _CHUNK)
    try:
        address = ctypes.addressof(ctypes.c_char.from_buffer(buf))
        if not kernel32.SetFilePointerEx(handle, offset, None, 0):
            raise OSError("could not seek %s (error %d)" % (path, ctypes.get_last_error()))
        done = 0
        while done < nbytes:
            got = wintypes.DWORD(0)
            want = min(_CHUNK, nbytes - done)
            if not kernel32.ReadFile(handle, address, want, ctypes.byref(got), None):
                raise OSError("could not read %s (error %d)" % (path, ctypes.get_last_error()))
            if got.value == 0:
                break
            done += got.value
        return done
    finally:
        kernel32.CloseHandle(handle)
        try:
            del address
            buf.close()
        except Exception:
            pass
