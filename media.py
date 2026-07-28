# ==============================================================================
# PART 1 OF 4: SYSTEM CONFIGURATION & CHANNELS PROFILE STORAGE ENGINE
# ==============================================================================

import os
import sys
import json
import copy
import ctypes
import threading
import shutil
import time
import logging

log = logging.getLogger(__name__)

# Shared libVLC instance reused by get_media_duration() for probing file
# durations. Creating a fresh vlc.Instance() per file is expensive (full
# engine init each call) and was the main cause of the lag/freeze seen when
# queuing media for multiple channels at once, since each channel's probing
# thread was spinning up its own libVLC instance concurrently. One shared
# instance, guarded by a lock, removes that per-file startup cost.
_duration_probe_instance = None
_duration_probe_lock = threading.Lock()

# In-flight guards for loudness and duration probes
# ─────────────────────────────────────────────────
# Two independent probe systems (the continuous _probe_upcoming_media background
# thread and the per-channel _probe_and_patch_durations workers) can both call
# get_media_loudness_gain / get_media_duration for the same file at the same
# time. Without a shared guard that spans BOTH call sites, the same ffmpeg or
# VLC process is launched 2-4× per file per boot -- see app_log.txt where
# short commercials and song files report identical LUFS values in back-to-back
# lines within milliseconds of each other (two threads finishing the same work
# at the same wall-clock moment). The fix lives at the function level so every
# call site, present and future, is covered automatically.
#
# Design choice: return 1.0 / None immediately if the same path is already
# being probed rather than blocking the caller. The file simply stays in the
# "unprobed" queue and gets picked up on the next pass (≤20 s later). Waiting
# on the in-flight result would be equally correct but adds complexity without
# meaningfully improving latency -- callers are already designed for retry.
_loudness_inflight      = set()
_loudness_inflight_lock = threading.Lock()
_duration_inflight      = set()
_duration_inflight_lock = threading.Lock()

def _get_duration_probe_instance():
    global _duration_probe_instance
    if _duration_probe_instance is None:
        with _duration_probe_lock:
            if _duration_probe_instance is None:
                import vlc
                _duration_probe_instance = vlc.Instance("--no-xlib", "--quiet")
    return _duration_probe_instance

def _natural_ep_key(item):
    """Sort key for episodes within one folder: ep2 < ep10, SxxExx aware.

    Marathon-mode movie insertion relies on this: a movie dropped into a
    Marathon folder and named e.g. "S01E23(Movie).mp4" matches the same
    S(\\d+)E(\\d+) pattern as any real episode (the regex only cares about the
    SxxExx prefix, trailing text is ignored), so it naturally sorts into
    position between S01E22 and S01E24 with no special-casing needed here.
    """
    import re as _re
    p = item.get("path", item.get("file", "")) if isinstance(item, dict) else str(item)
    name = os.path.splitext(os.path.basename(p))[0].lower()
    m = _re.search(r's(\d+)[ex](\d+)', name)
    if m: return (0, int(m.group(1)), int(m.group(2)), name)
    m = _re.search(r'season\s*(\d+).*?episode\s*(\d+)', name)
    if m: return (0, int(m.group(1)), int(m.group(2)), name)
    m = _re.search(r'ep(?:isode)?\W*(\d+)', name)
    if m: return (0, 0, int(m.group(1)), name)
    # Fallback: no recognized episode pattern (extras, trailers, misnamed
    # files, etc). Tag with tier=1 so it never gets tuple-compared
    # element-for-element against the tier=0 branches above (whose 2nd/3rd
    # slots are always ints) — that mismatch (int vs str) is what caused
    # the crash. Within the fallback itself, normalize every chunk to a
    # same-shaped (type_flag, value) pair so str/int chunks never collide
    # positionally with each other either.
    parts = _re.split(r'(\d+)', name)
    norm = tuple((0, int(x)) if x.isdigit() else (1, x) for x in parts)
    return (1, norm)


def build_ordered_tracks(items, sched_mode="natural", seed=0):
    """
    Pure function: flattens a raw schedule-folder listing into one fully-
    ordered episode list, grouped by source sub-folder with each folder's
    episodes always in natural (episode-number) order — ep2 < ep10,
    S01E02 < S01E10, etc. (via _natural_ep_key, same key group_shows uses).

    When sched_mode == "random_slots" the FOLDER order is shuffled (seeded)
    to mirror that mode's per-day show order; any other sched_mode —
    including Marathon's full-catalog ordering — keeps folders in stable
    sorted-path order.

    This is the SINGLE SOURCE OF TRUTH for this kind of full-catalog
    ordering: Marathon's continuous 24-hour playlist, and the "did the
    previous block's last show overflow into this one" lookahead both use
    it. Both the playback engine and the TV guide call this exact function
    so a Marathon channel (or an overflowing block) can never show/play a
    different lineup than what's actually airing.

    Accepts raw, unfiltered schedule entries (dicts with "path"/"file" and
    optionally "duration") — nonexistent paths are dropped and missing/
    invalid durations default to 1800s, same as the rest of the scheduler.
    """
    tracks = []
    for a in items:
        p = a.get("path", a.get("file", "")) if isinstance(a, dict) else str(a)
        if p and os.path.exists(p):
            d = a.get("duration", 1800) if isinstance(a, dict) else 1800
            d = d if d and d > 0 else 1800
            tracks.append({"path": p, "duration": d})
    if not tracks:
        return []

    import collections as _col
    dm = _col.defaultdict(list)
    for t in tracks:
        dm[os.path.dirname(t["path"])].append(t)
    for k in dm:
        dm[k].sort(key=_natural_ep_key)

    groups = [dm[k] for k in sorted(dm.keys())]
    if sched_mode == "random_slots":
        import random as _random
        rng = _random.Random(seed)
        rng.shuffle(groups)
    return [t for g in groups for t in g]


def group_shows(raw_tracks):
    """
    Groups a flat list of {"path":..., "duration":...} items into SHOWS.
    A "show" = the top-level folder immediately under the common root shared
    by all the tracks. This keeps Season 1 / Season 2 / etc of the same show
    together as one continuous, naturally-ordered catalog, while distinct
    shows stay separate.
    Returns (show_names_sorted, {show_name: [episode_dict, ...]}).
    This is the SINGLE SOURCE OF TRUTH for show grouping — both the playback
    engine and the TV guide must use this so they can never diverge again.
    """
    import collections as _col
    dir_map = _col.defaultdict(list)
    for t in raw_tracks:
        p = t.get("path", t.get("file", "")) if isinstance(t, dict) else str(t)
        dir_map[os.path.dirname(p)].append(t)
    for d in dir_map:
        dir_map[d].sort(key=_natural_ep_key)

    season_dirs = sorted(dir_map.keys())
    drive_roots = None
    if len(season_dirs) > 1:
        try:
            common_root = os.path.commonpath(season_dirs)
        except ValueError:
            # Windows raises this when the folders span different drives
            # (e.g. a library split across C:\ and a secondary/external
            # drive) -- there's no single path that's an ancestor of both.
            # Fall back to computing one common root PER drive, so shows
            # still group correctly by their real parent folder on each
            # drive instead of crashing the whole scheduler.
            common_root = None
            drive_roots = {}
            for d in season_dirs:
                drive_roots.setdefault(os.path.splitdrive(d)[0], []).append(d)
            for drv, dirs in drive_roots.items():
                try:
                    drive_roots[drv] = os.path.commonpath(dirs) if len(dirs) > 1 else os.path.dirname(dirs[0])
                except Exception:
                    drive_roots[drv] = os.path.dirname(dirs[0])
    elif season_dirs:
        common_root = os.path.dirname(season_dirs[0])
    else:
        common_root = ""

    show_map = _col.defaultdict(list)
    for d in season_dirs:
        root_for_d = drive_roots[os.path.splitdrive(d)[0]] if drive_roots is not None else common_root
        try:
            rel = os.path.relpath(d, root_for_d)
        except Exception:
            rel = d
        parts = rel.split(os.sep)
        show_name = parts[0] if parts and parts[0] not in ("", ".") else d
        show_map[show_name].append(d)

    show_names = sorted(show_map.keys())
    show_episodes = {}
    for sn in show_names:
        eps = []
        for sd in sorted(show_map[sn]):
            eps.extend(dir_map[sd])
        show_episodes[sn] = eps
    return show_names, show_episodes


# ==============================================================================
# MOVIE DETECTION & MOVIE-POOL SPLITTING
# ==============================================================================
# A track counts as a "movie" purely by runtime — no manual tagging, no folder
# convention. 80 minutes (4800s) sits comfortably above nearly all TV episode
# runtimes (sitcoms ~22min, hour dramas ~42-45min, even 2-part specials rarely
# clear 70min) while sitting at/below the low end of theatrical movie length.
# Exposed as a constant (not hardcoded inline) so a channel can override it later
# if a user's library skews unusual (e.g. anime OVAs, extended-cut TV specials).
MOVIE_LENGTH_THRESHOLD_SECONDS = 4800  # 80 minutes


def resolve_track_duration(media_probe_cache, path, stored_duration, default=1800):
    """
    SINGLE SOURCE OF TRUTH for turning a schedule entry's raw stored
    duration into the best REAL duration currently known for that file.
    Every place that builds a {"path":..., "duration":...} track --
    the live playback engine, the "peek next" preview, holiday playback,
    broken-file substitution, and the TV guide -- must call this instead
    of separately re-deriving "is this still a placeholder?", because
    that question was previously answered slightly differently at each
    call site.

    ROOT CAUSE this fixes: one call site (the main engine's per-tick
    scheduler in retro_tv_emulator.py) only consulted the probe cache
    when the entry's RAW stored duration was exactly the sentinel -1.
    New schedule entries aren't guaranteed to be stored as exactly -1
    (some paths write 0, some write nothing at all) -- so a freshly
    probed movie could sit in media_probe_cache with its real 2-hour
    length while this one call site kept reading the stored placeholder
    forever, because its narrow -1 check never matched and so never
    looked the real value up. Two visible symptoms of that one gap:
      1. The movie is misclassified as a series episode (is_movie_track's
         4800s threshold sees the placeholder ~1800s and comes back
         False), which for a flat "all movies in one folder" library
         means EVERY movie in that folder collapses into one giant
         group_shows() pseudo-show (grouping is by folder name) --
         so "the next episode of Movies" can be a completely different
         film than the one actually scheduled, and it never resolves
         itself no matter how long you wait, because re-probing changes
         media_probe_cache but this call site was never looking there.
      2. Even where classification wasn't affected, the guide displayed
         a 30-minute block for what is actually a 2-hour file.

    Always prefers a real probed length from the global, cross-channel
    media_probe_cache over the entry's own stored value -- the stored
    value is frequently stale/placeholder even after a real probe has
    landed, since the probe writes to the shared cache immediately but
    isn't required to also patch every schedule entry that references
    the same physical file. Falls back to the entry's own stored value,
    then to `default`, only when nothing is cached for this path yet.
    """
    if path:
        try:
            norm = os.path.normpath(str(path))
            cached = (media_probe_cache or {}).get(norm)
            cached_dur = cached.get("duration") if isinstance(cached, dict) else None
            if cached_dur and cached_dur > 0:
                return int(cached_dur)
        except Exception:
            pass
    d = stored_duration
    return d if d and d > 0 else default


def is_movie_track(track, threshold_seconds=MOVIE_LENGTH_THRESHOLD_SECONDS):
    """
    Classifies a single {"path":..., "duration":...} track as a movie purely
    by runtime. threshold_seconds is a parameter (not hardcoded) so callers —
    and eventually a per-channel override — can tune it without touching this
    function. Missing/invalid duration is never treated as a movie (defaults
    to 0 -> False) since an unprobed file shouldn't get misclassified before
    its real length is known.
    """
    if not isinstance(track, dict):
        return False
    dur = track.get("duration", 0)
    return bool(dur and dur >= threshold_seconds)


