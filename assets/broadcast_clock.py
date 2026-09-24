"""Single wall-clock authority for the TV scheduler and guide.

Windows local time is sampled at boot and periodically resynchronised. Between
samples, monotonic time keeps the displayed/scheduled clock live without
repeatedly depending on wall-clock reads every frame.
"""
from __future__ import annotations

import datetime as _datetime
import threading as _threading
import time as _time

_RESYNC_SECONDS = 60.0

# time.time() is a cheap single syscall (unlike datetime.now().astimezone(),
# which also resolves the local UTC offset), so it's checked on every now()
# call to detect a wall-clock step -- a manual time change, an NTP
# correction, a laptop/HTPC waking from sleep -- without adding a per-frame
# wall-clock read.
# When time.time() and time.monotonic() have drifted apart by more than this
# tolerance since the last resync, now() resyncs immediately instead of
# waiting for the periodic window below.
_STEP_TOLERANCE_SECONDS = 2.0

_lock = _threading.Lock()
_wall_anchor = _datetime.datetime.now().astimezone()
_mono_anchor = _time.monotonic()
_last_resync = _mono_anchor
_raw_anchor = _time.time()
# The UTC offset (in seconds) active at the moment _wall_anchor was captured.
# time.localtime(x).tm_gmtoff is ~3-4x cheaper than a full
# datetime.now().astimezone() call, so -- like the time.time() step check
# above -- it's cheap enough to check on every now() call. This is what
# catches a DST transition (spring-forward/fall-back): the underlying epoch
# never jumps at a DST boundary, only the local-time rendering of it does,
# so the existing raw/mono step check above never fires for DST. Without
# this, a DST change would only be picked up by the 60s periodic resync,
# during which seconds_since_midnight() (and therefore schedule position
# math) would be off by the full DST amount, then snap by a full hour in
# one instant once the periodic resync finally caught up.
_offset_anchor = _time.localtime(_raw_anchor).tm_gmtoff
# The last value returned by now(). Anchors are captured as two sequential,
# non-atomic calls (an astimezone() call, then a monotonic() call); under
# concurrent resyncs -- two threads racing through this window at once --
# the pairing can be misaligned by a few milliseconds, which without a
# monotonicity guard could make now() briefly return a value earlier than
# one it already returned. now() absorbs only that sub-second race jitter.
# A real backward correction (operator fixes a wrong system clock, a bad NTP
# sync gets corrected) is NOT swallowed here -- it's larger than this jitter
# tolerance, so it passes through untouched, exactly like any other step.
# Silently freezing on a stale time forever would be its own bug.
_last_returned: _datetime.datetime | None = None
_JITTER_TOLERANCE_SECONDS = 0.25


def now() -> _datetime.datetime:
    """Return the current local wall time as an aware datetime.

    Guaranteed to never return a value earlier than a value it has already
    returned (monotonically non-decreasing), even under concurrent callers
    and concurrent resyncs.
    """
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
                # Sub-second race jitter between two anchor pairs -- absorb it.
                result = _last_returned
            else:
                # Either moving forward normally, or a real backward step
                # larger than jitter -- let it through untouched.
                _last_returned = result
        else:
            _last_returned = result
        return result


def seconds_since_midnight(value: _datetime.datetime | None = None) -> float:
    value = value or now()
    return value.hour * 3600 + value.minute * 60 + value.second + value.microsecond / 1_000_000.0


def force_resync() -> None:
    """Resynchronise the wall-clock anchor immediately, without waiting for
    the next now() call or the periodic resync window.
    """
    global _wall_anchor, _mono_anchor, _last_resync, _raw_anchor, _offset_anchor, _last_returned
    mono = _time.monotonic()
    raw = _time.time()
    with _lock:
        _wall_anchor = _datetime.datetime.now().astimezone()
        _mono_anchor = mono
        _last_resync = mono
        _raw_anchor = raw
        _offset_anchor = _time.localtime(raw).tm_gmtoff
        # Same jitter-only absorption as now() -- see _JITTER_TOLERANCE_SECONDS.
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


__all__ = ["now", "date", "seconds_since_midnight", "force_resync", "isoformat", "strftime"]

force_resync()
