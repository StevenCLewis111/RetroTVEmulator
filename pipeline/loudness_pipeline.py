"""The loudness learning and smoothing every place shares: the program (regular mode and profile mode), a User PC's streamed channels and the server service with the program closed.

Owns: the show-wide smoothing floor, the series' learned dB offset (worked out from the owner's own gain changes saved in the library), the season check's neighbour test and the multi-part title key.
Does not own: measuring a file's loudness (media.py) or saving it (media_library.py).

WARNING: owner 2026-11-14: a measured gain is finished here (smoothing, then the learned offset) wherever it is measured, so a file measured by the server service gets the same gain the program would have given it.
"""
from __future__ import annotations

import logging
import math
import os
import threading

import media

log = logging.getLogger(__name__)

try:
    import performance_governor as _gov
except Exception:
    _gov = None

# WARNING: the fallback numbers below must match the same names in performance_governor.py; they are used only when that module fails to import.
SHOW_SMOOTH_TOLERANCE_DB = _gov.SHOW_SMOOTH_TOLERANCE_DB if _gov is not None else 0.3
LOUDNESS_LEARN_MIN_FILES = _gov.LOUDNESS_LEARN_MIN_FILES if _gov is not None else 3
LOUDNESS_LEARN_AGREE_DB = _gov.LOUDNESS_LEARN_AGREE_DB if _gov is not None else 2.0
LOUDNESS_LEARN_SINGLE_CHANGE_DB = _gov.LOUDNESS_LEARN_SINGLE_CHANGE_DB if _gov is not None else 3.0
LOUDNESS_LEARN_MAX_OFFSET_DB = _gov.LOUDNESS_LEARN_MAX_OFFSET_DB if _gov is not None else 12

show_loudness_floor = {}
show_loudness_floor_lock = threading.Lock()
loudness_learning_lock = threading.RLock()
learned_offset_pending = {}
LEARN_FIELDS = ("learn_series_key", "learn_change_db", "learn_change_at", "learn_baseline_db", "learn_applied_db")

def _log_swallowed(where, exc):
    log.warning("[LOUDNESS] %s failed (the rest goes on): %s", where, exc)


_hub_singleton = None
_hub_failed = False
_hub_lock = threading.Lock()


def get_gain_hub():
    """The shared gain reader on the persistent library, or None when the library cannot be opened (logged once)."""
    global _hub_singleton, _hub_failed
    if _hub_singleton is not None or _hub_failed:
        return _hub_singleton
    with _hub_lock:
        if _hub_singleton is None and not _hub_failed:
            try:
                import media_library as _ml
                _hub_singleton = _ml.shared_gain_hub()
            except Exception as exc:
                _hub_failed = True
                log.warning("[GAIN HUB] the library gain reader could not start, gains are not read from the library: %s", exc)
    return _hub_singleton


