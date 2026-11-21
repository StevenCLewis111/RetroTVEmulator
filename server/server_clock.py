"""The server's running clock for every channel: the show on air with its time left, and the next shows with what each one still lacks, read from the shared schedule hub."""
from __future__ import annotations

import copy
import logging
import os
import time

log = logging.getLogger(__name__)

# WARNING: the fallback number below must match SERVER_CLOCK_NEXT_SHOWS in performance_governor.py; it is used only when that module fails to import.
try:
    import performance_governor as _gov
    NEXT_SHOWS = int(_gov.SERVER_CLOCK_NEXT_SHOWS)
except Exception:
    NEXT_SHOWS = 2
# WARNING: the fallback number below must match SERVER_CLOCK_KEEP_S in performance_governor.py; it is used only when that module fails to import.
try:
    KEEP_S = float(_gov.SERVER_CLOCK_KEEP_S)
except Exception:
    KEEP_S = 5.0


def _moved_to(clocks, now_epoch):
    """The clocks with every show's seconds before it starts or ends worked out for now_epoch."""
    for clock in clocks.values():
        for show in [clock["now"]] + list(clock["next"]):
            show["starts_in"] = max(0.0, show["start"] - now_epoch)
            show["remaining"] = max(0.0, show["end"] - now_epoch)
    return clocks


def _show(row, now_epoch):
    """One hub row as a clock entry: its file, kind, full length, start, end and the seconds before it starts or ends."""
    start = float(row.get("planned_start") or 0.0)
    length = float(row.get("used_length") or 0.0)
    return {"file": row.get("path", ""), "kind": row.get("kind", ""), "length": length,
            "start": start, "end": start + length, "starts_in": max(0.0, start - now_epoch),
            "remaining": max(0.0, start + length - now_epoch)}


_clock_state = {}


def _scope_name(slot_id):
    """The prefix a slot's rows are read under, for the log."""
    if slot_id is None:
        return "the setup in use"
    import media_library
    return media_library.schedule_scope_of_slot(slot_id)


def _slot_scope(slot_id):
    """Reads the hub for one server profile's rows; with no slot the setup in use is read."""
    import contextlib
    if slot_id is None:
        return contextlib.nullcontext()
    import media_library
    return media_library.schedule_scope_for(media_library.schedule_scope_of_slot(slot_id))


def build_clock(hub, channel, now_epoch, next_shows=None, missing_fn=None):
    """{"now": entry, "next": [entries]} for one channel, or None when the hub holds no show covering now_epoch; the entry on air counts down from its full length."""
    next_shows = NEXT_SHOWS if next_shows is None else int(next_shows)
    row = hub.at(channel, now_epoch)
    if not row:
        return None
    current = _show(row, now_epoch)
    ahead = hub.upcoming(channel, current["end"], next_shows + 1)
    following = [_show(r, now_epoch) for r in ahead
                 if not (r.get("path") == current["file"] and abs(float(r.get("planned_start") or 0) - current["start"]) < 1.0)]
    shows = [current] + following[:next_shows]
    if missing_fn is not None:
        for show in shows:
            show["missing"] = list(missing_fn(show["file"]) or [])
    return {"now": current, "next": shows[1:]}


class ChannelClocks:
    """Reads the clock of every channel of a slot from the schedule hub; the scanner uses it to decide what to read first, and whatever hands the clocks to Users reads the same snapshot."""

    def __init__(self, now_fn=time.time, next_shows=None, keep_s=0.0, missing_fn=None):
        self._now = now_fn
        self._next_shows = next_shows
        # keep_s: one reading of a slot's channels is handed out again for this many seconds (0 reads the hub every time); missing_fn: the checklist used when a caller gives none.
        self._keep_s = float(keep_s or 0.0)
        self._missing_fn = missing_fn
        self._kept = {}

    def snapshot(self, hub, channels, missing_fn=None, slot_id=None):
        """{channel: clock} for the given channel keys; a channel with nothing on air is left out. missing_fn(path) -> what the checklist still lacks for that show's file, kept with each show so a User can tell the server is still preparing it."""
        if hub is None:
            log.warning("[SERVER CLOCK] there is no schedule hub to read, so no channel clocks are made")
            return {}
        now_epoch = self._now()
        missing_fn = missing_fn or self._missing_fn
        key = (slot_id, tuple(str(channel).zfill(2) for channel in channels), missing_fn is not None)
        kept = self._kept.get(key)
        if (kept is not None and now_epoch - kept[0] < self._keep_s
                and all(clock["now"]["end"] > now_epoch for clock in kept[1].values())):
            # WARNING: owner 2026-11-18 (audit A8): a kept reading is handed out with its seconds left worked out again for this moment, and never once a show in it has ended, so a User never counts down from an old moment.
            return _moved_to(copy.deepcopy(kept[1]), now_epoch)
        out = self._read(hub, channels, missing_fn, slot_id, now_epoch)
        if self._keep_s > 0:
            self._kept[key] = (now_epoch, copy.deepcopy(out))
        return out

    def _read(self, hub, channels, missing_fn, slot_id, now_epoch):
        out = {}
        for channel in channels:
            key = str(channel).zfill(2)
            try:
                with _slot_scope(slot_id):
                    clock = build_clock(hub, key, now_epoch, self._next_shows, missing_fn)
            except Exception as exc:
                log.warning("[SERVER CLOCK] channel %s clock failed: %s", key, exc)
                continue
            had = _clock_state.get((slot_id, key))
            _clock_state[(slot_id, key)] = bool(clock)
            if clock:
                out[key] = clock
                if had is False:
                    log.info("[SERVER CLOCK] slot %s channel %s: the clock is back (the hub holds a show on air again)", slot_id, key)
            elif had is not False:
                # WARNING: owner 2026-11-18: written each time a channel loses its clock, not once per run, so the log shows when a profile's rows ran out and when they came back.
                log.info("[SERVER CLOCK] slot %s channel %s: no clock; the hub holds no show on air (rows are read under %r)", slot_id, key,
                         _scope_name(slot_id))
        return out

    def now_files(self, hub, channels, slot_id=None):
        """{channel: the file the hub says airs now} for the slot's channels, from the same reading as the clocks."""
        return {ch: clock["now"]["file"] for ch, clock in self.snapshot(hub, channels, slot_id=slot_id).items()}

    def files_in_order(self, hub, channels, watched=(), slot_id=None):
        """[(channel, place, path)]: the files of the shows on air first, then of each channel's next shows, a channel a User watches ahead of the others; a file appears once."""
        clocks = self.snapshot(hub, channels, slot_id=slot_id)
        watching = {str(ch).zfill(2) for ch in watched}
        order = sorted(clocks, key=lambda ch: (0 if ch in watching else 1, ch))
        found, seen = [], set()
        for rank in range(0, 1 + max([len(clocks[ch]["next"]) for ch in order] or [0])):
            for ch in order:
                shows = [clocks[ch]["now"]] + clocks[ch]["next"]
                if rank < len(shows):
                    path = shows[rank]["file"]
                    if path and path not in seen:
                        seen.add(path)
                        found.append((ch, rank, os.path.normpath(path)))
        return found
