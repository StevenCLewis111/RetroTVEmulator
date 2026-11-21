"""One music scan order for the program, a server profile and the server service.

WARNING: owner 2026-11-14: the program scans a music channel's songs in this order (the playing song first, the quick songs and the next wave with the live shows, the rest slowly and never ahead of an unscanned TV show); the server service must pick songs through the same functions, never from a list of its own. `src` is the scan_pipeline.ScanSource of the place the channels live; `music_done(path, entry)` on it says whether a song already has its length and gain.
"""

import logging
import os

import scan_pipeline

try:
    import performance_governor
except Exception:
    performance_governor = None

log = logging.getLogger(__name__)

# The scan-class a music pace class counts as when it is lined up with the TV files (scan_pipeline.SCAN_CLASS_*): the quick songs scan with what airs now, the first two waves with the next shows, the rest with the deep backlog.
SCAN_CLASS_OF_PACE = {"now": scan_pipeline.SCAN_CLASS_NOW, "next": scan_pipeline.SCAN_CLASS_NEXT, "deep": scan_pipeline.SCAN_CLASS_DEEP}
PACE_RANK = {"now": 0, "next": 1, "deep": 2}


def source_items(source_key, songs, behind_state):
    """Turns one playlist's songs, in play order from the one playing (distance 0), into ranked scan items (pace class, distance, kind, channel, block, path); songs already scanned are left out and counted toward how far ahead the scan is. behind_state remembers per playlist whether the scan is falling behind."""
    ahead = 0
    counting = True
    pending = []
    for distance, (kind, ch_key, block_key, norm_p, done) in enumerate(songs):
        if done:
            if counting and distance > 0:
                ahead += 1
            continue
        if distance > 0:
            counting = False
        pending.append((distance, kind, ch_key, block_key, norm_p))
    if performance_governor is not None:
        behind = performance_governor.music_scan_falling_behind(behind_state.get(source_key, True), ahead)
    else:
        behind = True
    if behind != behind_state.get(source_key):
        if source_key in behind_state:
            log.info("[MUSIC PRECACHE] %s: %d next songs scanned, %s", source_key, ahead,
                     "falling behind, scanning fast" if behind else "far enough ahead, scanning slower")
        behind_state[source_key] = behind
    out = []
    for distance, kind, ch_key, block_key, norm_p in pending:
        if performance_governor is not None:
            cls = performance_governor.music_scan_class(distance, behind)
        else:
            cls = "now" if distance == 0 else "next"
        out.append((cls, distance, kind, ch_key, block_key, norm_p))
    return out


def channel_songs(rec, exists, music_done):
    """The songs of one music channel as (kind, channel, block, path, done) in play order from the one playing: the lineup first, then the songs still waiting for a length. A channel with no lineup (nothing has a length yet) is walked from the playing song when that is known."""
    songs = []
    seen = set()
    ordered = rec.get("ordered") or []
    current = rec.get("current_file", "")
    block_key = rec.get("block_key") or "Music Tracks"
    for entry in scan_pipeline.next_n_from_ordered(ordered, current, n=len(ordered)):
        for p in scan_pipeline.paths_from_entry(entry):
            if p and exists(p):
                norm_p = os.path.normpath(p)
                if norm_p not in seen:
                    seen.add(norm_p)
                    songs.append((entry, norm_p))
    # WARNING: a song gets into the lineup only once its length is known, so the songs still without one are walked too, after the lineup; without them a Visualizer whose songs are all new never starts (owner log 2026-11-06).
    extra = []
    for p in rec.get("pending", []) or []:
        norm_p = os.path.normpath(p)
        if norm_p not in seen and exists(norm_p):
            seen.add(norm_p)
            extra.append((None, norm_p))
    if not ordered and current:
        norm_cur = os.path.normpath(current)
        for i, (_e, p) in enumerate(extra):
            if p == norm_cur:
                extra = extra[i:] + extra[:i]
                break
    return [("channel", rec.get("channel", ""), block_key, p, bool(music_done(p, entry))) for entry, p in songs + extra]


def gather_queue(src, behind_state):
    """Every song of every active music channel that still needs a length or gain, as (pace class, distance, kind, channel, block, path); a channel a server owns is left to the server (the program reads only its lengths, see the program's own branch)."""
    ranked = []
    for i in range(5, 45):
        ch_str = str(i).zfill(2)
        info = src.channels().get(ch_str, {})
        if not info or not info.get("active", True) or not info.get("is_visualizer", False) or info.get("server_locked", False):
            continue
        rec = src.channel_record(ch_str)
        if rec is None:
            continue
        rec = dict(rec, channel=ch_str)
        ranked.extend(source_items("ch" + ch_str, channel_songs(rec, src.exists, src.music_done), behind_state))
    return ranked


def as_scan_items(ranked):
    """The ranked songs shaped like scan_pipeline.order_scan_pipeline's items (scan class, distance, kind, channel, block, path) so they can be lined up with the TV files by class."""
    return [(SCAN_CLASS_OF_PACE.get(cls, scan_pipeline.SCAN_CLASS_DEEP), distance, kind, ch, block, path)
            for cls, distance, kind, ch, block, path in ranked]
