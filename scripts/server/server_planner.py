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

# ============================================================================
# SECTION: Settings
# ============================================================================
try:
    import performance_governor as _gov
except Exception as _governor_error:
    _gov = None
    logging.getLogger(__name__).warning("[FAILURE] server_planner: performance_governor did not import, so the built-in numbers are used: %s", _governor_error)


def _log_swallowed(where, error):
    """Writes the shared, counted [FAILURE] line through performance_governor (a plain warning when it did not import)."""
    if _gov is not None:
        return _gov.log_swallowed(where, error)
    logging.getLogger(__name__).warning("[FAILURE] %s: %s: %s", where, type(error).__name__, error)
    return None


# WARNING: the fallback values below must match the same names in performance_governor.py; they are used only when that module fails to import.
HOURS_AHEAD = _gov.HUB_FUTURE_HOURS_AHEAD if _gov is not None else 192.0
ROWS_BEHIND_S = _gov.SERVER_ROWS_BEHIND_S if _gov is not None else 10800.0
PLAN_RETRY_S = _gov.SERVER_PLAN_RETRY_SECONDS if _gov is not None else 120.0


# ============================================================================
# SECTION: Planning one channel
# ============================================================================


def channel_is_active(ch_info):
    """True for a channel the profile airs; a channel record without the "active" mark is on, as everywhere in the program."""
    return isinstance(ch_info, dict) and bool(ch_info.get("active", True))


def newest_sets_only(rows):
    """The rows of each block's newest set ("<block>#g<n>"): a block planned again opens a new set and keeps the old one's unaired rows, which must not be sent twice."""
    # WARNING: owner logs 2026-11-25 (send_logs_tray5): every 2-minute planning pass opened new sets for ch07 and its rows ahead grew 486, 880, 1270, 1658 with the old sets sent alongside.
    def _set_of(row):
        key = str(row.get("slot_key", ""))
        base, _sep, gen = key.partition("#g")
        return base, int(gen) if gen.isdigit() else 0
    newest = {}
    for row in rows:
        base, gen = _set_of(row)
        newest[base] = max(newest.get(base, gen), gen)
    return [row for row in rows if _set_of(row)[1] == newest[_set_of(row)[0]]]


def planners_rows(rows):
    """The rows of the planner's own sets ("<block>@<start>") when there are any, else all of them, the newest set of each block only; rows the program on this computer filed under the plain block name while it worked a channel out itself never mix with the plan."""
    own = [r for r in rows if "@" in str(r.get("slot_key", ""))]
    return newest_sets_only(own if own else rows)


