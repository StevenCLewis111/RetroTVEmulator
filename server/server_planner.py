"""The server's own schedule for every profile it serves (owner 2026-11-24): "the profile is built. a schedule is made, its stored in the HUB, its shared with the user", with the program open or closed, "keeping a constant 7 day cache of scheduled info".

The service works each served profile's channels out with the program's own planner (schedule_planner, the code the guide's future rows use), from the positions saved in the profile, and keeps the rows in this server's HUB under the profile's scope for HUB_FUTURE_HOURS_AHEAD (8 days). It plans at start, when a profile's file changes, and at midnight on a station-clock countdown, when the day that ended drops off and a new last day is added.
"""
from __future__ import annotations

import logging
import threading

import server_clock
import server_scanner
import station_clock

log = logging.getLogger(__name__)

# WARNING: the fallback number below must match HUB_FUTURE_HOURS_AHEAD in performance_governor.py; it is used only when that module fails to import.
try:
    import performance_governor as _gov
    HOURS_AHEAD = float(_gov.HUB_FUTURE_HOURS_AHEAD)
except Exception:
    HOURS_AHEAD = 192.0
# WARNING: the fallback number below must match SERVER_ROWS_BEHIND_S in performance_governor.py; it is used only when that module fails to import.
try:
    ROWS_BEHIND_S = float(_gov.SERVER_ROWS_BEHIND_S)
except Exception:
    ROWS_BEHIND_S = 10800.0


