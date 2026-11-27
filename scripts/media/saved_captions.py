"""Saved captions: the caption text of each media file, made once (by the server's caption scan or by a PC's live captions) and kept in the app's own folder, so a file that airs again never has its captions worked out again.

Owns: the folder main/saved captions (one file per media file, named by the file's content key, so the same file on the server and on a User share one name), merging a new copy into a saved one, and the list of what is held.
Does not own: making captions (media.py), sending them between computers (server_sync.py) or the library record (media_library.py; the hook set by set_library_note writes it).

WARNING: owner 2026-11-24: "the server accepts what ever copy it receives first and ignores the second"; every computer merges the same way: a stretch of a file that is already saved keeps its saved text, and only stretches not yet saved are taken from a new copy.
WARNING: owner 2026-11-24: "we need to add marking what parts of captions were scanned in lite mode, standard mode, or performance mode. so that if user scans it but server has better results its rescanned"; each saved stretch carries a grade (GRADES: how the language model helped when it was made), a new copy replaces only stretches saved at a lower grade, and the first copy still wins between copies of the same grade.
WARNING: owner 2026-11-24: scanned data stays in the app's folders "so ppl dont have files popping up in their organized media"; nothing here is ever written beside a media file.
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

# ============================================================================
# SECTION: Settings
# ============================================================================
try:
    import performance_governor as _gov
except Exception as _governor_error:
    _gov = None
    log.warning("[FAILURE] saved_captions: performance_governor did not import, so the built-in numbers are used: %s", _governor_error)


def _log_swallowed(where, error):
    """Writes the shared, counted [FAILURE] line through performance_governor (a plain warning when it did not import)."""
    if _gov is not None:
        return _gov.log_swallowed(where, error)
    log.warning("[FAILURE] %s: %s: %s", where, type(error).__name__, error)
    return None


# WARNING: the fallback numbers below must match SAVED_CAPTIONS_JOIN_GAP_SECONDS and SAVED_CAPTIONS_INDEX_FRESH_SECONDS in performance_governor.py; they are used only when that module fails to import.
JOIN_GAP_SECONDS = _gov.SAVED_CAPTIONS_JOIN_GAP_SECONDS if _gov is not None else 1.0
INDEX_FRESH_SECONDS = _gov.SAVED_CAPTIONS_INDEX_FRESH_SECONDS if _gov is not None else 30.0

FOLDER_NAME = "saved captions"
FORMAT_VERSION = 1
# WARNING: how well a stretch was made, by the mode that made it (media.caption_language_check): Lite's "off", Standard's "unsure", and Performance's "every". A copy from before grades were kept, or one whose grade is not known, counts as Lite (UNKNOWN_GRADE); only a live stretch made at the quick beam is below it.
# WARNING: owner 2026-11-25: "as long as those get overwritten later with better ones thats fine"; a live stretch made at the quick beam to catch up ("fast") is below every mode, so any scan's copy replaces it.
GRADES = {"fast": 0, "off": 1, "unsure": 2, "every": 3}
LOWEST_GRADE = min(GRADES.values())
UNKNOWN_GRADE = GRADES["off"]

_lock = threading.Lock()
_root = [None]
_index = {}
_stamps = {}
_index_read_at = [-1e9]
_version = [0]
_library_note = [None]


# ============================================================================
# SECTION: Where the saved captions live
# ============================================================================


def set_root(folder):
    """Sets the main folder the saved captions live under (main/saved captions)."""
    with _lock:
        _root[0] = os.path.join(str(folder), FOLDER_NAME)
        _index.clear()
        _stamps.clear()
        _index_read_at[0] = -1e9


def set_library_note(note):
    """note(content_key, file name, seconds covered, complete) records a saved file on the file's library record; None records nothing."""
    _library_note[0] = note


def folder():
    """The folder the saved captions live in, made on first use; None before set_root."""
    root = _root[0]
    if root is None:
        return None
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as caught_error:
        _log_swallowed("saved_captions: making the saved captions folder", caught_error)
        return None
    return root


def _file_for(key):
    root = folder()
    if root is None or not key or any(c in str(key) for c in "\\/:*?\"<>|"):
        return None
    return os.path.join(root, "%s.json" % key)


# ============================================================================
# SECTION: Stretches and grades
# ============================================================================


def grade_for(check):
    """The grade of a stretch made with this language check ("off", "unsure", "every"), or "fast" for a live stretch made at the quick beam."""
    return GRADES.get(str(check), UNKNOWN_GRADE)


def merge_spans(spans):
    """The stretches [[start, end], ...] in order with every overlap or near gap joined (a grade after start and end is left out)."""
    out = []
    for a, b in sorted((float(sp[0]), float(sp[1])) for sp in spans if float(sp[1]) > float(sp[0])):
        if out and a <= out[-1][1] + JOIN_GAP_SECONDS:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def missing_stretches(spans, taken):
    """The parts of spans that no stretch in taken covers."""
    out = []
    for a, b in spans:
        pieces = [[a, b]]
        for ta, tb in taken:
            nxt = []
            for pa, pb in pieces:
                if tb <= pa or ta >= pb:
                    nxt.append([pa, pb])
                    continue
                if ta > pa:
                    nxt.append([pa, ta])
                if tb < pb:
                    nxt.append([tb, pb])
            pieces = nxt
        out.extend(p for p in pieces if p[1] - p[0] > 0.05)
    return out