def middle_value(values):
    """The middle number of a non-empty list."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def library_gain_is_current(record):
    """True when a stored scan was measured under today's loudness targets, convergence and scan version; a stamp an older version never wrote counts as current, and the owner's own gain always counts."""
    version = record.get("scan_version")
    if version == media.OWNER_GAIN_SCAN_VERSION:
        return True
    if version is not None and version != media.LOUDNESS_GAIN_SCAN_VERSION:
        return False
    target = record.get("target_lufs")
    if target is not None and not any(abs(float(target) - float(t)) < 1e-9 for t in (media.TARGET_LUFS, media.MUSIC_TARGET_LUFS)):
        return False
    convergence = record.get("convergence_db")
    if convergence is not None and abs(float(convergence) - float(media.LOUDNESS_CONVERGENCE_DB)) >= 1e-9:
        return False
    return True


def get_part_group_key(norm_path):
    if not norm_path:
        return None
    info = media._movie_part_info({"path": norm_path})
    if info is None:
        return None
    base, _num = info
    return f"{os.path.dirname(norm_path).lower()}::{base}"


def get_library_gain(norm_path, hub=None):
    """The scanned gain the library holds for a file, or None when it has none or it was measured under other settings."""
    hub = hub if hub is not None else get_gain_hub()
    hub = hub
    if hub is None or not norm_path:
        return None
    try:
        record = hub.read(norm_path)
    except Exception as _swallowed_error:
        _log_swallowed("_get_library_gain: reading the file's gain from the library", _swallowed_error)
        return None
    if record["gain"] and library_gain_is_current(record):
        return record["gain"]
    return None


def get_show_smoothing_key(norm_path):
    if not norm_path:
        return None
    parent = os.path.basename(os.path.dirname(norm_path))
    info = media._season_folder_base(parent)
    if info:
        return info[1]
    if media._bare_season_number(parent) is not None:
        grandparent = os.path.basename(os.path.dirname(os.path.dirname(norm_path)))
        return media._normalize_show_key(grandparent) if grandparent else None
    return None


def apply_show_loudness_smoothing(norm_p, gain, shortfall_info):
    show_key = get_show_smoothing_key(norm_p)
    if show_key is None or not shortfall_info:
        return gain
    achieved = shortfall_info.get("achieved_lufs")
    measured = shortfall_info.get("measured_lufs")
    peak_capped = shortfall_info.get("peak_capped")
    if achieved is None or measured is None:
        return gain
    with show_loudness_floor_lock:
        floor = show_loudness_floor.get(show_key)
        if peak_capped:
            if floor is None or achieved < floor:
                show_loudness_floor[show_key] = achieved
            return gain
        if floor is not None and achieved > floor + SHOW_SMOOTH_TOLERANCE_DB:
            _smoothed_gain = min(gain, 10 ** ((floor - measured) / 20.0))
            return _smoothed_gain
    return gain


def learning_read(norm_path, hub=None):
    """The file's loudness-learning fields from its library row ({} when the library cannot read them yet)."""
    hub = hub if hub is not None else get_gain_hub()
    hub = hub
    if hub is None or not norm_path:
        return {}
    return hub.read_file_fields(norm_path, LEARN_FIELDS) or {}


def series_offset_from_changes(changes):
    """The series offset in whole dB from the owner's counted changes, oldest first, held to LOUDNESS_LEARN_MAX_OFFSET_DB: the newest change when it is bigger than LOUDNESS_LEARN_SINGLE_CHANGE_DB; otherwise the middle of the last LOUDNESS_LEARN_MIN_FILES changes when they all go the same way and sit within LOUDNESS_LEARN_AGREE_DB of each other; 0 otherwise."""
    values = [float(change) for change, _at in changes]
    if not values:
        return 0
    if abs(values[-1]) > LOUDNESS_LEARN_SINGLE_CHANGE_DB:
        offset = values[-1]
    else:
        recent = values[-LOUDNESS_LEARN_MIN_FILES:]
        if (len(recent) < LOUDNESS_LEARN_MIN_FILES or not (all(v > 0 for v in recent) or all(v < 0 for v in recent))
                or max(recent) - min(recent) > LOUDNESS_LEARN_AGREE_DB):
            return 0
        offset = middle_value(recent)
    return int(max(-LOUDNESS_LEARN_MAX_OFFSET_DB, min(LOUDNESS_LEARN_MAX_OFFSET_DB, round(offset))))


def series_offset_for_key(show_key, hub=None):
    """The whole dB offset learned for one series from the owner's own gain changes on its files, 0 when they give none."""
    hub = hub if hub is not None else get_gain_hub()
    hub = hub
    if hub is None or show_key is None:
        return 0
    return series_offset_from_changes(hub.read_series_changes(show_key))


def learned_series_offset(norm_path, hub=None):
    """The whole dB offset learned for the file's series, or 0 when the series has none."""
    hub = hub if hub is not None else get_gain_hub()
    return series_offset_for_key(get_show_smoothing_key(norm_path), hub=hub)


def learned_offset_applied(norm_path, hub=None):
    """The dB offset that was added to this file's saved scanned gain, or 0 when none was."""
    hub = hub if hub is not None else get_gain_hub()
    value = learning_read(norm_path, hub=hub).get("learn_applied_db")
    return int(round(float(value))) if value else 0