def plan_channel(hub, channel_key, ch_info, config, now, path_exists):
    """Works one channel of a profile out from its saved positions and writes the rows of every block from the one on air to HOURS_AHEAD hours ahead into the hub (the caller has set the profile's scope); a Marathon or Visualizer channel goes to plan_loop_channel. Returns the number of rows written, or None when the channel cannot be planned here (no start number saved yet)."""
    import media
    import schedule_planner
    if ch_info.get("is_visualizer", False) or ch_info.get("scheduling_mode", "random_slots") == "marathon":
        return plan_loop_channel(hub, channel_key, ch_info, config, now, path_exists)
    snap = schedule_planner.snapshot(ch_info)
    seed = (snap.get("playback_log") or {}).get("intro_seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        return None
    forced = media.holiday_forced_key_for_channel(config, channel_key)
    projected = schedule_planner.project_station(channel_key, snap, now, path_exists, lambda offset: "%d:%d" % (seed, int(offset)),
                                                 forced, HOURS_AHEAD, require_block_on_air=False)
    if projected is None:
        return None
    now_epoch = now.timestamp()
    written = 0
    for _name, key, abs_start, timeline, total in projected[0]:
        start = schedule_planner.broadcast_epoch_of(abs_start)
        if start + float(total or 0) <= now_epoch:
            continue
        rows = media.schedule_rows_from_timeline(timeline, start)
        if rows:
            written += hub.place_rows(channel_key, "%s@%d" % (key, int(abs_start)), rows, now_epoch=now_epoch)[1]
    return written


def _visualizer_admission(config):
    """(unseekable song paths, unseekable extensions) the program found on this server, from the profile's settings."""
    unseekable = set((config.get("visualizer_unseekable") or {}).keys())
    formats = (config.get("mixer_seekable_formats") or {}).get("formats") or {}
    return unseekable, {ext for ext, ok in formats.items() if ok is False}


def _scanned_length(path):
    import media
    real, real_kind = media.read_track_duration(path, 0)
    return int(real) if real_kind == media.DURATION_MEASURED and real and real > 0 else None


def plan_loop_channel(hub, channel_key, ch_info, config, now, path_exists):
    """Works a Marathon or Visualizer channel of a profile out day by day with the program's own loop planner (schedule_planner.loop_plan) and writes each day's rows into the hub (the caller has set the profile's scope). Today's Visualizer lineup and a Marathon's clock anchor are kept in the hub, so a change of the songs during the day keeps the song on air and the loop stays on the clock. Returns the number of rows written, or None when the channel cannot be planned (no media, or a Marathon with no start number saved yet)."""
    # WARNING: owner 2026-11-24: Marathon and Visualizer work the same as the other modes; the server's HUB holds their 8 days, each day built with its own lineup.
    import datetime
    import media
    import schedule_planner
    naive = now.replace(tzinfo=None)
    log_ = ch_info.get("playback_log") or {}
    anchor = None
    if ch_info.get("is_visualizer", False):
        stack = ch_info.get("schedules", {}).get("Music Tracks", [])
        unseekable, exts = _visualizer_admission(config)

        def day_lineup(day):
            assets = media.admit_visualizer_assets(stack, unseekable, exts)
            if not assets:
                return [], 0, 0.0
            if day != naive.date():
                return schedule_planner.visualizer_tracks(schedule_planner.visualizer_lineup(
                    channel_key, assets, None, datetime.datetime.combine(day, datetime.time(0, 0)), _scanned_length)[0])
            kept = hub.plan_state(channel_key, "visualizer_lineup")
            day_key = "%d-%03d" % (naive.year, naive.timetuple().tm_yday)
            in_hub = isinstance(kept, dict) and kept.get("day") == day_key
            if not in_hub:
                kept = log_.get("visualizer_lineup")
            lineup, changed = schedule_planner.visualizer_lineup(channel_key, assets, kept, naive, _scanned_length)
            if changed or not in_hub:
                hub.set_plan_state(channel_key, "visualizer_lineup", lineup)
            return schedule_planner.visualizer_tracks(lineup)
    else:
        if not ch_info.get("schedules", {}).get("Marathon"):
            return None
        seed = log_.get("intro_seed")
        if not isinstance(seed, int) or isinstance(seed, bool):
            return None
        anchor = ch_info.get("marathon_anchor_epoch") or hub.plan_state(channel_key, "marathon_anchor")
        if not anchor:
            # WARNING: the same anchor the scheduler sets the first time a Marathon airs: the half hour the clock is in.
            seconds = naive.hour * 3600 + naive.minute * 60 + naive.second
            anchor = naive.toordinal() * 86400 + (seconds // 1800) * 1800
            hub.set_plan_state(channel_key, "marathon_anchor", anchor)

        def day_lineup(_day):
            return [], 0, 0.0
    plan = schedule_planner.loop_plan(channel_key, ch_info, now, path_exists, HOURS_AHEAD, day_lineup, anchor)
    if not plan:
        return None
    now_epoch = now.timestamp()
    written = 0
    for slot_key, day_start, timeline in plan:
        rows = media.schedule_rows_from_timeline(timeline, day_start)
        if rows and rows[-1]["planned_start"] + rows[-1]["used_length"] > now_epoch - ROWS_BEHIND_S:
            written += hub.place_rows(channel_key, slot_key, rows, now_epoch=now_epoch)[1]
    return written


class ProfilePlanner:
    """Service thread: keeps every served profile's schedule in this server's HUB; wake() plans again (a profile's file changed), and a station-clock countdown plans again just after each midnight."""

    def __init__(self, slots_fn, probe_library_fn, path_exists=None):
        self.slots_fn = slots_fn
        self.probe_library_fn = probe_library_fn
        self.path_exists = path_exists
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        # generation moves on after every planning run, so the rows sent to the Users are read from the HUB again only then.
        self.generation = 0
        self._rows_cache = {}

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="ProfilePlanner")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def wake(self, _key=None):
        self._wake.set()

    def plan_all(self):
        """Plans every active channel of every served profile; returns {slot id: rows written}."""
        import broadcast_clock
        import media
        import media_library
        path_exists = self.path_exists or media._cached_path_exists
        hub = media_library.ScheduleHub(self.probe_library_fn().library)
        now = broadcast_clock.now()
        done = {}
        for slot_id, channels, config in server_scanner.read_slots(self.slots_fn()):
            written, skipped = 0, []
            with server_clock._slot_scope(slot_id):
                for channel_key, ch_info in sorted((channels or {}).items()):
                    if not isinstance(ch_info, dict) or not ch_info.get("active", False):
                        continue
                    try:
                        got = plan_channel(hub, str(channel_key).zfill(2), ch_info, config, now, path_exists)
                    except Exception as exc:
                        log.warning("[SERVER PLAN] slot %s channel %s could not be planned: %s", slot_id, channel_key, exc)
                        continue
                    if got is None:
                        skipped.append(str(channel_key).zfill(2))
                    else:
                        written += got
            done[slot_id] = written
            self.generation += 1
            log.info("[SERVER PLAN] slot %s: the next %.0f hours are in the HUB (%d row(s) written)%s", slot_id, HOURS_AHEAD, written,
                     "; not planned here: %s" % ", ".join(skipped) if skipped else "")
        return done

    def rows_for(self, slot_id, channels):
        """(digest, {channel: [row]}) of the slot's planned rows from ROWS_BEHIND_S ago to HOURS_AHEAD ahead, as the HUB holds them after the last planning run; each row is [planned start, length, kind, slot key, position, guide entry]."""
        import hashlib
        import json
        import media_library
        cached = self._rows_cache.get(str(slot_id))
        if cached is not None and cached[0] == self.generation:
            return cached[1], cached[2]
        hub = media_library.ScheduleHub(self.probe_library_fn().library)
        now = station_clock.now_epoch()
        out = {}
        with server_clock._slot_scope(slot_id):
            for channel_key, ch_info in sorted((channels or {}).items()):
                if not isinstance(ch_info, dict) or not ch_info.get("active", False):
                    continue
                key = str(channel_key).zfill(2)
                rows = hub.planned_rows_between(key, now - ROWS_BEHIND_S, now + 3600.0 * HOURS_AHEAD)
                if rows:
                    out[key] = [[float(r["planned_start"]), float(r.get("used_length") or 0.0), r.get("kind", ""), str(r.get("slot_key", "")),
                                 int(r.get("position") or 0), r.get("guide_entry") or {"path": r.get("path", "")}] for r in rows]
        digest = hashlib.sha1(json.dumps(out, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        self._rows_cache[str(slot_id)] = (self.generation, digest, out)
        return digest, out

    def _seconds_to_midnight(self):
        import broadcast_clock
        import datetime
        now = broadcast_clock.now()
        nxt = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1.0, (nxt - now).total_seconds())

    def _run(self):
        while not self._stop.is_set():
            try:
                self.plan_all()
            except Exception:
                log.exception("[SERVER PLAN] planning the served profiles failed")
            # WARNING: owner 2026-11-24: at midnight the day that ended drops off and a new last day is added; nothing looks at the clock in between.
            station_clock.arm_in("server_plan:midnight", self._seconds_to_midnight() + 5.0, self.wake)
            self._wake.wait()
            self._wake.clear()


__all__ = ["ProfilePlanner", "plan_channel", "plan_loop_channel", "HOURS_AHEAD"]
