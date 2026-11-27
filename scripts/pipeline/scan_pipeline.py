"""The scan pipeline the program and the server service share: which files to scan, and in what order.

The program scans its own channels with it, and the server service (program closed, or a profile the program is not showing) scans a server profile's channels with the same code, so a file is picked in the same order wherever it is scanned: airing now on every channel, then upcoming commercials, then the next 3 shows by distance, then the deeper lineup, channels in the order they were set up."""
from __future__ import annotations

import logging
import os
import time

import media

log = logging.getLogger(__name__)

# ============================================================================
# SECTION: Scan classes
# ============================================================================
SCAN_CLASS_NOW = 0
SCAN_CLASS_COMMERCIAL = 1
SCAN_CLASS_NEXT = 2
SCAN_CLASS_DEEP = 3

# WARNING: owner 2026-11-14: the program, a server profile and the server service must all pick files through the functions in this file; a second copy of the order (the service once scanned in alphabetical order) leaves the other PCs without the facts of the files that air first. `src` is a ScanSource: the program fills it from its own channel database and the service from a profile's saved channels.


# ============================================================================
# SECTION: What the pipeline reads (ScanSource)
# ============================================================================


class ScanSource:
    """What the pipeline needs from the place the channels live; every field is a function the program and the server service each supply: channels() -> {"05": channel info}; ordered_content(ch, info) -> (lineup of the airing block, block name); now_file(ch) -> path airing now ("" when unknown); block_peek(ch, info, block name, now) -> (lineup, block name) of a block not on air; visualizer_pending(ch, info) -> songs of a music channel still waiting for a length; now() -> naive datetime; exists(path) -> bool; stages_pending(path, has gain, has duration, lookups) -> True while a scan is missing; timeline_next(ch, current file, n) -> upcoming (path, commercial) items or None; front_load(items) -> items with the watched channel's first; holiday_items() -> (on air, opening soon, later); log_missing(path, ch, block); channel_record(ch) -> the snapshot record of one channel; lookups -> how many library lookups one gather may make; holiday_key(ch) -> the holiday on air or None; today() -> date; holiday_lead_days -> how many days ahead holiday files are scanned."""

    def __init__(self, **fields):
        for name, value in fields.items():
            setattr(self, name, value)

    def holiday_items(self):
        """The unscanned holiday files, by the one holiday gather every place shares."""
        return gather_holiday_scan_items(self)


# ============================================================================
# SECTION: Entries and block names
# ============================================================================


def paths_from_entry(entry):
    if not isinstance(entry, dict):
        return [str(entry)] if entry else []
    if entry.get("is_pair"):
        out = []
        p1 = entry.get("pair_ep1_path", "")
        p2 = entry.get("pair_ep2_path", "")
        if p1:
            out.append(p1)
        if p2:
            out.append(p2)
        return out
    p = entry.get("path", entry.get("file", ""))
    return [p] if p else []


def next_n_from_ordered(ordered, current_file, n=4):
    if not ordered:
        return []
    start_idx = 0
    for i, e in enumerate(ordered):
        if current_file and current_file in paths_from_entry(e):
            start_idx = i
            break
    L = len(ordered)
    return [ordered[(start_idx + k) % L] for k in range(min(n, L))]


