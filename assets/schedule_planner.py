"""What a station airs, worked out from its saved settings and show positions: shared by the program (the guide's future rows) and the server service (each served profile's schedule in its HUB), so both always work it out the same way (owner 2026-11-24: "just pipeline or share code for midnight recache of the tv guide ... keeping a constant 7 day cache of scheduled info").

It plays nothing, changes no saved position and invents no file length (R16, R18): it steps forward from the positions the scheduler saved exactly as the catch-up steps through a gap (media.project_future_blocks).
"""
import copy
import datetime

import broadcast_clock
import media

try:
    from performance_governor import log_swallowed as _log_swallowed
except Exception:
    def _log_swallowed(where, error):
        """Fallback used only when performance_governor cannot be imported: the failure line is lost, nothing else changes."""
        return None


def snapshot(ch_info):
    """A copy of just the parts of a channel the projection reads, so the scheduler thread changing its saved state cannot change it half way through."""
    _log = ch_info.get("playback_log", {}) or {}
    _keep = ("sched_block_key", "show_pos", "show_order", "commercial_next_pos", "commercial_pos", "block_anchors",
             "sched_cycle_boundary", "sched_cycle_total", "intro_seed", media.BLOCK_OVERRUN_KEY)
    _snap = {k: copy.deepcopy(v) for k, v in ch_info.items() if k not in ("playback_log", "schedules")}
    _snap["schedules"] = {name: list(items) for name, items in (ch_info.get("schedules", {}) or {}).items()}
    _snap["playback_log"] = {k: copy.deepcopy(_log[k]) for k in _keep if k in _log}
    return _snap


