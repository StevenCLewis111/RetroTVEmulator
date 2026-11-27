"""Measures how many scans of each kind this PC can run at once before they stop getting faster, and remembers it."""
import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

try:
    import performance_governor as _governor
except Exception as _governor_import_error:
    _governor = None
    log.warning("[SCAN CAPACITY] could not import performance_governor, using the fallback values written in scan_capacity.py: %s", _governor_import_error)


# ============================================================================
# SECTION: Tunables
# ============================================================================

# WARNING: the fallback number below must match SCAN_CAPACITY_POLL_SECONDS in performance_governor.py; it is used only when that module fails to import.
POLL_SECONDS = _governor.SCAN_CAPACITY_POLL_SECONDS if _governor is not None else 0.1
CAPACITY_FILE_NAME = "scan_capacity.json"
# WARNING: raise this number whenever the way a step is measured changes; a saved result made the older way is then ignored and measured again, or a wrong old result keeps capping the scans.
CAPACITY_METHOD_VERSION = 2


# ============================================================================
# SECTION: Choosing the best step
# ============================================================================


def choose_best(steps, min_gain, cpu_ceiling):
    """From steps [{"workers", "rate", "cpu"}] in rising worker order: the most workers that still gained at least min_gain over the best so far while the processor stayed under cpu_ceiling; returns (workers, rate, peak cpu)."""
    best = None
    for step in steps:
        if best is None:
            best = step
            continue
        if (best.get("cpu") or 0.0) >= cpu_ceiling:
            break
        if step["rate"] < best["rate"] * (1.0 + min_gain):
            break
        best = step
    if best is None:
        return 1, 0.0, 0.0
    return max(1, int(best["workers"])), float(best["rate"]), float(best.get("cpu") or 0.0)


# ============================================================================
# SECTION: The saved results
# ============================================================================


class CapacityStore:
    """Per scan kind: {"workers", "rate", "cpu", "measured_at"}, kept in one small JSON file."""

    def __init__(self, path, clock=time.time):
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._data = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._data = {str(k): dict(v) for k, v in loaded.items() if isinstance(v, dict) and v.get("v") == CAPACITY_METHOD_VERSION}
        except FileNotFoundError:
            self._data = {}
        except (OSError, ValueError) as exc:
            log.warning("[SCAN CAPACITY] could not read %s, the capacity is measured again: %s", path, exc)
            self._data = {}

    def get(self, kind):
        with self._lock:
            return dict(self._data.get(kind, {}))

    def workers(self):
        """{kind: measured workers} for every kind that has been measured."""
        with self._lock:
            return {k: int(v["workers"]) for k, v in self._data.items() if v.get("workers")}

    def note(self, kind, workers, rate, cpu):
        with self._lock:
            self._data[kind] = {"workers": int(workers), "rate": round(float(rate), 2), "cpu": round(float(cpu), 1), "measured_at": self._clock(), "v": CAPACITY_METHOD_VERSION}
            tmp = self._path + ".tmp"
            try:
                os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh)
                os.replace(tmp, self._path)
            except OSError as exc:
                log.warning("[SCAN CAPACITY] could not save %s (kept for this run only): %s", self._path, exc)

    def never_measured(self, kind):
        """True when this scan kind has no measurement at all (a first start)."""
        return self.get(kind).get("measured_at") is None

    def due(self, kind, max_age_s):
        at = self.get(kind).get("measured_at")
        return at is None or self._clock() - float(at) > float(max_age_s)


# ============================================================================
# SECTION: Measuring
# ============================================================================


def measure(kind, spawn, cpu_percent, max_workers, media_seconds, should_stop, step_timeout_s, clock=time.monotonic, on_step=None):
    """Runs 1..max_workers copies of one scan kind at once and times each step; returns the steps, or what was done when should_stop() turned true. spawn(kind, n) returns n started processes."""
    steps = []
    for workers in range(1, int(max_workers) + 1):
        if should_stop():
            break
        procs = spawn(kind, workers)
        t0 = clock()
        samples = []
        ok = True
        while any(p.poll() is None for p in procs):
            if should_stop() or clock() - t0 > step_timeout_s:
                ok = False
                for p in procs:
                    if p.poll() is None:
                        try:
                            p.kill()
                        except OSError as exc:
                            log.warning("[SCAN CAPACITY] a %s test scan could not be stopped: %s", kind, exc)
                break
            seen = cpu_percent()
            if seen is not None:
                samples.append(seen)
            time.sleep(POLL_SECONDS)
        wall = clock() - t0
        # WARNING: the processor figure is the middle reading of the step, not its highest: the program's own loading work spikes the processor on a first start, and one spike made a single scan look like a full processor.
        if not samples:
            seen = cpu_percent()
            samples = [seen] if seen is not None else [0.0]
        peak = sorted(samples)[len(samples) // 2]
        if not ok or wall <= 0 or any(p.returncode not in (0, None) for p in procs):
            break
        steps.append({"workers": workers, "rate": workers * float(media_seconds) / wall, "cpu": peak})
        if on_step is not None:
            on_step(kind, workers, len(steps))
    return steps