def covered_seconds(spans):
    return sum(float(sp[1]) - float(sp[0]) for sp in spans)


def graded(data):
    """The stretches of a saved copy with their grades, [[start, end, grade], ...]; a copy saved before grades were kept counts as LOWEST_GRADE all through."""
    marks = (data or {}).get("grades")
    if isinstance(marks, list) and marks:
        return [[float(a), float(b), int(g)] for a, b, g in marks]
    return [[a, b, UNKNOWN_GRADE] for a, b in merge_spans((data or {}).get("covered") or [])]


def covered_at_least(data, grade):
    """The stretches of a saved copy made at this grade or better."""
    return merge_spans([[a, b] for a, b, g in graded(data) if g >= grade])


def below_grade(data, grade):
    """The stretches of a saved copy made at a lower grade than this one."""
    return merge_spans([[a, b] for a, b, g in graded(data) if g < grade])


def is_unsure_text(text):
    """True for a caption the caption model was unsure of: [inaudible], another language ([speaking Spanish]), a sound ([dog barking]) or music."""
    text = str(text or "").strip()
    return text.startswith("[") or "[inaudible]" in text or "\u266a" in text or "\u266b" in text


def unsure_spans(data, pad=0.0):
    """The stretches of a saved copy around every unsure caption (is_unsure_text), widened by pad seconds on each side and joined."""
    out = []
    for cue in (data or {}).get("cues") or []:
        if is_unsure_text(cue[2]):
            out.append([max(0.0, float(cue[0]) - pad), float(cue[1]) + pad])
    return merge_spans(out)


def overlap(spans, within):
    """The parts of spans that lie inside the stretches of within."""
    out = []
    for a, b in merge_spans(spans):
        for wa, wb in merge_spans(within):
            lo, hi = max(a, wa), min(b, wb)
            if hi - lo > 0.05:
                out.append([lo, hi])
    return merge_spans(out)


def graded_seconds(data):
    """Seconds covered, each counted by its grade: the number two computers compare to see whose copy of a file is better."""
    return sum((b - a) * g for a, b, g in graded(data))


def _with_grades(spans, grade):
    out = []
    for sp in spans or []:
        a, b = float(sp[0]), float(sp[1])
        if b > a:
            out.append([a, b, int(sp[2]) if len(sp) > 2 and sp[2] is not None else int(grade)])
    return out


def _tidy_grades(marks):
    """The graded stretches in order, with touching stretches of the same grade joined."""
    out = []
    for a, b, g in sorted(marks):
        if out and out[-1][2] == g and a <= out[-1][1] + 0.05:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b, g])
    return out


def is_complete(spans, length):
    """True when the stretches cover the whole file, from its start to its end."""
    if not length or length <= 0:
        return False
    spans = merge_spans(spans)
    return (len(spans) == 1 and spans[0][0] <= JOIN_GAP_SECONDS and spans[0][1] >= float(length) - JOIN_GAP_SECONDS)


# ============================================================================
# SECTION: Reading and saving one file's captions
# ============================================================================


def read(key):
    """The saved copy of a file as {"key", "length", "covered": [[start, end], ...], "cues": [[start s, end s, text], ...], "complete", "by"}, or None."""
    path = _file_for(key)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        # WARNING: the normal end: this file has no saved captions yet.
        return None
    except (OSError, ValueError) as caught_error:
        _log_swallowed("saved_captions: reading a saved caption file", caught_error)
        return None
    if not isinstance(data, dict) or data.get("v") != FORMAT_VERSION or data.get("key") != key:
        return None
    return data