def gain_bundle_members(norm_path):
    """The video files in the same season folder as norm_path as normalised paths; empty when the file is not in a season folder or the drive is slow."""
    if get_show_smoothing_key(norm_path) is None:
        return []
    folder = os.path.dirname(norm_path)
    if media._is_drive_degraded(folder):
        return []
    try:
        with os.scandir(folder) as entries:
            return sorted(os.path.normpath(e.path) for e in entries
                          if e.is_file() and os.path.splitext(e.name)[1].lower() in media.VIDEO_DISPLAY_EXTS)
    except OSError as e:
        log.warning("[LOUDNESS CHECK] could not list the season folder %s: %s", folder, e)
        return []


def gain_check_db(hub, norm_path):
    """A file's scanned gain in dB for the season comparison, or None when it has none on record, the owner changed it, or it belongs to a joined multi-part title."""
    if get_part_group_key(norm_path):
        return None
    record = hub.read(norm_path)
    if record["scan_version"] == media.OWNER_GAIN_SCAN_VERSION or record["manual_gain_db"]:
        return None
    gain = get_library_gain(norm_path, hub=hub)
    if gain is None or gain <= 0:
        return None
    return 20.0 * math.log10(gain) - learned_offset_applied(norm_path, hub=hub)


def season_others_middle_db(norm_path, hub=None):
    """The middle scanned dB of the other episodes in the file's season folder, without learned offsets, or None until enough of them are scanned."""
    hub = hub if hub is not None else get_gain_hub()
    hub = hub
    if hub is None:
        return None
    others = []
    for member in gain_bundle_members(norm_path):
        if member == norm_path:
            continue
        member_db = gain_check_db(hub, member)
        if member_db is not None:
            others.append(member_db)
    if len(others) < media.GAIN_CHECK_BUNDLE_MIN_SCANNED - 1:
        return None
    return middle_value(others)


def series_scan_is_outlier(norm_path, raw_db, hub=None):
    """True when enough of the season is already scanned and this file's own scanned dB is GAIN_CHECK_NEIGHBOR_DB or more from the middle of the others, the same test the season check uses."""
    hub = hub if hub is not None else get_gain_hub()
    middle = season_others_middle_db(norm_path, hub=hub)
    return middle is not None and round(abs(raw_db - middle), 2) >= media.GAIN_CHECK_NEIGHBOR_DB


def apply_learned_series_offset(norm_path, gain, hub=None):
    """Adds the series' learned offset to a freshly scanned gain unless this file's own scan is off from its siblings; that file is left without the offset until the season check scans it again and confirms it. The offset used is held until the gain is saved."""
    hub = hub if hub is not None else get_gain_hub()
    norm = os.path.normpath(norm_path)
    offset = learned_series_offset(norm, hub=hub)
    applied = 0
    if offset and gain and gain > 0:
        if series_scan_is_outlier(norm, 20.0 * math.log10(gain), hub=hub):
            log.info("[LOUDNESS LEARN] %s scanned differently from the rest of its series, so the learned %+d dB was left off it until it is scanned again",
                     os.path.basename(norm), offset)
        else:
            applied = offset
    with loudness_learning_lock:
        learned_offset_pending[norm] = applied
    return gain * 10 ** (applied / 20.0) if applied else gain


def finish_measured_gain(norm_path, gain, shortfall_info, apply_learned_offset=True, hub=None):
    """A freshly measured gain after the show's smoothing and, unless apply_learned_offset is False, the series' learned offset; None stays None."""
    hub = hub if hub is not None else get_gain_hub()
    if gain is None:
        return None
    gain = apply_show_loudness_smoothing(norm_path, gain, shortfall_info)
    if apply_learned_offset:
        return apply_learned_series_offset(norm_path, gain, hub=hub)
    with loudness_learning_lock:
        learned_offset_pending[os.path.normpath(norm_path)] = 0
    return gain


def take_learned_offset_applied(norm_path):
    """The offset held for the scan being saved for this file, once; 0 when none is held."""
    with loudness_learning_lock:
        return learned_offset_pending.pop(os.path.normpath(norm_path), 0)
