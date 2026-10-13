"""Single wall-clock authority for the TV scheduler and guide: monotonic-anchored local time, resync on clock steps and DST changes, and absorption of small backward jitter."""
from __future__ import annotations

import datetime as _datetime
import threading as _threading
import time as _time

# ============================================================================
# SECTION: Tunables & anchor state
# ============================================================================
# WARNING: every caller that needs "now" must use this module, not datetime.now(), so the scheduler and guide can never disagree.
# WARNING: the fallback numbers must match BROADCAST_CLOCK_* in performance_governor.py; they are used only when that module fails to import.
try:
    import performance_governor as _governor

    _RESYNC_SECONDS = float(_governor.BROADCAST_CLOCK_RESYNC_SECONDS)
    _STEP_TOLERANCE_SECONDS = float(_governor.BROADCAST_CLOCK_STEP_TOLERANCE_SECONDS)
    _JITTER_TOLERANCE_SECONDS = float(_governor.BROADCAST_CLOCK_JITTER_TOLERANCE_SECONDS)
except Exception:
    _RESYNC_SECONDS = 60.0
    _STEP_TOLERANCE_SECONDS = 2.0
    _JITTER_TOLERANCE_SECONDS = 0.25

_lock = _threading.Lock()
_wall_anchor = _datetime.datetime.now().astimezone()
_mono_anchor = _time.monotonic()
_last_resync = _mono_anchor
_raw_anchor = _time.time()
_offset_anchor = _time.localtime(_raw_anchor).tm_gmtoff
_last_returned: _datetime.datetime | None = None


# ============================================================================
# SECTION: Public API
# ============================================================================


def now() -> _datetime.datetime:
    """Returns the current local wall time as an aware datetime; a backward step of up to _JITTER_TOLERANCE_SECONDS is absorbed, a larger one passes through."""
    global _wall_anchor, _mono_anchor, _last_resync, _raw_anchor, _offset_anchor, _last_returned
    mono = _time.monotonic()
    raw = _time.time()
    current_offset = _time.localtime(raw).tm_gmtoff
    with _lock:
        _stepped = abs((raw - _raw_anchor) - (mono - _mono_anchor)) >= _STEP_TOLERANCE_SECONDS
        _dst_changed = current_offset != _offset_anchor
        if _stepped or _dst_changed or mono - _last_resync >= _RESYNC_SECONDS:
            _wall_anchor = _datetime.datetime.now().astimezone()
            _mono_anchor = mono
            _last_resync = mono
            _raw_anchor = raw
            _offset_anchor = _time.localtime(raw).tm_gmtoff
        result = _wall_anchor + _datetime.timedelta(seconds=mono - _mono_anchor)
        if _last_returned is not None:
            regression = (_last_returned - result).total_seconds()
            if 0 < regression <= _JITTER_TOLERANCE_SECONDS:
                result = _last_returned
            else:
                _last_returned = result
        else:
            _last_returned = result
        return result


def seconds_since_midnight(value: _datetime.datetime | None = None) -> float:
    value = value or now()
    return value.hour * 3600 + value.minute * 60 + value.second + value.microsecond / 1_000_000.0


def force_resync() -> None:
    """Resynchronises the wall-clock anchor immediately, without waiting for the next now() call or the periodic resync window."""
    global _wall_anchor, _mono_anchor, _last_resync, _raw_anchor, _offset_anchor, _last_returned
    mono = _time.monotonic()
    raw = _time.time()
    with _lock:
        _wall_anchor = _datetime.datetime.now().astimezone()
        _mono_anchor = mono
        _last_resync = mono
        _raw_anchor = raw
        _offset_anchor = _time.localtime(raw).tm_gmtoff
        if _last_returned is not None:
            regression = (_last_returned - _wall_anchor).total_seconds()
            if not (0 < regression <= _JITTER_TOLERANCE_SECONDS):
                _last_returned = _wall_anchor
        else:
            _last_returned = _wall_anchor


def date() -> _datetime.date:
    return now().date()


def isoformat() -> str:
    return now().isoformat()


def strftime(fmt: str) -> str:
    return now().strftime(fmt)


# ============================================================================
# SECTION: Exports & import-time init
# ============================================================================
__all__ = ["now", "date", "seconds_since_midnight", "force_resync", "isoformat", "strftime"]

force_resync()