# ==============================================================================
# MULTI-PART MOVIE COMBINING (Part 1 / Part 2 -> one guide slot, one movie)
# ==============================================================================
# Some movies ship as two separate files sharing one title, e.g.
# "Movie Name - Part 1.mp4" / "Movie Name - Part 2.mp4" (a full-length film
# split across two files/discs). These are meant to be watched as ONE movie
# start-to-finish, not two separate items a viewer could tune into mid-way —
# so they're combined into a single movie_pool entry (one guide slot, one
# combined duration, playing both files back-to-back) BEFORE random
# movie-slot selection or movie/series classification ever sees them. That
# ordering matters: a 100-minute film split into two ~50-minute parts has
# each part individually UNDER the movie-length threshold, so combining has
# to happen first for the pair to ever be classified as a movie at all
# instead of each part separately falling into the series/episode pipeline.
#
# This is DELIBERATELY narrower than "any file with 'Part 1'/'Part 2' in the
# name": a TV show's own two-part episode ("cliffhanger, part 2 next week")
# uses the exact same words but must NOT be combined — it's a completely
# normal episode that airs on its own, later, like it always has. The two
# cases are told apart by requiring ALL of:
#   1. An identical base title once the "Part N" marker is stripped — movie
#      parts share nothing else in the name (no season/episode marker).
#   2. Exactly two parts, numbered 1 and 2 — a lone "Part 1" with no
#      "Part 2" partner, or 3+ parts sharing a title, is left alone rather
#      than guessed at.
#   3. Combined duration clears the movie-length threshold — two ordinary
#      short clips that happen to say "Part 1"/"Part 2" are left alone.
#   4. NEITHER filename matches a season/episode marker (S01E05, "Season 1
#      Episode 5", "Episode 5", etc — the same patterns _natural_ep_key
#      already uses). A real TV two-parter is always tagged with one of
#      these somewhere in its filename so the show's own episode ordering
#      can find it in the first place; a standalone movie's parts never
#      are. This is the actual line between "this is a movie" and "this is
#      a show episode that happens to also say Part 2."

def _looks_like_episode_filename(stem):
    """True if *stem* carries a season/episode marker (S01E05, 'Season 1
    Episode 5', 'Episode 5', ...) — the same patterns _natural_ep_key uses
    to sort episodes. Used to keep multi-part MOVIE combining from ever
    touching a real TV episode that happens to also say "Part 2" in its
    filename — a genuine episode is always tagged this way so the show's
    own ordering can find it; a standalone movie's parts never are.
    """
    import re
    low = stem.lower()
    if re.search(r's\d+[ex]\d+', low):
        return True
    if re.search(r'season\s*\d+.*?episode\s*\d+', low):
        return True
    if re.search(r'\bep(?:isode)?\W*\d+', low):
        return True
    return False


def _movie_part_info(track):
    """
    Returns (base_title, part_number) if *track*'s filename matches a
    "Part 1"/"Part 2"-style movie naming convention, else None. base_title
    is the filename stem with the part marker and its surrounding
    punctuation stripped and normalized, so formatting differences around
    the marker itself ("Movie - Part 1" vs "Movie_Part_1") still group to
    the same key.
    """
    import re
    if not isinstance(track, dict):
        return None
    p = track.get("path", track.get("file", ""))
    stem = os.path.splitext(os.path.basename(p))[0]
    if _looks_like_episode_filename(stem):
        return None
    m = re.search(r'(?i)^(.*?)[\s._\-:]*\b(?:part|pt)\.?\s*(\d+)\b(?:\s*of\s*\d+)?(.*)$', stem)
    if not m:
        return None
    base = m.group(1) + " " + m.group(3)
    base = re.sub(r'[\s._\-:]+', ' ', base).strip().lower()
    if not base:
        return None
    return base, int(m.group(2))


def pair_movie_parts(raw_tracks, threshold_seconds=MOVIE_LENGTH_THRESHOLD_SECONDS):
    """
    Scans *raw_tracks* for "Part 1"/"Part 2" movie pairs (see module notes
    above) and merges each matched pair into a single combined track. Must
    run BEFORE movie/series classification — see the module notes for why.

    Reuses the exact same is_pair / pair_ep1_* / pair_ep2_* fields
    pair_short_episodes() already uses, so every downstream consumer (TV
    guide row rendering — including title cleanup, which already strips
    "Part N" so a combined pair displays as one clean movie title, not
    "Movie + Movie" — the playback engine's mid-slot file resolution, and
    schedule-change cache invalidation) already knows how to play and
    display this correctly with no separate code path.

    Only an exact, unambiguous Part-1/Part-2 pair whose COMBINED duration
    clears the movie-length threshold is merged. Anything else (a lone
    part, 3+ parts sharing a title, a duplicate part number, or a combined
    duration that doesn't clear the movie threshold) is left completely
    untouched and continues on through the normal pipeline unchanged.
    """
    import collections as _col
    by_base = _col.defaultdict(dict)
    untouched = []
    for t in raw_tracks:
        info = _movie_part_info(t)
        if info is None:
            untouched.append(t)
            continue
        base, num = info
        if num in by_base[base]:
            # Duplicate part number for the same title (e.g. two different
            # files both calling themselves "Part 1") — too ambiguous to
            # guess which is real. Flag so this title is never combined.
            by_base[base]["_ambiguous"] = True
        by_base[base][num] = t

    result = list(untouched)
    for base, parts in by_base.items():
        ambiguous = parts.pop("_ambiguous", False)
        if not ambiguous and set(parts.keys()) == {1, 2}:
            p1, p2 = parts[1], parts[2]
            p1_path = p1.get("path", p1.get("file", ""))
            p2_path = p2.get("path", p2.get("file", ""))
            p1_dur  = p1.get("duration", 0) or 0
            p2_dur  = p2.get("duration", 0) or 0
            combined_dur = p1_dur + p2_dur
            if combined_dur >= threshold_seconds:
                result.append({
                    "path":              p1_path,
                    "duration":          combined_dur,
                    "is_pair":           True,
                    "pair_ep1":          p1,
                    "pair_ep2":          p2,
                    "pair_ep1_path":     p1_path,
                    "pair_ep1_duration": p1_dur,
                    "pair_ep2_path":     p2_path,
                    "pair_ep2_duration": p2_dur,
                })
                continue
        # Not a clean, movie-length pair — leave every part exactly as it
        # was so it flows through the normal series/episode pipeline.
        result.extend(parts.values())
    return result


def split_movies_and_series(raw_tracks, threshold_seconds=MOVIE_LENGTH_THRESHOLD_SECONDS):
    """
    Splits a channel's raw track list into:
      - movie_pool:   a FLAT list of movie tracks, deliberately NOT grouped by
                       folder and NOT sorted alphabetically/naturally. Movies
                       are unrelated films, not episodes of something, so they
                       get no "next in line" ordering at all here — random
                       selection happens at the call site (build_show_rotation)
                       using the day's seeded RNG.
      - show_names:    same as group_shows()'s first return value, but built
                        ONLY from the remaining (non-movie) tracks, so a movie
                        dropped in a show's folder is pulled out of that show's
                        episode rotation entirely instead of being treated as
                        an "episode" of it.
      - show_episodes: same as group_shows()'s second return value, for the
                        remaining non-movie tracks.

    This is the SINGLE SOURCE OF TRUTH for movie/series separation — both the
    playback engine and the TV guide must call this (instead of reimplementing
    the length check) so a borderline-length file can never be classified as a
    movie in one place and an episode in the other.

    Before classification runs, raw_tracks is passed through
    pair_movie_parts() so a movie shipped as "Part 1"/"Part 2" files is
    combined into one track first — this must happen before the runtime
    check below, since each part is often individually UNDER the movie
    threshold on its own (see pair_movie_parts' module notes).
    """
    raw_tracks = pair_movie_parts(raw_tracks, threshold_seconds)
    movie_pool = []
    series_tracks = []
    for t in raw_tracks:
        if is_movie_track(t, threshold_seconds):
            movie_pool.append(t)
        else:
            series_tracks.append(t)
    show_names, show_episodes = group_shows(series_tracks)
    return movie_pool, show_names, show_episodes


# Episodes shorter than this (seconds) are paired two-at-a-time into one slot.
SHORT_EP_THRESHOLD = 900  # 15 minutes


def pair_short_episodes(episodes, threshold_seconds=900):
    """
    Merges consecutive episodes that are BOTH shorter than threshold_seconds into
    a single paired entry whose duration is the sum of the two.  The pair appears
    as one slot in the TV guide and plays both files back-to-back in the engine.
    Any trailing short episode with no partner keeps its original entry.
    Pass threshold_seconds=0 to disable pairing entirely.
    """
    if threshold_seconds <= 0:
        return list(episodes)
    result = []
    i = 0
    while i < len(episodes):
        ep  = episodes[i]
        dur = ep.get("duration", 1800) if isinstance(ep, dict) else 1800
        if dur < threshold_seconds and i + 1 < len(episodes):
            nxt     = episodes[i + 1]
            nxt_dur = nxt.get("duration", 1800) if isinstance(nxt, dict) else 1800
            if nxt_dur < threshold_seconds:
                ep_path  = ep.get("path",  ep.get("file",  "")) if isinstance(ep,  dict) else str(ep)
                nxt_path = nxt.get("path", nxt.get("file", "")) if isinstance(nxt, dict) else str(nxt)
                result.append({
                    "path":              ep_path,
                    "duration":          dur + nxt_dur,
                    "is_pair":           True,
                    "pair_ep1":          ep,
                    "pair_ep2":          nxt,
                    "pair_ep1_path":     ep_path,
                    "pair_ep1_duration": dur,
                    "pair_ep2_path":     nxt_path,
                    "pair_ep2_duration": nxt_dur,
                })
                i += 2
                continue
        result.append(ep)
        i += 1
    return result


# Movies are inserted into a block only when the channel actually has fewer
# than this many times as many movies as it has series episodes on hand for
# that block; past this ratio (or when there's no series content at all —
# a dedicated movie channel/block) the "no back-to-back movies" spacing rule
# is waived entirely, since there isn't enough series content to interleave
# with and forcing spacing would just be fighting the content mix.
MOVIE_HEAVY_RATIO = 2

# Legacy flat per-block cap. No longer used to decide HOW MANY movies air —
# see _movie_slot_count, which scales that off the block's real duration and
# the movie-pool/series balance instead — but kept as build_show_rotation's
# fallback assumption (movies_per_block worth of ~4-hour chunks) for any
# caller that doesn't yet pass block_duration_seconds.
MOVIES_PER_BLOCK = 2

# Reserved show_pos / show-name key movies are tracked under when they're
# folded into the rotation engine. Chosen to be something no real folder name
# could ever collide with.
MOVIE_ROTATION_KEY = "__MOVIES__"

# Used only when every movie in a pool is (still) missing a real probed
# duration -- extremely unlikely in practice (movies are exactly the large
# files is_movie_track requires a real duration to classify in the first
# place), but keeps the block-filling math sane rather than dividing by zero.
_MOVIE_DURATION_FALLBACK_SECONDS = 6000  # ~100 minutes


def _rotation_take_sequential(show_pos, name, count, eps):
    """
    Standalone version of build_show_rotation's sequential picker: strict
    next-in-line through `eps`, wrapping once exhausted. Pulled out to a
    module-level function (rather than staying a private closure) so movies
    can share the EXACT SAME repeat-avoidance/position-persistence logic as
    series episodes instead of a separate, easily-divergent implementation --
    see _movie_slot_count / build_show_rotation's movie section.
    Mutates and returns `show_pos` (a dict) in place.
    """
    if not eps:
        return []
    pos = show_pos.get(name, 0) % len(eps)
    out = [eps[(pos + n) % len(eps)] for n in range(count)]
    show_pos[name] = (pos + count) % len(eps)
    return out