def airing_block_name(ch_info, now):
    """The name of the block airing now on a channel: Marathon, Full Day, the four-hour block or the eight-hour block."""
    if ch_info.get("scheduling_mode", "random_slots") == "marathon":
        return "Marathon"
    block_mode = ch_info.get("block_mode", "full_day")
    if block_mode == "full_day":
        return "Full Day"
    if block_mode == "six_block":
        return media.SIX_HOUR_BLOCKS[now.hour // 4][0]
    return "Morning" if 4 <= now.hour < 12 else ("Evening" if 12 <= now.hour < 20 else "Night")


def block_names_after_airing(ch_info, now):
    """The block names of the channel's scheduling mode that air after the current one, in airing order; empty for visualizer, Marathon and Full Day channels."""
    if ch_info.get("is_visualizer", False):
        return []
    if ch_info.get("scheduling_mode", "random_slots") == "marathon":
        return []
    bmode = ch_info.get("block_mode", "full_day")
    if bmode == "full_day":
        return []
    if bmode == "six_block":
        names = list(media.SIX_HOUR_BLOCK_NAMES)
        cur = now.hour // 4
    else:
        names = ["Morning", "Evening", "Night"]
        cur = 0 if 4 <= now.hour < 12 else (1 if 12 <= now.hour < 20 else 2)
    return [names[(cur + k) % len(names)] for k in range(1, len(names))]


# ============================================================================
# SECTION: Scan snapshot
# ============================================================================


def build_scan_snapshot(src, include_server_locked=False):
    """One pass over every active channel (a channel a connected server or a viewed server profile owns only when include_server_locked is set, which the program sets so the Channels-tab bar counts those channels the way it counts its own; the scan queues skip them by themselves): its ordered content for the airing block, the lineups of its other blocks in airing order, the file playing now, and its commercials."""
    channels = {}
    _all = src.channels()
    for i in range(5, 45):
        ch_str = str(i).zfill(2)
        ch_info = _all.get(ch_str, {})
        if not ch_info or not ch_info.get("active", True) or (ch_info.get("server_locked", False) and not include_server_locked):
            continue
        try:
            ordered, block_key = src.ordered_content(ch_str, ch_info)
            current_file = src.now_file(ch_str)
        except Exception as e:
            log.warning("[SCAN SNAPSHOT] ch%s content gather failed (skipped): %s", ch_str, e)
            continue
        commercials_on = bool(ch_info.get("commercials_enabled", False))
        other_blocks = []
        try:
            _snap_now = src.now()
            for _other_name in block_names_after_airing(ch_info, _snap_now):
                _other_ordered, _other_key = src.block_peek(ch_str, ch_info, _other_name, _snap_now)
                if _other_ordered and _other_key:
                    other_blocks.append({"block_key": _other_key, "ordered": list(_other_ordered)})
        except Exception as e:
            log.warning("[SCAN SNAPSHOT] ch%s other-block gather failed (airing block kept): %s", ch_str, e)
            other_blocks = []
        _pending_block, _pending_airing, _pending_other = "", [], []
        if ch_info.get("is_visualizer", False):
            try:
                _pending_block = "Music Tracks"
                _pending_airing = src.visualizer_pending(ch_str, ch_info)
            except Exception as e:
                log.warning("[SCAN SNAPSHOT] ch%s pending-song gather failed (lineup kept): %s", ch_str, e)
                _pending_block, _pending_airing = "", []
        else:
            try:
                _pend_now = src.now()
                _pend_broken = set(ch_info.get("broken_files", []))
                _pend_schedules = ch_info.get("schedules", {})

                def _pending_paths(_name):
                    return [t["path"] for t in media.build_pending_tracks(_pend_schedules.get(_name, []), src.exists)
                            if t["path"] not in _pend_broken]

                _pending_block = airing_block_name(ch_info, _pend_now)
                _pending_airing = _pending_paths(_pending_block)
                for _dist, _other_name in enumerate(block_names_after_airing(ch_info, _pend_now)):
                    _other_pending = _pending_paths(_other_name)
                    if _other_pending:
                        _pending_other.append({"block_key": _other_name, "distance": _dist, "paths": _other_pending})
            except Exception as e:
                log.warning("[SCAN SNAPSHOT] ch%s pending-file gather failed (lineup kept): %s", ch_str, e)
                _pending_block, _pending_airing, _pending_other = "", [], []
        channels[ch_str] = {
            "ordered": list(ordered or []),
            "block_key": block_key,
            "pending_block": _pending_block,
            "pending": _pending_airing,
            "pending_other": _pending_other,
            "other_blocks": other_blocks,
            "current_file": current_file,
            "is_visualizer": bool(ch_info.get("is_visualizer", False)),
            "commercials": list(ch_info.get("schedules", {}).get("Commercials", [])) if commercials_on else [],
        }
    return {"built_at": time.monotonic(), "channels": channels}


# ============================================================================
# SECTION: The scan order
# ============================================================================


def gather_probe_priority_queue(src):
    tiers = [[], [], [], []]
    _pb_lookups = [src.lookups]

    # WARNING: the DVD player has no tier here and never will; DVD/disc-backup playback is never loudness-scanned.

    for i in range(5, 45):
        ch_str = str(i).zfill(2)
        ch_info = src.channels().get(ch_str, {})
        if not ch_info or not ch_info.get("active", True):
            continue
        if ch_info.get("server_locked", False):
            continue
        if ch_info.get("is_visualizer", False):
            _vis_rec = src.channel_record(ch_str)
            if _vis_rec is not None and not _vis_rec["ordered"]:
                # WARNING: owner 2026-11-14: when the song playing now is known (the server service reads it from the shared hub) it is the one that goes in the airing tier; the program has no playing song for a channel with no lineup, so for it the first waiting song is taken as before.
                _vis_now = os.path.normpath(_vis_rec.get("current_file") or "") if _vis_rec.get("current_file") else None
                for _pend_path in sorted(_vis_rec.get("pending", []), key=lambda _p: 0 if os.path.normpath(_p) == _vis_now else 1):
                    _pend_norm = os.path.normpath(_pend_path)
                    if src.exists(_pend_norm):
                        tiers[0].append(("channel", ch_str, "Music Tracks", _pend_norm))
                        break
            continue
        _snap_rec = src.channel_record(ch_str)
        if _snap_rec is None:
            continue
        current_file = _snap_rec["current_file"]
        ordered, block_key = _snap_rec["ordered"], _snap_rec["block_key"]

        # WARNING: a channel with no lineup has no file with a known duration; its first unscanned file goes in the airing tier so the channel can leave Static, the rest of its files follow in the deep backlog.
        if not ordered:
            for _pend_path in _snap_rec.get("pending", []):
                _pend_norm = os.path.normpath(_pend_path)
                if src.exists(_pend_norm) and src.stages_pending(_pend_norm, False, False, _pb_lookups):
                    tiers[0].append(("channel", ch_str, _snap_rec.get("pending_block") or "", _pend_norm))
                    break

        tl_upcoming = None
        if ch_info.get("commercials_enabled", False):
            tl_upcoming = src.timeline_next(ch_str, current_file, n=4)

        if tl_upcoming is not None:
            for k, tl_entry in enumerate(tl_upcoming):
                p = tl_entry.get("path", "")
                if not (p and src.exists(p)):
                    src.log_missing(p, ch_str, block_key or "Commercials")
                    continue
                norm_p = os.path.normpath(p)
                if not src.stages_pending(norm_p, False, False, _pb_lookups):
                    continue
                _label = "Commercials" if tl_entry.get("is_commercial") else (block_key or "")
                tiers[k].append(("channel", ch_str, _label, norm_p))
        elif ordered and block_key:
            upcoming = next_n_from_ordered(ordered, current_file, n=4)
            for k, entry in enumerate(upcoming):
                for p in paths_from_entry(entry):
                    if not (p and src.exists(p)):
                        src.log_missing(p, ch_str, block_key)
                        continue
                    norm_p = os.path.normpath(p)
                    if not src.stages_pending(norm_p, isinstance(entry, dict) and "gain" in entry, False, _pb_lookups):
                        continue
                    tiers[k].append(("channel", ch_str, block_key, norm_p))

        if ch_info.get("commercials_enabled", False) and tl_upcoming is None:
            _comm_list = ch_info.get("schedules", {}).get("Commercials", [])
            _added = 0
            for _casset in _comm_list:
                if _added >= 4:
                    break
                _cp = _casset.get("path", _casset.get("file", "")) if isinstance(_casset, dict) else str(_casset)
                if not (_cp and src.exists(_cp)):
                    src.log_missing(_cp, ch_str, "Commercials")
                    continue
                _cnorm = os.path.normpath(_cp)
                _chas_gain = isinstance(_casset, dict) and "gain" in _casset
                _chas_dur = isinstance(_casset, dict) and _casset.get("duration", -1) not in (-1, 0, None)
                if not src.stages_pending(_cnorm, _chas_gain, _chas_dur, _pb_lookups):
                    continue
                tiers[1].append(("channel", ch_str, "Commercials", _cnorm))
                _added += 1
    tiers = [src.front_load(_t) for _t in tiers]

    return tiers


def gather_deep_backlog_queue(src):
    """Every not-yet-scanned file behind each channel's next 3 shows, as (kind, channel, block, path): holiday files on air or opening soon first, then every channel's lineup by distance from the show on air (each channel's 4th show before any channel's 5th), commercials, the other blocks by how soon they air, and the far holiday files last."""
    out_content = []
    _ranked_content = []
    out_commercials = []
    out_other_blocks = []
    _pb_lookups = [src.lookups]
    for i in range(5, 45):
        ch_str = str(i).zfill(2)
        ch_info = src.channels().get(ch_str, {})
        if not ch_info or not ch_info.get("active", True):
            continue
        if ch_info.get("server_locked", False):
            continue
        _snap_rec = src.channel_record(ch_str)
        if _snap_rec is None:
            continue
        current_file = _snap_rec["current_file"]
        ordered, block_key = _snap_rec["ordered"], _snap_rec["block_key"]
        if ordered and block_key:
            L = len(ordered)
            if L > 4:
                start_idx = 0
                for _i, _e in enumerate(ordered):
                    if current_file and current_file in paths_from_entry(_e):
                        start_idx = _i
                        break
                for k in range(4, L):
                    entry = ordered[(start_idx + k) % L]
                    for p in paths_from_entry(entry):
                        if not (p and src.exists(p)):
                            continue
                        norm_p = os.path.normpath(p)
                        if not src.stages_pending(norm_p, isinstance(entry, dict) and "gain" in entry, False, _pb_lookups):
                            continue
                        _ranked_content.append((k, len(_ranked_content), ("channel", ch_str, block_key, norm_p)))

        if ch_info.get("commercials_enabled", False):
            for _casset in ch_info.get("schedules", {}).get("Commercials", []):
                _cp = _casset.get("path", _casset.get("file", "")) if isinstance(_casset, dict) else str(_casset)
                if not (_cp and src.exists(_cp)):
                    continue
                _cnorm = os.path.normpath(_cp)
                _chas_gain = isinstance(_casset, dict) and "gain" in _casset
                _chas_dur = isinstance(_casset, dict) and _casset.get("duration", -1) not in (-1, 0, None)
                if not src.stages_pending(_cnorm, _chas_gain, _chas_dur, _pb_lookups):
                    continue
                out_commercials.append(("channel", ch_str, "Commercials", _cnorm))

        _lineup_paths = set()
        for _lineup_entry in list(ordered or []) + [_e for _ob in _snap_rec.get("other_blocks", []) for _e in _ob["ordered"]]:
            _lineup_paths.update(os.path.normpath(_lp) for _lp in paths_from_entry(_lineup_entry) if _lp)
        _airing_pending_items = []
        for _pend_path in _snap_rec.get("pending", []):
            _pend_norm = os.path.normpath(_pend_path)
            if _pend_norm in _lineup_paths or not src.exists(_pend_norm):
                continue
            if src.stages_pending(_pend_norm, False, False, _pb_lookups):
                _airing_pending_items.append(("channel", ch_str, _snap_rec.get("pending_block") or "", _pend_norm))
        _pending_base = len(ordered) if ordered else 0
        for _pending_rank, _pending_item in enumerate(_airing_pending_items):
            _ranked_content.append((_pending_base + _pending_rank, len(_ranked_content), _pending_item))
        for _pob in _snap_rec.get("pending_other", []):
            _pob_items = []
            for _pend_path in _pob["paths"]:
                _pend_norm = os.path.normpath(_pend_path)
                if _pend_norm in _lineup_paths or not src.exists(_pend_norm):
                    continue
                if src.stages_pending(_pend_norm, False, False, _pb_lookups):
                    _pob_items.append(("channel", ch_str, _pob["block_key"], _pend_norm))
            out_other_blocks.append((_pob["distance"] + 0.5, _pob_items))
        for _dist, _ob in enumerate(_snap_rec.get("other_blocks", [])):
            _ob_items = []
            for entry in _ob["ordered"]:
                for p in paths_from_entry(entry):
                    if not (p and src.exists(p)):
                        continue
                    norm_p = os.path.normpath(p)
                    if not src.stages_pending(norm_p, isinstance(entry, dict) and "gain" in entry, False, _pb_lookups):
                        continue
                    _ob_items.append(("channel", ch_str, _ob["block_key"], norm_p))
            out_other_blocks.append((_dist, _ob_items))

    out_content = [_item for _d, _seq, _item in sorted(_ranked_content, key=lambda it: (it[0], it[1]))]
    _other_by_distance = sorted(out_other_blocks, key=lambda it: it[0])
    _other_flat = [_item for _d, _items in _other_by_distance for _item in _items]
    _hol_active, _hol_soon, _hol_later = src.holiday_items()
    out = src.front_load(_hol_active + _hol_soon + out_content + out_commercials + _other_flat + _hol_later)
    return out


def order_scan_pipeline(tiers, deep_items, rank_fn, seeds_fn):
    """One ordered list of (class, tier, kind, channel, block, path): airing files on every channel first, then upcoming commercials, then the next 3 shows by distance, then the deeper lineup. A path keeps its best slot."""
    deep_items = seeds_fn(deep_items)
    ranked = []
    for tier_idx, tier_items in enumerate(tiers):
        for kind, ch_key, block_key, norm_p in tier_items:
            if tier_idx == 0:
                cls = SCAN_CLASS_NOW
            elif block_key == "Commercials":
                cls = SCAN_CLASS_COMMERCIAL
            else:
                cls = SCAN_CLASS_NEXT
            ranked.append((cls, tier_idx, kind, ch_key, block_key, norm_p))
    for kind, ch_key, block_key, norm_p in deep_items:
        ranked.append((SCAN_CLASS_DEEP, 4, kind, ch_key, block_key, norm_p))
    # WARNING: within a scan class, channels go in the order they were set up (owner 2026-11-02), the same order as the first-show scan; the watched channel's own scan is started at the tune and never waits for this order.
    ranked.sort(key=lambda it: (it[0], it[1], rank_fn(it[3]) if it[2] == "channel" else (0, 0)))
    out, seen = [], set()
    for it in ranked:
        if it[5] in seen:
            continue
        seen.add(it[5])
        out.append(it)
    return out


def season_seeds_first(deep_items, min_scanned):
    """Moves the first `min_scanned` files of every season ahead of the rest of the backlog (the rest keep their order), so the season has the scanned episodes its other episodes borrow from."""
    seeds, rest, taken = [], [], {}
    for item in deep_items:
        guess_class, season_key = media._guess_class(item[3])
        if guess_class == "episode" and taken.get(season_key, 0) < min_scanned:
            taken[season_key] = taken.get(season_key, 0) + 1
            seeds.append(item)
        else:
            rest.append(item)
    return seeds + rest


def channel_scan_rank(ch_key, added_order):
    """Where a channel's files go among those of the same scan class: channels set up before this run by number, then channels set up during this run in the order they were set up (`added_order` = {channel: place})."""
    ch = str(ch_key or "").zfill(2)
    _added = added_order.get(ch)
    return (0, int(ch) if ch.isdigit() else 0) if _added is None else (1, _added)


# ============================================================================
# SECTION: Holiday files
# ============================================================================


def gather_holiday_scan_items(src):
    """The unscanned files of every channel's holiday lists as (kind, channel, holiday key, path) in three lists: on air now, opening within src.holiday_lead_days, and later; each later list is soonest first and a path appears once."""
    active, upcoming = [], []
    try:
        today = src.today()
        _pb_lookups = [src.lookups]
        seen = set()
        for i in range(5, 45):
            ch_str = str(i).zfill(2)
            ch_info = src.channels().get(ch_str, {})
            if not ch_info or not ch_info.get("active", True) or ch_info.get("server_locked", False):
                continue
            if ch_info.get("is_visualizer", False):
                continue
            broken = set(ch_info.get("broken_files", []))
            on_air_key = src.holiday_key(ch_str)
            for h_key in media.HOLIDAY_KEYS:
                entries = (ch_info.get("holiday_schedules") or {}).get(h_key) or []
                distance = 0 if on_air_key == h_key else media.holiday_days_until_open(h_key, today)
                if distance is None:
                    continue
                for entry in entries:
                    for p in paths_from_entry(entry):
                        if not (p and src.exists(p)):
                            continue
                        norm_p = os.path.normpath(p)
                        if norm_p in broken or norm_p in seen:
                            continue
                        if not src.stages_pending(norm_p, isinstance(entry, dict) and "gain" in entry, False, _pb_lookups):
                            continue
                        seen.add(norm_p)
                        item = ("channel", ch_str, h_key, norm_p)
                        if on_air_key == h_key:
                            active.append(item)
                        else:
                            upcoming.append((distance, item))
    except Exception as e:
        log.warning("[HOLIDAY SCAN] gathering the holiday files to scan failed (the rest of the scan continues): %s", e)
        return [], [], []
    upcoming.sort(key=lambda pair: pair[0])
    soon = [item for dist, item in upcoming if dist <= src.holiday_lead_days]
    later = [item for dist, item in upcoming if dist > src.holiday_lead_days]
    return active, soon, later