def _write(key, data):
    path = _file_for(key)
    if path is None:
        return False
    tmp = "%s.tmp%d" % (path, threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as write_error:
        _log_swallowed("saved_captions: writing a saved caption file", write_error)
        try:
            os.remove(tmp)
        except FileNotFoundError:
            # WARNING: the normal end: the temporary file was never written.
            pass
        except OSError as remove_error:
            _log_swallowed("saved_captions: removing a half-written caption file", remove_error)
        return False
    return True


def add(key, cues, spans, length, by, grade=None):
    """Merges a copy of a file's captions into the saved one: a stretch is taken, with the cues that start in it, where nothing is saved yet or where the saved stretch has a lower grade; the saved text of every other stretch is kept. spans are [start, end] (made at grade, LOWEST_GRADE when None) or [start, end, grade]. Returns True when something new was saved."""
    if not key:
        return False
    incoming = _with_grades(spans, UNKNOWN_GRADE if grade is None else grade)
    with _lock:
        held = read(key)
        held_marks = graded(held) if held else []
        taken = []
        for g in sorted({m[2] for m in incoming}, reverse=True):
            pieces = merge_spans([m for m in incoming if m[2] == g])
            as_good = [[a, b] for a, b, hg in held_marks if hg >= g] + [[a, b] for a, b, _g in taken]
            taken.extend([a, b, g] for a, b in missing_stretches(pieces, as_good))
        if not taken:
            return False

        def _in_taken(start):
            return any(a - 0.05 <= start < b for a, b, _g in taken)
        new_cues = [[float(s), float(e), str(t)] for s, e, t in (cues or []) if _in_taken(float(s))]
        kept_cues = [c for c in (held.get("cues") if held else []) if not _in_taken(float(c[0]))]
        all_cues = sorted(kept_cues + new_cues, key=lambda c: c[0])
        taken_ab = [[a, b] for a, b, _g in taken]
        marks = _tidy_grades([[pa, pb, hg] for a, b, hg in held_marks for pa, pb in missing_stretches([[a, b]], taken_ab)] + taken)
        all_spans = merge_spans(marks)
        length = float(length or (held.get("length") if held else 0) or 0)
        data = {"v": FORMAT_VERSION, "key": key, "length": length, "covered": all_spans, "grades": marks, "cues": all_cues,
                "complete": is_complete(all_spans, length), "by": (held.get("by") if held else None) or by, "at": time.time()}
        if not _write(key, data):
            return False
        _index[key] = (covered_seconds(all_spans), data["complete"], data["at"], graded_seconds(data))
        _version[0] += 1
    note = _library_note[0]
    if note is not None:
        try:
            note(key, os.path.basename(_file_for(key)), covered_seconds(all_spans), data["complete"])
        except Exception as caught_error:
            _log_swallowed("saved_captions: recording a saved caption file on the library record", caught_error)
    return True


# ============================================================================
# SECTION: What is held
# ============================================================================


def version():
    """A number that moves whenever this program saves something new."""
    return _version[0]


def held(refresh=False):
    """{content key: (seconds covered, complete, saved at, graded seconds)} of every saved file, read from the folder at most every INDEX_FRESH_SECONDS (or at once with refresh)."""
    with _lock:
        if refresh or time.monotonic() - _index_read_at[0] >= INDEX_FRESH_SECONDS:
            root = folder()
            if root is not None:
                found = {}
                try:
                    names = os.listdir(root)
                except OSError:
                    names = []
                for name in names:
                    if not name.endswith(".json"):
                        continue
                    key = name[:-5]
                    try:
                        stamp = os.stat(os.path.join(root, name)).st_mtime_ns
                    except OSError:
                        # WARNING: the normal end: the file was removed between listing the folder and reading its time; it is left out of the list.
                        continue
                    # WARNING: a file is read again only when it changed on disk; reading every saved file on each listing would parse megabytes of text every INDEX_FRESH_SECONDS.
                    seen = _stamps.get(key)
                    if seen is not None and seen[0] == stamp:
                        found[key] = seen[1]
                        continue
                    data = read(key)
                    if data is not None:
                        found[key] = (covered_seconds(data.get("covered") or []), bool(data.get("complete")), float(data.get("at") or 0.0), graded_seconds(data))
                        _stamps[key] = (stamp, found[key])
                if found != _index:
                    _version[0] += 1
                _index.clear()
                _index.update(found)
            _index_read_at[0] = time.monotonic()
        return dict(_index)


# ============================================================================
# SECTION: Cues for the screen and for sending
# ============================================================================


def cover_end(data, at_seconds):
    """The end of the saved stretch that holds this moment of the file, or None when the moment is not saved."""
    for a, b in merge_spans((data or {}).get("covered") or []):
        if a - JOIN_GAP_SECONDS <= at_seconds < b:
            return b
    return None


def cues_between(data, start, end):
    """The saved cues of a copy that overlap [start, end] seconds, as [start ms, end ms, text]."""
    return [[s * 1000.0, e * 1000.0, t] for s, e, t in (data or {}).get("cues") or [] if e >= start and s <= end]


def item_for(key):
    """The saved copy of a file as one item sent between computers ({"key", "length", "covered", "grades", "cues"}), or None."""
    data = read(key)
    if not data:
        return None
    return {"key": key, "length": data.get("length"), "covered": data.get("covered"), "grades": graded(data), "cues": data.get("cues")}


def add_item(item, by):
    """Merges an item another computer sent (item_for); a copy from before grades were kept counts as LOWEST_GRADE."""
    return add(str(item["key"]), item.get("cues") or [], item.get("grades") or item.get("covered") or [], float(item.get("length") or 0), by)


__all__ = ["set_root", "set_library_note", "folder", "read", "add", "add_item", "item_for", "held", "version", "cover_end", "cues_between",
           "merge_spans", "missing_stretches", "covered_seconds", "is_complete", "grade_for", "graded", "graded_seconds",
           "covered_at_least", "GRADES", "LOWEST_GRADE", "FOLDER_NAME", "UNKNOWN_GRADE", "below_grade", "is_unsure_text", "unsure_spans", "overlap"]