def _rotation_take_random(show_pos, name, count, eps, day_seed):
    """
    Standalone version of build_show_rotation's random-cycle picker: every
    entry in `eps` airs once before any of them repeat (a shuffled "cycle"
    that reshuffles once exhausted), and the same entry never plays twice in
    a row even across a cycle boundary. See _rotation_take_sequential for why
    this is a standalone twin of the closure inside build_show_rotation
    rather than a shared call — movies get the identical guarantee for free.
    Mutates and returns `show_pos` (a dict) in place.
    """
    import random as _random
    if not eps:
        return []
    n = len(eps)
    state = show_pos.get(name)
    if isinstance(state, dict):
        played = [i for i in state.get("played", []) if 0 <= i < n]
        last = state.get("last")
    else:
        played = []
        last = state if isinstance(state, int) else None

    rng = _random.Random(f"{day_seed}:{name}")
    out = []
    for _ in range(count):
        if n == 1:
            idx = 0
        else:
            remaining = [i for i in range(n) if i not in played]
            if not remaining:
                played = []
                remaining = [i for i in range(n) if i != last] or list(range(n))
            idx = rng.choice(remaining)
            played.append(idx)
        out.append(eps[idx])
        last = idx
    show_pos[name] = {"played": played, "last": last}
    return out


def _movie_slot_count(movie_pool, block_duration_seconds, series_total_episodes, scheduling_mode):
    """
    Decides how many movies should air in THIS block — replaces the old flat
    MOVIES_PER_BLOCK=2-for-every-block-regardless-of-length rule, which is
    why a "Full Day" (24h) movie channel used to cycle through only 2 movies
    total: the same flat cap that made sense for one 8-hour split-day block
    was being applied unchanged to a block 3x longer.

    Pure-movie channel (series_total_episodes == 0): movies fill the WHOLE
    block. n_pick = round(block_duration_seconds / average_movie_length), so
    an 8-hour split-day block and a 24-hour Full Day block both end up
    reasonably full instead of both getting the same flat count.

    Mixed channel (movies + series both present): movies get a TIME BUDGET
    proportional to how large the movie pool is relative to the series
    library on hand for this block, clamped to [10%, 90%] of the block so
    neither side can fully starve the other out. A channel with 100 movies
    and only 2 series leans mostly-movies; a channel with a handful of
    movies alongside a big series library stays mostly series-driven — the
    old rule ignored this balance entirely and always tried to wedge in
    exactly 2 movies regardless of what else was on the channel.

    scheduling_mode only nudges PACING here (grouping movies into back-to-
    back "double feature" pairs for two_slots channels happens in
    _place_movies_in_block) — for two_slots, round the count up to even so
    every movie has a pairing partner.
    """
    if not movie_pool or block_duration_seconds is None or block_duration_seconds <= 0:
        return 0

    durations = [t.get("duration", 0) for t in movie_pool if isinstance(t, dict) and t.get("duration", 0) > 0]
    avg_dur = (sum(durations) / len(durations)) if durations else _MOVIE_DURATION_FALLBACK_SECONDS
    avg_dur = max(avg_dur, 60)  # guard against a stray near-zero duration skewing the divide

    if series_total_episodes <= 0:
        budget = block_duration_seconds
    else:
        movie_share = len(movie_pool) / float(len(movie_pool) + series_total_episodes)
        movie_share = max(0.1, min(0.9, movie_share))
        budget = block_duration_seconds * movie_share

    n_pick = max(1, round(budget / avg_dur))
    n_pick = min(n_pick, len(movie_pool))

    if scheduling_mode == "two_slots" and 1 < n_pick < len(movie_pool) and n_pick % 2:
        n_pick += 1
    elif scheduling_mode == "two_slots" and n_pick % 2 and n_pick == len(movie_pool) and n_pick > 1:
        n_pick -= 1  # can't round up past the whole pool — drop to the nearest even count instead

    return n_pick


