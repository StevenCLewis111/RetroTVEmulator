"""A User's regular check of its own hub against the server's channel clocks: do both say the same show is on air at the same time, are the clocks still coming, and is the server still preparing the files of the channel being watched."""
from __future__ import annotations

import ntpath
import time

# WARNING: the fallback numbers below must match SERVER_CHECKIN_START_TOLERANCE_S and SERVER_CLOCKS_STALE_S in performance_governor.py; they are used only when that module fails to import.
try:
    import performance_governor as _gov
    TOLERANCE_S = float(_gov.SERVER_CHECKIN_START_TOLERANCE_S)
    STALE_S = float(_gov.SERVER_CLOCKS_STALE_S)
except Exception:
    TOLERANCE_S, STALE_S = 8.0, 20.0

AGREE, NO_CLOCK, NO_LOCAL, OTHER_FILE, OFFSET = "agree", "no_clock", "no_local", "other_file", "offset"


def judge(local_row, clock_show, same_file, tolerance_s=None):
    """Compares this computer's hub row for a channel with the server's show on air: AGREE, or why they differ."""
    tolerance_s = TOLERANCE_S if tolerance_s is None else tolerance_s
    if not clock_show:
        return NO_CLOCK
    if not local_row:
        return NO_LOCAL
    if not same_file(local_row.get("path", ""), clock_show.get("file", "")):
        return OTHER_FILE
    start = float(local_row.get("planned_start") or 0.0)
    end = start + float(local_row.get("used_length") or 0.0)
    if abs(start - float(clock_show.get("start") or 0.0)) > tolerance_s or abs(end - float(clock_show.get("end") or 0.0)) > tolerance_s:
        return OFFSET
    return AGREE


def same_path(a, b):
    """True when two file paths name the same file once written the Windows way."""
    return bool(a) and bool(b) and ntpath.normcase(ntpath.normpath(str(a))) == ntpath.normcase(ntpath.normpath(str(b)))


class ServerCheckin:
    """Remembers whether the server's clocks keep coming; step() compares this computer's hub with them and says what it found. A difference is never acted on here: the User keeps playing and catches up at a channel change or in a commercial break (owner 2026-11-18)."""

    def __init__(self, tolerance_s=None, stale_s=None, mono=time.monotonic):
        self.tolerance_s = TOLERANCE_S if tolerance_s is None else tolerance_s
        self.stale_s = STALE_S if stale_s is None else stale_s
        self._mono = mono
        self._stale = False

    def step(self, clocks_state, local_row_fn, same_file, watched=None):
        """clocks_state = {"received_at", "channels"} as the client keeps it (or None before any clock came); local_row_fn(channel) -> this computer's hub row on air now. Returns {"stale", "became_stale", "verdicts", "preparing"}: the verdict for each channel, and what the server still lacks for the watched channel's show on air and next."""
        out = {"stale": False, "became_stale": False, "verdicts": {}, "preparing": []}
        received = (clocks_state or {}).get("received_at")
        if received is None or self._mono() - float(received) > self.stale_s:
            out["stale"] = True
            out["became_stale"] = not self._stale
            self._stale = True
            return out
        self._stale = False
        channels = (clocks_state or {}).get("channels") or {}
        for channel, clock in channels.items():
            out["verdicts"][channel] = judge(local_row_fn(channel), (clock or {}).get("now"), same_file, self.tolerance_s)
        if watched is not None and str(watched) in channels:
            clock = channels[str(watched)] or {}
            shows = [clock.get("now")] + list((clock.get("next") or [])[:1])
            lacking = sorted({str(item) for show in shows if show for item in (show.get("missing") or [])})
            if lacking:
                out["preparing"] = lacking
        return out