def why_not_planned(ch_info):
    """The reason plan_channel gave no plan for a channel, in words for the log."""
    if ch_info.get("is_visualizer", False):
        return "no songs this server can play"
    if ch_info.get("scheduling_mode", "random_slots") == "marathon" and not ch_info.get("schedules", {}).get("Marathon"):
        return "no Marathon list"
    seed = (ch_info.get("playback_log") or {}).get("intro_seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        return "no start number saved yet (the program has not aired it)"
    import media
    if media.get_duration_hub() is None:
        return "no file lengths can be read (the service's length reader isn't installed)"
    paths = [i.get("path", i.get("file", "")) if isinstance(i, dict) else str(i) for items in (ch_info.get("schedules") or {}).values() for i in (items or [])]
    if paths and not any(media.read_track_duration(p, 0)[1] == media.DURATION_MEASURED for p in paths[:400]):
        return "none of its files has a measured length yet (they are planned once the scan has measured them)"
    return "nothing to air (no lineup could be built from its files)"


def _block_before(block_mode, now):
    """The key of the block that aired before the one now is inside (the last one before now when now is in a gap of the layout), or None."""
    import datetime
    import media
    on_air = media.block_key_containing(block_mode, now)
    moment = now
    for _step in range(4 * 48):
        moment = moment - datetime.timedelta(minutes=15)
        key = media.block_key_containing(block_mode, moment)
        if key and key != on_air:
            return key
    return None


def _cycle_on_air_rows(ch_info, built, now, before_epoch):
    """(set key, rows) of each cycle of the built block that the program recorded in the channel's guide ledger and that is still airing or still to come before before_epoch: the plan itself starts where that cycle ends, so without it nothing from the server covers now."""
    import media
    import schedule_planner
    now_abs = now.toordinal() * 86400 + media.seconds_since_midnight_of(now)
    out = []
    for rec_id, rec in sorted(((ch_info.get("playback_log") or {}).get(media.GUIDE_AIRED_LEDGER_KEY) or {}).items()):
        if not isinstance(rec, dict) or str(rec_id).rsplit("@", 1)[0] != str(built):
            continue
        start = int(rec.get("start", 0) or 0)
        timeline = rec.get("timeline") or []
        end = start + max(float(rec.get("total", 0) or 0),
                          max((float(e.get("start_time", 0) or 0) + float(e.get("duration", 0) or 0) for e in timeline if isinstance(e, dict)), default=0.0))
        if end <= now_abs:
            continue
        rows = [r for r in media.schedule_rows_from_timeline(timeline, schedule_planner.broadcast_epoch_of(start))
                if before_epoch is None or float(r.get("planned_start") or 0) < before_epoch - 0.5]
        if rows:
            out.append(("%s@%d" % (built, start), rows))
    return out


def plan_channel(hub, channel_key, ch_info, config, now, path_exists):
    """Works one channel of a profile out from its saved positions and writes the rows of every block from the one on air to HOURS_AHEAD hours ahead into the hub (the caller has set the profile's scope), then puts aside the sets an earlier pass wrote that this one no longer holds; a Marathon or Visualizer channel goes to plan_loop_channel. Returns the number of rows written, or None when the channel cannot be planned here (no start number saved yet)."""
    import media
    import schedule_planner
    if ch_info.get("is_visualizer", False) or ch_info.get("scheduling_mode", "random_slots") == "marathon":
        return plan_loop_channel(hub, channel_key, ch_info, config, now, path_exists)
    snap = schedule_planner.snapshot(ch_info)
    seed = (snap.get("playback_log") or {}).get("intro_seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        return None
    built = (snap.get("playback_log") or {}).get("sched_block_key") or ""
    if not built:
        # WARNING: owner logs 2026-11-25 (send_logs_tray6): a channel no program had built a block for yet (new, edited, or its mode changed) was "nothing to air" here forever, while the programs waited for this server's rows; the block on air is now worked out here from its start, from the saved positions, the same on every computer.
        snap["playback_log"]["sched_block_key"] = _block_before(snap.get("block_mode", "full_day"), now) or ""
    forced = media.holiday_forced_key_for_channel(config, channel_key)
    projected = schedule_planner.project_station(channel_key, snap, now, path_exists, lambda offset: "%d:%d" % (seed, int(offset)),
                                                 forced, HOURS_AHEAD, require_block_on_air=False)
    if projected is None or not projected[0]:
        return None
    now_epoch = now.timestamp()
    written = 0
    kept = []
    first_start = None
    for _name, key, abs_start, timeline, total in projected[0]:
        start = schedule_planner.broadcast_epoch_of(abs_start)
        if start + float(total or 0) <= now_epoch:
            continue
        first_start = start if first_start is None else min(first_start, start)
        rows = media.schedule_rows_from_timeline(timeline, start)
        if rows:
            set_key = "%s@%d" % (key, int(abs_start))
            written += hub.place_rows(channel_key, set_key, rows, now_epoch=now_epoch)[1]
            kept.append(set_key)
    if built:
        # WARNING: owner logs 2026-11-25 (send_logs60, tray6): the plan starts where the cycle on air ends, so both computers found the server's rows starting 8 to 10 hours away and each worked the hours between out on its own; the cycle on air comes from the ledger the program keeps of it.
        for set_key, rows in _cycle_on_air_rows(ch_info, built, now, first_start):
            written += hub.place_rows(channel_key, set_key, rows, now_epoch=now_epoch)[1]
            kept.append(set_key)
    if kept:
        put_aside = hub.set_aside_other_sets(channel_key, kept, now_epoch)
        if put_aside:
            log.info("[SERVER PLAN] ch%s: %d set(s) an earlier pass wrote were put aside (this pass planned those hours again), so every computer reads one schedule", channel_key, put_aside)
    return written


# ============================================================================
# SECTION: Marathon and Visualizer channels
# ============================================================================


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


# ============================================================================
# SECTION: The service's planning thread
# ============================================================================


class ProfilePlanner:
    """Service thread: keeps every served profile's schedule in this server's HUB; wake() plans again (a profile's file changed), and a station-clock countdown plans again just after each midnight."""

    def __init__(self, slots_fn, probe_library_fn, path_exists=None):
        self.slots_fn = slots_fn
        self.probe_library_fn = probe_library_fn
        self.path_exists = path_exists
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        # WARNING: generation moves on after every planning run, so the rows sent to the Users are read from the HUB again only then.
        self.generation = 0
        self._rows_cache = {}
        self._retry_only = None
        self._last_empty = set()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="ProfilePlanner")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def wake(self, _key=None):
        self._retry_only = None
        self._wake.set()

    def _wake_for_retry(self, _key=None):
        """The retry after a pass that left channels with no rows: only those channels are planned again."""
        # WARNING: owner logs 2026-11-25 (send_logs_tray5): one channel that could not be planned replanned every channel every 2 minutes, rewrote 6,900 rows each time and sent them all to the Users again.
        if self._retry_only is None:
            self._retry_only = set(self._last_empty)
        self._wake.set()

    def plan_all(self, only=None):
        """Plans every active channel of every served profile (only: just these (slot id, channel) pairs); returns {slot id: rows written}."""
        import broadcast_clock
        import media
        import media_library
        path_exists = self.path_exists or media._cached_path_exists
        hub = media_library.ScheduleHub(self.probe_library_fn().library)
        now = broadcast_clock.now()
        done = {}
        retry = False
        now_epoch = now.timestamp()
        for slot_id, channels, config in server_scanner.read_slots(self.slots_fn()):
            written, parts, empty = 0, [], []
            with server_clock._slot_scope(slot_id):
                for channel_key, ch_info in sorted((channels or {}).items()):
                    key = str(channel_key).zfill(2)
                    if not isinstance(ch_info, dict) or not key.isdigit() or not 5 <= int(key) <= 44:
                        continue
                    if not channel_is_active(ch_info):
                        parts.append("ch%s off" % key)
                        continue
                    if only is not None and (slot_id, key) not in only:
                        continue
                    try:
                        got = plan_channel(hub, key, ch_info, config, now, path_exists)
                    except Exception as exc:
                        log.warning("[SERVER PLAN] slot %s channel %s could not be planned: %s", slot_id, key, exc)
                        parts.append("ch%s failed" % key)
                        empty.append(key)
                        self._last_empty.add((slot_id, key))
                        continue
                    held = len(hub.planned_rows_between(key, now_epoch, now_epoch + 3600.0 * HOURS_AHEAD))
                    if got is None:
                        parts.append("ch%s not planned: %s" % (key, why_not_planned(ch_info)))
                    else:
                        written += got
                        parts.append("ch%s %d new, %d ahead in the HUB" % (key, got, held))
                    if not held:
                        empty.append(key)
                        self._last_empty.add((slot_id, key))
            done[slot_id] = written
            self.generation += 1
            log.info("[SERVER PLAN] slot %s: the next %.0f hours are in the HUB (%d row(s) written): %s", slot_id, HOURS_AHEAD, written,
                     "; ".join(parts) or "the profile has no channels 05 to 44")
            if empty:
                retry = True
                log.warning("[SERVER PLAN] slot %s: %s hold no rows ahead, so a User watching them has no schedule from this server; planned again in %.0f s",
                            slot_id, ", ".join("ch" + k for k in empty), PLAN_RETRY_S)
        if retry:
            station_clock.arm_in("server_plan:retry", PLAN_RETRY_S, self._wake_for_retry)
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
                if not channel_is_active(ch_info):
                    continue
                key = str(channel_key).zfill(2)
                rows = planners_rows(hub.planned_rows_between(key, now - ROWS_BEHIND_S, now + 3600.0 * HOURS_AHEAD))
                if rows:
                    out[key] = [[float(r["planned_start"]), float(r.get("used_length") or 0.0), r.get("kind", ""), str(r.get("slot_key", "")),
                                 int(r.get("position") or 0), r.get("guide_entry") or {"path": r.get("path", "")}] for r in rows]
        digest = hashlib.sha1(json.dumps(out, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        self._rows_cache[str(slot_id)] = (self.generation, digest, out)
        return digest, out

    def _run(self):
        while not self._stop.is_set():
            try:
                only, self._retry_only = self._retry_only, None
                self._last_empty = set()
                self.plan_all(only)
            except Exception:
                log.exception("[SERVER PLAN] planning the served profiles failed")
            # WARNING: owner 2026-11-24: at midnight the day that ended drops off and a new last day is added; nothing looks at the clock in between.
            station_clock.arm_in("server_plan:midnight", station_clock.seconds_to_midnight() + 5.0, self.wake)
            self._wake.wait()
            self._wake.clear()


__all__ = ["ProfilePlanner", "plan_channel", "plan_loop_channel", "channel_is_active", "why_not_planned", "HOURS_AHEAD"]