def _place_movies_in_block(video_tracks, picked_movies, day_seed, movie_heavy,
                            scheduling_mode="random_slots"):
    """
    Splices the ALREADY-CHOSEN `picked_movies` into an already-built series
    lineup for one block. Selection (which movies, in what order, with what
    repeat-avoidance guarantee) is entirely the caller's job now — see
    _movie_slot_count and build_show_rotation's movie section, which pick
    movies through the exact same shuffle-without-repeat machinery series
    episodes use. This function only decides WHERE picks land in the
    timeline.

    Movie-heavy / dedicated-movie blocks: no spacing constraint, movies can
    land anywhere including next to each other.

    Mixed blocks: movies are spread across roughly even segments of the block
    (with a little seeded jitter so it doesn't feel mechanical) and are never
    placed directly adjacent to another movie, so there's always at least one
    piece of series content between two movies.

    scheduling_mode == "two_slots" groups picked_movies into back-to-back
    PAIRS ("double feature" slots) at placement time instead of scattering
    each movie individually; one_slot/random_slots place them one at a time.
    This only changes how picks are grouped for insertion — it never
    re-picks or reorders which movies were chosen.

    Seeded off day_seed (offset so it doesn't reuse the same random stream as
    show-order shuffling), so a given day/block always produces the same
    movie placement for both the playback engine and the guide.
    """
    import random as _random

    if not picked_movies:
        return video_tracks

    rng = _random.Random(day_seed * 7919 + 31)

    if scheduling_mode == "two_slots" and len(picked_movies) > 1:
        units = [picked_movies[i:i + 2] for i in range(0, len(picked_movies), 2)]
    else:
        units = [[m] for m in picked_movies]

    result = list(video_tracks)

    if movie_heavy or not result:
        # No spacing constraint — scatter freely, adjacency allowed.
        for u in units:
            idx = rng.randint(0, len(result))
            result[idx:idx] = u
        return result

    # Spread picks across len(units) segments of the block, keeping each unit
    # off the immediate neighbor of another already-placed unit.
    used_idxs = []
    seg = max(1, len(result) // (len(units) + 1))
    for i, u in enumerate(units):
        base = seg * (i + 1)
        jitter = rng.randint(-max(0, seg // 3), max(0, seg // 3))
        idx = max(1, min(base + jitter, len(result)))
        guard = 0
        while (idx in used_idxs or (idx - 1) in used_idxs) and guard < len(result) + 2:
            idx += 1
            guard += 1
        idx = min(idx, len(result))
        used_idxs.append(idx)
        result[idx:idx] = u

    return result


def build_show_rotation(raw_tracks, scheduling_mode, show_pos, day_seed, pair_threshold_seconds=900,
                         movie_threshold_seconds=MOVIE_LENGTH_THRESHOLD_SECONDS,
                         movies_per_block=MOVIES_PER_BLOCK,
                         episode_order_mode="sequential",
                         fixed_show_order=None,
                         block_duration_seconds=None):
    """
    Pure function: the SINGLE SOURCE OF TRUTH for episode rotation order for
    the three per-day-block scheduling modes — Random Slots, One Slot, and
    Two Slots. Both the playback engine and the TV guide call this so they
    can never show/play different things again.

    (Marathon is NOT one of these modes: it doesn't reshuffle per day-block
    at all, it plays one fixed, fully-ordered 24-hour catalog on a continuous
    epoch clock. That catalog is built by build_ordered_tracks() instead —
    see get_marathon_content() in ui.py and the Marathon branch in
    calculate_slotted_playback_state() in retro_tv_emulator.py. Marathon has
    no per-episode "next up" pick at all, so episode_order_mode is meaningless
    there and the UI hides the control entirely in that mode.)

    Movies (see split_movies_and_series / is_movie_track — pure runtime
    classification, no tagging) are pulled OUT of the series rotation
    entirely, then picked and re-inserted after the series lineup is built.
    HOW MANY movies air is no longer a flat per-block cap: _movie_slot_count
    scales it off block_duration_seconds and the movie-pool/series balance —
    a pure-movie channel fills the WHOLE block (an 8-hour split-day block and
    a 24-hour Full Day block both end up reasonably full instead of both
    getting the same flat count), and a mixed channel gives movies a time
    budget proportional to how big the movie pool is relative to the series
    library on hand (100 movies + 2 series leans mostly-movies; a handful of
    movies alongside a big series library stays mostly series-driven).
    WHICH movies air, and in what order, is picked via the exact same
    shuffle-without-repeat-until-exhausted machinery series episodes use
    (_rotation_take_sequential/_rotation_take_random, tracked under the
    reserved show_pos key MOVIE_ROTATION_KEY) — respecting episode_order_mode
    ("sequential" walks the pool in a stable path-sorted order; "random"
    guarantees every movie airs once before any repeat, never twice in a row)
    exactly like a show does, so movies never just repeat the same couple of
    picks forever. Placement into the timeline (_place_movies_in_block) is
    spaced so movies are never back-to-back unless the block is movie-heavy
    or has no series content at all (see MOVIE_HEAVY_RATIO), except under
    scheduling_mode == "two_slots", which deliberately groups picks into
    back-to-back "double feature" pairs.

    episode_order_mode controls how each show's NEXT episode is picked:
      - "sequential" (default): strict next-in-line, S01E01 -> S01E02 -> ...,
        wrapping to the start once the library is exhausted. This is the
        original/only behavior before the toggle existed, so it stays the
        default for full backward compatibility with existing channels.
      - "random": picks a random episode each time, instead of following
        season/episode order — but still guarantees every episode in the
        show airs once before any of them repeat (a shuffled "cycle" that
        reshuffles once exhausted), and never picks the same episode twice
        in a row even across that cycle boundary.
      Either way, show_pos persists across calls/days so a show never resets
      or replays what it already covered — day 2 always continues from where
      day 1 left off (next-in-line for sequential, the same in-progress
      shuffled cycle — picking up wherever it left off — for random).
      Accepts either a single string (applies to every show in this block) or
      a dict of {show_name: "sequential"|"random"} with an optional
      "__default__" key, for future per-show granularity.

    show_pos is READ (not mutated in place) — a local copy is advanced and
    returned, so callers (like a guide preview that needs to peek ahead
    multiple blocks without committing real state) can decide whether to
    persist the result.

    fixed_show_order is the day-to-day POSITION template, used by ALL THREE
    scheduling modes: a list of show names giving the slot order to reuse
    instead of reshuffling via day_seed every call. Pass None (or an empty
    list) the first time a block is built for a channel — a fresh
    channel-seeded shuffle is used and returned as show_order_used so the
    caller can persist it. On every later call, pass that persisted list
    back in: shows already in it keep their exact slot, and any show name
    not yet in it (a newly added folder) is appended at the end (sorted)
    rather than reshuffling everyone else. Shows no longer present are
    simply dropped. This only affects WHICH SLOT a show occupies — which
    episode/movie plays in that slot still advances normally via
    show_pos/day_seed, so content keeps moving even though position holds
    still.

    Returns (video_tracks, new_show_pos, show_order_used). show_order_used
    is the show-name order actually used to build this block — persist it
    and pass it back in as fixed_show_order next time, for all three modes.
    """
    import random as _random

    show_pos = dict(show_pos or {})
    movie_pool, show_names, show_episodes = split_movies_and_series(raw_tracks, movie_threshold_seconds)

    # Apply pairing per-show BEFORE the rotation is assembled so that
    # short episodes from the SAME show are combined into two-episode
    # atomic units first, then the rotation picks from those pairs as
    # if each pair is a single episode.  Running this here (not after
    # the shows are mixed together) prevents a short ep from one show
    # from accidentally pairing with an unrelated ep from a different
    # show that happens to end up next to it in the mixed schedule.
    # Movies (in movie_pool) are excluded — they're always long enough
    # that pair_short_episodes would leave them untouched anyway.
    if pair_threshold_seconds > 0:
        show_episodes = {
            sn: pair_short_episodes(eps, threshold_seconds=pair_threshold_seconds)
            for sn, eps in show_episodes.items()
        }

    def _order_mode_for(show_name):
        if isinstance(episode_order_mode, dict):
            return episode_order_mode.get(show_name, episode_order_mode.get("__default__", "sequential"))
        return episode_order_mode or "sequential"

    def _take_sequential(show_name, count, eps):
        pos = show_pos.get(show_name, 0) % len(eps)
        out = [eps[(pos + n) % len(eps)] for n in range(count)]
        show_pos[show_name] = (pos + count) % len(eps)
        return out

    def _take_random(show_name, count, eps):
        n = len(eps)
        # show_pos holds a small dict tracking which episode indices have
        # already aired in the CURRENT cycle ("played"), plus the last one
        # actually shown ("last") so a fresh cycle never repeats it back-to-
        # back. This guarantees every episode airs once before any repeat,
        # while still never playing the same episode twice in a row.
        #
        # Backward compat: channels saved before this fix have show_pos[show]
        # as a plain int (just the last-shown index, no cycle memory). Treat
        # that as "cycle just started, nothing played yet" and carry the old
        # int forward as `last` so at least the immediate-repeat guarantee
        # holds while the full-cycle guarantee starts fresh from here on.
        state = show_pos.get(show_name)
        if isinstance(state, dict):
            played = [i for i in state.get("played", []) if 0 <= i < n]
            last = state.get("last")
        else:
            played = []
            last = state if isinstance(state, int) else None

        rng = _random.Random(f"{day_seed}:{show_name}")
        out = []
        for _ in range(count):
            if n == 1:
                idx = 0
            else:
                remaining = [i for i in range(n) if i not in played]
                if not remaining:
                    # Every episode has aired since the last reset — the
                    # cycle is exhausted. Reset it here (rather than the
                    # instant `played` first reached length n) so this
                    # exhaustion check still fires correctly, even across
                    # separate calls/days, and can exclude `last` to avoid
                    # an immediate repeat right at the cycle boundary.
                    played = []
                    remaining = [i for i in range(n) if i != last] or list(range(n))
                idx = rng.choice(remaining)
                played.append(idx)
            out.append(eps[idx])
            last = idx
        show_pos[show_name] = {"played": played, "last": last}
        return out

    def _take(show_name, count):
        eps = show_episodes.get(show_name) or []
        if not eps:
            return []
        if _order_mode_for(show_name) == "random":
            return _take_random(show_name, count, eps)
        return _take_sequential(show_name, count, eps)

    video_tracks = []

    # SERIES ORDER: for ALL THREE per-day-block modes, the order shows are
    # slotted in is established via a channel-seeded shuffle (day_seed
    # includes the channel number -- see callers), then reused every later
    # block via fixed_show_order until it's replaced -- i.e. "random series
    # order, held stable until you cycle back to the start of it". Shows not
    # yet in a persisted template are appended (sorted) at the end so a
    # newly-added folder doesn't reshuffle everyone else's existing slot.
    #
    # Previously only random_slots did this -- one_slot/two_slots always
    # walked plain alphabetical/path-sorted show_names with NO seed and NO
    # shuffle at all. That's harmless for a single channel, but it means two
    # DIFFERENT channels loaded with the same media (in one_slot/two_slots
    # mode) produced the exact same show order every time, with nothing
    # channel-specific differentiating them -- combined with "sequential"
    # episode_order_mode (also purely positional, no randomness), two such
    # channels ended up airing the identical episode at the identical
    # moment. Unifying all three modes on this same shuffle-once/persist
    # logic fixes that.
    if fixed_show_order:
        _known = set(show_names)
        order = [sn for sn in fixed_show_order if sn in _known]
        _placed = set(order)
        order.extend(sorted(sn for sn in show_names if sn not in _placed))
    else:
        rng = _random.Random(day_seed)
        order = list(show_names)
        rng.shuffle(order)
    show_order_used = order

    if scheduling_mode == "one_slot":
        for sn in order:
            video_tracks.extend(_take(sn, 1))

    elif scheduling_mode == "two_slots":
        # Goal: 2 real file-plays per show per rotation.
        # pair_short_episodes() fuses two short clips into one combined slot
        # (is_pair=True), so that slot already accounts for 2 real plays.
        # The old formula `n = 2 if len(eps) > 1 else 1` counted SLOTS, not
        # real files: a show with 4 short episodes becomes 2 paired slots →
        # n=2 → 4 real plays instead of 2. Fix: walk the episode list from the
        # current show_pos and accumulate slots until we've hit 2 real files.
        for sn in order:
            eps = show_episodes.get(sn) or []
            if not eps:
                continue
            # Determine current position so we peek at the right slots.
            _state = show_pos.get(sn)
            if isinstance(_state, int):
                _pos0 = _state % len(eps)
            elif isinstance(_state, dict):
                # Random mode: last-played index is stored; next is the slot
                # after it.  Use it as an approximation for peeking.
                _lp = _state.get("last")
                _pos0 = ((int(_lp) + 1) % len(eps)) if isinstance(_lp, int) else 0
            else:
                _pos0 = 0
            # Count slots until we've accumulated 2 real file-plays.
            _real = 0
            _n = 0
            for _i in range(len(eps)):
                if _real >= 2:
                    break
                _slot = eps[(_pos0 + _i) % len(eps)]
                _real += 2 if _slot.get("is_pair") else 1
                _n += 1
            video_tracks.extend(_take(sn, max(1, _n)))

    else:
        # "random_slots" — also the fallback default for any unrecognized value.
        # Goal: proportional runs up to 3 REAL file-plays for the biggest library.
        # Use real episode count (pairs = 2 real files) for the ratio so that a
        # show whose short episodes were all paired isn't under-represented vs a
        # show of full-length episodes of the same absolute library size.
        def _real_ep_count(eps_list):
            return sum(2 if e.get("is_pair") else 1 for e in eps_list)

        _real_counts = {sn: _real_ep_count(eps_list)
                        for sn, eps_list in show_episodes.items()}
        max_real = max(_real_counts.values(), default=1)
        for sn in order:
            eps = show_episodes.get(sn) or []
            if not eps:
                continue
            real_len = _real_counts.get(sn, len(eps))
            # Proportional target in real file-plays (1 ≤ run_real ≤ 3).
            run_real = max(1, min(real_len, round((real_len / max_real) * 3)))
            # Convert target real count to slot count by walking the list.
            _real = 0
            _n = 0
            for slot in eps:
                if _real >= run_real:
                    break
                _real += 2 if slot.get("is_pair") else 1
                _n += 1
            video_tracks.extend(_take(sn, max(1, _n)))

    # NOTE: per-show pairing was already applied to show_episodes above,
    # before rotation — no post-rotation pass here (which would pair eps
    # from different shows that happen to be adjacent in the mixed list).

    if movie_pool:
        series_total = sum(len(v) for v in show_episodes.values())
        # Fallback assumption for callers that don't yet pass
        # block_duration_seconds: treat it as movies_per_block worth of
        # ~4-hour chunks, so the old default (2) lands close to its old
        # real-world effect (~8 hours) rather than silently picking 0/1.
        _bds = block_duration_seconds if block_duration_seconds else max(movies_per_block, 1) * 4 * 3600
        n_movie_pick = _movie_slot_count(movie_pool, _bds, series_total, scheduling_mode)
        _ordered_pool = sorted(movie_pool, key=lambda t: t.get("path", ""))
        # Movies always use random rotation — every title plays once before any
        # repeats, but order is never alphabetical (no one wants that for films).
        picked_movies = _rotation_take_random(show_pos, MOVIE_ROTATION_KEY, n_movie_pick, _ordered_pool, day_seed)
        movie_heavy = (series_total == 0) or (len(movie_pool) > MOVIE_HEAVY_RATIO * series_total)
        video_tracks = _place_movies_in_block(video_tracks, picked_movies, day_seed, movie_heavy,
                                               scheduling_mode=scheduling_mode)

    return video_tracks, show_pos, show_order_used


def note_random_episode_played(show_pos, show_name, episode_count, played_index):
    """
    Record that `played_index` just aired for `show_name` under "random"
    episode_order_mode OUTSIDE of build_show_rotation's normal picking loop
    (currently: substituting a stand-in episode after the originally
    scheduled one turns out to be broken — see _resolve_broken_episode in
    retro_tv_emulator.py).

    Mutates show_pos in place so it stays in the same {"played": [...],
    "last": idx} shape build_show_rotation's random-mode picker expects and
    keeps updating — otherwise a substitution would either get clobbered on
    the next real pick or reset the show's cycle progress, undermining the
    "every episode airs before any repeat" guarantee.
    """
    state = show_pos.get(show_name)
    if isinstance(state, dict):
        played = [i for i in state.get("played", []) if 0 <= i < episode_count]
    else:
        played = []
    if played_index not in played:
        played.append(played_index)
    # NOTE: deliberately NOT reset to [] here even if this just completed the
    # cycle (len(played) == episode_count) — build_show_rotation's own
    # exhaustion check (computing `remaining` as empty) needs to see the full
    # `played` list on its next call to correctly exclude `last` and avoid an
    # immediate repeat right at the cycle boundary. See _take_random.
    show_pos[show_name] = {"played": played, "last": played_index}


# ==============================================================================
# EXACT BLOCK TIMELINE (SHOWS + REAL COMMERCIAL BREAKS)
# ==============================================================================

def build_block_timeline(video_tracks, commercial_tracks, commercial_placement, day_seed,
                          interrupt_floor_seconds=900,
                          max_interrupt_break_seconds=420,
                          max_end_break_seconds=600):
    """
    Pure function: the SINGLE SOURCE OF TRUTH for a block's actual playback
    timeline — every show AND every commercial break, each with its real,
    exact start_time/duration. Both calculate_slotted_playback_state
    (retro_tv_emulator.py, for live playback) and the TV guide (ui.py) call
    this exact function, so the guide can never show a show starting or
    stopping at a different moment than what actually airs.

    commercial_placement == "interrupt_half_hour": breaks are worked into
    long shows/movies (one roughly every 30 min of the show's own runtime)
    plus a final break, sized so the whole slot lands on the next :30/:00
    mark — unless the show is too short to interrupt (interrupt_floor_seconds),
    in which case only an end break is used. Each break is capped at
    max_interrupt_break_seconds (default 7 min / 420 s). Any fill time that
    can't fit within the cap is deferred and reinserted as small catch-up
    breaks (~3 min max each) between subsequent shows, keeping at least
    MIN_BREAK_GAP seconds of show content between any two breaks.
    commercial_placement == "end_of_show": one break after each show, sized
    to reach the next :30/:00 mark, capped at max_end_break_seconds (default
    10 min / 600 s). Same deferred-fill catch-up logic applies.
    Anything else (or no commercial_tracks): shows play back-to-back with
    their real, unpadded durations — no snapping at all.

    Returns (timeline, total_block_content). timeline is a list of dicts in
    ascending, contiguous start_time order:
      {"path", "duration", "start_time", "is_commercial", "seek_offset",
       [pair_* fields carried through for is_pair show entries]}
    """
    import random as _random

    HALF_HOUR      = 1800
    MAX_CATCHUP    = 180   # max 3 min for a single catch-up fill break
    MIN_BREAK_GAP  = 300   # at least 5 min of show between any two breaks

    rng_c = _random.Random(day_seed + 7777)
    commercial_pool = list(commercial_tracks or [])
    if commercial_pool:
        rng_c.shuffle(commercial_pool)

    timeline = []
    total_block_content = 0

    # Shared mutable state (modified by the closures below via nonlocal)
    _deferred_fill  = 0               # fill seconds owed but not yet placed
    _last_break_end = -MIN_BREAK_GAP  # position where the last break ended

    def _show_entry(show, duration, start_time, seek_offset=0):
        e = {"path": show["path"], "duration": duration,
             "start_time": start_time, "is_commercial": False,
             "seek_offset": seek_offset}
        if show.get("is_pair"):
            e["is_pair"]           = True
            e["pair_ep1_path"]     = show.get("pair_ep1_path", show["path"])
            e["pair_ep1_duration"] = show.get("pair_ep1_duration", 0)
            e["pair_ep2_path"]     = show.get("pair_ep2_path", "")
            e["pair_ep2_duration"] = show.get("pair_ep2_duration", 0)
        return e

    def _pack_break(budget_seconds, pool, start_idx):
        """Greedily fill up to budget_seconds with whole commercials from pool.

        One-pass rule: we iterate through the pool at most once (pool_len slots
        visited total). This prevents the same commercial from airing multiple
        times inside a single break when the break budget exceeds the total pool
        duration (e.g. a 7-minute break with only 3 × 30-second ads). Any
        unfilled budget is handled by _pack_and_round's trim step or carried
        into _deferred_fill for later catch-up breaks.
        """
        picked = []
        used = 0
        idx = start_idx
        if budget_seconds <= 0 or not pool:
            return picked, 0, start_idx
        pool_len = len(pool)
        # Count how many distinct pool positions we have visited this break.
        # Once we have looked at every position once, stop — never cycle back.
        seen_count = 0
        while used < budget_seconds and seen_count < pool_len:
            com = pool[idx % pool_len]
            com_dur = com["duration"]
            idx += 1
            seen_count += 1
            if com_dur <= 0:
                continue
            if used + com_dur <= budget_seconds:
                picked.append(com)
                used += com_dur
            # Non-fitting commercials are skipped but still count toward seen_count
            # so we don't loop forever on a pool full of oversized spots.
        return picked, used, idx

    def _append_break(picked, t_block_content):
        """Append picked commercial entries to timeline and update _last_break_end."""
        nonlocal _last_break_end
        t = t_block_content
        for com in picked:
            timeline.append({"path": com["path"], "duration": com["duration"],
                             "start_time": t, "is_commercial": True,
                             "seek_offset": 0})
            t += com["duration"]
        if picked:
            _last_break_end = t
        return t

    def _pack_and_round(budget_seconds, pool, start_idx, t_block_content, max_seconds=None):
        """Fill as close to budget_seconds as the pool allows, capped at
        max_seconds. Any overage beyond the cap is added to _deferred_fill so
        it can be reinserted as small catch-up breaks elsewhere. Tops up the
        last whole-commercial gap with one trimmed commercial to land exactly
        on the half-hour mark. Returns (new_total, next_idx)."""
        nonlocal _deferred_fill, _last_break_end
        if budget_seconds <= 0 or not pool:
            return t_block_content, start_idx
        # Apply per-break cap and defer any overage for later catch-up
        capped = min(budget_seconds, max_seconds) if max_seconds is not None else budget_seconds
        if capped < budget_seconds:
            _deferred_fill += budget_seconds - capped
        picked, used, nidx = _pack_break(capped, pool, start_idx)
        t = _append_break(picked, t_block_content)
        leftover = int(capped) - used
        if leftover > 0:
            for _ in range(len(pool)):
                com = pool[nidx % len(pool)]
                nidx += 1
                com_dur = com["duration"]
                if com_dur > 0:
                    # Trim one commercial to fill the remaining gap exactly.
                    # Play the TAIL of the commercial (seek_offset positions VLC
                    # near the end of the file) so the break fades into the
                    # half-hour mark naturally rather than replaying the opening
                    # of a spot the viewer may have just seen in full.
                    trim_seek = max(0, com_dur - leftover)
                    timeline.append({"path": com["path"], "duration": leftover,
                                     "start_time": t, "is_commercial": True,
                                     "seek_offset": trim_seek})
                    t += leftover
                    _last_break_end = t
                    break
        return t, nidx

    def _try_catchup(t, cidx):
        """If deferred fill is owed and we are at least MIN_BREAK_GAP seconds
        from the last break, insert a small catch-up commercial break (capped
        at MAX_CATCHUP seconds). Called between shows so it never interrupts
        mid-episode. Returns (new_t, new_cidx)."""
        nonlocal _deferred_fill
        if _deferred_fill <= 0 or not commercial_pool:
            return t, cidx
        if t - _last_break_end < MIN_BREAK_GAP:
            return t, cidx
        insert_amt = min(MAX_CATCHUP, _deferred_fill)
        picked, used, new_cidx = _pack_break(insert_amt, commercial_pool, cidx)
        if picked:
            t = _append_break(picked, t)
            _deferred_fill -= used
            cidx = new_cidx
        return t, cidx

    if commercial_tracks and commercial_placement == "interrupt_half_hour":
        com_idx = 0
        for show in video_tracks:
            show_dur = show["duration"]

            # Bleed off any deferred fill before this show starts
            total_block_content, com_idx = _try_catchup(total_block_content, com_idx)

            natural_end = total_block_content + show_dur
            next_mark = ((natural_end // HALF_HOUR) + (1 if natural_end % HALF_HOUR != 0 else 0)) * HALF_HOUR
            gap = next_mark - natural_end

            if gap <= 0:
                timeline.append(_show_entry(show, show_dur, total_block_content, 0))
                total_block_content += show_dur
                continue

            if show_dur < interrupt_floor_seconds:
                # Short show: no mid-show split; one end break only
                timeline.append(_show_entry(show, show_dur, total_block_content, 0))
                total_block_content += show_dur
                end_budget = next_mark - total_block_content
                total_block_content, com_idx = _pack_and_round(
                    end_budget, commercial_pool, com_idx, total_block_content,
                    max_seconds=max_interrupt_break_seconds)

            elif show_dur < HALF_HOUR:
                # Medium show (between floor and 30 min): one mid-break, one end break
                mid_budget = gap / 2.0
                mid_point  = show_dur // 2

                timeline.append(_show_entry(show, mid_point, total_block_content, 0))
                total_block_content += mid_point

                capped_mid = min(mid_budget, max_interrupt_break_seconds)
                if mid_budget > max_interrupt_break_seconds:
                    _deferred_fill += mid_budget - max_interrupt_break_seconds
                picked, used, com_idx = _pack_break(capped_mid, commercial_pool, com_idx)
                total_block_content = _append_break(picked, total_block_content)

                remaining_half = show_dur - mid_point
                timeline.append(_show_entry(show, remaining_half, total_block_content, mid_point))
                total_block_content += remaining_half

                end_budget = next_mark - total_block_content
                total_block_content, com_idx = _pack_and_round(
                    end_budget, commercial_pool, com_idx, total_block_content,
                    max_seconds=max_interrupt_break_seconds)

            else:
                # Long show / movie: one break per 30-min interior segment + end break
                num_interior_breaks = int(show_dur // HALF_HOUR)
                num_breaks          = num_interior_breaks + 1
                per_break           = gap / float(num_breaks)
                capped_per          = min(per_break, max_interrupt_break_seconds)
                if per_break > max_interrupt_break_seconds:
                    _deferred_fill += (per_break - max_interrupt_break_seconds) * num_interior_breaks

                seek_offset    = 0
                remaining_show = show_dur
                for i in range(num_interior_breaks):
                    seg = HALF_HOUR
                    timeline.append(_show_entry(show, seg, total_block_content, seek_offset))
                    total_block_content += seg
                    seek_offset         += seg
                    remaining_show      -= seg

                    picked, used, com_idx = _pack_break(capped_per, commercial_pool, com_idx)
                    total_block_content   = _append_break(picked, total_block_content)

                if remaining_show > 0:
                    timeline.append(_show_entry(show, remaining_show, total_block_content, seek_offset))
                    total_block_content += remaining_show

                end_budget = next_mark - total_block_content
                total_block_content, com_idx = _pack_and_round(
                    end_budget, commercial_pool, com_idx, total_block_content,
                    max_seconds=max_interrupt_break_seconds)

    elif commercial_tracks and commercial_placement == "end_of_show":
        com_idx = 0
        for show in video_tracks:
            # Bleed off any deferred fill before this show starts
            total_block_content, com_idx = _try_catchup(total_block_content, com_idx)

            timeline.append(_show_entry(show, show["duration"], total_block_content, 0))
            total_block_content += show["duration"]

            next_mark  = (((total_block_content // HALF_HOUR)
                           + (1 if total_block_content % HALF_HOUR != 0 else 0)) * HALF_HOUR)
            end_budget = next_mark - total_block_content
            total_block_content, com_idx = _pack_and_round(
                end_budget, commercial_pool, com_idx, total_block_content,
                max_seconds=max_end_break_seconds)

    else:
        for show in video_tracks:
            timeline.append(_show_entry(show, show["duration"], total_block_content, 0))
            total_block_content += show["duration"]

    return timeline, total_block_content


def get_media_duration(file_path):
    """
    Returns the duration of a media file in seconds using VLC, or None if
    the duration couldn't be determined (missing file, parse timeout/failure,
    or any exception).

    IMPORTANT: this must return None -- not a guessed fallback -- on any
    failure. A fallback number here is INDISTINGUISHABLE from a real probed
    value to every caller, so it gets written into the schedule entry's
    duration field as if it were fact and never re-probed (the -1 "unprobed"
    sentinel these callers rely on for retry only re-queues entries still
    sitting at -1). That's what caused movies to get stuck at a flat 1800s
    forever and, worse, get misclassified as short-form "series" content by
    is_movie_track's duration threshold -- so a real 2-hour movie whose probe
    happened to fail would silently drop out of the movie pool entirely
    instead of just being retried a little later. Callers already know how
    to leave an entry unprobed (see _safe_dur's read-time placeholder) and
    retry it on the next pass, so returning None here is always safe.
    """
    if not file_path or not os.path.exists(file_path):
        return None

    norm_p = os.path.normpath(os.path.abspath(file_path))
    with _duration_inflight_lock:
        if norm_p in _duration_inflight:
            # Another thread is already probing this file; let it finish and
            # retry on the next pass instead of launching a redundant VLC parse.
            return None
        _duration_inflight.add(norm_p)

    try:
        import vlc
        instance = _get_duration_probe_instance()
        # Serialize probes through the shared instance — libVLC media parsing
        # isn't meant to be hammered concurrently from many threads on one
        # instance, so this lock keeps multi-channel queuing from contending.
        with _duration_probe_lock:
            media = instance.media_new(file_path)
            media.parse_with_options(vlc.MediaParseFlag.local, 5000)  # 5 second timeout
            
            import time
            start_time = time.time()
            
            pending_status = None
            for attr in ['Pending', 'pending']:
                if hasattr(vlc.MediaParsedStatus, attr):
                    pending_status = getattr(vlc.MediaParsedStatus, attr)
                    break
                    
            if pending_status is None:
                complete_flags = []
                for attr in ['Done', 'done', 'Failed', 'failed', 'Timeout', 'timeout', 'Skipped', 'skipped']:
                    if hasattr(vlc.MediaParsedStatus, attr):
                        complete_flags.append(getattr(vlc.MediaParsedStatus, attr))
                
                while media.get_parsed_status() not in complete_flags:
                    if time.time() - start_time > 5:
                        break
                    time.sleep(0.01)  
            else:
                while media.get_parsed_status() == pending_status:
                    if time.time() - start_time > 5:
                        break
                    time.sleep(0.01)  
            
            duration_ms = media.get_duration()
            if duration_ms > 0:
                duration_sec = duration_ms / 1000.0
                print(f"[MEDIA DURATION] {os.path.basename(file_path)}: {duration_sec:.1f}s")
                return duration_sec
            else:
                print(f"[MEDIA DURATION] Could not parse duration for: {os.path.basename(file_path)} -- leaving unprobed for retry")
                return None
    except Exception as e:
        print(f"[MEDIA DURATION ERROR] {e} -- leaving unprobed for retry")
        return None
    finally:
        with _duration_inflight_lock:
            _duration_inflight.discard(norm_p)


# --- FFMPEG LOCATION (bundled binary, not a PATH dependency) ---------------
# Mirrors the same base_dir pattern retro_tv_emulator._bootstrap_libvlc()
# uses for libvlc.dll: look next to the running app first (handling both
# PyInstaller's onefile extraction dir via sys._MEIPASS and a plain onedir/
# source checkout via sys.executable/__file__), THEN fall back to whatever
# "ffmpeg" resolves to on PATH. This is what lets ffmpeg.exe be added as a
# PyInstaller "Additional File" (auto-py-to-exe's Additional Files picker,
# which is just --add-binary under the hood) and actually get bundled INTO
# the build, instead of being a loose .exe someone has to remember to copy
# next to the output every time it's rebuilt.
#
# Resolved once and cached -- this runs on every loudness probe otherwise,
# and the filesystem checks (three candidate paths, each stat'd) are pure
# overhead once the answer can't change for the life of the process.
_ffmpeg_exe_path = None
_ffmpeg_resolved = False


def _resolve_ffmpeg_path():
    global _ffmpeg_exe_path, _ffmpeg_resolved
    if _ffmpeg_resolved:
        return _ffmpeg_exe_path
    _ffmpeg_resolved = True

    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

    # 1) PyInstaller onefile: extracted next to the rest of the bundle.
    meipass = getattr(sys, "_MEIPASS", None)
    # 2) PyInstaller onedir / a frozen exe generally: next to the .exe itself.
    exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else None
    # 3) Running from source: next to this .py file.
    src_dir = os.path.dirname(os.path.abspath(__file__))

    for base_dir in (meipass, exe_dir, src_dir):
        if not base_dir:
            continue
        # Bundled directly next to the app...
        candidate = os.path.join(base_dir, exe_name)
        if os.path.exists(candidate):
            _ffmpeg_exe_path = candidate
            log.info("[FFMPEG] Using bundled binary: %s", candidate)
            return _ffmpeg_exe_path
        # ...or tucked in its own subfolder, same convention as the "vlc"
        # folder libvlc.dll lives in (keeps the project root tidy).
        candidate = os.path.join(base_dir, "ffmpeg", exe_name)
        if os.path.exists(candidate):
            _ffmpeg_exe_path = candidate
            log.info("[FFMPEG] Using bundled binary: %s", candidate)
            return _ffmpeg_exe_path

    # Nothing bundled found -- fall back to PATH so a dev machine with a
    # normal ffmpeg install still works without needing the bundled copy.
    found_on_path = shutil.which(exe_name)
    if found_on_path:
        log.info("[FFMPEG] No bundled binary found; using PATH copy: %s", found_on_path)
        _ffmpeg_exe_path = found_on_path
        return _ffmpeg_exe_path

    log.warning("[FFMPEG] No bundled binary and none found on PATH -- "
                "loudness normalization will stay disabled (gain=1.0) until "
                "one is available.")
    _ffmpeg_exe_path = exe_name  # let subprocess raise its normal FileNotFoundError
    return _ffmpeg_exe_path


def get_media_loudness_gain(file_path, target_lufs=-16.0):
    """
    Measures a file's integrated loudness using ffmpeg's `loudnorm` filter
    (EBU R128 / LUFS -- the same perceptual-loudness standard used by
    Spotify, YouTube, and broadcast TV) and returns a linear volume
    multiplier that would bring it to target_lufs.

    This is deliberately NOT a peak/RMS measurement (like ffmpeg's old
    volumedetect filter) -- peak/RMS doesn't track how loud something
    actually SOUNDS. A quiet dialogue-heavy show and a loud, compressed
    music track can have wildly different RMS while sounding equally loud,
    or the reverse. LUFS models the ear's actual loudness perception over
    time, which is what lets a whisper-quiet channel and a blaring one
    genuinely land at the same perceived volume instead of just the same
    average signal level.

    Returns 1.0 (no change) if the file can't be measured for any reason --
    including ffmpeg not being installed -- so a failed/missing probe can
    never silence or blast a channel. The result is clamped to [0.4, 2.5]:
    quiet content gets boosted, loud content gets attenuated, but nothing
    can be pushed into inaudibility or amplified into clipping/noise off a
    single bad reading.
    """
    if not file_path or not os.path.exists(file_path):
        return 1.0

    norm_p = os.path.normpath(os.path.abspath(file_path))
    with _loudness_inflight_lock:
        if norm_p in _loudness_inflight:
            # Another thread is already probing this file. Return 1.0 and let
            # the probe system retry on the next pass rather than launching a
            # second ffmpeg process for the same audio -- see duplicate-probe
            # note near _loudness_inflight above.
            return 1.0
        _loudness_inflight.add(norm_p)

    try:
        import subprocess, re, json
        # -vn: audio-only. loudnorm only ever looks at the audio stream, but
        # without this flag ffmpeg still fully decodes the VIDEO stream too
        # (nothing else consumes -f null's output, so that work is pure
        # waste) -- for a normal video file that means several CPU-heavy
        # minutes of video decode per probe for zero benefit, fighting the
        # live VLC/pygame playback for CPU and causing exactly the
        # system-wide lag (video, channel changes, everything) this was
        # supposed to run quietly underneath. Dropping video decode entirely
        # cuts each probe down to just the audio track.
        popen_kwargs = {}
        if os.name == "nt":
            # Also run below normal priority on Windows so even the
            # audio-only decode yields to the foreground app instead of
            # contending with it for CPU time. CREATE_NO_WINDOW is combined
            # in here too -- without it, every probe (i.e. every new media
            # file) launches ffmpeg.exe as a normal console app and Windows
            # flashes a black console window on top of this app for the
            # ~1-2 seconds the probe runs, since ffmpeg has no window of its
            # own to hide behind.
            popen_kwargs["creationflags"] = (
                subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
            )
        # -nostdin (and stdin=DEVNULL below) is the actual fix for the "every
        # single loudness probe times out" bug: launched from a windowed
        # (console-less) app, ffmpeg has no real stdin to read from and
        # defaults to trying to read interactive y/n prompts from it anyway --
        # a classic hang-forever trap. Without an explicit stdin redirect,
        # ffmpeg just sits there until this call's own timeout kills it,
        # meaning EVERY probe silently burned its full 60s doing nothing,
        # which is why duration-probing (running in the same worker loop,
        # right after this call) was also crawling at ~1 file/minute instead
        # of its real near-instant speed.
        # CRASH FIX: without an explicit encoding, text=True decodes
        # ffmpeg's stdout/stderr using the OS default locale codepage (on
        # Windows, cp1252). capture_output=True makes subprocess.run spin up
        # internal reader threads that decode each chunk as it streams in --
        # if ffmpeg's output contains a byte that isn't valid in that
        # codepage (e.g. non-ASCII characters in a filename or in ffmpeg's
        # own banner/metadata text), that decode blows up with a
        # UnicodeDecodeError INSIDE the reader thread, outside the reach of
        # this function's own try/except below (see crash_log.txt --
        # repeated "'charmap' codec can't decode byte ..." crashes in
        # subprocess.py's _readerthread, one per probe attempt on the
        # affected file). Explicitly decoding as UTF-8 with errors="replace"
        # makes the reader threads tolerant of any byte sequence instead of
        # crashing on the first one cp1252 can't map.
        # -t 120: cap analysis to the first 2 minutes of audio.
        # EBU R128 integrated loudness converges on a stable reading after
        # just 30-60 s of typical programme audio; 2 minutes is conservative
        # and gives the same gain result as a full-file decode for every
        # real-world file in this library.
        #
        # The previous -t 600 (10 minutes) was the root cause of nearly all
        # timeout failures seen in the log: even at BELOW_NORMAL priority,
        # decoding 10 min of audio from a 720p h264 MKV or DivX AVI while
        # competing with live VLC playback can take 3-4 minutes of wall-clock
        # time, which hit the 180 s subprocess.run timeout every single time
        # for files longer than ~30 min -- explaining the dropout rate in the
        # log that scales exactly with typical file duration (.mp3 probes fast
        # / succeeds; .mkv probes slow / times out; .avi somewhere in between).
        # Cutting the decode window from 10 min → 2 min brings worst-case
        # probe wall-clock time down to ~10-20 s even on a loaded system,
        # well inside the 180 s safety net below.
        #
        # timeout=180: kept generous as a hard safety valve. In practice
        # a 2-minute audio slice should decode in under 20 s even at
        # BELOW_NORMAL priority; 180 s is now pure insurance, not a likely
        # code path.
        result = subprocess.run(
            [_resolve_ffmpeg_path(), "-nostdin", "-i", file_path, "-vn", "-t", "120", "-af",
             f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, stdin=subprocess.DEVNULL,
            **popen_kwargs
        )
        # loudnorm dumps its measurement as a JSON object to stderr, mixed
        # in with ffmpeg's normal progress/log noise -- pull just that block out.
        match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", result.stderr)
        if not match:
            print(f"[LOUDNESS PROBE] No loudnorm stats returned for: {os.path.basename(file_path)}")
            return 1.0
        stats = json.loads(match.group(0))
        measured_i = float(stats.get("input_i", 0))
        if measured_i <= -70.0 or measured_i > 0.0:
            # -70 LUFS or below is effectively silence/a failed measurement;
            # positive LUFS isn't a physically sane reading either. Don't
            # trust either case enough to act on it.
            print(f"[LOUDNESS PROBE] Unreliable reading ({measured_i} LUFS) for: {os.path.basename(file_path)}")
            return 1.0
        gain_db = target_lufs - measured_i
        gain = 10 ** (gain_db / 20.0)
        gain = max(0.4, min(gain, 2.5))
        print(f"[LOUDNESS PROBE] {os.path.basename(file_path)}: {measured_i:.1f} LUFS -> gain x{gain:.2f}")
        return gain
    except FileNotFoundError:
        print("[LOUDNESS PROBE] ffmpeg not found (checked bundled locations and PATH) -- "
              "loudness normalization is disabled until it's available.")
        return 1.0
    except subprocess.TimeoutExpired:
        print(f"[LOUDNESS PROBE] Timed out measuring: {os.path.basename(file_path)}")
        return 1.0
    except Exception as e:
        print(f"[LOUDNESS PROBE ERROR] {os.path.basename(file_path)}: {e}")
        return 1.0
    finally:
        with _loudness_inflight_lock:
            _loudness_inflight.discard(norm_p)


class RetroDatabase:
    def __init__(self, detected_aspect_ratio=None):
        print("[TELEMETRY - DB PART 1] Instantiating application profile database engine.")
        self.base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        self.config_path = os.path.join(self.base_dir, "retro_config.json")
        # Screen aspect ratio is auto-detected fresh every boot (see
        # DETECTED_ASPECT_RATIO in retro_tv_emulator.py) — it is NOT a saved
        # user preference, even though it lives in self.config and gets
        # written to disk as a side effect of save_settings() dumping the
        # whole config. Stashed here so load_settings_async() can re-stamp
        # it onto self.config AFTER the async file read replaces that dict
        # (see below) — without this, whichever finishes last between "the
        # caller sets db.config['aspect_ratio']" and "the background load
        # thread overwrites self.config wholesale from disk" wins, which is
        # a race: it silently stuck callers with a stale saved value (e.g.
        # "16:9" from a previous display) instead of this boot's real
        # detection, which in turn made 4:3-only logic like the CRT-bezel
        # border auto-disable never fire.
        self._detected_aspect_ratio = detected_aspect_ratio
        
        self.themes = {
            "DefaultBlueGreen": {"ui": (0, 255, 128), "bg": (10, 15, 30), "text": (255, 255, 255)},
            "Classic Amber": {"ui": (255, 170, 0), "bg": (15, 10, 0), "text": (240, 220, 180)}
        }
        
        self.config = {}
        self.channels_db = {}
        self.audio_files = [] 
        self.ready_flag = False
        # Multiple background threads (per-channel duration probing) can call
        # save_settings() around the same time. Without a lock they race to
        # write the same JSON file concurrently, which both corrupts/clobbers
        # writes and adds disk-I/O contention that shows up as UI lag.
        self._save_lock = threading.Lock()
        
        threading.Thread(target=self.load_settings_async, daemon=True).start()

    def load_settings_async(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    data = json.load(f)
                    self.config = data.get("config", {})
                    # Mute is intentionally NOT restored across restarts — it's a
                    # per-session convenience, not a saved preference. If it were
                    # remembered, someone who muted the app once and forgot would
                    # boot into silence and reasonably assume audio was broken. The
                    # volume level itself still persists as normal; only the mute
                    # toggle resets. (Still saved to disk when set during a session,
                    # so anything reading is_muted mid-session — e.g. a menu label —
                    # sees the real live value; this only affects what's loaded at
                    # boot.)
                    self.config["is_muted"] = False
                    # See _detected_aspect_ratio comment in __init__: this
                    # must be re-applied here, AFTER self.config gets
                    # replaced wholesale above, or this boot's real
                    # detection loses the race to whatever stale value was
                    # last saved to disk.
                    if self._detected_aspect_ratio is not None:
                        self.config["aspect_ratio"] = self._detected_aspect_ratio
                        # 4:3 Test Mode is a saved user toggle that fakes 4:3 on
                        # a real 16:9 display. Re-stamping aspect_ratio to the
                        # detected value above would silently undo it on every
                        # reboot (fake toggle stays ON but the letterbox/border
                        # snap back to 16:9). So if the toggle was left on and
                        # the display really is 16:9, re-apply the fake 4:3 here
                        # too, keeping the two in sync. On a genuine 4:3 display
                        # aspect_ratio is already "4:3" and the toggle is moot.
                        if self.config.get("fake_43_test_mode_enabled", False) and self._detected_aspect_ratio != "4:3":
                            self.config["aspect_ratio"] = "4:3"
                    raw_channels = data.get("channels", {})
                    
                self.channels_db = {}
                # One-time migration: visualizer styles get renamed occasionally
                # (e.g. the old "KaleidoscopeEdge"/"KaleidoscopeBloom" engines were
                # replaced by "PrismShards"/"StainedGlass", "StainedGlass" was later
                # replaced by "LiquidChrome", "RibbonWave" was replaced by
                # "PulseRings", "ChromaWave" was replaced by "WarpTunnel", and
                # "AuroraPeaks" was replaced by "GlowBraid").
                # Settings files saved before a rename still have the old string on
                # disk, and that string no longer matches anything in
                # VisualizerDeck — so without translating it here, affected
                # channels would silently fail to render. Extend this map whenever
                # a visualizer is renamed.
                _VIS_STYLE_MIGRATIONS = {
                    "KaleidoscopeEdge": "PrismShards",
                    "KaleidoscopeBloom": "LiquidChrome",
                    "StainedGlass": "LiquidChrome",
                    "RibbonWave": "PulseRings",
                    "ChromaWave": "WarpTunnel",
                    "AuroraPeaks": "GlowBraid",
                }
                # One-time migration: scheduling modes were renamed for clarity
                # ("random" -> "random_slots", "round_robin" -> "one_slot",
                # "two_episodes" -> "two_slots"). Settings files saved before the
                # rename still have the old string on disk, so translate it here —
                # "marathon" is unchanged and passes through untouched.
                _SCHED_MODE_MIGRATIONS = {
                    "random":       "random_slots",
                    "round_robin":  "one_slot",
                    "two_episodes": "two_slots",
                }
                for i in range(5, 45):
                    ch_str = str(i).zfill(2)
                    incoming_profile = raw_channels.get(ch_str, {})

                    # --- Base schedule buckets (preserved from saved file) ---
                    saved_schedules = incoming_profile.get("schedules", {})

                    _raw_vis_style = incoming_profile.get("visualizer_style", "Random")
                    _vis_style = _VIS_STYLE_MIGRATIONS.get(_raw_vis_style, _raw_vis_style)

                    self.channels_db[ch_str] = {
                        # --- Identity ---
                        "active":            incoming_profile.get("active", True if ch_str == "05" else False),
                        "is_visualizer":     incoming_profile.get("is_visualizer", False),
                        "visualizer_style":  _vis_style,
                        "name":              incoming_profile.get("name", f"STATION {ch_str}"),

                        # --- Scheduling mode ---
                        # "random_slots" : shuffle all content, repeat when exhausted (current behaviour)
                        # "one_slot"     : one episode of each show, then ep2 of each, etc.
                        # "two_slots"    : two episodes of the same show before moving on
                        # "marathon"     : one fixed, fully-ordered 24h catalog on a continuous clock
                        "scheduling_mode": _SCHED_MODE_MIGRATIONS.get(
                            incoming_profile.get("scheduling_mode", "random_slots"),
                            incoming_profile.get("scheduling_mode", "random_slots"),
                        ),

                        # Episode order: "sequential" preserves folder order each pass;
                        # "random" reshuffles on each rotation. Persisted so user
                        # toggle changes survive restarts. Was previously omitted from
                        # this load template, which silently reset it to "sequential"
                        # on every reboot even when the user had toggled it to "random".
                        "episode_order_mode": incoming_profile.get("episode_order_mode", "sequential"),

                        # --- Short-episode pairing ---
                        # Episodes whose duration is LESS THAN this many minutes are automatically
                        # merged two-at-a-time into one slot.  0 = pairing disabled.
                        "pair_threshold_minutes": incoming_profile.get("pair_threshold_minutes", 15),

                        # --- Commercial / bumper settings ---
                        "commercials_enabled":   incoming_profile.get("commercials_enabled", False),
                        "commercial_placement":  incoming_profile.get("commercial_placement", "interrupt_half_hour"),
                        "intros_enabled":        incoming_profile.get("intros_enabled", False),
                        "outros_enabled":        incoming_profile.get("outros_enabled", False),

                        # --- Standard time-block schedule buckets ---
                        "schedules": {
                            "Morning":      saved_schedules.get("Morning",      []),
                            "Evening":      saved_schedules.get("Evening",      []),
                            "Night":        saved_schedules.get("Night",        []),
                            "Full Day":     saved_schedules.get("Full Day",     []),
                            "Commercials":  saved_schedules.get("Commercials",  []),
                            "Intros":       saved_schedules.get("Intros",       []),
                            "Outros":       saved_schedules.get("Outros",       []),
                            "Music Tracks": saved_schedules.get("Music Tracks", []),
                            "Marathon":     saved_schedules.get("Marathon",     []),
                        },

                        # --- Holiday override schedules ---
                        # Each key holds a list of media entries (same format as regular schedules).
                        # When today's date matches a holiday window the engine substitutes
                        # this list in place of the normal time-block content.
                        "holiday_schedules": {
                            "halloween":    incoming_profile.get("holiday_schedules", {}).get("halloween",    []),
                            "christmas":    incoming_profile.get("holiday_schedules", {}).get("christmas",    []),
                            "valentine":    incoming_profile.get("holiday_schedules", {}).get("valentine",    []),
                            "thanksgiving": incoming_profile.get("holiday_schedules", {}).get("thanksgiving", []),
                            "new_year":     incoming_profile.get("holiday_schedules", {}).get("new_year",     []),
                        },

                        # --- Playback tracking log ---
                        # Used by one_slot / two_slots / marathon modes to remember
                        # where they left off so they don't repeat content until the pool
                        # is exhausted.  Format varies by mode:
                        #   one_slot / two_slots : { "played_paths": [...], "episode_index": 0 }
                        #   marathon              : { "marathon_position": 0 }
                        # Reset deliberately by factory_reset; never cleared by a channel change.
                        "playback_log": incoming_profile.get("playback_log", {}),

                        # --- Playback anchor ---
                        # Written by change_channel() the moment VLC starts a file.
                        # calculate_slotted_playback_state() honours this until the file ends,
                        # preventing a mid-show reshuffle when new media is added to the library.
                        # Format: { file, wall_start, seek_offset, duration, mode, block }
                        "playback_anchor": incoming_profile.get("playback_anchor", {}),

                        # --- Marathon anchor ---
                        # Epoch (whole days * 86400 + seconds-into-day) of the half-hour
                        # mark when marathon mode was enabled. Episode 1 of the continuous
                        # 24h timeline is pinned to this moment; without persisting it,
                        # every app restart re-anchors to "now" and the guide/scheduler
                        # lose track of where the marathon actually is.
                        "marathon_anchor_epoch": incoming_profile.get("marathon_anchor_epoch"),
                    }

                self.ready_flag = True

                # ── Resume any probe jobs interrupted by a previous close ────────
                # Entries saved with duration == -1 were never probed (the probe
                # thread was killed when the user closed the app mid-import).
                # Collect them now and fire a low-priority background worker to
                # finish the job, so every file eventually gets its real duration.
                def _resume_interrupted_probes(channels_db, save_fn):
                    import time as _time
                    _time.sleep(8)  # let boot finish and VLC settle first
                    for ch_key, ch_data in channels_db.items():
                        for container in ("schedules", "holiday_schedules"):
                            blocks = ch_data.get(container, {})
                            for block_key, entries in blocks.items():
                                unprobed = [
                                    e["path"] for e in entries
                                    if isinstance(e, dict) and e.get("duration", 0) == -1
                                    and e.get("path")
                                ]
                                if not unprobed:
                                    continue
                                print(f"[RESUME PROBE] {len(unprobed)} unprobed file(s) in ch {ch_key}/{block_key} — resuming...")
                                is_vis = ch_data.get("is_visualizer", False)
                                save_counter = 0
                                pool_size = len(unprobed)
                                batch_size = 5 if pool_size < 50 else 25
                                for i, item_path in enumerate(unprobed):
                                    norm_p = os.path.normpath(item_path)
                                    try:
                                        from media import get_media_duration as _gmd
                                        probed = _gmd(norm_p)
                                    except Exception:
                                        probed = None
                                    # FIXED: this used to hardcode `assigned = 210`
                                    # for every visualizer/music entry whenever
                                    # is_vis was true, completely discarding
                                    # `probed` even when the probe succeeded. That
                                    # meant resumed probes could never actually fix
                                    # a music track's duration -- every song ended
                                    # up permanently reported as exactly 210
                                    # seconds to the wall-clock scheduler, which is
                                    # what caused real songs to get cut off/skipped
                                    # early or restart before they should. Now it
                                    # uses the same pattern as the non-visualizer
                                    # branch: fall back to the appropriate default
                                    # (210 for music, 1800 for shows) only when the
                                    # probe itself failed, otherwise use the real
                                    # probed length.
                                    fallback_default = 210 if is_vis else 1800
                                    assigned = int(probed) if probed and probed > 0 else fallback_default
                                    block_entries = channels_db.get(ch_key, {}).get(container, {}).get(block_key, [])
                                    for entry in block_entries:
                                        if isinstance(entry, dict) and os.path.normpath(entry.get("path", "")) == norm_p:
                                            entry["duration"] = assigned
                                            break
                                    save_counter += 1
                                    if save_counter % batch_size == 0:
                                        try: save_fn()
                                        except Exception as e: log.warning("Periodic settings save failed during resume-probe for %s/%s: %s", ch_key, block_key, e)
                                    # Very gentle throttle for the resume path —
                                    # this runs in the background while the user is watching TV.
                                    if i < 2:
                                        _time.sleep(0.1)
                                    else:
                                        _time.sleep(1.2 if pool_size > 100 else 0.6)
                                try: save_fn()
                                except Exception as e: log.warning("Final settings save failed after resume-probe for %s/%s: %s", ch_key, block_key, e)
                                print(f"[RESUME PROBE] Done with ch {ch_key}/{block_key}.")

                threading.Thread(
                    target=_resume_interrupted_probes,
                    args=(self.channels_db, self.save_settings),
                    daemon=True
                ).start()

                return
            except Exception as load_err:
                print(f"[DB ERROR] Settings file reload recovery pass triggered: {load_err}")
                # CRITICAL: the file existed and had SOMETHING in it -- we just
                # failed to parse/migrate it. Falling through below rebuilds
                # self.config/self.channels_db from scratch, and used to call
                # self.save_settings() a few lines later, which would
                # immediately overwrite this file with those empty defaults --
                # turning one transient read failure (a lock, a partial write,
                # a bad value in one channel) into permanent, unrecoverable
                # loss of every saved channel/media selection. Preserve the
                # original file under a .bak name first, so even in the worst
                # case the user's real data still exists on disk afterward,
                # and skip the auto-reseed-save below so we don't destroy it
                # ourselves on this boot.
                self._recovered_from_load_error = True
                try:
                    if os.path.exists(self.config_path):
                        backup_path = self.config_path + f".corrupt-{int(time.time())}.bak"
                        shutil.copy2(self.config_path, backup_path)
                        print(f"[DB ERROR] Preserved unparsed settings file at: {backup_path}")
                except Exception as backup_err:
                    print(f"[DB ERROR] Could not back up unparsed settings file: {backup_err}")

        # FIRST BOOT SEED PROTOCOL: Only Channel 05 is active. All stations default to TV Mode.
        # FIXED: Removed game_channel_enabled and duplicate keys from startup variables [INDEX]
        self.config = {
            "global_volume": 70,
            "current_theme": "DefaultBlueGreen",
            "show_controls_on_launch": True,
            "tv_guide_enabled": True,
            "start_on_boot": False,
            "kiosk_mode_enabled": False,
            "is_muted": False,
            "audio_stereo": True,
            "brightness": 50,
            "contrast": 50,
            "color": 50,
            "sharpness": 50,
            "tint": 50,
            "vlc_decode_res": "sd",
            "theme_ui_color": "Green",
            "theme_bg_hue": 220,
            "theme_ui_hue": 140,
            "transition_type": "black",  # "black" (grey flash) or "static" (TV static flash + noise burst) between channels
            "aspect_ratio": self._detected_aspect_ratio or "16:9",
            # --- 4:3 TEST MODE (Channels tab, bottom-left) ---
            # Lets a 16:9 dev/user preview 4:3 behavior (letterboxing, VLC aspect,
            # console border availability) without touching real Windows display
            # settings. OFF by default -- this never changes what a real 4:3
            # display already gets, it only fakes it for everyone else.
            "fake_43_test_mode_enabled": False,
            # Border overlay toggle, only relevant while fake_43_test_mode_enabled
            # is on. ON by default the moment 4:3 test mode is turned on (see the
            # SYSTEM/CHANNEL_LIST toggle handler), same border art as the PSX/
            # home-console CRT bezel.
            "fake_43_border_enabled": True,
            "screen_offset_x": 0,
            "screen_offset_y": 0,
            "screen_edge_left": 0,
            "screen_edge_right": 0,
            "screen_edge_top": 0,
            "screen_edge_bottom": 0,
            "remote_bindings": {},
        }
        
        self.channels_db = {}
        for i in range(5, 45):
            ch_str = str(i).zfill(2)
            is_initially_active = True if ch_str == "05" else False

            self.channels_db[ch_str] = {
                # --- Identity ---
                "active":           is_initially_active,
                "is_visualizer":    False,
                "visualizer_style": "Random",
                "name":             f"STATION {ch_str}",

                # --- Scheduling mode ---
                "scheduling_mode": "random_slots",
                "episode_order_mode": "sequential",

                # --- Short-episode pairing ---
                "pair_threshold_minutes": 15,

                # --- Commercial / bumper settings ---
                "commercials_enabled":  False,
                "commercial_placement": "interrupt_half_hour",
                "intros_enabled":       False,
                "outros_enabled":       False,

                # --- Standard time-block schedule buckets ---
                "schedules": {
                    "Morning":      [],
                    "Evening":      [],
                    "Night":        [],
                    "Full Day":     [],
                    "Commercials":  [],
                    "Intros":       [],
                    "Outros":       [],
                    "Music Tracks": [],
                    "Marathon":     [],
                },

                # --- Holiday override schedules ---
                "holiday_schedules": {
                    "halloween":    [],
                    "christmas":    [],
                    "valentine":    [],
                    "thanksgiving": [],
                    "new_year":     [],
                },

                # --- Playback tracking log ---
                "playback_log": {},

                # --- Playback anchor ---
                "playback_anchor": {},

                # --- Marathon anchor epoch ---
                "marathon_anchor_epoch": None,
            }
        # On a genuine first-ever launch (no file yet) it's correct to write
        # these defaults out immediately. But if we got here via the
        # exception-recovery path above, the on-disk file is the backup we
        # just made, not empty -- don't immediately clobber the working
        # directory with defaults on top of it. Leave it as the .bak until
        # the user does something that legitimately triggers a save (or fix
        # the file and relaunch); this app instance still runs fine on
        # in-memory defaults for the session either way.
        if not getattr(self, "_recovered_from_load_error", False):
            self.save_settings()
        self.ready_flag = True

    def save_settings(self):
        # Guard: if load_settings_async hasn't finished yet, channels_db is still
        # the empty dict set in __init__. Writing now would overwrite the real
        # on-disk data with an empty file — a silent total data loss.
        if not getattr(self, "ready_flag", False):
            return
        try:
            with self._save_lock:
                # ATOMIC WRITE: write to a temp file first, then rename into place.
                # open(path,"w") truncates the target file immediately, so a crash
                # or SIGKILL mid-write leaves a partial/empty JSON that
                # json.load() cannot parse on the next boot, triggering the
                # backup-and-wipe recovery path and losing all user data.
                # os.replace() on both Windows (MoveFileEx MOVEFILE_REPLACE_EXISTING)
                # and POSIX (rename(2)) is guaranteed atomic at the filesystem level:
                # readers always see either the old file or the new file, never a
                # partial write.
                tmp_path = self.config_path + ".tmp"
                with open(tmp_path, "w") as f:
                    json.dump({"config": self.config, "channels": self.channels_db}, f, indent=4)
                os.replace(tmp_path, self.config_path)
        except Exception as save_err:
            print(f"[DB ERR] Failed to write settings block to file disk profile: {save_err}")

    def save_settings_async(self, force=False):
        """Same result as save_settings(), but the actual file write happens
        off the main thread so a slow disk (older/spinning-disk PCs, real-time
        antivirus scanning every write, etc.) can never stall pygame's render
        loop — not even for a single call. save_settings() writes the ENTIRE
        config + every channel's schedules/playback logs from scratch every
        time, so even one call can be a real, felt stall on slower hardware;
        this is the general fix, not just a fix for rapid repeated calls.

        Snapshots the data on the calling (main) thread first — cheap, just
        references/a shallow+deep copy — so the background thread never reads
        a dict that's still being mutated live elsewhere.

        force=True writes synchronously instead (still snapshotted the same
        way, just no thread hand-off) — used right before the app actually
        exits, where a fire-and-forget daemon thread might not finish before
        the process dies.
        """
        try:
            snapshot = {"config": dict(self.config), "channels": copy.deepcopy(self.channels_db)}
        except Exception as e:
            print(f"[DB ERR] Snapshot for async save failed, saving synchronously instead: {e}")
            self.save_settings()
            return

        def _write(data):
            try:
                with self._save_lock:
                    tmp_path = self.config_path + ".tmp"
                    with open(tmp_path, "w") as f:
                        json.dump(data, f, indent=4)
                    os.replace(tmp_path, self.config_path)
            except Exception as save_err:
                print(f"[DB ERR] Async settings write failed: {save_err}")

        if force:
            _write(snapshot)
        else:
            threading.Thread(target=_write, args=(snapshot,), daemon=True).start()

    def factory_reset(self):
        # SOFT RESET: clears all media/video schedules from every channel (05-44) and
        # restores system settings to defaults, but preserves:
        #   - Each channel's active/inactive state
        #   - Each channel's custom name
        #   - Each channel's is_visualizer mode and visualizer_style
        #   - game_channel_enabled (Channel 03 on/off toggle)
        #   - tv_guide_enabled (Channel 04 on/off toggle)

        # Preserve flags and settings that should survive a reset
        game_ch_flag   = self.config.get("game_channel_enabled", False)
        guide_ch_flag  = self.config.get("tv_guide_enabled", True)
        theme_name     = self.config.get("current_theme", "DefaultBlueGreen")
        theme_bg_color = self.config.get("theme_bg_color", "Blue")
        theme_ui_color = self.config.get("theme_ui_color", "Green")
        theme_bg_hue   = self.config.get("theme_bg_hue", 220)
        theme_ui_hue   = self.config.get("theme_ui_hue", 140)
        menu_opacity   = self.config.get("menu_opacity", 50)
        transition_type = self.config.get("transition_type", "black")
        remote_bindings = self.config.get("remote_bindings", {})

        # Reset all system/display settings to factory defaults
        self.config = {
            "global_volume": 70,
            "current_theme": theme_name,
            "show_controls_on_launch": True,
            "tv_guide_enabled": guide_ch_flag,
            "game_channel_enabled": game_ch_flag,
            "start_on_boot": False,
            "kiosk_mode_enabled": False,
            "is_muted": False,
            "audio_stereo": True,
            "brightness": 50,
            "contrast": 50,
            "color": 50,
            "sharpness": 50,
            "tint": 50,
            "vlc_decode_res": "sd",
            "theme_bg_color": theme_bg_color,
            "theme_ui_color": theme_ui_color,
            "theme_bg_hue": theme_bg_hue,
            "theme_ui_hue": theme_ui_hue,
            "menu_opacity": menu_opacity,
            "transition_type": transition_type,
            "aspect_ratio": self._detected_aspect_ratio or "16:9",
            "fake_43_test_mode_enabled": False,
            "fake_43_border_enabled": True,
            "screen_offset_x": 0,
            "screen_offset_y": 0,
            "screen_edge_left": 0,
            "screen_edge_right": 0,
            "screen_edge_top": 0,
            "screen_edge_bottom": 0,
            "remote_bindings": remote_bindings
        }

        # Clear media content only — preserve identity, scheduling config, and bumper settings.
        # Scheduling mode / commercial flags survive a reset because they are user config choices.
        # Playback logs are cleared because the media they reference is also being cleared.
        empty_schedules = {
            "Morning": [], "Evening": [], "Night": [], "Full Day": [],
            "Commercials": [], "Intros": [], "Outros": [], "Music Tracks": [],
            "Marathon": [],
        }
        empty_holidays = {
            "halloween": [], "christmas": [], "valentine": [],
            "thanksgiving": [], "new_year": [],
        }
        for ch_str, ch_data in self.channels_db.items():
            ch_data["schedules"]        = {k: [] for k in empty_schedules}
            ch_data["holiday_schedules"] = {k: [] for k in empty_holidays}
            ch_data["playback_log"]      = {}

            # Migrate any channel that pre-dates Phase 1 — add missing keys with safe defaults
            ch_data.setdefault("scheduling_mode",      "random_slots")
            ch_data.setdefault("episode_order_mode",   "sequential")
            ch_data.setdefault("commercials_enabled",  False)
            ch_data.setdefault("commercial_placement", "interrupt_half_hour")
            ch_data.setdefault("intros_enabled",       False)
            ch_data.setdefault("outros_enabled",       False)

        self.save_settings()
        print("[DB RESET] Media schedules and playback logs cleared. Channel config and scheduling settings preserved.")