def broadcast_epoch_of(abs_second):
    """The clock second (as now().timestamp() counts) of a broadcast wall time given as day number * 86400 + whole seconds since that day's midnight, read in the zone the broadcast clock is in."""
    # WARNING: owner log 2026-11-23: a streaming computer one hour off its server reads the server's wall time; turning it into a clock second in this computer's own zone put every future row an hour late, and the guide drew OFF AIR and cut cells around them.
    _moment = datetime.datetime.fromordinal(int(abs_second) // 86400) + datetime.timedelta(seconds=int(abs_second) % 86400)
    _offset = broadcast_clock.server_utc_offset()
    if _offset is None:
        return _moment.timestamp()
    return _moment.replace(tzinfo=datetime.timezone(datetime.timedelta(seconds=int(_offset)))).timestamp()


def missed_block_day_seeds(prev_block_key, block_name, channel_num, block_offset, now_dt, max_missed=21900):
    if not prev_block_key:
        return []
    parts = prev_block_key.split("_", 2)
    if len(parts) < 2:
        return []
    try:
        prev_year = int(parts[0])
        prev_yday = int(parts[1])
        prev_date = datetime.date(prev_year, 1, 1) + datetime.timedelta(days=prev_yday - 1)
    except (TypeError, ValueError, OverflowError) as _swallowed_error:
        _log_swallowed("missed_block_day_seeds: prev_year = int(parts[0])", _swallowed_error)
        return []

    now_date = now_dt.date()
    gap_days = (now_date - prev_date).days
    if gap_days <= 1:
        return []

    n_missed = min(gap_days - 1, max_missed)
    start_offset = gap_days - n_missed
    seeds = []
    for i in range(n_missed):
        d = prev_date + datetime.timedelta(days=start_offset + i)
        seeds.append(media.block_day_seed(d, channel_num, block_offset))
    return seeds


def project_station(channel_key, snap, now, path_exists, order_seed_for, forced_holiday_key, hours_ahead, require_block_on_air=True):
    """(records, holiday_today): the (block name, key, start second, timeline, length) records of what a Full Day or block station airs until hours_ahead hours from now, holiday days included, in start order, and whether today is one of its holiday days; with require_block_on_air, None when the block on air is not the last block the scheduler built (the program leaves that channel to its scheduler). Without it (the server service, where no scheduler runs the profile), the records start from the last block built, so the blocks since then and the one on air are worked out the same way the catch-up does."""
    _items = snap.get("schedules", {}).get("Commercials", [])
    _commercials = media.build_commercial_tracks(_items, path_exists) if snap.get("commercials_enabled", False) and _items else []
    _playable = {}

    def _holiday_of(_d):
        _k = media.holiday_key_for_date(_d, forced_holiday_key, now.date())
        if not _k:
            return None
        if _k not in _playable:
            _playable[_k] = bool(media.holiday_day_tracks(snap, _k, path_exists)[0])
        return _k if _playable[_k] else None

    _is_holiday = lambda _d: _holiday_of(_d) is not None
    _log = snap.get("playback_log", {}) or {}
    _mode = snap.get("block_mode", "full_day")
    _holiday_today = _is_holiday(now.date())
    if require_block_on_air and not _holiday_today and (not _log.get("sched_block_key") or _log.get("sched_block_key") != media.block_key_containing(_mode, now)):
        return None
    _records = [] if _holiday_today else media.project_future_blocks(
        channel_key, snap, now, path_exists, _commercials, order_seed_for, _is_holiday, hours_ahead)
    _records = _records + media.project_holiday_days(
        channel_key, snap, now, path_exists, _commercials, order_seed_for, _holiday_of, missed_block_day_seeds, hours_ahead)
    _records.sort(key=lambda _r: _r[2])
    return _records, _holiday_today


def visualizer_lineup(channel_key, assets, saved, now, scanned_length):
    """(lineup, changed): a Visualizer channel's lineup for now's day, {"day", "members", "order", "durations", "origin"}, kept from saved when saved is that day's lineup. A new day's lineup is the day's own shuffle of the admitted songs (assets, as (path, asset) pairs) starting at midnight; during the day a change of the songs or a newly measured length keeps the order and moves the origin so the song airing now keeps its exact position. scanned_length(path) is the measured length in whole seconds or None."""
    # WARNING: this is the ONLY place a Visualizer lineup is worked out; the program's scheduler, its guide and the server service all use it, so they can never disagree.
    import random
    current_seconds = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1_000_000.0
    day_key = "%d-%03d" % (now.year, now.timetuple().tm_yday)
    members = sorted({p for p, _a in assets})
    day_seed = now.year * 1000 + now.timetuple().tm_yday + int(str(channel_key).zfill(2)) + 400

    def _best_length(path, asset):
        real = scanned_length(path)
        if real is not None:
            return real
        return int(asset["duration"])

    lineup = copy.deepcopy(saved) if isinstance(saved, dict) else None
    if (not isinstance(lineup, dict) or lineup.get("day") != day_key
            or not lineup.get("order")
            or any(not lineup.get("durations", {}).get(p, 0) for p in lineup.get("order", []))):
        order = [p for p, _a in assets]
        random.Random(day_seed).shuffle(order)
        lineup = {"day": day_key, "members": members, "order": order,
                  "durations": {p: _best_length(p, a) for p, a in assets}, "origin": 0.0}
    else:
        upgrades = {}
        for p in lineup.get("unmeasured", []):
            real = scanned_length(p)
            if real is not None:
                upgrades[p] = real
        if lineup.get("members") == members and not upgrades:
            return lineup, False
        old_order = lineup["order"]
        old_durs = lineup["durations"]
        old_members = set(lineup.get("members", []))
        old_total = sum(old_durs[p] for p in old_order)
        old_pos = (current_seconds - lineup.get("origin", 0.0)) % old_total
        airing_idx, airing_seek, walked = 0, 0.0, 0
        for i, p in enumerate(old_order):
            d = old_durs[p]
            if walked + d > old_pos:
                airing_idx, airing_seek = i, old_pos - walked
                break
            walked += d
        member_set = set(members)
        airing_survived = old_order[airing_idx] in member_set
        survivors_before = sum(1 for p in old_order[:airing_idx] if p in member_set)
        added = [p for p, _a in assets if p not in old_members]
        random.Random(day_seed).shuffle(added)
        new_order = [p for p in old_order if p in member_set] + added
        asset_by_path = {}
        for p, a in assets:
            asset_by_path.setdefault(p, a)
        new_durs = {}
        for p in members:
            if p in upgrades:
                new_durs[p] = upgrades[p]
            elif p in old_members and p in old_durs:
                new_durs[p] = old_durs[p]
            else:
                new_durs[p] = _best_length(p, asset_by_path.get(p, {}))
        new_total = sum(new_durs[p] for p in new_order)
        new_idx = survivors_before % len(new_order)
        new_start = sum(new_durs[p] for p in new_order[:new_idx])
        new_seek = airing_seek if airing_survived else 0.0
        lineup["members"] = members
        lineup["order"] = new_order
        lineup["durations"] = new_durs
        lineup["origin"] = (current_seconds - (new_start + new_seek)) % new_total
    lineup["unmeasured"] = [p for p in members if scanned_length(p) is None]
    return lineup, True


def visualizer_tracks(lineup):
    """(track list, total seconds, origin seconds) of a Visualizer lineup; the loop position is (seconds since midnight - origin) % total."""
    durs = lineup["durations"]
    track_list = [{"path": p, "duration": durs[p]} for p in lineup["order"]]
    return track_list, sum(t["duration"] for t in track_list), float(lineup.get("origin", 0.0))


def _entry_parts_for_cut(entry):
    """The (file, length) parts an entry plays, in order, the way media.resolve_lineup_playback walks them."""
    if entry.get("is_pair"):
        return [(entry.get("pair_ep1_path", entry.get("path", "")), entry.get("pair_ep1_duration", 0)), (entry.get("pair_ep2_path", ""), entry.get("pair_ep2_duration", 0))]
    if entry.get("is_bundle"):
        return list(zip(entry.get("bundle_item_paths") or media.lineup_paths([entry]), entry.get("bundle_item_durations") or []))
    return None


def cut_entry_front(entry, cut):
    """The entries that air what is left of entry once its first cut seconds are gone (the clock moved to a new lineup part way into it): one entry for a plain show or commercial, and one per file still to play for a pair or bundle, the first starting part way in. Each carries duration and seek_offset; start_time is left to the caller."""
    cut = float(cut)
    parts = _entry_parts_for_cut(entry)
    if parts is None:
        piece = dict(entry)
        piece["seek_offset"] = float(entry.get("seek_offset", 0) or 0) + cut
        piece["duration"] = float(entry.get("duration", 0) or 0) - cut
        piece["interior_cut"] = True
        return [piece] if piece["duration"] > 0.05 else []
    pieces = []
    walked = 0.0
    for path, length in parts:
        length = float(length or 0)
        if not path or length <= 0:
            continue
        if walked + length <= cut + 0.05:
            walked += length
            continue
        into = max(0.0, cut - walked)
        piece = {"path": path, "duration": length - into, "is_commercial": False}
        if into > 0:
            piece["seek_offset"] = into
            piece["interior_cut"] = True
        pieces.append(piece)
        walked += length
    return pieces


def loop_day_timeline(timeline, total, ref_abs, day_ordinal):
    """One day of a looping channel (Marathon or Visualizer) as a timeline from that day's midnight (start_time in seconds after it): passes of timeline end to end, the loop position at any moment being (moment - ref_abs) % total in broadcast wall seconds. The entry on air at midnight starts there with what is left of it, and the one on air at the next midnight ends there, because the next day airs its own lineup from that moment."""
    start_abs = int(day_ordinal) * 86400
    end_abs = start_abs + 86400
    total = float(total)
    out = []
    if total <= 0 or not timeline:
        return out
    p = start_abs - ((start_abs - float(ref_abs)) % total)
    guard = 0
    while p < end_abs and guard < 5000:
        guard += 1
        for entry in timeline:
            length = float(entry.get("duration", 0) or 0)
            begins = p + float(entry.get("start_time", 0) or 0)
            if length <= 0 or begins + length <= start_abs:
                continue
            if begins >= end_abs:
                break
            pieces = cut_entry_front(entry, start_abs - begins) if begins < start_abs else [dict(entry)]
            at = max(begins, start_abs)
            for piece in pieces:
                piece["start_time"] = at - start_abs
                if at + float(piece["duration"]) > end_abs:
                    piece["duration"] = end_abs - at
                at += float(piece["duration"])
                out.append(piece)
        p += total
    return out


def loop_plan(channel_key, ch_info, now, path_exists, hours_ahead, day_lineup, marathon_anchor):
    """What a Marathon or Visualizer channel airs from the start of today until hours_ahead hours from now, as (slot key, the day's midnight clock second, the day's timeline) for each day, each day built with that day's own lineup the way the scheduler builds it: a Marathon with that day's breaks around the same series order, kept on the clock by marathon_anchor (broadcast wall seconds); a Visualizer with day_lineup(date) -> (track list, total seconds, origin seconds). An empty list when the channel cannot be worked out."""
    naive = now.replace(tzinfo=None)
    first = naive.toordinal()
    last = int((naive.toordinal() * 86400 + naive.hour * 3600 + naive.minute * 60 + naive.second + float(hours_ahead) * 3600.0) // 86400)
    plan = []
    if ch_info.get("is_visualizer", False):
        for ordinal in range(first, last + 1):
            day = datetime.datetime.fromordinal(ordinal)
            tracks, total, origin = day_lineup(day.date())
            if not tracks or total <= 0:
                continue
            cycle, at = [], 0.0
            for t in tracks:
                d = float(t.get("duration", 0) or 0)
                if d > 0:
                    cycle.append({"path": t["path"], "duration": d, "start_time": at, "is_commercial": False})
                    at += d
            day_tl = loop_day_timeline(cycle, at, ordinal * 86400 + float(origin), ordinal)
            if day_tl:
                plan.append(("%s@%d" % (media.block_key_for(day, "Music"), ordinal * 86400), broadcast_epoch_of(ordinal * 86400), day_tl))
        return plan
    if not marathon_anchor:
        return plan
    items = ch_info.get("schedules", {}).get("Marathon", [])
    raw = media.build_raw_tracks(items, path_exists)
    seed = (ch_info.get("playback_log") or {}).get("intro_seed")
    if not raw or not isinstance(seed, int) or isinstance(seed, bool):
        return plan
    ordered = media.build_ordered_tracks(raw, "marathon", "%d:marathon" % seed)
    durs = (ch_info.get("playback_log") or {}).get("marathon_cached_durations", {}) or {}
    tracks = media.apply_cached_durations(ordered, durs) if durs else ordered
    commercials = []
    if ch_info.get("commercials_enabled", False):
        citems = ch_info.get("schedules", {}).get("Commercials", [])
        commercials = media.build_commercial_tracks(citems, path_exists) if citems else []
    state = copy.deepcopy((ch_info.get("playback_log") or {}).get("commercial_pos"))
    for ordinal in range(first, last + 1):
        day = datetime.datetime.fromordinal(ordinal)
        day_seed = media.block_day_seed(media.block_start_date(day, "Marathon"), channel_key, media.block_seed_offset("Marathon", 0))
        timeline, total, _next_state = media.build_channel_timeline(tracks, commercials, ch_info, day_seed, commercial_state=copy.deepcopy(state))
        day_tl = loop_day_timeline(timeline or [], float(total or 0), float(marathon_anchor), ordinal)
        if day_tl:
            plan.append(("%s@%d" % (media.block_key_for(day, "Marathon"), ordinal * 86400), broadcast_epoch_of(ordinal * 86400), day_tl))
    return plan
