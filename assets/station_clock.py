"""The station clock (owner 2026-11-24): one clock for everything that runs on the schedule, and a light countdown for each channel.

Every task that needs the time of the schedule reads now_epoch(), which is the TV guide clock (broadcast_clock): this computer's clock, or the server's time while a server's schedule is on screen. A task that has to act when a show ends does not keep checking the time: it starts a countdown with arm_in() when it reads the schedule (the one moment the clock is read), and the countdown calls it back when the time comes. The countdowns of every channel share one thread that sleeps until the soonest one is due.
"""
from __future__ import annotations

import heapq
import logging
import threading
import time

import broadcast_clock

log = logging.getLogger(__name__)


def now_epoch():
    """The station's time as seconds since 1970: the TV guide clock, which follows this computer's clock or, while a server's schedule is on screen, the server's."""
    return broadcast_clock.now().timestamp()


_lock = threading.Lock()
_wake = threading.Event()
_heap = []
_armed = {}
_seq = [0]
_thread = [None]


def arm_in(key, seconds, fn):
    """Starts (or starts again) the countdown named key: fn(key) runs once, on the countdown thread, after this many seconds of the schedule. An older countdown of the same name is dropped."""
    # WARNING: fn must return quickly (hand the work to its own thread); every channel's countdown shares this one thread.
    due = time.monotonic() + max(0.0, float(seconds))
    with _lock:
        _seq[0] += 1
        _armed[key] = (_seq[0], due, fn)
        heapq.heappush(_heap, (due, _seq[0], key))
        if _thread[0] is None or not _thread[0].is_alive():
            _thread[0] = threading.Thread(target=_run, daemon=True, name="StationClock")
            _thread[0].start()
    _wake.set()


def arm_at(key, at_epoch, fn):
    """arm_in for a time of the schedule (seconds since 1970 on the station clock); the clock is read here, once."""
    arm_in(key, float(at_epoch) - now_epoch(), fn)


def cancel(key):
    """Drops the countdown named key, if any."""
    with _lock:
        _armed.pop(key, None)


def due_in(key):
    """Seconds left on the countdown named key, or None when there is none."""
    with _lock:
        held = _armed.get(key)
    return None if held is None else max(0.0, held[1] - time.monotonic())


def _run():
    while True:
        fire = None
        with _lock:
            while _heap and _armed.get(_heap[0][2], (None,))[0] != _heap[0][1]:
                heapq.heappop(_heap)
            if _heap:
                due, seq, key = _heap[0]
                wait = due - time.monotonic()
                if wait <= 0:
                    heapq.heappop(_heap)
                    fire = (key, _armed.pop(key)[2])
            else:
                wait = None
        if fire is not None:
            try:
                fire[1](fire[0])
            except Exception as exc:
                log.warning("[STATION CLOCK] the countdown %s failed when it ran out: %s", fire[0], exc)
            continue
        _wake.wait(wait)
        _wake.clear()
