
import os
import re
import sys
import json
import copy
import ctypes
import difflib
import threading
import shutil
import time
import types
import logging
import math
import subprocess
import numpy as np
import pygame
try:
    # Optional. Only used by apply_video_effects() below to run the Video-tab
    # Brightness/Contrast/Tint LUT and Color blend as SIMD passes straight on
    # the frame's own pixels instead of several full-frame NumPy temporaries.
    # Without it everything still works, through the original NumPy path.
    import cv2 as _cv2_fx
except Exception:  # ImportError, or a broken native install
    _cv2_fx = None

log = logging.getLogger(__name__)

# Single shared control for how thick a focus/selection highlight border is
# drawn everywhere in the UI (menus, submenus, toggles, buttons, yes/no
# dialogs, etc). Bump this one value to make every highlight border thicker
# across the whole app instead of hunting down each draw call individually.
# (Does not affect the TV guide's selected time slot, which is indicated by
# a filled background color rather than a border, or static/decorative
# panel outlines that aren't tied to selection state.)
SELECTION_BORDER_WIDTH = 3

def _media_warning(_key, message, *args):
    """Write one inexpensive line for a recoverable media failure."""
    log.warning(message, *args)


# ---------------------------------------------------------------------------
# Shortcut (Windows .lnk / .url, and POSIX symlink) recognition + resolution.
#
# Project folders (channel content, ROM folders, splash/menu asset folders,
# etc.) are sometimes organized with shortcuts instead of real files/folders
# -- e.g. keeping one show's episodes in a single place on disk but wanting
# it to also appear under a second channel's folder, without duplicating the
# files. Every folder walk/listing in the app that's meant to discover
# "content someone put in this project folder" should recognize a shortcut
# entry, follow it to whatever it actually points at, and use that target
# (not the shortcut file itself) as the thing that gets listed/scanned/
# added. This section is the single shared implementation of that so the
# file explorer, the channel content scanner, and the ROM/BIOS/splash
# scanners all treat shortcuts identically instead of three subtly
# different one-off implementations.
# ---------------------------------------------------------------------------

SHORTCUT_EXTENSIONS = {".lnk", ".url"}


def is_shortcut_path(path):
    """True if `path` names a Windows shortcut file (.lnk/.url) or a
    symlink -- i.e. something that points at other content rather than
    being that content itself."""
    try:
        if os.path.islink(path):
            return True
    except OSError:
        pass
    return os.path.splitext(str(path))[1].lower() in SHORTCUT_EXTENSIONS


def _read_lnk_target(path):
    """Pull the target path out of a Windows .lnk (Shell Link) file with no
    external dependency. Minimal parser for the MS-SHLLINK binary format:
    reads the LinkInfo block's LocalBasePath, which covers a normal
    "shortcut to a local file or folder" link. Anything this can't
    confidently parse (corrupt link, network-only target, unrecognized
    layout) returns None rather than guessing at a path."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None

    # Header is a fixed 0x4C bytes, and starts with the Shell Link class
    # GUID's first 4 bytes as a fixed literal signature.
    if len(data) < 0x4C or data[:4] != b"\x4C\x00\x00\x00":
        return None

    flags = int.from_bytes(data[0x14:0x18], "little")
    HAS_LINK_TARGET_ID_LIST = 0x01
    HAS_LINK_INFO           = 0x02
    offset = 0x4C

    if flags & HAS_LINK_TARGET_ID_LIST:
        if offset + 2 > len(data):
            return None
        idlist_size = int.from_bytes(data[offset:offset + 2], "little")
        offset += 2 + idlist_size

    if not (flags & HAS_LINK_INFO) or offset + 4 > len(data):
        return None

    li_size = int.from_bytes(data[offset:offset + 4], "little")
    li = data[offset:offset + li_size]
    if len(li) < 16:
        return None

    li_flags = int.from_bytes(li[8:12], "little")
    VOLUME_ID_AND_LOCAL_BASE_PATH = 0x01
    if not (li_flags & VOLUME_ID_AND_LOCAL_BASE_PATH):
        return None  # network-share-only target (CommonNetworkRelativeLink)

    local_base_off = int.from_bytes(li[16:20], "little")
    if not local_base_off or local_base_off >= len(li):
        return None

    end = li.find(b"\x00", local_base_off)
    if end == -1:
        end = len(li)
    raw = li[local_base_off:end]
    try:
        return raw.decode("mbcs" if os.name == "nt" else "cp1252", errors="replace") or None
    except LookupError:
        return raw.decode("utf-8", errors="replace") or None


def resolve_shortcut_target(path):
    """Return the local path a shortcut points at, or None if it can't be
    resolved (broken link, unreadable file, or a shortcut that doesn't
    point at a local file/folder at all -- e.g. a web URL)."""
    try:
        if os.path.islink(path):
            target = os.readlink(path)
            if not os.path.isabs(target):
                target = os.path.join(os.path.dirname(path), target)
            return os.path.normpath(target)
    except OSError:
        pass

    ext = os.path.splitext(str(path))[1].lower()
    if ext == ".lnk":
        target = _read_lnk_target(path)
        return os.path.normpath(target) if target else None

    if ext == ".url":
        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line[:4].lower() == "url=":
                        raw = line[4:].strip()
                        if raw.lower().startswith("file:///"):
                            from urllib.parse import unquote
                            local = unquote(raw[8:]).replace("/", os.sep)
                            return os.path.normpath(local)
                        return None  # a real web URL, not a local shortcut
        except OSError:
            return None

    return None


def resolve_shortcut_chain(path, max_hops=8):
    """Follow a shortcut (or a shortcut pointing at another shortcut) down
    to a real file/folder path. Returns `path` unchanged if it isn't a
    shortcut at all, or the last path reached if a link in the chain can't
    be resolved / the chain loops back on itself -- callers should still
    check the result actually exists before trusting it."""
    current = path
    seen = set()
    for _ in range(max_hops):
        if not is_shortcut_path(current):
            return current
        if current in seen:
            return current
        seen.add(current)
        target = resolve_shortcut_target(current)
        if not target:
            return current
        current = target
    return current


def strip_shortcut_display_suffix(name):
    """Clean a shortcut's raw filename for display: drop the .lnk/.url
    extension and Windows' auto-appended ' - Shortcut' suffix (what a
    copied shortcut, or a freshly-created one, is named by default) so a
    shortcut is shown under the same name as whatever it points to,
    instead of an extra '<name>.lnk' / '<name> - Shortcut' label."""
    import re as _re
    base, ext = os.path.splitext(name)
    if ext.lower() in SHORTCUT_EXTENSIONS:
        name = base
    name = _re.sub(r'(?i)\s*-\s*shortcut(?:\s*\(\d+\))?\s*$', '', name).strip()
    return name or base


def resolve_dir_entry(entry_path, entry_name):
    """Resolve one os.scandir() entry (by path/name) to what it should
    actually be treated as: follows a shortcut/symlink to its real
    target so callers list/scan/play the thing it points at instead of
    the shortcut file itself.

    Returns (kind, real_path, display_name) where kind is "dir", "file",
    or None (broken/unresolvable shortcut, or a shortcut that doesn't
    point at anything reachable -- caller should skip the entry).
    display_name already has the shortcut suffix stripped (see
    strip_shortcut_display_suffix) so a shortcut named "Show - Shortcut"
    or "Show.lnk" reads the same as the real folder/file it points to.
    """
    target = entry_path
    if is_shortcut_path(entry_path):
        resolved = resolve_shortcut_chain(entry_path)
        if resolved == entry_path or not os.path.exists(resolved):
            return None, None, None
        target = resolved

    display_name = strip_shortcut_display_suffix(entry_name)
    try:
        if os.path.isdir(target):
            return "dir", os.path.normpath(target), display_name
        if os.path.isfile(target):
            return "file", os.path.normpath(target), display_name
    except OSError:
        pass
    return None, None, None


def iter_files_resolving_shortcuts(root, ignored_dir_names=None, valid_exts=None,
                                    is_drive_responsive=None):
    """Recursively yield real file paths under `root`, the way os.walk()
    would, except a directory entry that's a shortcut (.lnk/.url) or a
    symlink is followed to whatever real folder it points at and that
    folder's contents are walked too -- so a channel's project folder can
    pull in another folder's shows via a shortcut instead of duplicating
    files on disk, exactly like a normal sub-folder would.

    `ignored_dir_names`: optional set of lowercase folder names to skip
    entirely (not descended into), matching the existing plain os.walk
    scanners' ignore lists.
    `valid_exts`: optional set of lowercase extensions -- only matching
    files are yielded; None yields every file.
    `is_drive_responsive`: optional callable(path) -> bool so a caller
    that already skips unresponsive drives on a plain os.walk can apply
    the same guard here (an unresponsive drive is skipped rather than
    walked, same reasoning as the non-shortcut-aware scanners).

    Guards against shortcut loops -- a folder that, through some chain of
    shortcuts, ends up pointing back at one of its own ancestors -- by
    tracking the real (canonical) path of every directory already walked
    in this call and never entering the same one twice.
    """
    ignored_dir_names = ignored_dir_names or set()
    seen_dirs = set()

    def _walk(dir_path):
        if is_drive_responsive is not None and not is_drive_responsive(dir_path):
            return
        try:
            real = os.path.realpath(dir_path)
        except OSError:
            real = dir_path
        if real in seen_dirs:
            return
        seen_dirs.add(real)
        try:
            entries = list(os.scandir(dir_path))
        except (PermissionError, OSError):
            return

        subdirs = []
        files = []
        for entry in entries:
            try:
                kind, real_path, _disp = resolve_dir_entry(entry.path, entry.name)
            except OSError:
                continue
            if kind == "dir":
                if entry.name.lower() not in ignored_dir_names:
                    subdirs.append(real_path)
            elif kind == "file":
                files.append((entry.name, real_path))

        subdirs.sort()
        files.sort(key=lambda x: x[0].lower())
        for fname, fpath in files:
            if valid_exts is None or os.path.splitext(fpath.lower())[1] in valid_exts:
                yield fpath
        for sd in subdirs:
            yield from _walk(sd)

    yield from _walk(root)


# ---------------------------------------------------------------------------
# Shared "hide extra text" display-title cleanup.
#
# Every surface that shows a video's name (TV Guide rows, the file explorer/
# DVD-splash-picker's video rows) shares clean_video_display_title, and
# every surface that shows a music track's name (the visualizer's now-
# playing card, the file explorer's audio rows) shares
# clean_music_display_title -- one implementation per media type instead of
# a slightly different regex list on each screen that happens to show that
# type of title.
# ---------------------------------------------------------------------------

VIDEO_DISPLAY_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv",
    ".mpg", ".mpeg", ".ts", ".m2ts",
}
AUDIO_DISPLAY_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".wma"}


def clean_video_display_title(raw_title):
    """Canonical 'hide extra text' cleanup for a video's display name --
    strips season/episode markers, resolution, codec/rip-group tags, and
    the folder path/file extension, leaving just the show/movie title.
    e.g. "Show.Name.S01E02.1080p.WEB-DL.x264-GROUP.mkv" becomes
    "Show Name"."""
    import re as _re

    def _strip_markers(text):
        t = text.replace("_", " ")
        t = _re.sub(r'[\[\(][^\]\)]*[\]\)]', ' ', t)

        _CUT_PATTERNS = [
            r's\d{1,2}[\.\s_-]*[ex]\d{1,3}\b',
            r'\bseason[\.\s_-]*\d+\b',              # Season 1 / Season.1 / Season_1
            r'\b\d{1,2}x\d{2,3}\b',
            r'\bepisode[\.\s_-]*\d+\b',
            r'\bep[\.\s_-]*\d+\b',          # Ep15 / Ep 15 / Ep.15 / Ep_15
            r'\bpart[\.\s_-]*\d+\b',
            r'\bpt\.?[\.\s_-]*\d+\b',
            r'\b(?:19|20)\d{2}\b',                  # a bare year: 1999 / 2020
            r'\b(?:1080p|720p|480p|2160p|4k)\b',
            r'\b(?:dvdrip|webrip|web-?dl|blu-?ray|hdtv|xvid|divx|x264|x265|h26[45]|hevc|aac|ac3|dts|remux|repack|proper|extended|unrated)\b',
        ]
        # Marker-FIRST names ("S01E15 - Show Name", "1x15.Show.Name",
        # "Episode 15 Show Name"): everything is cut at the earliest marker
        # below, so a leading marker used to cut the name down to nothing.
        # Drop leading episode markers (and the separators after them) first
        # so the name that FOLLOWS the marker is what gets kept. Only the
        # episode-style markers are stripped here -- a leading year/part
        # number/etc. is left to the original behavior.
        # A bare "E15" (no season) is only stripped when it LEADS the name,
        # never mid-string, so a real title ending in a letter+number is
        # left alone.
        _LEADING_EP_PATTERNS = _CUT_PATTERNS[:5] + [r'e\d{1,3}\b']
        for _ in range(4):
            _lead = None
            for pat in _LEADING_EP_PATTERNS:
                _lead = _re.match(r'^[\s\.\-\u2013\u2014:]*(?:' + pat + r')', t, _re.IGNORECASE)
                if _lead:
                    break
            if not _lead:
                break
            t = t[_lead.end():]
            t = _re.sub(r'^[\s\.\-\u2013\u2014:]+', '', t)

        cut_at = len(t)
        for pat in _CUT_PATTERNS:
            m = _re.search(pat, t, _re.IGNORECASE)
            if m and m.start() < cut_at:
                cut_at = m.start()

        t = t[:cut_at]
        t = t.replace(".", " ").replace("-", " ")
        t = _re.sub(r'\s+', ' ', t).strip()
        return t

    s = raw_title if isinstance(raw_title, str) else str(raw_title)
    _original_raw = s
    _folder_fallback = ""
    if "/" in s or "\\" in s:
        _parent = os.path.dirname(s)
        if _parent:
            _folder_fallback = os.path.basename(_parent)
        s = os.path.basename(s)
    if "." in s:
        s = os.path.splitext(s)[0]

    cleaned = _strip_markers(s)
    if cleaned:
        return cleaned

    # Filename cleaned down to nothing -- try the folder name instead. A
    # bare season folder ("Season 1", "S01") cleans to nothing too, so keep
    # walking up to the enclosing show folder.
    if "/" in _original_raw or "\\" in _original_raw:
        _folder = os.path.dirname(_original_raw)
        for _ in range(3):
            if not _folder:
                break
            cleaned_folder = _strip_markers(os.path.basename(_folder))
            if cleaned_folder:
                return cleaned_folder
            _next = os.path.dirname(_folder)
            if _next == _folder:
                break
            _folder = _next

    log.error("Unable to derive a video title from %r", _original_raw)
    return cleaned


_MUSIC_TITLE_JUNK_PATTERNS = [
    # Album/compilation branding that rides along in a lot of ripped
    # filenames but isn't part of the actual song name -- same complaint
    # class as clean_video_display_title, just a different junk
    # vocabulary (album repackaging blurbs instead of season/codec tags).
    # Longer/more specific phrases are listed before the shorter phrases
    # they contain (e.g. "the complete greatest hits" before "greatest
    # hits") so re.sub's first-match-wins per phrase can't leave a stray
    # "the complete" behind after "greatest hits" alone matched.
    r'\bthe\s+complete\s+greatest\s+hits\b',
    r'\bcomplete\s+greatest\s+hits\b',
    r'\bgreatest\s+hits\b',
    r'\bthe\s+very\s+best\s+of\b',
    r'\bvery\s+best\s+of\b',
    r'\bthe\s+best\s+of\b',
    r'\bthe\s+complete\s+collection\b',
    r'\bcomplete\s+collection\b',
    r'\bdefinitive\s+collection\b',
    r'\bultimate\s+collection\b',
    r'\bessential\s+collection\b',
    r'\banthology\b',
    r'\bbox\s*set\b',
    # Disc/track code shorthand ("D1T15", "DISC 2", "CD1", "(CD)") --
    # tells you where the track sits in a multi-disc rip, nothing about
    # the song itself.
    r'\bd\d{1,2}t\d{1,3}\b',
    r'\bdisc\s*\d+\b',
    r'\bcd\s*\d+\b',
    r'[\[\(]\s*cd\s*[\]\)]',
    # Site watermarks some rip sources stamp into the filename.
    r'_?www\.[a-z0-9\-]+\.[a-z]{2,}',
    r'\b[a-z0-9\-]+\.(?:com|net|org|info)\b',
    # Remaster / re-issue notes, optionally led by the remaster's own year
    # ("2009 Remaster", "1999 Digitally Remastered") -- the year-led forms
    # go first so the bare "remaster(ed)" pattern below doesn't leave the
    # year sitting on its own afterward.
    r'\b(?:19|20)\d{2}\s+digitally\s+remastered\b',
    r'\b(?:19|20)\d{2}\s+remaster(?:ed)?\b',
    r'\bdigitally\s+remastered\b',
    r'\bremaster(?:ed)?\b',
    # Bonus/exclusive-track notes.
    r'\bbonus\s+track\b',
    r'\bappended\s+track\b',
    # Reissue/repackaging edition labels.
    r'\bdeluxe\s+edition\b',
    r'\bexpanded\s+edition\b',
    r'\bspecial\s+edition\b',
    # "Live"/"Live Acoustic" -- bracket/paren-scoped only (unlike the
    # album-branding phrases above, "live" alone is a common English word
    # that can legitimately appear inside a real song title, e.g. "Live
    # and Let Die" or "Stayin' Alive" -- so this only strips it when it's
    # clearly set off as its own tag, not wherever the word shows up).
    r'[\[\(]\s*live\s+acoustic\s*[\]\)]',
    r'[\[\(]\s*live\s*[\]\)]',
    # Track number / side notes some rippers add alongside the disc/CD
    # numbering already handled above.
    r'\btrack\s*\d+\b',
    r'\btk\s*\d+\b',
    r'\bside\s*[ab]\b',
    # Source/rip notes. "CD Rip"/"Vinyl Rip" are unambiguous phrases, but
    # bare "Rip" is, like "live" above, an ordinary word that can appear
    # in a real title -- only stripped when set off in brackets/parens.
    r'\bvinyl\s+rip\b',
    r'\bcd\s+rip\b',
    r'[\[\(]\s*rip\s*[\]\)]',
    # Quality-claim labels. "High Quality" is unambiguous; the two-letter
    # "HQ"/"HD" abbreviations are bracket/paren-scoped for the same
    # false-positive reason as "live"/"rip".
    r'\bhigh\s+quality\b',
    r'[\[\(]\s*hq\s*[\]\)]',
    r'[\[\(]\s*hd\s*[\]\)]',
    # Uploader/sharing spam tags.
    r'[\[\(]\s*free\s+download\s*[\]\)]',
    r'\buploaded\s+by\s+\S+',
    r'[\[\(]\s*exclusive\s*[\]\)]',
    # Audio format/codec and bitrate/quality notes -- technical metadata,
    # not part of the song name.
    r'\bmp3\b',
    r'\bwav\b',
    r'\bflac\b',
    r'\baac\b',
    r'\b\d{2,3}\s*kbps\b',
    r'\bvbr\b',
    r'\blossless\b',
]


def clean_music_display_title(raw_title):
    """Canonical 'hide extra text' cleanup for a music track's display
    name -- strips album/compilation branding, disc-track codes, and site
    watermarks out of a music filename, e.g. "EAGLES - THE COMPLETE
    GREATEST HITS - D1T15 - TAKE IT TO THE LIMIT" becomes "EAGLES - TAKE
    IT TO THE LIMIT". Mirrors clean_video_display_title in spirit (regex-
    driven junk removal instead of trusting the raw filename), but music
    junk usually sits in the MIDDLE of the string (between artist and
    song), not just the end, so this substitutes each pattern out
    wherever it appears rather than cutting the string short at the
    first match."""
    import re as _re
    t = raw_title if isinstance(raw_title, str) else str(raw_title)
    if "/" in t or "\\" in t:
        t = os.path.basename(t)
    if "." in t:
        t = os.path.splitext(t)[0]
    for pat in _MUSIC_TITLE_JUNK_PATTERNS:
        t = _re.sub(pat, ' ', t, flags=_re.IGNORECASE)
    # Several of the patterns above (year/remaster, edition, track/side,
    # rip, kbps, etc.) aren't bracket-scoped in the pattern itself, so a
    # source like "Song Name (2009 Remaster)" or "Song Name [Deluxe
    # Edition]" is left with an empty, now-meaningless bracket/paren pair
    # once the junk text inside it is gone -- strip those out too.
    t = _re.sub(r'[\(\[]\s*-?\s*[\)\]]', ' ', t)
    # Junk removal above can leave a run of now-empty " - " separators
    # behind (e.g. "EAGLES -  - TAKE IT TO THE LIMIT") -- collapse any
    # separator that has nothing but whitespace on both sides down to a
    # single space, then let the final whitespace collapse merge that
    # into the surviving " - " on either side.
    t = _re.sub(r'\s*-\s*(?=(?:-\s*)*(?:$|-))', ' ', t)
    t = _re.sub(r'\s+', ' ', t)
    # Same leftover-debris problem as the empty-bracket case above, just
    # punctuation instead of brackets: a mid-string tag like ".mp3" or
    # "Appended Track:" leaves a stray period/colon/comma stranded at the
    # edge of the string once the tag text itself is gone (e.g. "Song
    # Name.mp3" -> "Song Name." after "mp3" is stripped). Trim those
    # alongside the existing dash/space trim rather than leaving them.
    t = t.strip(' -:.,')
    return t


def clean_display_title_for_ext(name, ext):
    """Route a file's display-name cleanup to the right cleaner based on
    its extension: clean_video_display_title for video,
    clean_music_display_title for audio, or just the shortcut-stripped/
    extension-stripped name for anything else (ROMs, BIOS dumps, cores,
    images, etc. have their own dedicated naming elsewhere and shouldn't
    have video/music junk-word removal applied to them)."""
    ext = (ext or "").lower()
    if ext in VIDEO_DISPLAY_EXTS:
        return clean_video_display_title(name)
    if ext in AUDIO_DISPLAY_EXTS:
        return clean_music_display_title(name)
    base = os.path.splitext(name)[0] if ext else name
    return base


# ---------------------------------------------------------------------------
# Shared Video-tab image effects (Brightness/Contrast/Color/Sharpness/Tint).
# Lives here (not in retro_tv_emulator.py) so both retro_tv_emulator.py's TV
# channel/visualizer rendering AND game_deck.py's DVD player rendering can
# apply the exact same filter to whatever VLC frame they're blitting, without
# a circular import between those two modules.
# ---------------------------------------------------------------------------

_video_effects_cache = {
    "params":      None,
    "channel_lut": None,   # np.uint8 (3, 256): [R_lut, G_lut, B_lut]
    "sat_factor":  1.0,
    # cv2 fast path (see _apply_video_effects_cv2): tables laid out in the
    # scratch surface's own BYTE order, rebuilt only when the sliders or the
    # byte order change.
    "cv2_key":     None,
    "cv2_lut":     None,   # np.uint8 (256, 1, 3) for cv2.LUT
    "cv2_mat":     None,   # np.float32 (3, 4) for cv2.transform
}
# Flips to True (once, with one warning) if the cv2 path ever raises, so a
# broken cv2 build costs one frame of retry and then permanently uses the
# NumPy path instead of failing every frame.
_video_effects_cv2_state = {"disabled": False}
_effect_scratch = {
    "surf":      None,   # reusable pygame.Surface (downscaled work buffer) — avoids per-frame allocation
    "float_buf": None,   # reusable float32 ndarray for saturation pass
}


def _get_video_effects(db):
    brightness = db.config.get("brightness", 50) if db else 50
    contrast   = db.config.get("contrast",   50) if db else 50
    color_sat  = db.config.get("color",      50) if db else 50
    sharpness  = db.config.get("sharpness",  50) if db else 50
    tint       = db.config.get("tint",       50) if db else 50
    fx_active  = (brightness != 50 or contrast != 50 or color_sat != 50
                  or sharpness != 50 or tint != 50)
    return brightness, contrast, color_sat, sharpness, tint, fx_active


def _fx_channel_offsets(surf):
    """Byte offset of the R, G and B channels within each pixel of a 24-bit
    pygame surface, as (r_off, g_off, b_off), or None when the layout is
    anything the cv2 path doesn't handle (big-endian, non-24-bit, odd masks).
    Read from the surface itself rather than assumed, because pygame's
    default 24-bit layout is B,G,R in memory (its pixels3d() view hides that
    behind a negative channel stride, which cv2 can't consume)."""
    try:
        if sys.byteorder != "little" or surf.get_bytesize() != 3:
            return None
        shifts = surf.get_shifts()[:3]
        if any(sh % 8 for sh in shifts):
            return None
        offs = tuple(sh // 8 for sh in shifts)
        return offs if sorted(offs) == [0, 1, 2] else None
    except Exception:
        return None


def _apply_video_effects_cv2(cached_surf, offs, current_params, channel_lut, color):
    """In-place Brightness/Contrast/Tint LUT + Color blend + Sharpness on the
    24-bit scratch surface using cv2.

    Same math as the NumPy path in apply_video_effects(): per-channel LUT
    first, then pull each channel toward/away from Rec.601 luma by
    color/50. The blend is a single 3x3 matrix per pixel
    (out = sat*c + (1-sat)*luma), so cv2.transform does it in one pass with
    no float copy of the frame. A -0.5 offset makes cv2's round-to-nearest
    behave like the NumPy path's truncating float->uint8 cast; results match
    the NumPy path to within 1/255 on the few pixels where the two float
    pipelines round differently.

    Sharpness runs last, after color grading (the order a normal video
    pipeline sharpens in), via a standard unsharp mask: blur the frame,
    then add back (or subtract) the blurred/original difference scaled by
    how far the slider sits from its neutral midpoint of 50 -- >50
    sharpens, <50 softens, 50 is a no-op, matching every other slider's
    convention in this app. cv2.addWeighted saturates to uint8 on write, so
    no separate clip is needed.

    Works on the surface's own bytes (no copy, honours row padding), so the
    surface stays locked only inside this function -- the view objects are
    always released before returning, even on error."""
    brightness, contrast, color_p, tint, sharpness = current_params
    key = (current_params, offs)
    cache = _video_effects_cache
    if cache["cv2_key"] != key:
        lut3 = np.empty((256, 1, 3), dtype=np.uint8)
        for ch in range(3):
            lut3[:, 0, offs[ch]] = channel_lut[ch]
        sat = color_p / 50.0
        luma = (0.299, 0.587, 0.114)
        luma_by_byte = [0.0, 0.0, 0.0]
        for ch in range(3):
            luma_by_byte[offs[ch]] = luma[ch]
        mat = np.zeros((3, 4), dtype=np.float32)
        for i in range(3):
            for j in range(3):
                mat[i, j] = (sat if i == j else 0.0) + (1.0 - sat) * luma_by_byte[j]
            mat[i, 3] = -0.5
        cache["cv2_lut"], cache["cv2_mat"], cache["cv2_key"] = lut3, mat, key

    w, h, pitch = cached_surf.get_width(), cached_surf.get_height(), cached_surf.get_pitch()
    buf = cached_surf.get_buffer()
    view = None
    try:
        view = np.frombuffer(buf, dtype=np.uint8, count=pitch * h).reshape(h, pitch)[:, :w * 3].reshape(h, w, 3)
        # An all-50 Brightness/Contrast/Tint LUT is exactly the identity, so
        # skip that pass when only Color/Sharpness is being adjusted.
        if not (brightness == 50 and contrast == 50 and tint == 50):
            _cv2_fx.LUT(view, cache["cv2_lut"], dst=view)
        if color_p != 50:
            _cv2_fx.transform(view, cache["cv2_mat"], dst=view)
        if sharpness != 50:
            _sharp_strength = (sharpness - 50) / 50.0  # -1.0 (soften) .. +1.0 (sharpen)
            _blurred = _cv2_fx.GaussianBlur(view, (0, 0), 1.0)
            # out = view + strength*(view - blurred), done in one pass:
            # addWeighted(view, 1+strength, blurred, -strength, 0).
            _cv2_fx.addWeighted(view, 1.0 + _sharp_strength, _blurred, -_sharp_strength, 0, dst=view)
    finally:
        view = None
        del buf


def apply_video_effects(surface, brightness=50, contrast=50, color=50, sharpness=50, tint=50):
    global _video_effects_cache, _effect_scratch

    if brightness == 50 and contrast == 50 and color == 50 and tint == 50 and sharpness == 50:
        return surface

    try:
        current_params = (brightness, contrast, color, tint, sharpness)

        if _video_effects_cache["params"] != current_params:
            # Rebuild LUT only when a slider value changes (not every frame).
            brightness_offset = (brightness - 50) * 2.55
            contrast_factor   = contrast / 50.0

            base = np.arange(256, dtype=np.float32)
            base = base + brightness_offset
            base = (base - 128) * contrast_factor + 128

            tint_shift = (tint - 50) * 1.5
            r_lut = np.clip(base + tint_shift * 0.5, 0, 255).astype(np.uint8)
            g_lut = np.clip(base - tint_shift,        0, 255).astype(np.uint8)
            b_lut = np.clip(base + tint_shift * 0.5, 0, 255).astype(np.uint8)

            _video_effects_cache["params"]      = current_params
            _video_effects_cache["channel_lut"] = np.stack([r_lut, g_lut, b_lut])
            _video_effects_cache["sat_factor"]  = color / 50.0

        channel_lut = _video_effects_cache["channel_lut"]
        sat_factor  = _video_effects_cache["sat_factor"]

        w, h = surface.get_width(), surface.get_height()

        cached_surf = _effect_scratch["surf"]
        if cached_surf is None or cached_surf.get_width() != w or cached_surf.get_height() != h:
            _effect_scratch["surf"]      = pygame.Surface((w, h), 0, 24)
            _effect_scratch["float_buf"] = None   # size changed — invalidate float buf
            cached_surf = _effect_scratch["surf"]

        # Blit into 24-bit scratch so SDL handles BGRA→RGB conversion cleanly.
        cached_surf.blit(surface, (0, 0))

        # Fast path: cv2 LUT + one-pass color matrix + Gaussian unsharp
        # mask, in place on the scratch surface's own bytes. Measured ~10x
        # faster than the NumPy path below at 1080p (which spends most of
        # its time in fancy-index LUT gathers and full-frame float
        # temporaries).
        _cv2_fx_failed = False
        if _cv2_fx is not None and not _video_effects_cv2_state["disabled"]:
            _offs = _fx_channel_offsets(cached_surf)
            if _offs is not None:
                try:
                    _apply_video_effects_cv2(cached_surf, _offs, current_params, channel_lut, color)
                    return cached_surf
                except Exception as _cv2_fx_error:
                    _video_effects_cv2_state["disabled"] = True
                    _cv2_fx_failed = True
                    _media_warning(
                        "video_effects_cv2_error",
                        "[VIDEO EFFECTS] cv2 fast path failed; using the NumPy path from now on: %s",
                        # str(), not the exception object: a log handler that
                        # buffers records would otherwise keep the traceback
                        # (and through it the locked pixel view) alive.
                        str(_cv2_fx_error),
                    )
        if _cv2_fx_failed:
            # Recovery happens OUTSIDE the except block on purpose: while it
            # is running, the exception's traceback can still be holding
            # references to the pixel view, which keeps the scratch surface
            # locked and would make the blit below raise. The failed attempt
            # may also have already edited pixels in place -- start over
            # from the untouched source so the NumPy path below can't
            # double-apply anything.
            import gc
            gc.collect()
            cached_surf.blit(surface, (0, 0))

        arr = pygame.surfarray.pixels3d(cached_surf)

        arr[:, :, 0] = channel_lut[0][arr[:, :, 0]]
        arr[:, :, 1] = channel_lut[1][arr[:, :, 1]]
        arr[:, :, 2] = channel_lut[2][arr[:, :, 2]]

        if color != 50:
            shape = arr.shape

            # Reuse pre-allocated float32 scratch buffer; never allocate mid-frame
            fb = _effect_scratch["float_buf"]
            if fb is None or fb.shape != shape:
                fb = np.empty(shape, dtype=np.float32)
                _effect_scratch["float_buf"] = fb

            # Copy uint8 pixels into float32 buffer in-place (no new allocation)
            np.copyto(fb, arr, casting="unsafe")

            # Luminance-weighted grayscale, then blend toward/away from gray
            gray = (0.299 * fb[:, :, 0] +
                    0.587 * fb[:, :, 1] +
                    0.114 * fb[:, :, 2])[:, :, None]
            fb -= gray
            fb *= sat_factor
            fb += gray
            np.clip(fb, 0, 255, out=fb)

            # Write back into the uint8 surface array in-place
            np.copyto(arr, fb, casting="unsafe")

        if sharpness != 50:
            # Pure-NumPy unsharp mask -- this path only runs when cv2 is
            # unavailable or its own fast path just failed, so it doesn't
            # need cv2.GaussianBlur. A 3x3 edge-padded box blur stands in
            # for the Gaussian the cv2 path uses; visually close enough at
            # this radius and cheap relative to the rest of this fallback,
            # which is already the slow path.
            strength = (sharpness - 50) / 50.0  # -1.0 (soften) .. +1.0 (sharpen)

            fb = _effect_scratch["float_buf"]
            if fb is None or fb.shape != arr.shape:
                fb = np.empty(arr.shape, dtype=np.float32)
                _effect_scratch["float_buf"] = fb
            np.copyto(fb, arr, casting="unsafe")

            padded = np.pad(fb, ((1, 1), (1, 1), (0, 0)), mode="edge")
            blurred = (
                padded[0:-2, 0:-2] + padded[0:-2, 1:-1] + padded[0:-2, 2:] +
                padded[1:-1, 0:-2] + padded[1:-1, 1:-1] + padded[1:-1, 2:] +
                padded[2:,   0:-2] + padded[2:,   1:-1] + padded[2:,   2:]
            ) * (1.0 / 9.0)

            # out = view + strength*(view - blurred)
            fb += strength * (fb - blurred)
            np.clip(fb, 0, 255, out=fb)
            np.copyto(arr, fb, casting="unsafe")

        del arr

        return cached_surf

    except Exception as effect_error:
        _media_warning(
            "video_effects_error",
            "[VIDEO EFFECTS] Processing failed; using the unmodified surface: %s",
            effect_error,
        )
        return surface


def _now_ms():
    return time.time() * 1000.0


_path_exists_cache = {}
_path_exists_cache_lock = threading.Lock()

# The per-read TTL check in _cached_path_exists only refreshes/evicts a key
# when that exact path is looked up again. A path checked once and never
# again (e.g. an episode played once in a large library) would otherwise
# keep its small (bool, float) tuple entry in the dict forever, since
# nothing else ever sweeps the dict itself. Periodically sweep out anything
# past its TTL regardless of whether it's been re-read, and also cap the
# dict's raw size as a cheap backstop in case sweeps fall behind a burst of
# distinct one-off paths.
_PATH_EXISTS_CACHE_SWEEP_INTERVAL_MS = 60000
_PATH_EXISTS_CACHE_MAX_ENTRIES = 20000
_last_path_exists_cache_sweep_ms = 0.0

_PATH_NOT_FOUND_TTL_MS = 5000
# Positive hits use a longer TTL than not-found results so deleted files are eventually rechecked.
_PATH_FOUND_TTL_MS = 30000
# UNKNOWN (drive unresponsive, or the bounded probe itself timed out) is
# inherently a "we don't actually know" result -- caching it anywhere near
# as long as a confirmed PRESENT would mean a file that's merely going
# through a rough patch gets treated as confirmed-good for up to 30s. Give
# it its own short TTL so the next schedule build re-checks it soon,
# without re-probing on literally every single frame.
_PATH_UNKNOWN_TTL_MS = 1500

# Tri-state path status. PRESENT/MISSING come from a bounded os.path.exists()
# that actually completed; UNKNOWN means the drive is known-unresponsive or
# the bounded probe itself timed out -- i.e. genuinely "don't know", not
# "confirmed either way". Scheduling code should treat UNKNOWN like PRESENT
# for *eligibility* (never silently drop a file just because its drive had a
# rough moment) but is free to log/surface it differently, which a plain
# bool can't express. _cached_path_exists() below stays the boolean
# fail-open-compatible wrapper every existing caller already expects;
# _path_status() is the new tri-state entry point for callers -- like the
# schedule builder -- that need to tell "confirmed gone" apart from
# "temporarily couldn't tell".
PATH_PRESENT = "present"
PATH_MISSING = "missing"
PATH_UNKNOWN = "unknown"

# Limit background disk reads and keep fast metadata probes separate from
# long-running loudness analysis.
FAST_DISK_IO_MAX_CONCURRENT = 3
_FAST_DISK_IO_SEMAPHORE = threading.BoundedSemaphore(FAST_DISK_IO_MAX_CONCURRENT)

# App-wide hard ceiling on simultaneous loudness-analysis ffmpeg processes
# (shared by the regular probe pass and the two-pass renorm sweep). This is
# the one number to change to raise/lower total loudness-scan concurrency
# app-wide.
LOUDNESS_PROBE_MAX_CONCURRENT = 2
_LOUDNESS_PROBE_SEMAPHORE = threading.BoundedSemaphore(LOUDNESS_PROBE_MAX_CONCURRENT)

# Separate, dedicated ceiling for music-track loudness scans (ch03 menu
# music, visualizer songs) -- its own semaphore, not a slice of the video
# one above. Music tracks are short (a single whole-file scan each, see
# _sample_lufs_readings' no-bracket path) so a dedicated slot lets the
# music queue's own worker thread (retro_tv_emulator.py's
# _music_precache_worker) clear its backlog quickly, running concurrently
# with whatever movie/show scan the main probe pass is in the middle of,
# instead of queueing up behind it for a shared slot. Kept at 1: music
# scans are cheap and quick regardless, and this only needs to guarantee
# music never has to wait on video, not to add a second music process.
MUSIC_LOUDNESS_PROBE_MAX_CONCURRENT = 1
_MUSIC_LOUDNESS_PROBE_SEMAPHORE = threading.BoundedSemaphore(MUSIC_LOUDNESS_PROBE_MAX_CONCURRENT)

# Delay new background reads while a live channel change opens its media file.
# Existing reads continue, and the wait is bounded so probing cannot starve.
_LIVE_PLAYBACK_PRIORITY = threading.Event()
_LIVE_PLAYBACK_PRIORITY_MAX_WAIT_S = 2.5


def set_live_playback_priority(active):
    """Call with True right before opening a new file for live playback,
    and False (in a finally) once that open attempt has finished, whether
    it succeeded or failed."""
    if active:
        _LIVE_PLAYBACK_PRIORITY.set()
    else:
        _LIVE_PLAYBACK_PRIORITY.clear()


def _yield_to_live_playback(label=""):
    """Best-effort wait for a live channel change to finish opening its
    file before this background op starts its own disk read. Bounded so
    background work is delayed, never starved."""
    if not _LIVE_PLAYBACK_PRIORITY.is_set():
        return
    _wait_start = _now_ms()
    # threading.Event.wait() blocks until the flag becomes SET -- it
    # returns immediately if the flag is already set, which it is here.
    # There's no built-in "wait until cleared" on Event, so poll for it
    # instead. 20ms granularity is fine: it reacts promptly once the
    # channel-change file-open finishes and clears the flag, without
    # burning CPU on a background thread that's supposed to be idling.
    _deadline_ms = _wait_start + (_LIVE_PLAYBACK_PRIORITY_MAX_WAIT_S * 1000)
    while _LIVE_PLAYBACK_PRIORITY.is_set():
        if _now_ms() >= _deadline_ms:
            # Flag is still set after the cap -- proceed anyway rather
            # than risk background probing stalling indefinitely behind
            # a stuck flag.
            break
        time.sleep(0.02)
    _waited_ms = _now_ms() - _wait_start

import contextlib


@contextlib.contextmanager
def _fast_disk_io_slot(label=""):
    """Like `with _FAST_DISK_IO_SEMAPHORE:`, but logs slot-wait contention."""
    _yield_to_live_playback(label)
    _wait_start = _now_ms()
    with _FAST_DISK_IO_SEMAPHORE:
        _waited_ms = _now_ms() - _wait_start
        yield


def _sweep_path_exists_cache(now_ms):
    """Drop entries whose TTL has expired, regardless of whether they've
    been re-read. Must be called with _path_exists_cache_lock held."""
    expired = []
    for path, (status, cached_at) in _path_exists_cache.items():
        if status == PATH_MISSING:
            ttl = _PATH_NOT_FOUND_TTL_MS
        elif status == PATH_PRESENT:
            ttl = _PATH_FOUND_TTL_MS
        else:
            ttl = _PATH_UNKNOWN_TTL_MS
        if now_ms - cached_at >= ttl:
            expired.append(path)
    for path in expired:
        _path_exists_cache.pop(path, None)


def _maybe_sweep_path_exists_cache(now_ms):
    """Best-effort periodic + size-triggered sweep. Called opportunistically
    from _cached_path_exists rather than a dedicated thread, so it only does
    work while the cache is actually being used."""
    global _last_path_exists_cache_sweep_ms
    due_by_time = (now_ms - _last_path_exists_cache_sweep_ms) >= _PATH_EXISTS_CACHE_SWEEP_INTERVAL_MS
    due_by_size = len(_path_exists_cache) > _PATH_EXISTS_CACHE_MAX_ENTRIES
    if not (due_by_time or due_by_size):
        return
    _sweep_path_exists_cache(now_ms)
    _last_path_exists_cache_sweep_ms = now_ms


_PATH_EXISTS_PROBE_TIMEOUT_MS = 2500  # matches _DRIVE_CHECK_TIMEOUT_MS below


def _bounded_os_path_exists(p, timeout_ms=_PATH_EXISTS_PROBE_TIMEOUT_MS):
    """os.path.exists(p), but run on a background thread and bounded to
    timeout_ms -- so a hung filesystem call (a dropped network share, a
    yanked USB/external drive, a disc that's stopped responding, etc.) can
    never block the CALLING thread indefinitely, including the main
    thread.

    This is what protects a brand-new drive root that has no responsiveness
    history yet in _is_drive_responsive() below -- that check only guards
    paths on a drive already known to be unresponsive, so the first-ever
    check against a new drive still needs its own bound.

    Python threads can't be forcibly killed, so a probe that never returns
    leaves one daemon thread parked forever -- harmless, and vastly better
    than freezing the caller. If the probe doesn't finish in time, the
    whole drive is immediately marked unresponsive (not just this one
    path) so every other in-flight or future check on that drive fails
    open right away too, instead of each one separately re-discovering the
    same hang the hard way, or waiting on the health-monitor loop's own
    separate polling cycle to notice."""
    _box = []
    _done = threading.Event()

    def _worker():
        try:
            _box.append(os.path.exists(p))
        except Exception:
            _box.append(False)
        finally:
            _done.set()

    threading.Thread(target=_worker, daemon=True, name="PathExistsProbe").start()
    if _done.wait(timeout_ms / 1000.0) and _box:
        return PATH_PRESENT if _box[0] else PATH_MISSING

    root = _drive_root_of(p)
    if root is not None:
        with _drive_health_lock:
            _watched_drive_roots.add(root)
            _drive_health[root] = {"responsive": False, "checked_at": _now_ms(), "checking_since": None}
        log.warning(
            "[DRIVE HEALTH] %s marked unresponsive -- a direct os.path.exists() "
            "probe on %s took over %dms and never returned. Path-existence "
            "checks on this drive will fail OPEN (assume present) until it "
            "recovers; genuinely missing files are still caught by the "
            "normal playback-failure path.",
            root, p, timeout_ms,
        )
    return PATH_UNKNOWN


def _path_status(p):
    """Tri-state existence check: PATH_PRESENT / PATH_MISSING / PATH_UNKNOWN.

    Single source of truth for path existence -- _cached_path_exists() below
    is a thin boolean wrapper over this, so every existing caller keeps its
    exact current fail-open behavior. New callers that need to tell
    "confirmed gone" apart from "couldn't tell right now" (the schedule
    builder, episode-substitution lookup) should call this directly instead.
    """
    if not p:
        return PATH_MISSING
    now_ms = _now_ms()
    with _path_exists_cache_lock:
        _maybe_sweep_path_exists_cache(now_ms)
        cached = _path_exists_cache.get(p)
    if cached is not None:
        # cached is a (status, cached_at_ms) tuple -- still fresh?
        _cached_status, _cached_at = cached
        if _cached_status == PATH_MISSING:
            _ttl = _PATH_NOT_FOUND_TTL_MS
        elif _cached_status == PATH_PRESENT:
            _ttl = _PATH_FOUND_TTL_MS
        else:
            _ttl = _PATH_UNKNOWN_TTL_MS
        if now_ms - _cached_at < _ttl:
            return _cached_status
        # TTL expired -- treat as unknown again and fall through to a real check.

    if not _is_drive_responsive(p):
        # Drive responsiveness alone isn't evidence this file is missing --
        # it's evidence we don't know. Report that honestly instead of
        # quietly claiming PRESENT; a genuinely broken file is still caught
        # by the normal playback-failure path (_mark_file_broken).
        status = PATH_UNKNOWN
    else:
        # Bounded, not a raw os.path.exists(p) call -- see _bounded_os_path_exists
        # for why the unbounded version was able to wedge the main loop.
        status = _bounded_os_path_exists(p)

    with _path_exists_cache_lock:
        _path_exists_cache[p] = (status, now_ms)
    return status


def _cached_path_exists(p):
    """Boolean, fail-open-compatible view of _path_status(): PRESENT and
    UNKNOWN both read as True (matches every existing caller's current
    expectation -- an uncertain drive was never treated as "file gone"
    here), only a confirmed MISSING reads as False."""
    return _path_status(p) != PATH_MISSING


_drive_health = {}
_drive_health_lock = threading.Lock()
_watched_drive_roots = set()

# The monitor stops when request_media_shutdown() sets this event.
_media_shutting_down = threading.Event()


def request_media_shutdown():
    """Call once, from the main app's quit handler, so media.py's own
    background loops (currently just the drive health monitor) wind down
    instead of being left spinning until the process exits."""
    _media_shutting_down.set()


_DRIVE_CHECK_TIMEOUT_MS         = 2500
_DRIVE_RECHECK_INTERVAL_MS      = 4000  # how often a currently-healthy root gets re-probed
_DRIVE_UNRESPONSIVE_RECHECK_MS  = 3000


def _drive_root_of(path):
    if not path:
        return None
    if sys.platform == "win32":
        drive, _ = os.path.splitdrive(str(path))
        return (drive + "\\") if drive else None
    return "/"


def _purge_stale_negative_cache_for_root(root):
    """Drop cached "not found" verdicts for every path on `root`.

    Called when a drive transitions back to responsive. Negative cache
    entries normally self-heal after _PATH_NOT_FOUND_TTL_MS (5s), but
    there's no reason to make playback wait that out once we already know
    the drive answered -- clearing them immediately lets the next schedule
    build re-verify (and recover) those paths right away instead of
    continuing to treat them as missing for up to another 5 seconds.
    """
    with _path_exists_cache_lock:
        stale = [p for p, v in _path_exists_cache.items()
                 if isinstance(v, tuple) and v[0] in (PATH_MISSING, PATH_UNKNOWN)
                 and _drive_root_of(p) == root]
        for p in stale:
            _path_exists_cache.pop(p, None)
    if stale:
        log.info(
            "[DRIVE HEALTH] %s is responsive again -- cleared %d stale "
            "'not found'/'unknown' cache entries so playback re-verifies them "
            "immediately instead of waiting out their TTL", root, len(stale),
        )


def _drive_probe_worker(root):
    try:
        ok = os.path.exists(root)
    except Exception:
        ok = False
    now_ms = _now_ms()
    with _drive_health_lock:
        _was_responsive = _drive_health.get(root, {}).get("responsive", True)
        _drive_health[root] = {"responsive": ok, "checked_at": now_ms, "checking_since": None}
    if ok and not _was_responsive:
        log.info("[DRIVE HEALTH] %s recovered", root)
        _purge_stale_negative_cache_for_root(root)


def _drive_health_monitor_loop():
    while True:
        if _media_shutting_down.is_set():
            return
        try:
            now_ms = _now_ms()
            with _drive_health_lock:
                roots = list(_watched_drive_roots)
            for root in roots:
                with _drive_health_lock:
                    entry = _drive_health.get(root)
                if entry is None:
                    with _drive_health_lock:
                        _drive_health[root] = {"responsive": True, "checked_at": now_ms, "checking_since": now_ms}
                    threading.Thread(target=_drive_probe_worker, args=(root,), daemon=True).start()
                    continue
                checking_since = entry.get("checking_since")
                if checking_since is not None:
                    if now_ms - checking_since > _DRIVE_CHECK_TIMEOUT_MS and entry.get("responsive", True):
                        with _drive_health_lock:
                            if _drive_health.get(root, {}).get("checking_since") == checking_since:
                                _drive_health[root]["responsive"] = False
                                log.warning(
                                    "[DRIVE HEALTH] %s marked unresponsive -- "
                                    "its own os.path.exists() probe took over "
                                    "%dms. Path-existence checks on this drive "
                                    "will fail OPEN (assume present) until it "
                                    "recovers; genuinely missing files are still "
                                    "caught by the normal playback-failure path.",
                                    root, _DRIVE_CHECK_TIMEOUT_MS,
                                )
                    continue
                interval = _DRIVE_UNRESPONSIVE_RECHECK_MS if not entry.get("responsive", True) else _DRIVE_RECHECK_INTERVAL_MS
                if now_ms - entry.get("checked_at", 0) >= interval:
                    with _drive_health_lock:
                        _drive_health[root]["checking_since"] = now_ms
                    threading.Thread(target=_drive_probe_worker, args=(root,), daemon=True).start()
        except Exception as e:
            _media_warning(
                "drive_health_monitor",
                "_drive_health_monitor_loop: iteration failed: %s",
                e,
            )
        if _media_shutting_down.wait(0.5):
            return


def _is_drive_responsive(path):
    root = _drive_root_of(path)
    if root is None:
        return True
    with _drive_health_lock:
        _watched_drive_roots.add(root)
        entry = _drive_health.get(root)
    return True if entry is None else entry.get("responsive", True)


def _unresponsive_drive_roots():
    with _drive_health_lock:
        return sorted(r for r, e in _drive_health.items() if not e.get("responsive", True))


import collections

_drive_op_latency = {}
_drive_op_latency_lock = threading.Lock()
_DRIVE_LATENCY_SAMPLE_WINDOW = 6   # how many recent real-read samples to keep per drive root
# A loudness window's ffmpeg run scales with how much audio it decodes (up to
# 900 s per window, see LOUDNESS_SCAN_BRACKETS), so raw wall time says nothing
# about the drive. Each sample is stored as ffmpeg milliseconds per second of
# audio scanned, after allowing a fixed per-run overhead (process start,
# container open, seek) that would otherwise dominate short clips.
# Measured on this machine with video playing and every prober running,
# healthy whole-episode scans cost 24-34 ms per audio second; 100 is ~3x the
# worst healthy figure (decoding slower than ~10x realtime).
_DRIVE_LATENCY_FIXED_OVERHEAD_MS = 2000
_DRIVE_LATENCY_DEGRADED_MS_PER_AUDIO_SEC = 100.0  # recent-sample average at/above this flags the drive as degraded
_DRIVE_LATENCY_MIN_SAMPLES = 3     # don't flag off 1-2 slow samples -- could be one-off live-playback contention, not the drive itself
_DRIVE_LATENCY_SAMPLE_MAX_AGE_MS = 120_000  # samples older than this stop counting toward "degraded" (see _is_drive_degraded)


def _record_drive_op_latency(path, elapsed_ms, audio_sec=None):
    """Feed a real disk-touching operation's wall time (currently: each
    ffmpeg loudness-window pass in _lufs_window_ffmpeg, including timeouts)
    into a per-drive rolling latency tracker.

    audio_sec is how much audio that run was asked to scan. The sample is
    normalized to ffmpeg milliseconds per audio second so a long window is
    not mistaken for a slow drive. A run whose audio length is unknown is
    not recorded: raw wall time cannot be judged without it.

    Deliberately separate from _drive_health above: that only checks
    whether a drive ROOT still answers os.path.exists() at all, which
    stays fast even on a drive that's failing to read actual file data --
    a physically degrading drive (slow, retrying, silently failing reads
    on specific files/sectors) can sail through that check while every
    real read against it crawls. This tracks the thing that actually
    matters for "should background work back off": how long real media
    reads from this drive have actually been taking lately, not whether
    the drive letter itself still exists.
    """
    root = _drive_root_of(path)
    if root is None:
        return
    try:
        audio_sec = float(audio_sec) if audio_sec is not None else 0.0
    except (TypeError, ValueError):
        audio_sec = 0.0
    if audio_sec <= 0.0:
        return
    per_audio_sec_ms = max(0.0, elapsed_ms - _DRIVE_LATENCY_FIXED_OVERHEAD_MS) / audio_sec
    now_ms = _now_ms()
    with _drive_op_latency_lock:
        dq = _drive_op_latency.setdefault(root, collections.deque(maxlen=_DRIVE_LATENCY_SAMPLE_WINDOW))
        dq.append((now_ms, per_audio_sec_ms))


def _is_drive_degraded(path):
    """True if this drive's recent real media-read operations have been
    running slow enough that background work should back off it. See
    _record_drive_op_latency for why this is a different (and for a
    physically failing drive, a more meaningful) signal than
    _is_drive_responsive().
    """
    root = _drive_root_of(path)
    if root is None:
        return False
    with _drive_op_latency_lock:
        dq = _drive_op_latency.get(root)
        if not dq:
            return False
        # Only recent samples count. Without an age limit the flag could
        # lock itself on: a "degraded" drive gets skipped, skipped drives
        # produce no new samples, so the old slow samples never rotate out.
        _cutoff = _now_ms() - _DRIVE_LATENCY_SAMPLE_MAX_AGE_MS
        samples = [ms for t, ms in dq if t >= _cutoff]
        if len(samples) < _DRIVE_LATENCY_MIN_SAMPLES:
            return False
    return (sum(samples) / len(samples)) >= _DRIVE_LATENCY_DEGRADED_MS_PER_AUDIO_SEC


def _drive_latency_summary(path):
    """Human-readable snippet for a log line, or None if there's not
    enough data yet to say anything."""
    root = _drive_root_of(path)
    if root is None:
        return None
    with _drive_op_latency_lock:
        dq = _drive_op_latency.get(root)
        if not dq:
            return None
        _cutoff = _now_ms() - _DRIVE_LATENCY_SAMPLE_MAX_AGE_MS
        samples = [ms for t, ms in dq if t >= _cutoff]
        if not samples:
            return f"{root} no recent reads"
    return (f"{root} avg {sum(samples) / len(samples):.1f}ms of ffmpeg time per audio second "
            f"over last {len(samples)} recent reads (degraded at >= "
            f"{_DRIVE_LATENCY_DEGRADED_MS_PER_AUDIO_SEC:.0f})")


# --- File-level drive recovery probe ---------------------------------------
#
# Everything above (_is_drive_responsive / _unresponsive_drive_roots) only
# proves whether a drive ROOT still answers os.path.exists(). For the boot/
# system drive in particular, that answers near-instantly almost always --
# even while an individual file on it is failing every open/read. That kind
# of failure (fast rejection, not a slow hang) never sets the root
# unresponsive in the first place, so it can never be seen "recovering"
# either: nothing keyed off _unresponsive_drive_roots() can ever clear a
# latch caused by this failure mode, and the caller is left stuck until the
# app restarts. That gap is explicitly called out in
# retro_tv_emulator.py's _drive_confirmed_recovered() docstring.
#
# This is the missing signal: a real, bounded open+read against an actual
# file that is known to have failed on that drive. A confirmed successful
# read of real bytes from that exact file is direct evidence the drive
# itself is readable again -- much stronger than a root ping, and the only
# way to ever self-heal the fast-rejection failure mode above.
_file_probe_state = {}
_file_probe_lock = threading.Lock()
_FILE_PROBE_RECHECK_MS   = 5000   # don't hammer a struggling drive with retries
_FILE_PROBE_TIMEOUT_MS   = 3000   # a real read that takes longer than this is treated as failed, not "still checking" forever
_FILE_PROBE_READ_BYTES   = 65536


def _file_probe_worker(root, path):
    ok = False
    try:
        with open(path, "rb") as f:
            f.read(_FILE_PROBE_READ_BYTES)
        ok = True
    except Exception:
        ok = False
    now_ms = _now_ms()
    with _file_probe_lock:
        entry = _file_probe_state.get(root)
        # Only accept this result if it's still the probe that's currently
        # in flight for this root/path pair -- a slow read that finally
        # returns after a newer probe (different path, or a fresher retry)
        # was already started must not clobber that newer state.
        if entry is not None and entry.get("path") == path and entry.get("checking_since") is not None:
            _was_ok = entry.get("last_result")
            _file_probe_state[root] = {
                "last_result": ok, "checked_at": now_ms, "checking_since": None, "path": path,
            }
            if ok and _was_ok is not True:
                log.info(
                    "[DRIVE HEALTH] file-level probe confirmed %s is actually "
                    "readable again -- real read of %s succeeded", root, path,
                )
            elif not ok:
                log.warning(
                    "[DRIVE HEALTH] file-level probe: real read of %s still "
                    "fails -- %s not confirmed recovered yet", path, root,
                )


def probe_file_level_recovery(root, candidate_path):
    """Return the most recent confirmed result of a real open+read against
    `candidate_path` -- normally the exact file whose failure got a channel
    latched dead on `root` -- as True (a real read has succeeded recently),
    False (the most recent real read failed or timed out), or None (no
    result yet, or too long since the last check to say).

    Never blocks: the actual read always happens on a background thread,
    at most once every _FILE_PROBE_RECHECK_MS per root, so a genuinely dead
    drive isn't hammered with retries every frame.
    """
    if root is None or not candidate_path:
        return None
    now_ms = _now_ms()
    with _file_probe_lock:
        entry = _file_probe_state.get(root)
        if entry is not None and entry.get("path") != candidate_path:
            # The remembered failing file for this root has changed since
            # the last probe (a different file on the same drive has since
            # failed) -- a stale result for the OLD file says nothing about
            # whether THIS file/drive is readable, so drop it and start
            # fresh rather than reporting recovery based on the wrong file.
            entry = None
        checking_since = entry.get("checking_since") if entry else None
        if checking_since is not None:
            if now_ms - checking_since > _FILE_PROBE_TIMEOUT_MS:
                # The in-flight read has been running longer than a real
                # file read reasonably should -- treat it as a fresh
                # negative result (not a permanent hang) so this can be
                # retried again after the recheck interval below, rather
                # than reporting "no result" forever while a stuck worker
                # thread never comes back.
                _file_probe_state[root] = {
                    "last_result": False, "checked_at": now_ms,
                    "checking_since": None, "path": candidate_path,
                }
                return False
            return entry.get("last_result")
        due = entry is None or (now_ms - entry.get("checked_at", 0) >= _FILE_PROBE_RECHECK_MS)
        if due:
            _prior_result = entry.get("last_result") if entry else None
            _file_probe_state[root] = {
                "last_result": _prior_result,
                "checked_at": entry.get("checked_at", 0) if entry else 0,
                "checking_since": now_ms,
                "path": candidate_path,
            }
            threading.Thread(target=_file_probe_worker, args=(root, candidate_path), daemon=True).start()
            return _prior_result
        return entry.get("last_result")



DRIVE_CDROM = 5  # Windows GetDriveTypeW() return value for CD/DVD/BD drives

_disc_info_cache = {}
_disc_info_lock  = threading.Lock()
_DISC_INFO_RECHECK_MS = 4000


def _win_drive_type_raw(drive_root):
    if sys.platform != "win32":
        return None
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive_root))
    except Exception:
        return None


def is_optical_drive_root(path):
    if sys.platform != "win32" or not path:
        return False
    p = str(path)
    if not (len(p) == 3 and p[1] == ":" and p[2] in ("\\", "/")):
        return False
    return _win_drive_type_raw(p[0] + ":\\") == DRIVE_CDROM


def _get_volume_label_raw(drive_root):
    if sys.platform != "win32":
        return None
    try:
        vol_buf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive_root), vol_buf, ctypes.sizeof(vol_buf),
            None, None, None, None, 0,
        )
        if not ok:
            return None
        label = vol_buf.value.strip()
        return label or None
    except Exception:
        return None


def _has_video_ts_raw(drive_root):
    try:
        return os.path.isdir(os.path.join(drive_root, "VIDEO_TS"))
    except Exception:
        return False


def _disc_info_probe_worker(drive_root):
    label        = _get_volume_label_raw(drive_root)
    has_video_ts = _has_video_ts_raw(drive_root)
    with _disc_info_lock:
        _disc_info_cache[drive_root] = {
            "label": label, "has_video_ts": has_video_ts,
            "checked_at": _now_ms(), "checking": False,
        }


def get_disc_info(drive_root):
    now = _now_ms()
    need_probe = False
    with _disc_info_lock:
        entry = _disc_info_cache.get(drive_root)
        if entry is None:
            entry = {"label": None, "has_video_ts": False, "checked_at": 0, "checking": True}
            _disc_info_cache[drive_root] = entry
            need_probe = True
        elif not entry.get("checking") and now - entry.get("checked_at", 0) >= _DISC_INFO_RECHECK_MS:
            entry["checking"] = True
            need_probe = True
        result = dict(entry)
    if need_probe:
        threading.Thread(target=_disc_info_probe_worker, args=(drive_root,), daemon=True).start()
    return result


def is_dvd_disc_source(path):
    if not path:
        return False
    if os.path.splitext(str(path))[1].lower() == ".iso":
        return True
    if is_optical_drive_root(path):
        drive_root = str(path)[0] + ":\\"
        return bool(get_disc_info(drive_root).get("has_video_ts"))
    parent_name = os.path.basename(os.path.dirname(str(path).rstrip("\\/"))).upper()
    if parent_name == "VIDEO_TS":
        return True
    return False


def dvd_disc_mrl(path):
    if os.path.splitext(str(path))[1].lower() == ".iso":
        return "dvdnav:///" + os.path.abspath(path).replace("\\", "/")
    parent_dir = os.path.dirname(str(path).rstrip("\\/"))
    if os.path.basename(parent_dir).upper() == "VIDEO_TS":
        disc_root = os.path.dirname(parent_dir) or parent_dir
        drive_letter = disc_root.rstrip("\\/")
        if len(drive_letter) == 2 and drive_letter[1] == ":":
            return "dvd:///" + drive_letter + "\\"
        return "dvdnav:///" + os.path.abspath(disc_root).replace("\\", "/")
    drive_letter = str(path).rstrip("\\/")
    if len(drive_letter) == 2 and drive_letter[1] == ":":
        # Physical drive root -- MRL format required for menu-capable playback.
        return "dvd:///" + drive_letter + "\\"
    return "dvdnav://" + drive_letter

_loudness_inflight      = set()
_loudness_inflight_lock = threading.Lock()
_duration_inflight      = set()
_duration_inflight_lock = threading.Lock()
_dims_inflight          = set()
_dims_inflight_lock     = threading.Lock()

try:
    _cpu_n = os.cpu_count() or 4
except Exception:
    _cpu_n = 4
_DURATION_POOL_SIZE = max(2, min(6, _cpu_n - 1))
_duration_pool = []
_duration_pool_lock = threading.Lock()
_duration_pool_rr = 0

def _get_duration_probe_slot():
    global _duration_pool, _duration_pool_rr
    if not _duration_pool:
        with _duration_pool_lock:
            if not _duration_pool:
                import vlc
                _duration_pool = [[vlc.Instance("--no-xlib", "--quiet"),
                                   threading.Lock()]
                                  for _ in range(_DURATION_POOL_SIZE)]
    with _duration_pool_lock:
        slot = _duration_pool[_duration_pool_rr % len(_duration_pool)]
        _duration_pool_rr += 1
    return slot

def _natural_ep_key(item):
    import re as _re
    p = item.get("path", item.get("file", "")) if isinstance(item, dict) else str(item)
    name = os.path.splitext(os.path.basename(p))[0].lower()
    m = _re.search(r's(\d+)[\s._-]*[ex][\s._-]*(\d+)', name)
    if m: return (0, int(m.group(1)), int(m.group(2)), name)
    m = _re.search(r'season[\s._-]*(\d+).*?episode[\s._-]*(\d+)', name)
    if m: return (0, int(m.group(1)), int(m.group(2)), name)
    m = _re.search(r'ep(?:isode)?\W*(\d+)', name)
    if m: return (0, 0, int(m.group(1)), name)
    parts = _re.split(r'(\d+)', name)
    norm = tuple((0, int(x)) if x.isdigit() else (1, x) for x in parts)
    return (1, norm)


def build_ordered_tracks(items, sched_mode="natural", seed=0):
    tracks = []
    for a in items:
        p = a.get("path", a.get("file", "")) if isinstance(a, dict) else str(a)
        if p and _cached_path_exists(p):
            d = a.get("duration", 1800) if isinstance(a, dict) else 1800
            d = d if d and d > 0 else 1800
            tracks.append({"path": p, "duration": d})
    if not tracks:
        return []

    if sched_mode == "marathon":
        show_names, show_episodes = group_shows_flexible(tracks)
        import random as _random
        rng = _random.Random(seed)
        rng.shuffle(show_names)
        return [ep for sn in show_names for ep in show_episodes[sn]]

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


def _flex_path_parts(path):
    import re as _re
    parts = [p for p in _re.split(r"[\\/]+", str(path)) if p and p not in (".", "..")]
    # A Windows drive root ("C:") is a location, not a show folder. Without
    # this it survives the generic-folder filter below, so a file sitting
    # under a generic wrapper ("C:\TV\Cartoons\S01E15 - Show Name.mkv")
    # gets titled "C:" and the filename fallback never runs.
    if parts and _re.fullmatch(r"[A-Za-z]:", parts[0]):
        parts = parts[1:]
    return parts


def _flex_episode_marker(text):
    import re as _re
    low = str(text).lower()
    m = _re.search(r"\bs(\d+)[\s._-]*[ex][\s._-]*(\d+)\b", low)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _re.search(r"\b(\d{1,2})[\s._-]*x[\s._-]*(\d{1,3})\b", low)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _re.search(r"\bseason[\s._-]*(\d+).*?\bepisode[\s._-]*(\d+)\b", low)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _re.search(r"\bep(?:isode)?[\s._-]*(\d+)\b", low)
    if m:
        return 0, int(m.group(1))
    return None


def _flex_clean_series_title(name):
    import re as _re
    s = _strip_leading_index(str(name)).strip()
    s = _re.sub(r"(?i)[\s._\-(]+seasons?[\s._-]*\d+(?:[\s._-]*to[\s._-]*\d+)?\b.*$", "", s)
    s = _re.sub(r"(?i)[\s._\-(]+s\d{1,2}(?:[\s._-]*-[\s._-]*s?\d{1,2})?\b.*$", "", s)
    # Keep meaningful punctuation such as the hyphen in "SG-1"; only
    # release-style separators are normalized for the display title.
    s = _re.sub(r"[._]+", " ", s)
    s = _re.sub(r"\s+", " ", s).strip(" -_.()")
    return s


def _flex_is_generic_folder(name):
    return _normalize_show_key(name) in {
        "tv", "television", "shows", "series", "media", "videos", "video",
        "collections", "collection", "new folder", "complete series",
        "complete tv series", "adventure", "comedy", "drama", "cartoons",
        "cartoon", "anime", "documentaries", "documentary",
    }


def _flex_is_non_episode_track(track):
    """Exclude obvious extras while preserving normal and aired pilots."""
    import re as _re
    path = track.get("path", track.get("file", "")) if isinstance(track, dict) else str(track)
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    return bool(_re.search(
        r"(?i)(?:^|[ ._()\[\]-])(?:unaired[ ._-]*pilot|unaired|bonus|extras?|featurette|trailer|sample)(?:$|[ ._()\[\]-])",
        stem,
    ))


def _flex_series_and_season(track):
    import re as _re
    path = track.get("path", track.get("file", "")) if isinstance(track, dict) else str(track)
    parts = _flex_path_parts(path)
    if not parts:
        return "", None, None
    filename = parts[-1]
    folders = parts[:-1]
    filename_stem = filename.rsplit(".", 1)[0]
    marker = _flex_episode_marker(filename_stem)
    season_number = marker[0] if marker else None

    title = None
    # Prefer the nearest folder explicitly identifying a season.  Its parent
    # is the series unless the folder itself contains the title.
    for idx in range(len(folders) - 1, -1, -1):
        folder = folders[idx]
        bare = _bare_season_number(folder)
        suffix = _season_folder_base(folder)
        if suffix:
            title = suffix[0]
            season_number = season_number if season_number is not None else suffix[2]
            break
        if bare is not None:
            season_number = season_number if season_number is not None else bare
            if idx > 0:
                title = folders[idx - 1]
            break

    if not title:
        for folder in reversed(folders):
            cleaned = _flex_clean_series_title(folder)
            if cleaned and not _flex_is_generic_folder(cleaned):
                title = cleaned
                break

    # A generic wrapper can be resolved from the filename title before its
    # episode marker, but only when the filename contains a reliable marker.
    if (not title or _flex_is_generic_folder(title)) and marker:
        before = _re.split(r"(?i)\bs\d+[\s._-]*e\d+\b|\b\d{1,2}[\s._-]*x[\s._-]*\d+\b",
                           filename_stem, maxsplit=1)[0]
        cleaned = _flex_clean_series_title(before)
        if not cleaned and _re.search(r"[A-Za-z]", _re.sub(
                r"(?i)\bs\d+[\s._-]*e\d+\b|\b\d{1,2}[\s._-]*x[\s._-]*\d+\b", " ", filename_stem)):
            # Marker-FIRST filename ("S01E15 - Show Name.mkv"): nothing
            # precedes the marker, so take the name that follows it --
            # same rule the guide's display cleaner uses.
            cleaned = clean_video_display_title(filename)
        if cleaned:
            title = cleaned

    title = _flex_clean_series_title(title or os.path.splitext(filename)[0])
    return title, season_number, marker[1] if marker else None


def group_shows_flexible(raw_tracks):
    """Resolve arbitrary layouts into stable series/season/episode groups."""
    import collections as _col
    import re as _re
    groups = _col.defaultdict(list)
    display_names = {}
    for track in raw_tracks:
        if not isinstance(track, dict):
            track = {"path": str(track)}
        if _flex_is_non_episode_track(track):
            continue
        title, season, episode = _flex_series_and_season(track)
        if not title:
            continue
        key = _normalize_show_key(title)
        if not key:
            continue
        item = dict(track)
        item["_series_key"] = key
        item["_series_title"] = title
        # Keep the resolved folder-aware title available to the TV Guide.  The
        # guide must not reconstruct a show name from an episode filename.
        item["title"] = title
        item["_season_number"] = season if season is not None else 0
        item["_episode_number"] = episode if episode is not None else 0
        display_names.setdefault(key, title)
        groups[key].append(item)

    result = {}
    for key, items in groups.items():
        # Semantic duplicate key catches .ia, Copy, and alternate-release
        # twins when they identify the same series/season/episode.
        best = {}
        order = []
        for item in items:
            path = item.get("path", item.get("file", ""))
            stem = os.path.splitext(os.path.basename(str(path)))[0]
            stem = _re.sub(r"(?i)(?:\.ia|[\s._-]+copy(?:[\s._-]+\d+)?|\s*\(\d+\))+$", "", stem)
            identity = (key, item["_season_number"], item["_episode_number"],
                        _normalize_show_key(stem) if not item["_episode_number"] else "")
            if identity not in best:
                best[identity] = item
                order.append(identity)
            else:
                old = best[identity]
                old_path = str(old.get("path", ""))
                # Prefer the unmarked/non-copy file; otherwise retain the
                # first deterministic scanner result.
                old_marked = bool(_re.search(r"(?i)(\.ia\.|- copy|\(\d+\))", old_path))
                new_marked = bool(_re.search(r"(?i)(\.ia\.|- copy|\(\d+\))", str(path)))
                if old_marked and not new_marked:
                    best[identity] = item
        clean = [best[k] for k in order]
        clean.sort(key=lambda x: (
            x["_season_number"], x["_episode_number"],
            _natural_ep_key(x),
        ))
        result[display_names[key]] = clean

    show_names = sorted(result)
    return show_names, {sn: result[sn] for sn in show_names}


def _strip_leading_index(name):
    import re as _re
    return _re.sub(r'^\s*\d+\s*[.)\-_]\s+', '', name).strip()


def _normalize_show_key(s):
    import re as _re3
    s = _re3.sub(r'[._\-]+', ' ', s)
    s = _re3.sub(r'\s+', ' ', s)
    return s.strip().lower()


def _bare_season_number(dirname):
    import re as _re
    name = dirname.strip()
    m = _re.match(r'^seasons?[\s._-]*(\d+)(?!\s*-\s*\d)\b', name, _re.IGNORECASE)
    if not m:
        # Exactly 1-2 digits, same as _season_folder_base's S01-style check.
        m = _re.match(r'^s(\d{1,2})(?:[\s._-]|$)', name, _re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1))


def _season_folder_base(show_name):
    import re as _re
    m = _re.match(r'^(.*?)[\s._\-(]+seasons?[\s._-]*(\d+)(?!\s*-\s*\d)\b', show_name, _re.IGNORECASE)
    if not m:
        m = _re.match(r'^(.*?)[\s._\-(]+s(\d{2})(?:[\s._-]|$)', show_name, _re.IGNORECASE)
    if not m:
        return None
    base_raw = m.group(1).strip(' ._-(')
    if not base_raw:
        return None
    return base_raw, _normalize_show_key(base_raw), int(m.group(2))


MOVIE_LENGTH_THRESHOLD_SECONDS = 4800


SIX_HOUR_BLOCKS = [
    ("12-4am", 0, 4),
    ("4-8am", 4, 8),
    ("8-12pm", 8, 12),
    ("12-4pm", 12, 16),
    ("4-8pm", 16, 20),
    ("8-12am", 20, 24),
]
SIX_HOUR_BLOCK_NAMES = [b[0] for b in SIX_HOUR_BLOCKS]


def resolve_track_duration(media_probe_cache, path, stored_duration, default=1800):
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


def track_duration_is_estimated(media_probe_cache, path, stored_duration):
    """Report whether a resolved duration still came from the fallback.

    ``resolve_track_duration`` intentionally returns a usable value for an
    unprobed entry, but that value must not erase the distinction between an
    actual 30-minute file and a ``-1``/missing-duration placeholder.  Callers
    attach this provenance to the track before the schedule builder sees it.
    """
    try:
        if path:
            cached = (media_probe_cache or {}).get(os.path.normpath(str(path)))
            cached_dur = cached.get("duration") if isinstance(cached, dict) else None
            if cached_dur and cached_dur > 0:
                return False
    except Exception:
        pass
    return not (stored_duration and stored_duration > 0)


def _cached_lineup_durations_stale(cached_durations, media_probe_cache,
                                    movie_threshold_seconds=None,
                                    material_diff_seconds=60):
    """Report whether a {path: duration} snapshot frozen onto a schedule
    (e.g. playback_log["sched_cached_durations"]) has drifted from what
    the live media probe cache now knows for those same paths -- either
    a real probe result crossing the movie-length line relative to the
    frozen value, or differing from it by more than
    ``material_diff_seconds``. Used to decide whether a cached lineup can
    keep being reused as-is or needs to be rebuilt with fresh durations.

    Mirrors the identically-named helper in retro_tv_emulator.py (kept
    here too so callers that only import media, like ui.py's TV guide
    renderer, don't need the main module).
    """
    if not cached_durations or not isinstance(media_probe_cache, dict):
        return False
    if movie_threshold_seconds is None:
        movie_threshold_seconds = MOVIE_LENGTH_THRESHOLD_SECONDS
    for path, frozen in cached_durations.items():
        if not (frozen and frozen > 0):
            continue
        probed = media_probe_cache.get(path)
        real = probed.get("duration") if isinstance(probed, dict) else probed
        if not (real and real > 0):
            continue
        crosses_movie_line = (
            (frozen >= movie_threshold_seconds) != (real >= movie_threshold_seconds))
        if crosses_movie_line or abs(float(real) - float(frozen)) > material_diff_seconds:
            return True
    return False


def is_movie_track(track, threshold_seconds=MOVIE_LENGTH_THRESHOLD_SECONDS):
    if not isinstance(track, dict):
        return False
    path = track.get("path") or track.get("file") or ""
    if path:
        try:
            p = str(path).lower().replace("\\", "/")
            if "/movies/" in p:
                return True
            if any(marker in p for marker in ("/tv/", "/series/", "/shows/")):
                return False
        except Exception:
            pass
    dur = track.get("duration", 0)
    return bool(dur and dur >= threshold_seconds)


def stable_series_id(series_title, channel_id=None):
    """Return a filesystem-independent identity for playback state.

    Channel scope prevents the same series on two channels from sharing a
    playback cursor.  Omitting channel_id preserves compatibility with old
    callers and old configuration keys.
    """
    import re as _re
    value = _normalize_show_key(series_title)
    value = _re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    value = value or "unknown-series"
    if channel_id is not None and str(channel_id).strip():
        channel = _re.sub(r"[^a-z0-9]+", "-", _normalize_show_key(channel_id)).strip("-")
        if channel:
            return f"{channel}:{value}"
    return value


def migrate_show_positions(show_pos, show_names, channel_id=None):
    """Copy legacy title-keyed positions to channel-scoped keys.

    Existing stable keys win.  Legacy keys are retained for rollback and
    compatibility, while the returned report makes the migration auditable.
    """
    migrated = dict(show_pos or {})
    report = {"migrated": [], "already_scoped": [], "unmatched": []}
    for title in show_names or []:
        old_key = str(title)
        new_key = stable_series_id(title, channel_id)
        if new_key == old_key:
            report["already_scoped"].append(old_key)
            continue
        if new_key in migrated:
            report["already_scoped"].append(new_key)
            continue
        if old_key in migrated:
            migrated[new_key] = copy.deepcopy(migrated[old_key])
            report["migrated"].append({"from": old_key, "to": new_key})
        else:
            report["unmatched"].append(old_key)
    return migrated, report






def _looks_like_episode_filename(stem):
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
        result.extend(parts.values())
    return result


import re as _re_dedupe

_DUPLICATE_FILE_MARKER_RE = _re_dedupe.compile(r'\.(?:ia)(\.[^.\\/]+)$', _re_dedupe.IGNORECASE)


def _dedupe_key_for_path(path):
    return _DUPLICATE_FILE_MARKER_RE.sub(r'\1', str(path))


def dedupe_duplicate_episode_files(episodes):
    best_by_key = {}
    order = []
    for ep in episodes:
        path = ep.get("path", ep.get("file", "")) if isinstance(ep, dict) else str(ep)
        key = _dedupe_key_for_path(path)
        if key not in best_by_key:
            best_by_key[key] = ep
            order.append(key)
        else:
            existing_path = best_by_key[key].get("path", best_by_key[key].get("file", "")) \
                if isinstance(best_by_key[key], dict) else str(best_by_key[key])
            existing_is_marked = existing_path != key
            current_is_marked = path != key
            if existing_is_marked and not current_is_marked:
                best_by_key[key] = ep
    return [best_by_key[k] for k in order]


def _dedupe_key_ignore_extension(path):
    d = os.path.dirname(str(path))
    base = os.path.splitext(os.path.basename(str(path)))[0]
    return os.path.join(d, base).lower()


# Preference order when the same episode/movie exists in more than one
# container -- earlier entries are the more stable/robust format and win.
_EXTENSION_STABILITY_PRIORITY = [
    ".mkv",   # most robust container -- resilient to incomplete/corrupt writes, no size ceiling
    ".mp4",   # widely compatible, well-standardized
    ".m4v",   # mp4 variant, same stability profile
    ".mov",   # stable but more codec-restrictive / Apple-oriented
    ".webm",  # solid open format, mainly web-targeted
    ".avi",   # older container, known indexing/subtitle/codec quirks
    ".wmv",   # proprietary, limited modern tooling support
    ".flv",   # Flash video format.
]
_EXTENSION_STABILITY_RANK = {ext: i for i, ext in enumerate(_EXTENSION_STABILITY_PRIORITY)}


def _extension_stability_rank(path):
    ext = os.path.splitext(str(path))[1].lower()
    return _EXTENSION_STABILITY_RANK.get(ext, len(_EXTENSION_STABILITY_PRIORITY))


def dedupe_same_name_different_extension(episodes):
    """Same folder + same filename but a different container (e.g.
    'Show.S01E01.mp4' next to 'Show.S01E01.mkv') is the same episode encoded
    twice -- keep only the most stable container (see
    _EXTENSION_STABILITY_PRIORITY) and drop the rest from scheduling."""
    best_by_key = {}
    order = []
    for ep in episodes:
        path = ep.get("path", ep.get("file", "")) if isinstance(ep, dict) else str(ep)
        key = _dedupe_key_ignore_extension(path)
        if key not in best_by_key:
            best_by_key[key] = ep
            order.append(key)
        else:
            existing = best_by_key[key]
            existing_path = existing.get("path", existing.get("file", "")) \
                if isinstance(existing, dict) else str(existing)
            if _extension_stability_rank(path) < _extension_stability_rank(existing_path):
                best_by_key[key] = ep
    return [best_by_key[k] for k in order]


# split_movies_and_series() does several O(N) passes of regex-based parsing
# per file (dedupe key extraction, part-number matching, season/episode
# extraction in group_shows_flexible, etc). For a normal-sized library this
# is invisible, but a channel with thousands of files in one schedule block
# turns "several regex substitutions per file" into real, main-thread-blocking
# work if it's redone on every tune-in to that channel (build_show_rotation
# is called from several places in
# calculate_slotted_playback_state/_get_channel_ordered_content per channel
# change). The actual movie/series grouping only needs to be redone when the
# underlying file set (or a resolved duration that flips a movie/episode
# classification) actually changes, so we memoize the result per channel,
# keyed on a cheap fingerprint of (path, duration) pairs -- building that
# fingerprint is plain attribute access + tuple construction (no regex), so
# even the cache-key computation itself is far cheaper than the work it lets
# us skip.
_split_movies_cache_lock = threading.Lock()
_split_movies_cache = {}  # channel_id -> (fingerprint, (movie_pool, show_names, show_episodes))


def _split_movies_fingerprint(raw_tracks, threshold_seconds):
    try:
        return (threshold_seconds, tuple(
            (t.get("path", t.get("file", "")) if isinstance(t, dict) else str(t),
             t.get("duration", 0) if isinstance(t, dict) else 0)
            for t in raw_tracks
        ))
    except Exception:
        return None


def split_movies_and_series(raw_tracks, threshold_seconds=MOVIE_LENGTH_THRESHOLD_SECONDS, channel_id=None):
    _fp = _split_movies_fingerprint(raw_tracks, threshold_seconds) if channel_id is not None else None
    if _fp is not None:
        with _split_movies_cache_lock:
            _cached = _split_movies_cache.get(channel_id)
        if _cached is not None and _cached[0] == _fp:
            _c_movie_pool, _c_show_names, _c_show_episodes = _cached[1]
            return (list(_c_movie_pool), list(_c_show_names),
                    {sn: list(eps) for sn, eps in _c_show_episodes.items()})

    # Remove exact/marker twins before combining short episodes or movie
    # parts.  Otherwise two copies can become an apparently intentional pair.
    raw_tracks = dedupe_duplicate_episode_files(raw_tracks)
    raw_tracks = dedupe_same_name_different_extension(raw_tracks)
    raw_tracks = pair_movie_parts(raw_tracks, threshold_seconds)
    movie_pool = []
    series_tracks = []
    for t in raw_tracks:
        if is_movie_track(t, threshold_seconds):
            movie_pool.append(t)
        else:
            series_tracks.append(t)
    show_names, show_episodes = group_shows_flexible(series_tracks)
    show_episodes = {
        sn: dedupe_same_name_different_extension(dedupe_duplicate_episode_files(eps))
        for sn, eps in show_episodes.items()
    }

    if _fp is not None:
        with _split_movies_cache_lock:
            _split_movies_cache[channel_id] = (
                _fp,
                (list(movie_pool), list(show_names),
                 {sn: list(eps) for sn, eps in show_episodes.items()}),
            )

    return movie_pool, show_names, show_episodes



SHORT_EPISODE_PAIRING_VERSION = 3
LEFTOVERS_TITLE = "The Leftovers"


def _episode_series_key(episode):
    """Return the series identity used by short-episode pairing.

    build_show_rotation() normally calls pair_short_episodes() on one
    show-group at a time, but cached schedules are flattened and can contain
    several shows.  Do not let two unrelated short files become a pair just
    because they happen to be adjacent in that flattened list.
    """
    if isinstance(episode, dict):
        explicit = episode.get("_series_key")
        if explicit:
            return str(explicit)
        path = episode.get("path", episode.get("file", ""))
    else:
        path = str(episode)
    try:
        title, _season, _episode = _flex_series_and_season(
            {"path": path}
        )
        return _normalize_show_key(title) if title else ""
    except Exception:
        return ""


def pair_short_episodes(episodes, threshold_seconds=900):
    if threshold_seconds <= 0:
        return list(episodes)
    result = []
    leftovers = []
    i = 0
    while i < len(episodes):
        ep = episodes[i]
        dur = ep.get("duration", 1800) if isinstance(ep, dict) else 1800
        if dur < threshold_seconds and i + 1 < len(episodes):
            nxt = episodes[i + 1]
            nxt_dur = nxt.get("duration", 1800) if isinstance(nxt, dict) else 1800
            same_series = (_episode_series_key(ep) and
                           _episode_series_key(ep) == _episode_series_key(nxt))
            if nxt_dur < threshold_seconds and same_series:
                ep_path = ep.get("path", ep.get("file", "")) if isinstance(ep, dict) else str(ep)
                nxt_path = nxt.get("path", nxt.get("file", "")) if isinstance(nxt, dict) else str(nxt)
                result.append({"path": ep_path, "duration": dur + nxt_dur,
                               "is_pair": True, "pair_ep1": ep, "pair_ep2": nxt,
                               "pair_ep1_path": ep_path, "pair_ep1_duration": dur,
                               "pair_ep2_path": nxt_path, "pair_ep2_duration": nxt_dur})
                i += 2
                continue
        if dur < threshold_seconds:
            leftovers.append(ep)
        else:
            result.append(ep)
        i += 1
    if leftovers:
        result.append({"path": leftovers[0].get("path", ""),
                       "duration": sum(e.get("duration", 0) for e in leftovers),
                       "title": LEFTOVERS_TITLE, "_series_title": LEFTOVERS_TITLE,
                       "is_bundle": True, "bundle_items": leftovers})
    return result


MOVIE_HEAVY_RATIO = 2

MOVIES_PER_BLOCK = 2

MOVIE_ROTATION_KEY = "__MOVIES__"

_MOVIE_DURATION_FALLBACK_SECONDS = 6000


def _movie_runtime_budget(movie_pool, block_duration_seconds,
                           series_total_episodes, series_total_runtime_seconds=None):
    durations = [
        t.get("duration", 0) for t in movie_pool
        if isinstance(t, dict) and t.get("duration", 0) > 0
    ]
    movie_total_runtime = sum(durations)
    if series_total_runtime_seconds is not None and series_total_runtime_seconds > 0:
        series_runtime = float(series_total_runtime_seconds)
    else:
        series_runtime = float(max(series_total_episodes, 0) * _MOVIE_DURATION_FALLBACK_SECONDS)
    if series_runtime <= 0:
        return float(block_duration_seconds)
    return float(block_duration_seconds) * (
        movie_total_runtime / float(movie_total_runtime + series_runtime)
    )


def _fit_movies_to_runtime(candidates, target_seconds, max_movies):
    """Play whatever the rotation drew -- don't drop candidates for a
    tighter runtime fit.


    enough. But `candidates` always arrives already sized to `max_movies`
    from the shuffle bag (the caller draws exactly n_movie_pick candidates
    and passes n_movie_pick here too) -- so "closest fit" was never trimming
    overflow, only second-guessing a fair draw and skipping movies whose
    duration didn't combine well, over and over, no matter how the shuffle
    bag ordered them. Since a movie can't be skipped forever or replayed
    ahead of others in the rotation, and any resulting over/under vs. the
    half-hour target already gets absorbed by the commercial-break flex
    rules downstream, there's no need to be precise here: just play the
    draw, capped at max_movies as a safety bound.
    """
    if not candidates or max_movies <= 0:
        return []
    return list(candidates[:max_movies])


RANDOM_REPEAT_HOLD_BACK = 10  # default "don't repeat until N others have played" window


def _draw_from_shuffle_bag(state, n, count, seed_prefix, hold_back=RANDOM_REPEAT_HOLD_BACK):
    """Draw `count` indices from a persistent, per-cycle shuffle bag.

    Old approach (still visible in git history) called `random.Random(seed)`
    fresh on *every* draw with a seed that only changes once a day, then
    pulled one item at a time from a shrinking "remaining" list. Because the
    RNG always restarted from the same point and the remaining list barely
    changed call to call, consecutive draws landed on nearly the same index
    every time -- so a full lap looked like a few long runs of in-order
    episodes reshuffled in big chunks, and the next lap replayed almost the
    same sequence.

    This version builds one full shuffled permutation of the whole pool at
    the start of each cycle (a proper Fisher-Yates over all n indices, not a
    sequence of independent single picks) and hands them out one at a time.
    A `cycle` counter, persisted alongside the bag, is folded into the seed
    for each new bag, so lap 2 gets a different shuffle than lap 1, lap 3
    a different one again, and so on -- while remaining fully deterministic
    and reconstructable from persisted state (day_seed + name + cycle), same
    as everything else in this scheduler.

    `hold_back` additionally keeps the most recently played items out of
    circulation across the cycle boundary: whatever's still in the "last
    `hold_back` played" history gets pushed to the back of the new bag, so
    those items don't come back around until `hold_back` *other* items have
    played first. This is the same idea as the single-item "don't repeat the
    very last one" check, generalized to a whole window instead of one item.
    It self-scales down for small pools -- e.g. a 6-item pool never tries to
    hold back 10 things, which would leave nothing eligible to pick -- so it
    never deadlocks and never blocks a 1-item pool from "repeating" itself,
    since there's nothing else it could possibly play.

    `state` is the show_pos entry for this rotation (a dict with "bag" /
    "cycle" / "last" / "recent", a legacy {"played": [...], "last": ...}
    dict, a bare legacy int, or None). Returns (picked_indices, new_state).
    """
    import random as _random

    if isinstance(state, dict) and "bag" in state:
        bag = [i for i in state.get("bag", []) if 0 <= i < n]
        cycle = int(state.get("cycle", 0) or 0)
        last = state.get("last")
        recent = [i for i in state.get("recent", []) if 0 <= i < n]
    else:
        # Unsupported persisted state starts a fresh cycle while preserving
        # any available last-played value.
        bag = []
        cycle = 0
        if isinstance(state, dict):
            last = state.get("last")
        else:
            last = state if isinstance(state, int) else None
        recent = [last] if isinstance(last, int) else []

    # Never hold back more than roughly half the pool -- a small pool (say
    # 6 commercials) still needs most of itself eligible on every lap, it
    # just can't support a 10-deep memory the way a 200-item pool can.
    eff_hold_back = min(hold_back, max(0, (n - 1) // 2)) if n > 1 else 0

    picked = []
    for _ in range(count):
        if n == 1:
            idx = 0
        else:
            if not bag:
                cycle += 1
                bag = list(range(n))
                bag_rng = _random.Random(f"{seed_prefix}:{cycle}")
                bag_rng.shuffle(bag)
                if eff_hold_back > 0:
                    blocked = set(recent[-eff_hold_back:])
                    if blocked and len(blocked) < len(bag):
                        allowed = [i for i in bag if i not in blocked]
                        held = [i for i in bag if i in blocked]
                        bag = allowed + held  # held-back items wait at the tail
                elif len(bag) > 1 and bag[0] == last:
                    j = bag_rng.randrange(1, len(bag))
                    bag[0], bag[j] = bag[j], bag[0]
            idx = bag.pop(0)
        picked.append(idx)
        last = idx
        recent.append(idx)
        if hold_back > 0:
            recent = recent[-hold_back:]

    return picked, {"bag": bag, "cycle": cycle, "last": last, "recent": recent}


def _rotation_take_random(show_pos, name, count, eps, day_seed):
    if not eps:
        return []
    n = len(eps)
    state = show_pos.get(name)
    idxs, new_state = _draw_from_shuffle_bag(state, n, count, f"{day_seed}:{name}")
    show_pos[name] = new_state
    return [eps[i] for i in idxs]


def _movie_slot_count(movie_pool, block_duration_seconds, series_total_episodes,
                      scheduling_mode, series_total_runtime_seconds=None):
    if not movie_pool or block_duration_seconds is None or block_duration_seconds <= 0:
        return 0

    durations = [t.get("duration", 0) for t in movie_pool if isinstance(t, dict) and t.get("duration", 0) > 0]
    avg_dur = (sum(durations) / len(durations)) if durations else _MOVIE_DURATION_FALLBACK_SECONDS
    avg_dur = max(avg_dur, 60)  # guard against a stray near-zero duration skewing the divide

    budget = _movie_runtime_budget(
        movie_pool, block_duration_seconds, series_total_episodes,
        series_total_runtime_seconds,
    )

    n_pick = max(1, round(budget / avg_dur))
    n_pick = min(n_pick, len(movie_pool))

    if scheduling_mode == "two_slots" and 1 < n_pick < len(movie_pool) and n_pick % 2:
        n_pick += 1
    elif scheduling_mode == "two_slots" and n_pick % 2 and n_pick == len(movie_pool) and n_pick > 1:
        n_pick -= 1  # can't round up past the whole pool — drop to the nearest even count instead

    return n_pick


def _place_movies_in_block(video_tracks, picked_movies, day_seed, movie_heavy,
                            scheduling_mode="random_slots"):
    if not picked_movies:
        return video_tracks

    if scheduling_mode == "two_slots" and len(picked_movies) > 1:
        units = [picked_movies[i:i + 2] for i in range(0, len(picked_movies), 2)]
    else:
        units = [[m] for m in picked_movies]

    result = list(video_tracks)

    if not result:
        return [item for unit in units for item in unit]

    # Place each movie unit at an evenly distributed target in the available
    # series runtime, rather than treating a 20-minute episode and a
    # 60-minute episode as equal-sized schedule positions.
    series_runtime = sum(
        float(item.get("duration", 0) or 0)
        for item in result if isinstance(item, dict)
    )
    if series_runtime <= 0:
        series_runtime = float(len(result))
    used_idxs = []
    for i, u in enumerate(units):
        target = series_runtime * (i + 1) / float(len(units) + 1)
        elapsed = 0.0
        idx = len(result)
        for candidate, item in enumerate(result):
            elapsed += float(item.get("duration", 0) or 0) \
                if isinstance(item, dict) else 1.0
            if elapsed >= target:
                idx = candidate + 1
                break
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
                         block_duration_seconds=None,
                         channel_id=None):
    import random as _random

    show_pos = dict(show_pos or {})
    movie_pool, show_names, show_episodes = split_movies_and_series(raw_tracks, movie_threshold_seconds, channel_id=channel_id)
    show_pos = migrate_show_positions(show_pos, show_names, channel_id)[0]

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
        state_key = stable_series_id(show_name, channel_id)
        stored = show_pos.get(state_key, show_pos.get(show_name, 0))
        pos = stored % len(eps) if isinstance(stored, int) else 0
        out = [eps[(pos + n) % len(eps)] for n in range(count)]
        show_pos[state_key] = (pos + count) % len(eps)
        return out

    def _take_random(show_name, count, eps):
        n = len(eps)
        state_key = stable_series_id(show_name, channel_id)
        state = show_pos.get(state_key, show_pos.get(show_name))
        idxs, new_state = _draw_from_shuffle_bag(state, n, count, f"{day_seed}:{show_name}")
        show_pos[state_key] = new_state
        return [eps[i] for i in idxs]

    def _take(show_name, count):
        eps = show_episodes.get(show_name) or []
        if not eps:
            return []
        if _order_mode_for(show_name) == "random":
            return _take_random(show_name, count, eps)
        return _take_sequential(show_name, count, eps)

    def _fair_show_order(order, requested_counts):
        """Skip over-visited small shows without creating empty schedules.

        The requested count remains authoritative: one_slot still means one
        episode and two_slots still means two.  Fairness only decides whether
        a show participates in this build.  State also accepts the existing
        integer show_pos values.
        """
        fairness = show_pos.get("__FAIRNESS__")
        if not isinstance(fairness, dict):
            fairness = {}
        usage = fairness.get("used", {})
        if not isinstance(usage, dict):
            usage = {}
        run = int(fairness.get("run", 0) or 0) + 1
        fairness["run"] = run

        usable = [sn for sn in order if show_episodes.get(sn)]
        if len(usable) <= 1:
            show_pos["__FAIRNESS__"] = {"run": run, "used": usage}
            return usable

        ratios = []
        for sn in usable:
            n = max(1, len(show_episodes.get(sn) or []))
            ratios.append(float(usage.get(sn, 0) or 0) / n)
        ratios.sort()
        baseline = ratios[len(ratios) // 2] if ratios else 0.0
        pressure_limit = max(0.25, baseline * 1.5)

        eligible = []
        for sn in usable:
            n = max(1, len(show_episodes.get(sn) or []))
            projected = (float(usage.get(sn, 0) or 0) +
                         requested_counts.get(sn, 1)) / n
            if projected <= pressure_limit:
                eligible.append(sn)

        # Never allow fairness to remove the entire channel.  If all shows
        # have reached the same relative usage, start another balanced pass.
        if not eligible:
            eligible = usable

        for sn in eligible:
            usage[sn] = int(usage.get(sn, 0) or 0) + requested_counts.get(sn, 1)
        fairness["used"] = usage
        show_pos["__FAIRNESS__"] = fairness
        return [sn for sn in order if sn in set(eligible)]

    video_tracks = []

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
        requested_counts = {sn: 1 for sn in order}
        for sn in _fair_show_order(order, requested_counts):
            video_tracks.extend(_take(sn, requested_counts[sn]))

    elif scheduling_mode == "two_slots":
        requested_counts = {sn: 2 for sn in order}
        for sn in _fair_show_order(order, requested_counts):
            video_tracks.extend(_take(sn, requested_counts[sn]))

    else:
        _slot_counts = {sn: len(eps_list)
                        for sn, eps_list in show_episodes.items()}
        max_slots = max(_slot_counts.values(), default=1)
        requested_counts = {}
        for sn in order:
            slot_len = _slot_counts.get(sn, 0)
            requested_counts[sn] = max(1, min(slot_len, round((slot_len / max_slots) * 3))) \
                if slot_len else 0
        for sn in _fair_show_order(order, requested_counts):
            eps = show_episodes.get(sn) or []
            if not eps:
                continue
            run_slots = requested_counts[sn]
            video_tracks.extend(_take(sn, run_slots))


    if movie_pool:
        series_total = sum(len(v) for v in show_episodes.values())
        series_runtime = sum(
            (ep.get("duration", 0) if isinstance(ep, dict) else 0)
            for eps in show_episodes.values()
            for ep in eps
        )
        _bds = block_duration_seconds if block_duration_seconds else max(movies_per_block, 1) * 4 * 3600
        n_movie_pick = _movie_slot_count(
            movie_pool, _bds, series_total, scheduling_mode,
            series_total_runtime_seconds=series_runtime,
        )
        _ordered_pool = sorted(movie_pool, key=lambda t: t.get("path", ""))
        picked_movies = _rotation_take_random(show_pos, MOVIE_ROTATION_KEY, n_movie_pick, _ordered_pool, day_seed)
        movie_budget = _movie_runtime_budget(
            movie_pool, _bds, series_total, series_runtime
        )
        picked_movies = _fit_movies_to_runtime(
            picked_movies, movie_budget, n_movie_pick
        )
        movie_heavy = (series_total == 0) or (len(movie_pool) > MOVIE_HEAVY_RATIO * series_total)
        video_tracks = _place_movies_in_block(video_tracks, picked_movies, day_seed, movie_heavy,
                                               scheduling_mode=scheduling_mode)

    return video_tracks, show_pos, show_order_used


def _episode_label(path):
    import re as _re
    name = os.path.splitext(os.path.basename(str(path)))[0]
    m = _re.search(r's(\d+)[\s._-]*[ex][\s._-]*(\d+)', name, _re.IGNORECASE)
    if m:
        return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
    m = _re.search(r'season[\s._-]*(\d+).*?episode[\s._-]*(\d+)', name, _re.IGNORECASE)
    if m:
        return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
    m = _re.search(r'ep(?:isode)?\W*(\d+)', name, _re.IGNORECASE)
    if m:
        return f"Episode {int(m.group(1))}"
    return None


def _slots_per_block_for_show(show_name, show_episodes, scheduling_mode):
    eps = show_episodes.get(show_name) or []
    if not eps:
        return 0
    if scheduling_mode == "one_slot":
        return 1
    if scheduling_mode == "two_slots":
        return 2
    slot_counts = {sn: len(v) for sn, v in show_episodes.items()}
    max_slots = max(slot_counts.values(), default=1)
    slot_len = slot_counts.get(show_name, len(eps))
    return max(1, min(slot_len, round((slot_len / max_slots) * 3)))


def fast_forward_show_pos(raw_tracks, scheduling_mode, show_pos, day_seeds,
                           episode_order_mode="sequential", pair_threshold_seconds=900,
                           fixed_show_order=None, block_duration_seconds=None,
                           movies_per_block=MOVIES_PER_BLOCK, max_simulated_blocks=21900,
                           channel_id=None):
    show_pos = dict(show_pos or {})
    if not day_seeds:
        return show_pos, fixed_show_order, {}

    seeds = list(day_seeds)[-max_simulated_blocks:]
    n_blocks = len(seeds)

    movie_pool, show_names, show_episodes = split_movies_and_series(
        raw_tracks, channel_id=channel_id
    )
    if pair_threshold_seconds > 0:
        # Keep catch-up accounting in the same episode-unit space as
        # build_show_rotation().  Without this, a show with two short pairs
        # reports four raw episodes here while normal scheduling reports two
        # paired slots, shifting its saved position on a block catch-up.
        show_episodes = {
            sn: pair_short_episodes(eps, threshold_seconds=pair_threshold_seconds)
            for sn, eps in show_episodes.items()
        }

    def _order_mode_for(show_name):
        if isinstance(episode_order_mode, dict):
            return episode_order_mode.get(show_name, episode_order_mode.get("__default__", "sequential"))
        return episode_order_mode

    def _label_before(show_name):
        eps = show_episodes.get(show_name) or []
        if not eps:
            return None
        state = show_pos.get(stable_series_id(show_name, channel_id),
                             show_pos.get(show_name))
        if _order_mode_for(show_name) == "random":
            idx = state.get("last") if isinstance(state, dict) else (state if isinstance(state, int) else None)
        else:
            nxt = state if isinstance(state, int) else 0
            idx = (nxt - 1) % len(eps)
        if idx is None or not (0 <= idx < len(eps)):
            return None
        return _episode_label(eps[idx].get("path", ""))

    def _label_after(show_name):
        eps = show_episodes.get(show_name) or []
        if not eps:
            return None
        state = show_pos.get(stable_series_id(show_name, channel_id),
                             show_pos.get(show_name))
        if _order_mode_for(show_name) == "random":
            idx = state.get("last") if isinstance(state, dict) else (state if isinstance(state, int) else None)
        else:
            idx = state if isinstance(state, int) else 0
        if idx is None or not (0 <= idx < len(eps)):
            return None
        return _episode_label(eps[idx % len(eps)].get("path", ""))

    before_labels = {sn: _label_before(sn) for sn in show_names}

    order = fixed_show_order
    for seed in seeds:
        _tracks, show_pos, used_order = build_show_rotation(
            raw_tracks, scheduling_mode, show_pos, seed,
            pair_threshold_seconds=pair_threshold_seconds,
            episode_order_mode=episode_order_mode,
            fixed_show_order=order,
            block_duration_seconds=block_duration_seconds,
            movies_per_block=movies_per_block,
            channel_id=channel_id,
        )
        order = order or used_order

    report = {}
    for sn in show_names:
        per_block = _slots_per_block_for_show(sn, show_episodes, scheduling_mode)
        report[sn] = {
            "slots_skipped": per_block * n_blocks,
            "label_before": before_labels.get(sn),
            "label_after": _label_after(sn),
        }
    return show_pos, order, report


def note_random_episode_played(show_pos, show_name, episode_count, played_index,
                               channel_id=None):
    state_key = stable_series_id(show_name, channel_id)
    state = show_pos.get(state_key, show_pos.get(show_name))
    if isinstance(state, dict) and "bag" in state:
        # Pull the substituted episode out of the current cycle's bag so it
        # doesn't turn around and get drawn again before the bag empties.
        bag = [i for i in state.get("bag", []) if 0 <= i < episode_count and i != played_index]
        cycle = int(state.get("cycle", 0) or 0)
        recent = [i for i in state.get("recent", []) if 0 <= i < episode_count]
    else:
        bag = []
        cycle = 0
        recent = []
    recent = (recent + [played_index])[-RANDOM_REPEAT_HOLD_BACK:]
    show_pos[state_key] = {"bag": bag, "cycle": cycle, "last": played_index, "recent": recent}


# ---------------------------------------------------------------------------
# Commercial duration bundles
# ---------------------------------------------------------------------------
# Real ad libraries cluster hard around a handful of standard broadcast
# lengths (15s/30s/60s spots) with a thin tail of longer "infomercial"-style
# pieces. Treating the whole library as one shuffled pool -- first-fit
# whatever comes up next into whatever gap needs filling -- lets that
# clustering warp playback: a 15s spot fits almost any leftover budget and
# gets pulled disproportionately often, while a 3-minute piece only fits the
# rare big gap and can go long stretches without airing, or clump up the few
# times a big gap does show up.
#
# Splitting the pool into duration bundles fixes both ends: each bundle gets
# its own independent "everyone plays once before anyone repeats" rotation,
# and a break gets filled with the closest-fitting bundle for its remaining
# budget instead of a single mixed pool's random walk.
#
# Ceilings sit close together at the common lengths (15/30/45/60s), where a
# real library is usually dense enough that a tight bundle still has plenty
# of variety, then widen out for the rarer long-form lengths (90s/2min/3min/
# 5min) where a tight ceiling would leave a bundle with only one or two ads
# in it -- too thin, and it just repeats itself constantly. Go too coarse
# instead (say, one giant "under 5 minutes" bucket) and gap-filling loses
# the whole point of bundling: a 20s gap could get handed a bundle full of
# 4-minute pieces that don't actually fit well, right back to the original
# problem. This spacing is a starting point, not a hard rule -- if a
# particular library's mix of lengths wants different boundaries, adjust
# COMMERCIAL_BUNDLE_CEILINGS.
#
# COMMERCIAL_MAX_SECONDS mirrors the "too long to be a commercial" cutoff
# already enforced wherever commercials get probed/added to a channel; it's
# also the ceiling of the last (catch-all) bundle here.
COMMERCIAL_MAX_SECONDS = 5 * 60
COMMERCIAL_BUNDLE_CEILINGS = (15, 30, 45, 60, 90, 120, 180, COMMERCIAL_MAX_SECONDS)


def commercial_bundle_ceiling(duration):
    """Round a commercial's duration UP to the ceiling of the bundle it
    belongs in. Rounding up (never down) guarantees every ad placed into a
    bundle actually fits any break budget that bundle gets chosen to fill."""
    for ceiling in COMMERCIAL_BUNDLE_CEILINGS:
        if duration <= ceiling:
            return ceiling
    return COMMERCIAL_BUNDLE_CEILINGS[-1]


def build_block_timeline(video_tracks, commercial_tracks, commercial_placement, day_seed,
                          interrupt_floor_seconds=900,
                          max_interrupt_break_seconds=360,
                          max_end_break_seconds=600,
                          media_probe_cache=None,
                          commercial_state=None):
    import random as _random

    HALF_HOUR         = 1800
    MAX_CATCHUP       = 180   # max 3 min for a single catch-up fill break
    MIN_BREAK_GAP     = 300   # at least 5 min of show between any two breaks
    MIN_BREAK_SECONDS = 120   # no aired commercial break may be shorter than this

    def _dur_is_estimated(show):
        try:
            if isinstance(show, dict) and show.get("_duration_estimated"):
                return True
            if media_probe_cache is None:
                return False
            p = show.get("path") if isinstance(show, dict) else None
            if not p:
                return False
            cached = media_probe_cache.get(os.path.normpath(str(p)))
            cdur = cached.get("duration") if isinstance(cached, dict) else None
            if cdur and cdur > 0:
                return False   # real length known -> not an estimate
            stored = show.get("duration") if isinstance(show, dict) else None
            return not (stored and stored > 0 and stored != HALF_HOUR)
        except Exception:
            return False

    # Split the flat commercial list into duration bundles (see
    # commercial_bundle_ceiling / COMMERCIAL_BUNDLE_CEILINGS above), each
    # with its own persisted cycle counter and its own "recently played"
    # hold-back -- so one bundle's rotation never bleeds into another's.
    # An older, pre-bundle commercial_state has no "bundles" key, so every
    # bundle just starts on a fresh first lap; that costs nothing beyond one
    # extra lap of "no repeats yet" and self-corrects on the very next save.
    _bundles = {}
    for _com in (commercial_tracks or []):
        _cdur = _com.get("duration", 0) if isinstance(_com, dict) else 0
        if not _cdur or _cdur <= 0:
            continue
        _bundles.setdefault(commercial_bundle_ceiling(_cdur), []).append(_com)

    _saved_bundles = (commercial_state or {}).get("bundles")
    if not isinstance(_saved_bundles, dict):
        _saved_bundles = {}
    _bundle_cycle  = {}
    _bundle_recent = {}
    for _ceiling in _bundles:
        _saved = _saved_bundles.get(str(_ceiling))
        if not isinstance(_saved, dict):
            _saved = {}
        _bundle_cycle[_ceiling]  = int(_saved.get("cycle", 0) or 0)
        _bundle_recent[_ceiling] = [p for p in _saved.get("recent", []) if p]

    def _shuffled_ad_lap(pool_items, ceiling):
        nonlocal _bundle_cycle
        _bundle_cycle[ceiling] = _bundle_cycle.get(ceiling, 0) + 1
        n = len(pool_items)
        order = list(range(n))
        lap_rng = _random.Random(f"{day_seed}:__commercials__:{ceiling}:{_bundle_cycle[ceiling]}")
        lap_rng.shuffle(order)
        recent = _bundle_recent.get(ceiling, [])
        # Hold back as many recently-played items as the pool can spare --
        # up to n-1, leaving exactly one guaranteed-different pick rather
        # than only ever protecting half the pool. That's the strongest
        # no-repeat guarantee this bundle's size can support: a commercial
        # can't come back around until every other item in its duration
        # class has had a turn, for however many breaks that buys. Still
        # capped by RANDOM_REPEAT_HOLD_BACK overall so a big pool doesn't
        # hold back more than the "recent" history we actually track.
        hold_back = min(RANDOM_REPEAT_HOLD_BACK, n - 1) if n > 1 else 0
        if hold_back > 0:
            blocked_paths = set(recent[-hold_back:])
            blocked = {i for i in order if pool_items[i]["path"] in blocked_paths}
            if blocked and len(blocked) < len(order):
                allowed = [i for i in order if i not in blocked]
                held = [i for i in order if i in blocked]
                order = allowed + held
        return [pool_items[i] for i in order]

    _bundle_pools = {}
    _bundle_idx   = {}
    for _ceiling, _items in _bundles.items():
        _bundle_pools[_ceiling] = _shuffled_ad_lap(_items, _ceiling)
        _bundle_idx[_ceiling]   = 0

    # Which bundle gets tried *first* for a given slot rotates through every
    # bundle in turn, rather than always picking whichever bundle best-fits
    # the remaining budget. Always-best-fit would hand almost every slot to
    # whichever bundle's ceiling happens to divide evenly into most breaks'
    # budgets, starving the other duration classes. Round-robin instead:
    # try the bundle whose turn it is, and only skip to the next one in
    # rotation if it doesn't fit the remaining budget (or is fully used up
    # already within this break) -- so every duration class gets a turn
    # roughly as often as any other, while a slot still only ever gets
    # filled by something that actually fits it.
    _bundle_order = sorted(_bundle_pools.keys())
    _saved_turn_ceiling = (commercial_state or {}).get("turn_ceiling")
    _bundle_turn = _bundle_order.index(_saved_turn_ceiling) if _saved_turn_ceiling in _bundle_order else 0

    # Kept around only for the handful of "is there anything to work with at
    # all" truthiness checks below -- actual selection always goes through
    # the bundles above now.
    commercial_pool = list(commercial_tracks or [])

    timeline = []
    total_block_content = 0

    # Shared mutable state (modified by the closures below via nonlocal)
    _deferred_fill  = 0               # fill seconds owed but not yet placed
    _last_break_end = -MIN_BREAK_GAP  # position where the last break ended
    _last_played_path = (commercial_state or {}).get("last")

    # Hard "at most 2 breaks per half-hour" cap for the short-show chain
    # case: several different short shows (each under interrupt_floor_
    # seconds) landing in the same half hour would otherwise each get their
    # own end break, which respects each show's own budget but not the
    # half-hour as a whole. Once 2 breaks have aired in the current half-hour
    # bucket, a short show's end break is skipped entirely -- not
    # capped-and-deferred like elsewhere, fully skipped -- and its owed
    # commercial time carries forward to combine with the NEXT show's own
    # end break instead (not the generic top-of-loop
    # _maybe_flush_deferred_fill/_snap_to_mark catch-up, which fires
    # *before* the next show's content and would glue a break onto the
    # front of it -- this carries into that show's own end-of-show break
    # so the merged break plays after it).
    MAX_BREAKS_PER_HALF_HOUR = 2
    _half_hour_bucket        = 0      # which HALF_HOUR-sized window we're in
    _breaks_this_half_hour   = 0

    def _register_half_hour(t):
        nonlocal _half_hour_bucket, _breaks_this_half_hour
        bucket = int(t) // HALF_HOUR
        if bucket != _half_hour_bucket:
            _half_hour_bucket      = bucket
            _breaks_this_half_hour = 0

    def _half_hour_capped(t):
        """True once MAX_BREAKS_PER_HALF_HOUR breaks have already aired in
        t's half-hour window -- checked by every break-triggering path
        (short-show end breaks, _snap_to_mark, _maybe_flush_deferred_fill)
        so none of them can sneak a 3rd break in independently. Whatever
        gets blocked stays in _deferred_fill and pays out once the window
        rolls over (or, for the short-show branch specifically, gets folded
        straight into a later show's own end break)."""
        _register_half_hour(t)
        return _breaks_this_half_hour >= MAX_BREAKS_PER_HALF_HOUR

    def _note_ad_played(ceiling, path):
        nonlocal _last_played_path
        _last_played_path = path
        recent = _bundle_recent.setdefault(ceiling, [])
        recent.append(path)
        if len(recent) > RANDOM_REPEAT_HOLD_BACK:
            _bundle_recent[ceiling] = recent[-RANDOM_REPEAT_HOLD_BACK:]

    def _next_from_bundle(ceiling, exclude_paths):
        """Pull the next not-already-used-in-this-break item out of
        `ceiling`'s own rotation, starting a fresh reshuffled lap once the
        current one runs out. Returns None only if every item in the bundle
        is already spoken for by this break (exclude_paths)."""
        pool = _bundle_pools.get(ceiling)
        if not pool:
            return None
        n = len(pool)
        seen = 0
        while seen < n:
            idx = _bundle_idx[ceiling]
            if idx > 0 and idx % n == 0:
                pool = _bundle_pools[ceiling] = _shuffled_ad_lap(pool, ceiling)
            com = pool[idx % n]
            _bundle_idx[ceiling] = idx + 1
            seen += 1
            if com["duration"] <= 0 or com["path"] in exclude_paths:
                continue
            return com
        return None

    def _pick_one(budget_seconds, exclude_paths):
        """Pick a single commercial that fits `budget_seconds`, trying
        bundles in round-robin order starting from whichever bundle's turn
        it is (see _bundle_order/_bundle_turn above). Returns (item,
        ceiling) or (None, None) if nothing in any bundle fits/qualifies."""
        nonlocal _bundle_turn
        n = len(_bundle_order)
        for offset in range(n):
            idx = (_bundle_turn + offset) % n
            ceiling = _bundle_order[idx]
            if ceiling > budget_seconds:
                continue
            com = _next_from_bundle(ceiling, exclude_paths)
            if com is None:
                continue
            _bundle_turn = (idx + 1) % n
            return com, ceiling
        return None, None

    def _show_entry(show, duration, start_time, seek_offset=0, interior_cut=False):
        e = {"path": show["path"], "duration": duration,
             "start_time": start_time, "is_commercial": False,
             "seek_offset": seek_offset}
        if _dur_is_estimated(show):
            e["duration_estimated"] = True
        if interior_cut:
            e["interior_cut"] = True
        if show.get("is_pair"):
            e["is_pair"]           = True
            e["pair_ep1_path"]     = show.get("pair_ep1_path", show["path"])
            e["pair_ep1_duration"] = show.get("pair_ep1_duration", 0)
            e["pair_ep2_path"]     = show.get("pair_ep2_path", "")
            e["pair_ep2_duration"] = show.get("pair_ep2_duration", 0)
        return e

    def _pack_break(budget_seconds, pool=None, start_idx=None):
        # `pool`/`start_idx` are accepted-but-unused so every call site
        # below keeps working unchanged (the returned "index" is likewise a
        # meaningless placeholder, only ever fed back into another call
        # here). Selection runs against the duration bundles built above,
        # one slot at a time via _pick_one's round-robin -- so a break gets
        # filled with ads that actually fit, spread fairly across every
        # duration class, instead of a single flat pool's random first-fit
        # walk (which is what let short ads dominate every gap they always
        # happened to fit).
        picked = []
        used = 0
        if budget_seconds <= 0 or not _bundle_pools:
            return picked, 0, 0
        break_paths = set()
        while True:
            remaining = budget_seconds - used
            if remaining <= 0:
                break
            com, ceiling = _pick_one(remaining, break_paths)
            if com is None:
                break
            picked.append(com)
            used += com["duration"]
            break_paths.add(com["path"])
            _note_ad_played(ceiling, com["path"])
        return picked, used, 0

    def _append_break(picked, t_block_content):
        """Append picked commercial entries to timeline and update _last_break_end.

        Single chokepoint for every actual break insertion (_pack_and_round,
        _pack_even_interior_breaks, the deferred-fill flush), so it's also
        the one place that counts against MAX_BREAKS_PER_HALF_HOUR -- any
        break placed here, however it got triggered, uses up part of the
        half-hour's quota.
        """
        nonlocal _last_break_end, _breaks_this_half_hour
        t = t_block_content
        for com in picked:
            timeline.append({"path": com["path"], "duration": com["duration"],
                             "start_time": t, "is_commercial": True,
                             "seek_offset": 0})
            t += com["duration"]
        if picked:
            _last_break_end = t
            _register_half_hour(t_block_content)
            _breaks_this_half_hour += 1
        return t

    def _pack_and_round(budget_seconds, pool, start_idx, t_block_content, max_seconds=None):
        # `pool`/`start_idx` accepted-but-unused -- see _pack_break.
        nonlocal _deferred_fill, _last_break_end
        if budget_seconds <= 0 or not _bundle_pools:
            return t_block_content, 0
        # Apply per-break cap and defer any overage for later catch-up
        capped = min(budget_seconds, max_seconds) if max_seconds is not None else budget_seconds
        if capped < budget_seconds:
            _deferred_fill += budget_seconds - capped
        picked, used, _ = _pack_break(capped)
        t = _append_break(picked, t_block_content)
        leftover = int(capped) - used
        if leftover > 0:
            # Nothing left in any bundle can close this last stretch
            # without either repeating a commercial already used earlier
            # in this same break, or reaching for one that overshoots it --
            # and a commercial must never repeat within one break. Rather
            # than force either of those, the break just ends here: the
            # leftover carries forward as owed time through the same
            # _deferred_fill catch-up system every other capped/skipped
            # break already uses (see _maybe_flush_deferred_fill), and
            # gets paid back once a later break has room, instead of an
            # unnatural trim, a forced repeat, or dead air. This means the
            # next show can start a little early relative to the :00/:30
            # mark on breaks where the pool runs thin -- by design, not a
            # bug -- and a future break simply runs a bit longer (or, if
            # a later break happens to overshoot its own cap for the same
            # reason, a bit shorter) to pay the difference back and keep
            # the schedule drifting back toward the marks over time.
            _deferred_fill += leftover
        return t, 0

    def _pack_even_interior_breaks(show, show_dur, gap, num_breaks, t_block_content, cidx, next_mark):
        nonlocal _deferred_fill
        per_break  = gap / float(num_breaks)
        capped_per = min(per_break, max_interrupt_break_seconds) if max_interrupt_break_seconds else per_break
        if per_break > capped_per:
            _deferred_fill += (per_break - capped_per) * (num_breaks - 1)
        # Symmetric floor: dividing gap across many breaks can land under
        # MIN_BREAK_SECONDS just as easily as it can land over the max.
        capped_per = max(capped_per, MIN_BREAK_SECONDS)

        base_seg    = int(show_dur) // num_breaks
        seek_offset = 0
        for i in range(num_breaks):
            is_last = (i == num_breaks - 1)
            seg = (show_dur - seek_offset) if is_last else base_seg
            timeline.append(_show_entry(show, seg, t_block_content, seek_offset,
                                        interior_cut=not is_last))
            t_block_content += seg
            seek_offset     += seg
            if is_last:
                end_budget = next_mark - t_block_content
                if 0 < end_budget < MIN_BREAK_SECONDS:
                    # Too small a remainder to air on its own -- defer it
                    # instead of forcing a sub-floor break just to land
                    # exactly on this mark; the next show's snap-to-mark
                    # (floor-aware) realigns properly, and the deferred
                    # amount gets paid back by _maybe_flush_deferred_fill.
                    _deferred_fill += end_budget
                else:
                    t_block_content, cidx = _pack_and_round(
                        end_budget, commercial_pool, cidx, t_block_content,
                        max_seconds=max_interrupt_break_seconds)
            else:
                picked, used, cidx = _pack_break(capped_per, commercial_pool, cidx)
                t_block_content = _append_break(picked, t_block_content)
        return t_block_content, cidx

    def _ceil_div(a, b):
        """Integer ceiling division, safe on a non-positive divisor."""
        if not b or b <= 0:
            return 1
        return int(-(-a // b))

    def _maybe_flush_deferred_fill(t, cidx):
        # Pays back ad time that got deferred elsewhere (capped-down breaks,
        # or a break skipped for being under MIN_BREAK_SECONDS) by inserting
        # a real bonus break once enough owed time has piled up and enough
        # show has played since the last break.
        nonlocal _deferred_fill
        if _deferred_fill <= 0 or not commercial_pool:
            return t, cidx
        if t - _last_break_end < MIN_BREAK_GAP:
            return t, cidx
        if _half_hour_capped(t):
            return t, cidx   # already at the 2-per-half-hour cap -- stays owed
        insert_amt = min(MAX_CATCHUP, _deferred_fill)
        if insert_amt < MIN_BREAK_SECONDS:
            return t, cidx   # not enough owed yet to make a floor-sized break
        picked, used, new_cidx = _pack_break(insert_amt, commercial_pool, cidx)
        if picked:
            t = _append_break(picked, t)
            _deferred_fill -= used
            cidx = new_cidx
        return t, cidx

    def _snap_to_mark(t, cidx, cap_seconds):
        nonlocal _deferred_fill
        if t % HALF_HOUR == 0:
            return t, cidx          # already on a :00/:30 mark — no-op
        snap_budget = HALF_HOUR - (t % HALF_HOUR)
        if snap_budget < MIN_BREAK_SECONDS:
            # Landing on the very next mark would mean a sub-floor break --
            # push one more half hour out so there's room to do it properly.
            snap_budget += HALF_HOUR
        # This only fires a break here if there's been enough real show
        # time since the last break (same MIN_BREAK_GAP gate
        # _maybe_flush_deferred_fill uses) -- otherwise it just adds the
        # whole snap budget to _deferred_fill and leaves the schedule
        # off-mark for now. It gets paid back (respecting the floor and
        # cap) once _maybe_flush_deferred_fill sees enough real gap, same
        # as any other deferred overage.
        if t - _last_break_end < MIN_BREAK_GAP:
            _deferred_fill += snap_budget
            return t, cidx
        if _half_hour_capped(t):
            # Same idea as the MIN_BREAK_GAP gate above, but for the
            # 2-per-half-hour cap: don't sneak a break in here just because
            # enough show-gap has passed -- stays owed until the window
            # rolls over.
            _deferred_fill += snap_budget
            return t, cidx
        return _pack_and_round(snap_budget, commercial_pool, cidx, t,
                               max_seconds=cap_seconds)

    def _resolved_show_duration(show):
        # Resolve probe results before using stored durations so sentinel
        # values cannot enter schedule calculations.
        if isinstance(show, dict) and show.get("is_pair"):
            # A pair's path is the first episode for playback identity, but
            # its schedule duration is the sum of both episodes.  Resolving
            # only the first path through media_probe_cache would collapse a
            # 14:32 pair back to (for example) 7:32 and make the guide draw
            # the pair and a standalone short at the same width.
            p1 = show.get("pair_ep1_path", show.get("path", ""))
            p2 = show.get("pair_ep2_path", "")
            d1 = resolve_track_duration(
                media_probe_cache, p1, show.get("pair_ep1_duration", 0), default=0
            )
            d2 = resolve_track_duration(
                media_probe_cache, p2, show.get("pair_ep2_duration", 0), default=0
            )
            if d1 > 0 and d2 > 0:
                return int(d1 + d2)
        p = show.get("path") if isinstance(show, dict) else None
        stored = show.get("duration") if isinstance(show, dict) else None
        return resolve_track_duration(media_probe_cache, p, stored, default=HALF_HOUR)

    def _mid_split_point(show, show_dur):
        # Where to cut a block in two for a single interior commercial
        # break. For a paired two-episode block, the arithmetic midpoint of
        # the *combined* runtime (show_dur // 2) has nothing to do with
        # where episode 1 actually ends -- a 650s+618s pair's true seam is
        # at 650s, but show_dur // 2 lands at 634s, 16s before it, so the
        # break fires while episode 1 is still playing. Snap to the real
        # seam (episode 1's resolved duration) whenever this is a pair and
        # that duration is known and falls inside the block; otherwise fall
        # back to the plain midpoint (non-paired shows, or a pair whose
        # ep1 duration hasn't been probed yet).
        if isinstance(show, dict) and show.get("is_pair"):
            p1 = show.get("pair_ep1_path", show.get("path", ""))
            d1 = resolve_track_duration(
                media_probe_cache, p1, show.get("pair_ep1_duration", 0), default=0
            )
            if 0 < d1 < show_dur:
                raw_mid = show_dur // 2
                if abs(int(d1) - raw_mid) >= 1:
                    log.info(
                        "[BLOCK TIMELINE] paired-show mid-break snapped to ep1 seam: "
                        "%s -- seam=%ds vs raw_midpoint=%ds (combined=%ds)",
                        os.path.basename(p1) if p1 else "?", int(d1), raw_mid, show_dur)
                return int(d1)
        return show_dur // 2

    if commercial_tracks and commercial_placement == "interrupt_half_hour":
        com_idx = 0
        for show in video_tracks:
            show_dur = _resolved_show_duration(show)
            _est = _dur_is_estimated(show)

            total_block_content, com_idx = _snap_to_mark(total_block_content, com_idx, max_interrupt_break_seconds)
            total_block_content, com_idx = _maybe_flush_deferred_fill(total_block_content, com_idx)
            _register_half_hour(total_block_content)

            natural_end = total_block_content + show_dur
            next_mark = ((natural_end // HALF_HOUR) + (1 if natural_end % HALF_HOUR != 0 else 0)) * HALF_HOUR
            gap = next_mark - natural_end
            if _est and gap <= 0:
                # An unprobed entry resolves to HALF_HOUR only so it can
                # occupy a stable broadcast slot while VLC catches up.  Do
                # not turn that placeholder into a long-form program with a
                # full fallback commercial break; the next probe will rebuild
                # this lineup using the real runtime.
                if show_dur == HALF_HOUR:
                    timeline.append(_show_entry(show, show_dur,
                                                 total_block_content, 0))
                    total_block_content += show_dur
                    continue
                next_mark += HALF_HOUR
                gap = next_mark - natural_end

            # A gap that's positive but too small to hold a real break would
            # otherwise force a sub-floor break just to land exactly on the
            # next :00/:30 mark. Push the target out one more half hour so
            # there's room to do it properly -- same idea as the estimated-
            # duration case just above.
            if 0 < gap < MIN_BREAK_SECONDS:
                next_mark += HALF_HOUR
                gap = next_mark - natural_end

            if gap <= 0:
                # An exact half-hour landing still receives its normal
                # commercial breaks, sized from max_interrupt_break_seconds.
                _fallback_break_len = max_interrupt_break_seconds if max_interrupt_break_seconds else MIN_BREAK_SECONDS

                if show_dur < interrupt_floor_seconds:
                    # Short show: same one-end-break shape as its normal
                    # branch below, sized off the cap directly and still
                    # subject to the same 2-per-half-hour cap check.
                    timeline.append(_show_entry(show, show_dur, total_block_content, 0))
                    total_block_content += show_dur
                    if _half_hour_capped(total_block_content):
                        _deferred_fill += _fallback_break_len
                    else:
                        total_block_content, com_idx = _pack_and_round(
                            _fallback_break_len, commercial_pool, com_idx, total_block_content,
                            max_seconds=max_interrupt_break_seconds)

                elif show_dur < HALF_HOUR:
                    # Same mid-break + end-break shape as the normal branch
                    # below, each sized off the cap directly.
                    mid_point = _mid_split_point(show, show_dur)
                    timeline.append(_show_entry(show, mid_point, total_block_content, 0,
                                                interior_cut=True))
                    total_block_content += mid_point
                    picked, used, com_idx = _pack_break(_fallback_break_len, commercial_pool, com_idx)
                    total_block_content = _append_break(picked, total_block_content)
                    remaining_half = show_dur - mid_point
                    timeline.append(_show_entry(show, remaining_half, total_block_content, mid_point))
                    total_block_content += remaining_half
                    total_block_content, com_idx = _pack_and_round(
                        _fallback_break_len, commercial_pool, com_idx, total_block_content,
                        max_seconds=max_interrupt_break_seconds)

                else:
                    # Movie: one break every half hour straight through the
                    # runtime -- e.g. a 90-minute movie landing exactly on
                    # the mark gets 3 breaks at max_interrupt_break_seconds
                    # each (30/30/30), 18 minutes of commercial total for
                    # the default 6-minute cap, instead of the zero it used
                    # to get. Same "no gap to close" shape as elsewhere in
                    # this branch, and (like the rest of the long-form
                    # path) not subject to the 2-per-half-hour cap that
                    # only governs chained short shows.
                    num_interior_breaks = max(1, int(show_dur // HALF_HOUR))
                    seek_offset    = 0
                    remaining_show = show_dur
                    for _i in range(num_interior_breaks):
                        seg = HALF_HOUR
                        _is_true_tail = (seek_offset + seg) >= show_dur
                        timeline.append(_show_entry(show, seg, total_block_content, seek_offset,
                                                    interior_cut=not _is_true_tail))
                        total_block_content += seg
                        seek_offset         += seg
                        remaining_show      -= seg
                        picked, used, com_idx = _pack_break(_fallback_break_len, commercial_pool, com_idx)
                        total_block_content   = _append_break(picked, total_block_content)

                    if remaining_show > 0:
                        timeline.append(_show_entry(show, remaining_show, total_block_content, seek_offset))
                        total_block_content += remaining_show
                        total_block_content, com_idx = _pack_and_round(
                            _fallback_break_len, commercial_pool, com_idx, total_block_content,
                            max_seconds=max_interrupt_break_seconds)
                continue

            if show_dur < interrupt_floor_seconds:
                # Short show: no mid-show split; one end break only. Always
                # capped at max_interrupt_break_seconds like everywhere else,
                # including when we just pushed the mark out to satisfy the
                # floor -- an uncapped break here can wildly overshoot the
                # max against a run of sub-floor shows. Capping and deferring the
                # overage does NOT create a "glued-on" second break: the
                # deferred amount only gets paid back once
                # _maybe_flush_deferred_fill sees real show-gap (>=
                # MIN_BREAK_GAP) since the last break, which a run of
                # back-to-back short shows won't have yet -- it naturally
                # waits until there's room, then pays back up to
                # MAX_CATCHUP at a time.
                timeline.append(_show_entry(show, show_dur, total_block_content, 0))
                total_block_content += show_dur
                end_budget = next_mark - total_block_content
                if _deferred_fill > 0:
                    end_budget += _deferred_fill
                    _deferred_fill = 0
                if end_budget > 0 and _half_hour_capped(total_block_content):
                    # Already at the 2-per-half-hour cap -- skip this show's
                    # break entirely (not capped-and-aired, not flushed
                    # early -- fully skipped) and carry the owed time
                    # forward as deferred fill, to combine with whichever
                    # show's end break next has room in a fresh half hour.
                    _deferred_fill += end_budget
                elif end_budget > 0:
                    total_block_content, com_idx = _pack_and_round(
                        end_budget, commercial_pool, com_idx, total_block_content,
                        max_seconds=max_interrupt_break_seconds)

            elif show_dur < HALF_HOUR:
                if gap < 2 * MIN_BREAK_SECONDS:
                    # Not enough gap to split into a mid-break AND an end
                    # break that are each floor-sized -- one single break
                    # at the end instead (same shape as the short-show path).
                    timeline.append(_show_entry(show, show_dur, total_block_content, 0))
                    total_block_content += show_dur
                    end_budget = next_mark - total_block_content
                    total_block_content, com_idx = _pack_and_round(
                        end_budget, commercial_pool, com_idx, total_block_content,
                        max_seconds=max_interrupt_break_seconds)
                else:
                    # Hard rule: at most 2 breaks per half-hour slot, each
                    # capped at max_interrupt_break_seconds (so up to 12 min
                    # combined at the default 6-min cap). If the natural gap
                    # needs more than that, we do NOT spill into a 3rd break
                    # to close it precisely -- the excess is deferred and
                    # paid back later (or not) via _maybe_flush_deferred_fill,
                    # same best-effort philosophy as the short-show path.
                    mid_budget = gap / 2.0
                    if max_interrupt_break_seconds:
                        mid_budget = min(mid_budget, max_interrupt_break_seconds)
                    mid_point  = _mid_split_point(show, show_dur)

                    timeline.append(_show_entry(show, mid_point, total_block_content, 0,
                                                interior_cut=True))
                    total_block_content += mid_point

                    picked, used, com_idx = _pack_break(mid_budget, commercial_pool, com_idx)
                    total_block_content = _append_break(picked, total_block_content)

                    remaining_half = show_dur - mid_point
                    timeline.append(_show_entry(show, remaining_half, total_block_content, mid_point))
                    total_block_content += remaining_half

                    # end_budget is computed fresh from the actual position
                    # after the (possibly capped) mid break, so whatever the
                    # mid break didn't cover naturally shows up here -- and
                    # this call's own max_seconds cap defers anything still
                    # left over beyond the 2nd break's 6-min allowance,
                    # rather than inventing a 3rd break.
                    end_budget = next_mark - total_block_content
                    total_block_content, com_idx = _pack_and_round(
                        end_budget, commercial_pool, com_idx, total_block_content,
                        max_seconds=max_interrupt_break_seconds)

            else:
                num_interior_breaks = int(show_dur // HALF_HOUR)
                baseline_breaks      = num_interior_breaks + 1
                baseline_per_break   = gap / float(baseline_breaks)

                if max_interrupt_break_seconds and baseline_per_break > max_interrupt_break_seconds:
                    num_breaks = max(baseline_breaks, _ceil_div(gap, max_interrupt_break_seconds))
                    total_block_content, com_idx = _pack_even_interior_breaks(
                        show, show_dur, gap, num_breaks, total_block_content, com_idx, next_mark)
                else:
                    capped_per = min(baseline_per_break, max_interrupt_break_seconds) if max_interrupt_break_seconds else baseline_per_break
                    # Same symmetric floor as _pack_even_interior_breaks --
                    # dividing the tail gap across several interior breaks
                    # can land any of them under MIN_BREAK_SECONDS.
                    capped_per = max(capped_per, MIN_BREAK_SECONDS)

                    seek_offset    = 0
                    remaining_show = show_dur
                    for i in range(num_interior_breaks):
                        seg = HALF_HOUR
                        _is_true_tail = (seek_offset + seg) >= show_dur
                        timeline.append(_show_entry(show, seg, total_block_content, seek_offset,
                                                    interior_cut=not _is_true_tail))
                        total_block_content += seg
                        seek_offset         += seg
                        remaining_show      -= seg

                        picked, used, com_idx = _pack_break(capped_per, commercial_pool, com_idx)
                        total_block_content   = _append_break(picked, total_block_content)

                    if remaining_show > 0:
                        timeline.append(_show_entry(show, remaining_show, total_block_content, seek_offset))
                        total_block_content += remaining_show

                    end_budget = next_mark - total_block_content
                    if 0 < end_budget < MIN_BREAK_SECONDS:
                        # Defer rather than force a sub-floor break to close
                        # out this mark -- see the matching comment in
                        # _pack_even_interior_breaks.
                        _deferred_fill += end_budget
                    else:
                        total_block_content, com_idx = _pack_and_round(
                            end_budget, commercial_pool, com_idx, total_block_content,
                            max_seconds=max_interrupt_break_seconds)

    elif commercial_tracks and commercial_placement == "end_of_show":
        com_idx = 0
        for show in video_tracks:
            total_block_content, com_idx = _snap_to_mark(total_block_content, com_idx, max_end_break_seconds)
            total_block_content, com_idx = _maybe_flush_deferred_fill(total_block_content, com_idx)

            _dur = _resolved_show_duration(show)
            if _dur_is_estimated(show) and _dur == HALF_HOUR:
                timeline.append(_show_entry(show, _dur, total_block_content, 0))
                total_block_content += _dur
                continue
            timeline.append(_show_entry(show, _dur, total_block_content, 0))
            total_block_content += _dur

            next_mark  = (((total_block_content // HALF_HOUR)
                           + (1 if total_block_content % HALF_HOUR != 0 else 0)) * HALF_HOUR)
            end_budget = next_mark - total_block_content
            if 0 < end_budget < MIN_BREAK_SECONDS:
                next_mark += HALF_HOUR
                end_budget = next_mark - total_block_content
            # End-of-show mode has exactly one break per show, capped at
            # max_end_break_seconds.
            total_block_content, com_idx = _pack_and_round(
                end_budget, commercial_pool, com_idx, total_block_content,
                max_seconds=max_end_break_seconds)

    else:
        for show in video_tracks:
            _dur = _resolved_show_duration(show)
            timeline.append(_show_entry(show, _dur, total_block_content, 0))
            total_block_content += _dur

    new_commercial_state = {
        "bundles": {
            str(_ceiling): {"cycle": _bundle_cycle.get(_ceiling, 0),
                             "recent": list(_bundle_recent.get(_ceiling, []))}
            for _ceiling in _bundles
        },
        "turn_ceiling": _bundle_order[_bundle_turn] if _bundle_order else None,
        "last": _last_played_path,
    }
    return timeline, total_block_content, new_commercial_state


# A generous ceiling on plausible bitrate for a real consumer video file.
# Even high-bitrate 4K HEVC / ProRes rarely exceeds this. Used only to
# catch a duration that's wildly too *short* for the file's size (e.g. a
# VLC parse that reports "done" early with a partial/garbage duration) --
# never to second-guess a duration that looks normal or long.
_DURATION_SANITY_MAX_PLAUSIBLE_MBPS = 60


def _duration_looks_implausible(file_path, duration_sec):
    """True if file_size / duration implies a bitrate no real video would
    have -- i.e. the probe almost certainly returned a truncated/bogus
    duration rather than the file's actual runtime. This is what catches a
    22-minute episode that gets reported as 41 seconds: at that file's
    size, 41 seconds would mean an absurd instantaneous bitrate, so the
    number gets rejected instead of cached. A genuinely short clip (a
    5-second bumper, a 30-second commercial) still passes fine, because a
    short clip's *file size* is small too -- the ratio is what matters,
    not the duration alone."""
    try:
        if not duration_sec or duration_sec <= 0:
            return False
        size_bytes = os.path.getsize(file_path)
        implied_mbps = (size_bytes * 8 / 1_000_000) / duration_sec
        return implied_mbps > _DURATION_SANITY_MAX_PLAUSIBLE_MBPS
    except Exception:
        return False


def get_media_duration(file_path):
    # Polls get_parsed_status() at 10Hz (100ms). None of this function's
    # callers sit on the path that starts visible playback, so a slower
    # poll rate costs nothing the user can see while cutting GIL-contending
    # checks/sec on background probe threads.
    if not file_path or not _cached_path_exists(file_path):
        return None

    norm_p = os.path.normpath(os.path.abspath(file_path))
    with _duration_inflight_lock:
        if norm_p in _duration_inflight:
            return None
        _duration_inflight.add(norm_p)

    try:
        import vlc
        _slot = _get_duration_probe_slot()
        instance, _slot_lock = _slot[0], _slot[1]
        # Back off briefly if a channel change is actively opening a file
        # right now (see _LIVE_PLAYBACK_PRIORITY / set_live_playback_priority).
        # Without this, a background probe run (e.g. catching up on a large
        # batch of never-probed files) can be mid-parse on the same physical
        # drive the instant the user changes channels -- the new file's own
        # open then queues up behind this one at the OS/drive level, and
        # since change_channel() runs synchronously on the main thread, that
        # stalls the main loop (and the next pygame.event.get() call) right
        # along with it. Bounded wait, so a steady stream of channel changes
        # can't starve probing indefinitely -- see _yield_to_live_playback.
        _yield_to_live_playback(f"media_duration_probe({os.path.basename(file_path)})")
        with _slot_lock:
            media = instance.media_new(file_path)
            media.parse_with_options(vlc.MediaParseFlag.local, 5000)
            
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
                    time.sleep(0.10)
            else:
                while media.get_parsed_status() == pending_status:
                    if time.time() - start_time > 5:
                        break
                    time.sleep(0.10)
            
            duration_ms = media.get_duration()
            _parse_ms = (time.time() - start_time) * 1000.0
            if duration_ms > 0:
                duration_sec = duration_ms / 1000.0
                if _duration_looks_implausible(file_path, duration_sec):
                    _media_warning(
                        "duration_implausible",
                        "[MEDIA DURATION] %s parsed as %.0fs, but that "
                        "implies an unrealistic bitrate for the file's "
                        "size -- treating this as a bad probe rather than "
                        "trusting/caching it, leaving unprobed for retry.",
                        os.path.basename(file_path), duration_sec,
                    )
                    return None
                return duration_sec
            else:
                try:
                    _final_status = media.get_parsed_status()
                    _status_name = next(
                        (n for n in dir(vlc.MediaParsedStatus)
                         if not n.startswith("_") and getattr(vlc.MediaParsedStatus, n) == _final_status),
                        str(_final_status))
                except Exception:
                    _status_name = "unknown"
                _media_warning(
                    "duration_parse",
                    "[MEDIA DURATION] Could not parse duration for %s "
                    "(parsed_status=%s, waited=%.0fms) -- leaving unprobed for retry",
                    os.path.basename(file_path), _status_name, _parse_ms,
                )
                return None
    except Exception as e:
        _media_warning(
            "duration_error",
            "[MEDIA DURATION ERROR] %s -- leaving unprobed for retry",
            e,
        )
        return None
    finally:
        with _duration_inflight_lock:
            _duration_inflight.discard(norm_p)


def get_media_video_dimensions(file_path):
    if not file_path or not _cached_path_exists(file_path):
        return None

    norm_p = os.path.normpath(os.path.abspath(file_path))
    with _dims_inflight_lock:
        if norm_p in _dims_inflight:
            return None
        _dims_inflight.add(norm_p)

    try:
        import subprocess, re

        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
            )

        with _fast_disk_io_slot(f"ffprobe_duration({os.path.basename(file_path)})"):
            result = subprocess.run(
                [_resolve_ffmpeg_path(), "-nostdin", "-hide_banner", "-i", file_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, stdin=subprocess.DEVNULL,
                **popen_kwargs
            )
        match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", result.stderr)
        if not match:
            # The dimensions probe is shared by the media preloader and the
            # VLC playback path, so it also sees audio-only files.  An MP3
            # having no video stream is expected and should not look like a
            # recoverable video-probe failure in the application log.  Keep
            # the warning for inputs that have neither a recognizable video
            # nor an audio stream, since those may still be corrupt or
            # unsupported media.
            audio_stream = re.search(
                r"(?im)^\s*Stream #\d+:\d+.*\bAudio:",
                result.stderr,
            )
            if audio_stream:
                return None

            _media_warning(
                "dimensions_no_video",
                "[MEDIA DIMENSIONS] No video stream found for %s -- leaving unprobed for retry",
                os.path.basename(file_path),
            )
            return None

        w, h = int(match.group(1)), int(match.group(2))
        if w <= 0 or h <= 0:
            _media_warning(
                "dimensions_invalid",
                "[MEDIA DIMENSIONS] Invalid dimensions parsed for %s -- leaving unprobed for retry",
                os.path.basename(file_path),
            )
            return None

        _tail = result.stderr[match.end():match.end() + 200]
        dar_match = re.search(r"DAR\s+(\d+):(\d+)", _tail)
        if dar_match:
            dar_num, dar_den = int(dar_match.group(1)), int(dar_match.group(2))
            if dar_num > 0 and dar_den > 0:
                raw_ratio = w / float(h)
                dar_ratio = dar_num / float(dar_den)
                if abs(raw_ratio - dar_ratio) > 0.02:
                    corrected_w = int(round(h * dar_ratio))
                    corrected_w = max(2, corrected_w - (corrected_w % 2))
                    return (corrected_w, h)

        return (w, h)
    except subprocess.TimeoutExpired:
        _media_warning(
            "dimensions_timeout",
            "[MEDIA DIMENSIONS] Probe timed out for %s -- leaving unprobed for retry",
            os.path.basename(file_path),
        )
        return None
    except Exception as e:
        _media_warning(
            "dimensions_error",
            "[MEDIA DIMENSIONS ERROR] %s -- leaving unprobed for retry",
            e,
        )
        return None
    finally:
        with _dims_inflight_lock:
            _dims_inflight.discard(norm_p)


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
    # 3) Running from source: next to this .py file's parent, i.e. the
    # project root -- this file now lives in assets/, not root.
    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    for base_dir in (meipass, exe_dir, src_dir):
        if not base_dir:
            continue
        # Bundled directly next to the app...
        candidate = os.path.join(base_dir, exe_name)
        if os.path.exists(candidate):
            _ffmpeg_exe_path = candidate
            log.info("[FFMPEG] Using bundled binary: %s", candidate)
            return _ffmpeg_exe_path
        candidate = os.path.join(base_dir, "ffmpeg", exe_name)
        if os.path.exists(candidate):
            _ffmpeg_exe_path = candidate
            log.info("[FFMPEG] Using bundled binary: %s", candidate)
            return _ffmpeg_exe_path

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


_resolved_main_dir = None


def resolve_main_dir():
    global _resolved_main_dir
    if _resolved_main_dir is not None:
        return _resolved_main_dir

    if getattr(sys, "frozen", False):
        candidates = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, "main"))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "main"))
        for candidate in candidates:
            if os.path.isdir(candidate):
                _resolved_main_dir = candidate
                log.info("[MAIN DIR] Found existing 'main' folder at: %s", candidate)
                return _resolved_main_dir
        _resolved_main_dir = candidates[0]
        log.warning("[MAIN DIR] No existing 'main' folder found in any candidate "
                    "location; defaulting to %s", _resolved_main_dir)
        return _resolved_main_dir

    # Not frozen -- plain source checkout. This file now lives in assets/,
    # so go up one level to the project root before looking for main/.
    _resolved_main_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main")
    return _resolved_main_dir


# Set once per frame by retro_tv_emulator.py, before any menu/dialog UI is
# drawn, to whatever _apply_crt_shader's own gating will evaluate to for
# that same frame (Bezel active + CRT shader enabled + not true 4:3). Read
# by ui.py's _aa_rounded_rect so it only pays for anti-aliasing supersample
# work when the warp that would otherwise re-jaggy a plain pygame.draw.rect
# edge is actually going to run this frame.
CRT_WARP_ACTIVE = False

_bezel_style_files_cache = {"key": None, "files": [], "checked_at": 0.0}
_BEZEL_STYLE_DISCOVERY_TTL_SEC = 2.0

# resolve_bezel_dir() sits on the same hot per-frame path as
# discover_bezel_style_files() -- both are reached via
# _resolve_current_bezel_path -> _load_full_tv_border_surface, which
# geometry, CRT-shader gating, aspect-boxing checks, OSD placement, and
# the border draw itself each call independently: 6-10+ times per
# rendered frame while Bezel is on. discover_bezel_style_files() already
# throttles its own directory-fingerprint recheck (see
# _BEZEL_STYLE_DISCOVERY_TTL_SEC above), but every one of those 6-10+
# calls -- cache hit or not -- was still paying for THIS function's own
# os.path.isdir() stat first, since it ran unconditionally before any
# cache check, both here and again inside discover_bezel_style_files
# (which also calls resolve_bezel_dir() itself). That's up to ~20 stat
# syscalls a frame, 60 times a second, just to pick between
# "images/bezel" and "images/border" -- a choice that's fixed for the
# entire run unless the user manually renames the folder on disk. Same
# fix as discover_bezel_style_files: throttle the recheck itself, not
# just cache the answer, using the same TTL so a renamed folder is still
# picked up within a couple seconds without a restart.
_bezel_dir_cache = {"dir": None, "checked_at": 0.0}
_BEZEL_DIR_RECHECK_TTL_SEC = _BEZEL_STYLE_DISCOVERY_TTL_SEC


def resolve_bezel_dir():
    now = time.monotonic()
    cached = _bezel_dir_cache
    if cached["dir"] is not None and (now - cached["checked_at"]) < _BEZEL_DIR_RECHECK_TTL_SEC:
        return cached["dir"]
    d = os.path.join(resolve_main_dir(), "images", "bezel")
    if not os.path.isdir(d):
        d = os.path.join(resolve_main_dir(), "images", "border")
    cached["dir"] = d
    cached["checked_at"] = now
    return d


def discover_bezel_style_files():
    """Bezel images available for the user to pick between via the
    Channels-menu Bezel style selector, in style order.

    An unnumbered bezel.png (or border.png) counts as style 1 if present,
    since that's the original single-bezel naming and it should stay the
    default without needing to be renamed. Any numbered files (bezel1.png,
    bezel2.png, ... or border1.png, border2.png, ...) are appended after
    it in ascending numeric order, regardless of what number they carry --
    the number only controls sort order among themselves, not the final
    style index. So a folder with just bezel.png + bezel1.png yields two
    styles: [bezel.png, bezel1.png] -> style 1, style 2.

    Returns an empty list if the folder has nothing usable -- callers
    fall back to their old single-image lookup in that case.

    Lives here (rather than in retro_tv_emulator.py, which owns the actual
    bezel rendering) so both it and ui.py -- which retro_tv_emulator.py
    imports, so it can't import back -- can reach this without a circular
    import.

    This is called several times per rendered frame while Bezel is on
    (border geometry, CRT-shader gating, aspect-boxing checks, the
    border draw itself all route through it), so the directory
    "fingerprint" check below (mtime + entry count, so a bezel<N>.png
    dropped in mid-session is picked up without a restart) is throttled
    to run at most once every _BEZEL_STYLE_DISCOVERY_TTL_SEC of wall
    time, not on every call. The previous version recomputed
    os.path.isdir + os.path.getmtime + os.listdir on the bezel folder
    on EVERY call just to check whether the cached file list was still
    valid -- i.e. a real directory listing 6-10+ times per frame, 60
    times a second, for as long as Bezel was on, regardless of which
    console/core was running. That's real per-frame filesystem I/O this
    function has no business doing that often for a folder that
    essentially never changes mid-session; throttling the *check itself*
    (not just caching its result) is what actually removes it.
    """
    import re as _re_bezel
    import time as _time_bezel
    b_dir = resolve_bezel_dir()
    cached = _bezel_style_files_cache
    now = _time_bezel.monotonic()
    if (cached["key"] is not None
            and cached["key"][0] == b_dir
            and (now - cached["checked_at"]) < _BEZEL_STYLE_DISCOVERY_TTL_SEC):
        return cached["files"]

    try:
        _dir_fingerprint = (os.path.getmtime(b_dir), len(os.listdir(b_dir))) if os.path.isdir(b_dir) else None
    except Exception:
        _dir_fingerprint = None
    _cache_key = (b_dir, _dir_fingerprint)
    cached["checked_at"] = now
    if cached["key"] == _cache_key:
        return cached["files"]

    files = []
    if os.path.isdir(b_dir):
        exts = (".png", ".jpg", ".jpeg", ".bmp", ".gif")
        unnumbered_path = None
        numbered = {}
        try:
            for f in os.listdir(b_dir):
                stem, ext = os.path.splitext(f)
                if ext.lower() not in exts:
                    continue
                if stem.lower() in ("bezel", "border") and unnumbered_path is None:
                    unnumbered_path = os.path.join(b_dir, f)
                    continue
                m = _re_bezel.match(r"^(?:bezel|border)(\d+)$", stem, _re_bezel.IGNORECASE)
                if m:
                    numbered[int(m.group(1))] = os.path.join(b_dir, f)
        except Exception as e:
            log.warning("discover_bezel_style_files: could not list %s: %s", b_dir, e)

        ordered_numbered = [numbered[n] for n in sorted(numbered.keys())]
        if unnumbered_path is not None:
            files = [unnumbered_path] + ordered_numbered
        else:
            files = ordered_numbered

    cached["key"] = _cache_key
    cached["files"] = files
    return files


def bezel_style_count():
    """How many selectable bezel styles are available. Always >= 1 so
    caller code can safely check `> 1` to decide whether to show a cycle
    control."""
    return max(1, len(discover_bezel_style_files()))


_loudness_dir_ready = False


def resolve_loudness_dir():
    """Return the program's own folder for renormalization work files.

    Two-pass loudness normalization needs a temp/backup copy of whatever
    file it's fixing. Those must never live inside a user's media folders
    -- they aren't programming, they get picked up by the folder scanner
    as if they were, and nothing in this codebase ever reads them back
    to restore anything, so leaving them scattered next to media serves
    no purpose. This folder lives next to the program's other owned
    assets (images/, videos/, roms/, cores/) and follows the app -- same
    resolve_main_dir() used everywhere else, so it still resolves
    correctly after the app is packaged into an .exe and moved.
    """
    global _loudness_dir_ready
    path = os.path.join(resolve_main_dir(), "loudness")
    if not _loudness_dir_ready:
        try:
            os.makedirs(path, exist_ok=True)
            _loudness_dir_ready = True
        except OSError as e:
            log.warning("[LOUDNESS DIR] could not create %s: %s", path, e)
    return path


_captions_dir_ready = False


def resolve_captions_dir():
    """Return the program's own folder for generated/extracted caption
    files (.srt).

    Same reasoning as resolve_loudness_dir(): files THIS app produces
    must never land next to a user's media -- a stray .srt dropped into
    their folder would get picked up by the folder scanner and nothing
    here ever reads it back from there. Sidecar caption files the user
    already placed next to a video are only ever READ from wherever they
    are; this folder is only for what gets WRITTEN -- a subtitle stream
    pulled out of a container, or an auto-generated ASR transcript --
    so it stays contained and can be wiped/rebuilt freely.
    """
    global _captions_dir_ready
    path = os.path.join(resolve_main_dir(), "captions")
    if not _captions_dir_ready:
        try:
            os.makedirs(path, exist_ok=True)
            _captions_dir_ready = True
        except OSError as e:
            log.warning("[CAPTIONS DIR] could not create %s: %s", path, e)
    return path


# Sidecar caption files are only ever looked for next to a video using one
# of these extensions, checked in this priority order -- text-based
# formats first (usable directly by both the DVD engine's add_slave and
# the TV/visualizer cue parser below), .sub/.smi last since they're the
# least common and sometimes ambiguous with DVD-style .sub/.idx pairs.
CAPTION_SIDECAR_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".sub", ".smi")

# Subtitle codecs ffmpeg can losslessly remux straight to .srt with no
# re-encode (near-instant, same cost tier as the duration probe below).
# PGS/VOBSUB/DVD-subtitle are bitmap formats -- ffmpeg can't turn those
# into text without OCR, so an embedded stream in one of those codecs is
# treated the same as "no usable embedded track" for the plain-text
# overlay and the waterfall falls through to sidecar/ASR instead. (The
# DVD engine's own VLC-native SPU path is unaffected by this distinction
# -- it renders bitmap subtitle tracks fine on its own; this only matters
# for the paths that need actual text data to draw, not a burned-in
# picture VLC composites for you.)
_TEXT_SUBTITLE_CODECS = ("subrip", "ass", "ssa", "webvtt", "mov_text", "text")
_BITMAP_SUBTITLE_CODECS = ("dvd_subtitle", "hdmv_pgs_subtitle", "xsub", "dvb_subtitle")


def _probe_embedded_subtitle_streams(file_path):
    """List the subtitle streams a plain video file's container reports.

    Uses the exact same lightweight `ffmpeg -i` header read
    _probe_duration_seconds_fast() already does elsewhere in this file --
    reads container metadata only, no decode -- so this is the same cost
    tier as the duration probe (a few milliseconds), not the loudness
    probe's full-audio-decode cost. It's a separate subprocess call today
    rather than reusing duration's own result (the two aren't currently
    threaded together); see the caption precache worker for where that
    could be fused later if it's ever worth the coupling.

    Returns a list of dicts: {"index": int, "lang": str|None,
    "codec": str|None, "is_text": bool}, in stream order. Empty list on
    any probe failure or if there's nothing found -- including, by
    design, for dvdnav:// / physical-disc paths, which plain ffmpeg -i
    can't inspect. That's expected: the DVD engine already handles
    embedded-track discovery for real discs/ISOs itself, live, via VLC's
    video_get_spu_description(). This function only serves the paths
    nothing else already answers -- ordinary video files on TV-station/
    visualizer channels, and the DVD player's own plain-file (non-disc)
    case as an extra fallback source.
    """
    if not file_path or not _cached_path_exists(file_path):
        return []
    try:
        import subprocess, re
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
            )
        with _fast_disk_io_slot(f"ffprobe_subs({os.path.basename(file_path)})"):
            result = subprocess.run(
                [_resolve_ffmpeg_path(), "-nostdin", "-hide_banner", "-i", file_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, stdin=subprocess.DEVNULL,
                **popen_kwargs
            )
        streams = []
        for m in re.finditer(
            r"Stream #\d+:(\d+)(?:\((\w+)\))?[^\n]*:\s*Subtitle:\s*(\w+)",
            result.stderr,
        ):
            idx, lang, codec = m.groups()
            streams.append({
                "index": int(idx),
                "lang": lang,
                "codec": codec,
                "is_text": codec in _TEXT_SUBTITLE_CODECS,
            })
        return streams
    except Exception as e:
        log.warning("_probe_embedded_subtitle_streams: failed for %s: %s", file_path, e)
        return []


def _find_sidecar_caption_file(file_path):
    """Look for a same-named caption file next to `file_path`, trying
    CAPTION_SIDECAR_EXTS in priority order. Returns the first match's
    full path, or None."""
    if not file_path:
        return None
    base, _ = os.path.splitext(file_path)
    for ext in CAPTION_SIDECAR_EXTS:
        candidate = base + ext
        if _cached_path_exists(candidate):
            return candidate
    return None


def _extract_embedded_subtitle_to_file(file_path, stream_index, out_path, timeout=30):
    """Remux one text-based embedded subtitle stream straight to a
    standalone .srt -- no re-encode, no audio/video decode, so this stays
    fast regardless of the source file's length. Only call this for a
    stream _probe_embedded_subtitle_streams() reported as is_text=True;
    a bitmap codec will simply fail to produce readable output here (by
    design -- see _BITMAP_SUBTITLE_CODECS above)."""
    try:
        import subprocess
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
            )
        with _fast_disk_io_slot(f"extract_subs({os.path.basename(file_path)})"):
            result = subprocess.run(
                [_resolve_ffmpeg_path(), "-nostdin", "-y", "-i", file_path,
                 "-map", f"0:{stream_index}", "-c:s", "srt", out_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL,
                **popen_kwargs
            )
        return os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    except Exception as e:
        log.warning("_extract_embedded_subtitle_to_file: failed for %s: %s", file_path, e)
        return False


def resolve_caption_source(file_path, cache_entry=None):
    """The shared embedded -> sidecar waterfall used ahead of ASR, for
    both caption consumers (the DVD/media player's VLC-native path, via
    its own separate live SPU check, and the TV-station/visualizer
    plain-text overlay, via this function). Everything here is a static,
    no-playback-required check -- cheap enough to run from a background
    precache pass. ASR is deliberately NOT attempted here; callers decide
    whether to fall back to it (a genuinely expensive step) when this
    returns kind=None.

    Checks embedded before sidecar -- same priority order the DVD
    engine's own _apply_dvd_captions_state already uses (checks
    video_get_spu_description before a sidecar file via add_slave), kept
    consistent here so a video with both an embedded track and a stray
    sidecar file behaves the same way in both places.

    `cache_entry`, if given, is the caption_cache[norm_path] dict already
    on file for this path (see db.config["caption_cache"], wired up by
    the caller) -- if its stored mtime/size still match the file on disk
    and its srt_path still exists, it's trusted and returned immediately
    without re-probing anything.

    Returns a dict:
      {"kind": "cache"|"embedded"|"sidecar"|None,
       "srt_path": str|None,       # ready to parse, or to add_slave
       "stream_index": int|None,   # only set for a fresh "embedded" hit
                                    # (caller must extract it before this
                                    # is usable by the TV/visualizer cue
                                    # parser -- see
                                    # _extract_embedded_subtitle_to_file)
       "mtime": float|None, "size": int|None}  # of file_path, for caching
    kind=None means nothing usable was found (no embedded track, no
    sidecar file) -- for the TV/visualizer overlay, this is exactly the
    signal to start a live, in-memory-only ASR pass instead (see
    LiveCaptionSession below); this function never resolves ASR results
    itself, on disk or otherwise, so a caller never needs to distinguish
    "no captions" from "captions not generated yet" here the way the old
    "asr"/"asr_partial" kinds used to.
    """
    result = {"kind": None, "srt_path": None, "stream_index": None,
              "mtime": None, "size": None}
    if not file_path or not _cached_path_exists(file_path):
        return result
    try:
        st = os.stat(file_path)
        result["mtime"], result["size"] = st.st_mtime, st.st_size
    except OSError:
        return result

    if (cache_entry and cache_entry.get("mtime") == result["mtime"]
            and cache_entry.get("size") == result["size"]
            and cache_entry.get("srt_path")
            and _cached_path_exists(cache_entry["srt_path"])):
        result["kind"] = "cache"
        result["srt_path"] = cache_entry["srt_path"]
        return result

    for stream in _probe_embedded_subtitle_streams(file_path):
        if stream["is_text"]:
            result["kind"] = "embedded"
            result["stream_index"] = stream["index"]
            return result

    sidecar = _find_sidecar_caption_file(file_path)
    if sidecar:
        result["kind"] = "sidecar"
        result["srt_path"] = sidecar
        return result

    # No embedded track, no sidecar file: kind stays None. Unlike the old
    # code, ASR is not resolved here anymore for the TV/visualizer overlay
    # -- see LiveCaptionSession below. Those captions run live, in memory
    # only, and are never written to (or read back from) disk, so there is
    # nothing for a static resolve like this one to find. (The DVD
    # player's own ASR fallback in game_deck.py does not go through this
    # function at all -- it calls generate_captions_via_asr_chunked
    # directly -- so this change doesn't affect it.)
    return result


_CUE_TAG_RE = None


def parse_caption_cues(srt_or_vtt_path):
    """Parse a .srt or .vtt file into a flat, time-sorted cue list:
    [(start_ms, end_ms, text), ...].

    Used only by the TV-station/visualizer plain-text overlay -- the DVD
    engine never calls this, it hands sidecar/extracted files straight to
    VLC's add_slave and lets VLC's own renderer handle timing and
    compositing.

    Multi-line cues are joined with a space (the overlay draws one line
    at a time, centered) and basic markup (SRT's {\\an8}-style tags,
    <i>/<b>/<font> HTML-ish tags VTT/SRT both sometimes carry) is
    stripped, since the overlay is always plain, single-colored text --
    there's nothing here to render styling info with.
    """
    import re
    global _CUE_TAG_RE
    if _CUE_TAG_RE is None:
        _CUE_TAG_RE = re.compile(r"<[^>]+>|\{[^}]+\}")

    if not srt_or_vtt_path or not _cached_path_exists(srt_or_vtt_path):
        return []
    try:
        with open(srt_or_vtt_path, "r", encoding="utf-8-sig", errors="replace") as f:
            raw = f.read()
    except OSError as e:
        log.warning("parse_caption_cues: could not read %s: %s", srt_or_vtt_path, e)
        return []

    # SRT and VTT share the same "HH:MM:SS,mmm --> HH:MM:SS,mmm" timing
    # line shape -- VTT just allows '.' instead of ',' and sometimes
    # omits the hours digit. One regex covers both formats.
    time_re = re.compile(
        r"(\d{1,2}:)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{1,2}:)?(\d{2}):(\d{2})[.,](\d{3})"
    )

    def _to_ms(h, m, s, ms):
        h = int(h.rstrip(":")) if h else 0
        return ((h * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)

    cues = []
    blocks = re.split(r"\r?\n\r?\n+", raw.strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        if not lines:
            continue
        time_line_idx = None
        for i, ln in enumerate(lines):
            if time_re.search(ln):
                time_line_idx = i
                break
        if time_line_idx is None:
            continue  # e.g. the leading "WEBVTT" header block, or a stray index-only block
        m = time_re.search(lines[time_line_idx])
        start_ms = _to_ms(m.group(1), m.group(2), m.group(3), m.group(4))
        end_ms   = _to_ms(m.group(5), m.group(6), m.group(7), m.group(8))
        text_lines = lines[time_line_idx + 1:]
        text = " ".join(_CUE_TAG_RE.sub("", ln).strip() for ln in text_lines).strip()
        text = (text.replace("&amp;", "&").replace("&lt;", "<")
                    .replace("&gt;", ">").replace("&nbsp;", " "))
        if text and end_ms > start_ms:
            cues.append((start_ms, end_ms, text))

    cues.sort(key=lambda c: c[0])
    return cues


_asr_model_dir_ready = False


def resolve_whisper_model_dir():
    """Where the faster-whisper model weights live -- same app-owned-
    folder reasoning as resolve_captions_dir()/resolve_loudness_dir()
    (see those docstrings), so the model can be bundled at packaging time
    and still resolve correctly after the app is frozen and moved. If
    nothing's bundled here, WhisperModel() falls back to its own default
    cache dir and downloads on first use -- this is only a preferred
    location, not a hard requirement.
    """
    global _asr_model_dir_ready
    path = os.path.join(resolve_main_dir(), "models", "whisper")
    if not _asr_model_dir_ready:
        try:
            os.makedirs(path, exist_ok=True)
            _asr_model_dir_ready = True
        except OSError as e:
            log.warning("[ASR MODEL DIR] could not create %s: %s", path, e)
    return path


# Auto-captioning is the one genuinely expensive step in the whole
# captions feature (full audio decode + speech-to-text inference), so
# it's capped at exactly one job at a time -- unlike duration probing
# (parallel, up to 6 workers) or even loudness probing (2 concurrent
# slots). Running two whisper jobs at once would fight each other for
# the same CPU cores with no throughput win.
_ASR_SEMAPHORE = threading.BoundedSemaphore(1)

# English-only "small.en" is both faster and more accurate for English than
# the multilingual "small" at the same size. Every title on this app's
# channels is English; if that ever changes, set _ASR_MODEL_SIZE = "small"
# and _ASR_LANGUAGE = None to go back to multilingual auto-detect.
#
# ACCURACY PASS: was "base.en". base.en's word-error-rate is noticeably
# worse than small.en's on any speech that isn't clean/neutral-accent
# studio audio -- exactly the "missed a line, strung two people's
# sentences together, got words wrong on an accent" complaint. small.en
# is the standard step up (roughly 2-3x base's compute, well within the
# headroom this app has: even base.en beam=1 was transcribing several
# 15-18s chunks per second of wall time -- see LIVE_CAPTION_MAX_LEAD_SEC's
# comment on why that was actually a problem on its own). If a slower
# machine can't keep small.en comfortably ahead of real-time playback
# (watch for "chunk N/M ... in Xs" taking close to or over the chunk's own
# length in the log), drop this back to "base.en" first before touching
# beam size or VAD below.
_ASR_MODEL_SIZE = "small.en"
# Fixed language: skips per-chunk language detection (extra work on every
# 5-minute chunk, and it can flip languages on a quiet or musical stretch).
_ASR_LANGUAGE = "en"
# ACCURACY PASS: was 1 (greedy decoding -- fastest, but the least accurate
# search faster-whisper supports, and the first thing to fall over on an
# unfamiliar accent since it never reconsiders an early wrong guess).
# Whisper's own upstream default is 5; this trades some of the CPU
# headroom above for meaningfully fewer wrong-word substitutions.
_ASR_BEAM_SIZE = 5
# ACCURACY PASS ("strung two people's sentences together"): faster-whisper's
# VAD (Silero) defaults to a long min_silence_duration_ms, so a quick
# back-and-forth between two speakers with less than ~2s of dead air
# between them reads as one continuous speech span and gets handed to
# Whisper as a single segment to transcribe -- Whisper then has to guess
# where one utterance ends and the next begins, and often doesn't put a
# break in at all. A much shorter minimum silence lets the VAD actually
# split on the gap between speakers (or between sentences generally),
# giving Whisper more, shorter, correctly-bounded segments instead of one
# long run-on. Passed as vad_parameters to model.transcribe() below.
_ASR_VAD_PARAMETERS = {"min_silence_duration_ms": 500}
# Transcription is CPU-only (ctranslate2 has no Intel Arc path). Use a
# healthy share of the cores so a chunk finishes quickly, but never all of
# them -- the render loop and VLC need headroom while a title is playing.
_ASR_CPU_THREADS = max(4, min(8, (os.cpu_count() or 8) // 2))
_whisper_model = None
_whisper_model_load_failed = False
_whisper_model_lock = threading.Lock()


@contextlib.contextmanager
def _asr_slot(label=""):
    """Like `with _ASR_SEMAPHORE:`, but yields to live playback first
    (same courtesy _fast_disk_io_slot gives channel changes -- see
    _yield_to_live_playback) and logs real contention."""
    _yield_to_live_playback(label)
    _wait_start = _now_ms()
    with _ASR_SEMAPHORE:
        _waited_ms = _now_ms() - _wait_start
        yield


def _get_whisper_model():
    """Lazily load the faster-whisper model once and reuse it for every
    subsequent call. faster-whisper is an optional dependency -- if it
    isn't installed, this logs one warning (not one per file) and every
    caller treats a None return the same as "ASR unavailable right now",
    falling back to no captions rather than crashing."""
    global _whisper_model, _whisper_model_load_failed
    if _whisper_model is not None:
        return _whisper_model
    if _whisper_model_load_failed:
        return None
    with _whisper_model_lock:
        if _whisper_model is not None or _whisper_model_load_failed:
            return _whisper_model
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            _whisper_model_load_failed = True
            log.warning(
                "[ASR] faster-whisper is not installed -- auto-captioning is "
                "disabled (embedded/sidecar captions still work normally). "
                "Install with: pip install faster-whisper"
            )
            return None
        try:
            try:
                # Already downloaded? Load straight from disk. Without this,
                # faster-whisper phones huggingface.co on every launch first
                # (visible in the logs as a GET .../revision/main right before
                # "model ready"), adding network latency to the first tune-in.
                _whisper_model = WhisperModel(
                    _ASR_MODEL_SIZE, device="cpu", compute_type="int8",
                    cpu_threads=_ASR_CPU_THREADS,
                    download_root=resolve_whisper_model_dir(),
                    local_files_only=True,
                )
            except Exception:
                _whisper_model = WhisperModel(   # not on disk yet: normal download path
                    _ASR_MODEL_SIZE, device="cpu", compute_type="int8",
                    cpu_threads=_ASR_CPU_THREADS,
                    download_root=resolve_whisper_model_dir(),
                )
            log.info("[ASR] faster-whisper '%s' model ready (CPU, int8, %d threads).",
                     _ASR_MODEL_SIZE, _ASR_CPU_THREADS)
        except Exception as e:
            _whisper_model_load_failed = True
            log.warning("[ASR] failed to load whisper model: %s", e)
            return None
        # BUG FIX (live captions' first chunk runs at ~1x real-time instead
        # of faster): a freshly loaded ctranslate2 model pays real one-time
        # setup cost (thread pool spin-up, buffer allocation, kernel
        # selection) on its FIRST transcribe() call, not on WhisperModel()
        # construction. Left alone, that cost lands on whichever chunk
        # happens to be first -- almost always a live caption session's
        # opening chunk right after someone turns captions on or changes
        # channel, which is exactly when there's no lead built yet to
        # absorb it. Paying it here instead, on a throwaway 1s silent
        # buffer immediately after load (once per app run, off the
        # critical path of any real chunk), means the first real chunk
        # transcribes at the same speed every later chunk does. Best
        # effort: if this fails for any reason, fall through and let the
        # first real chunk pay the cost the old way rather than treating
        # a warm-up failure as a load failure.
        try:
            import numpy as _np
            _silence = _np.zeros(16000, dtype="float32")  # 1s @ 16kHz
            _segs, _ = _whisper_model.transcribe(
                _silence, language=_ASR_LANGUAGE, beam_size=1)
            list(_segs)  # force the generator to actually run inference
        except Exception as e:
            log.warning("[ASR] model warm-up skipped: %s", e)
    return _whisper_model


# ---------------------------------------------------------------------------
# Speech-to-text core shared by the DVD player's disk-cached captions
# (generate_captions_via_asr_chunked) and the TV overlay's live captions
# (LiveCaptionSession).
#
# Pipeline for one chunk WAV:
#   1. Silero VAD finds the speech islands (in the chunk's OWN timeline, so
#      every timestamp below is something measured here, not remapped by
#      faster-whisper's internal vad_filter bookkeeping -- that remap drifted
#      on chunks with a lot of silence).
#   2. Islands separated by a short pause are merged into one clip, so
#      Whisper hears whole sentences (context = correct spelling) and pays its
#      fixed per-call cost once instead of once per fragment.
#   3. Each clip is transcribed with word timestamps.
#   4. Every Whisper segment is split into short, caption-sized cues.
# ---------------------------------------------------------------------------
_asr_vad_utils_load_failed = False

# Silero VAD sensitivity. The library default (0.5) is tuned for clean speech;
# dialogue under music/effects scores lower and whole chunks came back with no
# speech at all. The no-speech/logprob guard in _asr_filter_cues still removes
# hallucinations over pure music. Raise toward 0.5 if captions appear over
# non-speech.
_ASR_VAD_THRESHOLD = 0.35
_ASR_VAD_MIN_SPEECH_MS = 150       # library default 250 drops short exclamations ("No!")
# Speech islands closer together than this become one clip (the pause stays in
# the clip, so timestamps remain exact); a merged clip never exceeds
# _ASR_MAX_CLIP_SEC (Whisper's own window is 30s).
_ASR_MERGE_GAP_SEC = 1.5
# Live captions only: the extract used to hear the file's RAW audio (no gain,
# no leveling), so a quieter second speaker (e.g. a kid answering an adult)
# could fall under the VAD threshold and never reach Whisper. Local dynamic
# leveling lifts quiet voices without touching playback audio.
_ASR_LIVE_AUDIO_FILTER = "dynaudnorm=f=150:g=15:p=0.9:m=20"
_ASR_MAX_CLIP_SEC = 25.0

# Caption-sized cues. Whisper segments can run 10-20s across several
# sentences; the overlay only holds a cue on screen for a few seconds from its
# START, and _asr_merge_cues treats a short cue inside a long one as a
# duplicate and drops it. So every segment is split at sentence ends / pauses /
# a length cap using its word timestamps.
_ASR_CUE_MAX_SEC = 5.0
_ASR_CUE_MAX_CHARS = 84            # two lines of ~42
_ASR_CUE_GAP_BREAK_SEC = 0.8
_ASR_CUE_MIN_SHOW_SEC = 1.2
_ASR_CUE_TRAIL_HOLD_SEC = 0.3
_ASR_SENTENCE_END = (".", "?", "!", "\u2026")

# Hallucination guard: Whisper's own "this is silence" signal.
_ASR_NO_SPEECH_PROB_MAX = 0.75
_ASR_AVG_LOGPROB_MIN = -1.5

import re as _re_asr

_ASR_PROMPT_JUNK_RE = _re_asr.compile(
    r"^(s\d{1,2}\s*e\d{1,3}|e\d{1,3}|\d{1,4}|\d{1,2}x\d{1,3}|season\s*\d+|episode\s*\d+)$",
    _re_asr.I)


def _asr_prompt_for(file_path):
    """Short vocabulary hint for Whisper built from the file's own names, e.g.
    ".../Aladdin - 101 - Air Feathered Friends.mkv" -> "Aladdin. Air Feathered
    Friends." Biases spelling of character names and title words. None if
    nothing useful can be derived."""
    try:
        stem = os.path.splitext(os.path.basename(file_path))[0]
        stem = _re_asr.sub(r"[\(\[][^\)\]]*[\)\]]", " ", stem)          # (480p - DVDRip), [YIFY]
        stem = _re_asr.sub(r"[._]+", " ", stem)
        parts = [p.strip() for p in stem.split(" - ")]
        parts = [p for p in parts if p and not _ASR_PROMPT_JUNK_RE.match(p)]
        if len(parts) == 1:
            # "Ace Ventura Pet Detective 1994 1080p ..." -- cut at the year/quality tag
            parts = [_re_asr.split(r"\b(19|20)\d{2}\b|\b(480|720|1080|2160)p\b", parts[0])[0].strip()]
        parts = [p for p in parts if p]
        if not parts:
            return None
        return (". ".join(parts) + ".")[:120]
    except Exception:
        return None


def _asr_merge_spans(spans, sampling_rate):
    """Merge VAD spans (dicts of sample indices) whose gap is under
    _ASR_MERGE_GAP_SEC, without letting a merged clip exceed _ASR_MAX_CLIP_SEC."""
    merged = []
    max_gap = int(_ASR_MERGE_GAP_SEC * sampling_rate)
    max_len = int(_ASR_MAX_CLIP_SEC * sampling_rate)
    for sp in spans:
        if merged:
            last = merged[-1]
            if sp["start"] - last["end"] <= max_gap and sp["end"] - last["start"] <= max_len:
                last["end"] = sp["end"]
                continue
        merged.append({"start": sp["start"], "end": sp["end"]})
    return merged


def _asr_split_segment(seg, offset=0.0):
    """One Whisper segment -> list of short caption cues (SimpleNamespace with
    start/end/text/no_speech_prob/avg_logprob), times shifted by `offset`."""
    nsp = getattr(seg, "no_speech_prob", None)
    alp = getattr(seg, "avg_logprob", None)

    def mk(a, b, text):
        return types.SimpleNamespace(start=a, end=b, text=text,
                                     no_speech_prob=nsp, avg_logprob=alp)

    whole = [mk(offset + float(seg.start), offset + float(seg.end), (seg.text or "").strip())]
    words = getattr(seg, "words", None) or []
    if not words or any(getattr(w, "start", None) is None or getattr(w, "end", None) is None
                        for w in words):
        return whole
    cues, cur = [], []

    def flush():
        if cur:
            text = "".join((w.word or "") for w in cur).strip()
            if text:
                cues.append(mk(offset + float(cur[0].start), offset + float(cur[-1].end), text))
            del cur[:]

    for w in words:
        wtext = w.word or ""
        if cur:
            gap = w.start - cur[-1].end
            dur = w.end - cur[0].start
            chars = len("".join((x.word or "") for x in cur)) + len(wtext)
            if gap >= _ASR_CUE_GAP_BREAK_SEC or dur > _ASR_CUE_MAX_SEC or chars > _ASR_CUE_MAX_CHARS:
                flush()
        cur.append(w)
        tail = wtext.rstrip().rstrip("\"'\u201d\u2019)")
        chars_now = len("".join((x.word or "") for x in cur).strip())
        if tail.endswith(_ASR_SENTENCE_END) and chars_now >= 12:
            flush()
        elif tail.endswith((",", ";", ":")) and chars_now > 55:
            flush()
    flush()
    return cues or whole


def _asr_finalize_cue_timing(cues):
    """Give each cue a readable minimum on-screen time and a short trailing
    hold, without ever running into the next cue."""
    cues.sort(key=lambda c: c.start)
    for i, c in enumerate(cues):
        end = max(c.end + _ASR_CUE_TRAIL_HOLD_SEC, c.start + _ASR_CUE_MIN_SHOW_SEC)
        if i + 1 < len(cues):
            end = min(end, max(cues[i + 1].start, c.end))
        c.end = max(end, c.end)


class _SegList(list):
    """list of cue-like objects plus VAD stats for the per-chunk log line."""
    vad_islands = 0
    vad_speech_sec = 0.0


def _asr_transcribe_chunk(model, wav_path, language, beam_size, initial_prompt=None,
                          temperature=None, should_stop=None):
    """Transcribe one already-extracted chunk WAV. Returns a list of cue-like
    objects (.start/.end/.text/.no_speech_prob/.avg_logprob) whose times are
    chunk-relative seconds in wav_path's own timeline, so callers just add the
    chunk's absolute file position.

    temperature=None keeps faster-whisper's default fallback ladder (re-decode
    up to 5 more times, sampling, when a decode looks bad). Live captions pass
    0.0 = one decode pass: a single bad clip (screaming, effects) was observed
    taking ~50s through that ladder, which put captions 30s+ behind playback.

    should_stop: optional zero-arg callable polled between clips and between
    segments so a channel change doesn't wait out a long decode.

    Falls back to faster-whisper's built-in vad_filter (same 500ms silence
    split) if its VAD utilities can't be imported.
    """
    global _asr_vad_utils_load_failed

    def _stopped():
        try:
            return bool(should_stop and should_stop())
        except Exception:
            return False

    temp_kw = {} if temperature is None else {"temperature": temperature}
    if not _asr_vad_utils_load_failed:
        try:
            from faster_whisper.audio import decode_audio
            from faster_whisper.vad import get_speech_timestamps, VadOptions
        except ImportError:
            _asr_vad_utils_load_failed = True
            log.warning("[ASR] faster-whisper VAD utilities unavailable -- falling back to "
                        "the built-in vad_filter (caption sync may drift after long silences).")

    if _asr_vad_utils_load_failed:
        segments_iter, _info = model.transcribe(
            wav_path, language=language, beam_size=beam_size, vad_filter=True,
            vad_parameters=_ASR_VAD_PARAMETERS, condition_on_previous_text=False,
            initial_prompt=initial_prompt, word_timestamps=True, **temp_kw,
        )
        out = _SegList()
        for seg in segments_iter:
            if _stopped():
                break
            out.extend(_asr_split_segment(seg, 0.0))
        _asr_finalize_cue_timing(out)
        return out

    audio = decode_audio(wav_path)  # mono float32 @ 16 kHz
    sampling_rate = 16000
    min_silence = _ASR_VAD_PARAMETERS["min_silence_duration_ms"]
    try:
        vad_opts = VadOptions(threshold=_ASR_VAD_THRESHOLD, min_silence_duration_ms=min_silence,
                              min_speech_duration_ms=_ASR_VAD_MIN_SPEECH_MS)
    except TypeError:  # older faster-whisper without these fields
        vad_opts = VadOptions(min_silence_duration_ms=min_silence)
    raw_spans = get_speech_timestamps(audio, vad_opts)
    out = _SegList()
    out.vad_islands = len(raw_spans)
    out.vad_speech_sec = sum((sp["end"] - sp["start"]) for sp in raw_spans) / sampling_rate
    for span in _asr_merge_spans(raw_spans, sampling_rate):
        if _stopped():
            break
        clip = audio[span["start"]:span["end"]]
        if clip.size == 0:
            continue
        span_start_sec = span["start"] / sampling_rate
        t_clip = time.monotonic()
        segments_iter, _info = model.transcribe(
            clip, language=language, beam_size=beam_size, vad_filter=False,
            condition_on_previous_text=False, initial_prompt=initial_prompt,
            word_timestamps=True, **temp_kw,
        )
        for seg in segments_iter:
            if _stopped():
                break
            out.extend(_asr_split_segment(seg, span_start_sec))
        t_clip = time.monotonic() - t_clip
        if t_clip > 8.0:
            log.warning("[ASR SLOW] %.1fs of speech took %.1fs to transcribe (beam=%s)",
                        clip.size / float(sampling_rate), t_clip, beam_size)
    _asr_finalize_cue_timing(out)
    return out


def _asr_filter_cues(cues, chunk_start, chunk_end, at_file_end):
    """Turn a chunk's cue-like objects into absolute [start, end, text] cues,
    dropping the ones that don't belong to this chunk or aren't speech.
    Returns (kept, drops) where drops counts each reason.

    A cue that STARTS at or after chunk_end (audio only present as overlap)
    belongs to the next chunk, which transcribes it from its own start.
    """
    kept = []
    drops = {"next_chunk": 0, "empty": 0, "not_speech": 0}
    for c in cues:
        s_abs = chunk_start + float(c.start)
        e_abs = chunk_start + float(c.end)
        if not at_file_end and s_abs >= chunk_end:
            drops["next_chunk"] += 1
            continue
        text = (c.text or "").strip()
        if not text or e_abs <= s_abs:
            drops["empty"] += 1
            continue
        nsp = getattr(c, "no_speech_prob", None)
        alp = getattr(c, "avg_logprob", None)
        if (nsp is not None and alp is not None
                and nsp > _ASR_NO_SPEECH_PROB_MAX and alp < _ASR_AVG_LOGPROB_MIN):
            drops["not_speech"] += 1
            continue
        kept.append([s_abs, e_abs, text])
    return kept, drops


def asr_known_unavailable():
    """True once this run has found faster-whisper missing or its model
    unloadable. Cheap (no I/O, no model load), so background workers can
    skip ASR-only work entirely instead of pacing themselves through a whole
    backlog just to be told "unavailable" on every file."""
    return bool(_whisper_model_load_failed)


def _srt_timestamp(seconds):
    ms_total = max(0, round(seconds * 1000))
    h, rem = divmod(ms_total, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------------------------------------------------------------------
# ASR result reasons.
#
# generate_captions_via_asr() / generate_captions_via_asr_chunked() return
# (srt_path_or_None, reason). The reason is what lets the caller tell
# "this file genuinely has nothing to caption" (permanent -- safe to cache as
# "no captions" forever) apart from "we couldn't do it right now" (ASR not
# installed, a crashed ffmpeg, a shutdown mid-job -- must be retried, never
# cached as "none"). Caching the second kind as "none" is what used to
# poison every title the first time this ran without faster-whisper.
# ---------------------------------------------------------------------------
ASR_REASON_SUCCESS = "ok"
ASR_REASON_UNAVAILABLE = "unavailable"            # faster-whisper missing / model failed to load
ASR_REASON_NO_AUDIO = "no_audio"                  # file has no audio stream       (permanent)
ASR_REASON_NO_SPEECH = "no_speech"                # whole file transcribed, no speech (permanent)
ASR_REASON_EXTRACT_FAILED = "extract_failed"      # ffmpeg couldn't read/extract the audio
ASR_REASON_TRANSCRIBE_FAILED = "transcribe_failed"
ASR_REASON_INTERRUPTED = "interrupted"            # should_stop() fired; partial progress kept

# Chunking. The first chunk is deliberately short so the first cues show up
# within seconds of the job starting; after that, 5-minute chunks keep the
# per-chunk overhead (audio extract + VAD warm-up) small relative to the
# transcription itself.
_ASR_FIRST_CHUNK_SEC = 60.0
_ASR_CHUNK_SEC = 300.0
# Each chunk's audio runs this far past its nominal end so a sentence that
# starts inside the chunk can be finished by it. Cues that START in the overlap
# are left for the next chunk (see _asr_filter_cues). Cues are split to <=5s
# (see _ASR_CUE_MAX_SEC), so 8s always covers the tail of the last cue.
_ASR_CHUNK_OVERLAP_SEC = 8.0
# A leftover shorter than this is folded into the previous chunk instead of
# becoming its own tiny job.
_ASR_MIN_TAIL_SEC = 15.0
_ASR_META_VERSION = 1


def _asr_paths(file_path):
    """(srt_path, meta_path) for a plain media file. The .srt keeps the same
    digest-based name the old whole-file ASR used; the .asr.json beside it is
    the resume record (which time ranges are already transcribed)."""
    import hashlib
    norm_path = os.path.normpath(os.path.abspath(file_path))
    digest = hashlib.sha1(norm_path.encode("utf-8", "replace")).hexdigest()[:16]
    out_dir = resolve_captions_dir()
    return (os.path.join(out_dir, f"{digest}.srt"),
            os.path.join(out_dir, f"{digest}.asr.json"))


def _atomic_write_text(path, text):
    """Write `text` to `path` so a concurrent reader (the on-screen overlay
    re-parsing a growing .srt) sees either the old complete file or the new
    complete file, never a half-written one. Retries the final rename a few
    times because on Windows os.replace() fails while a reader still has the
    destination open -- and the reader only holds it for a moment."""
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except OSError as e:
        log.warning("[ASR] could not write %s: %s", tmp_path, e)
        return False
    last_err = None
    for _attempt in range(20):
        try:
            os.replace(tmp_path, path)
            return True
        except OSError as e:
            last_err = e
            time.sleep(0.05)
    log.warning("[ASR] could not replace %s: %s", path, last_err)
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    return False


def _asr_serialize_srt(cues):
    """cues: list of [start_sec, end_sec, text] -> SRT text."""
    parts = []
    for i, (start, end, text) in enumerate(cues, 1):
        parts.append(f"{i}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{text}\n\n")
    return "".join(parts)


def _asr_read_valid(file_path, mtime, size):
    """Return {"srt_path", "meta", "srt_exists"} for an existing ASR resume
    record that still belongs to this exact file (same mtime/size) and is
    internally consistent, else None. Never raises."""
    try:
        srt_path, meta_path = _asr_paths(file_path)
        if not os.path.isfile(meta_path):
            return None
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, dict) or meta.get("version") != _ASR_META_VERSION:
            return None
        if meta.get("mtime") != mtime or meta.get("size") != size:
            return None
        srt_exists = os.path.isfile(srt_path) and os.path.getsize(srt_path) > 0
        if int(meta.get("cue_count", 0) or 0) > 0 and not srt_exists:
            return None  # transcript was deleted out from under the record
        return {"srt_path": srt_path, "meta": meta, "srt_exists": srt_exists}
    except Exception:
        return None


def _asr_discard_state(file_path):
    """Delete a stale/invalid ASR resume record and its partial transcript."""
    try:
        srt_path, meta_path = _asr_paths(file_path)
    except Exception:
        return
    for p in (srt_path, meta_path, srt_path + ".tmp", meta_path + ".tmp"):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


def _asr_merge_ranges(ranges):
    """Merge [start, end] pairs into a sorted list of disjoint [start, end]."""
    out = []
    for a, b in sorted((float(a), float(b)) for a, b in ranges if float(b) > float(a)):
        if out and a <= out[-1][1] + 0.01:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _asr_subtract_ranges(lo, hi, done):
    """The pieces of [lo, hi) not covered by `done` (merged + sorted),
    in ascending order."""
    out = []
    pos = lo
    for a, b in done:
        if b <= pos:
            continue
        if a >= hi:
            break
        if a > pos:
            out.append((pos, min(a, hi)))
        pos = max(pos, b)
        if pos >= hi:
            break
    if pos < hi:
        out.append((pos, hi))
    return out


def _asr_plan_chunks(duration, start_sec, done):
    """Ordered list of (start, end) chunks still to transcribe: forward from
    start_sec to the end of the file first (that's the part about to air),
    then wrap around and fill 0 -> start_sec so the whole file is eventually
    covered. Ranges already in `done` are skipped, which is what makes an
    interrupted job resume instead of restart."""
    start = min(max(0.0, float(start_sec or 0.0)), float(duration))
    out = []
    first = True
    for lo, hi in ((start, float(duration)), (0.0, start)):
        for a, b in _asr_subtract_ranges(lo, hi, done):
            pos = a
            while b - pos > 0.5:
                size = _ASR_FIRST_CHUNK_SEC if first else _ASR_CHUNK_SEC
                end = min(b, pos + size)
                if b - end < _ASR_MIN_TAIL_SEC:
                    end = b
                out.append((pos, end))
                pos = end
                first = False
    if not out and not done and duration > 0.1:
        # A very short file can split into two slivers, both below the
        # "worth a chunk" threshold -- never plan nothing for a file that
        # has had nothing done yet.
        out.append((0.0, float(duration)))
    return out


_ASR_DUP_TEXT_SIMILARITY_MIN = 0.6

# Below this length, text-only matching is too easy to satisfy by accident
# (see _asr_cues_are_same_speech) -- a real second "No!" a beat after the
# first is a false duplicate under text matching alone.
_ASR_SHORT_TEXT_CHARS = 6
_ASR_SHORT_TEXT_OVERLAP_MIN = 0.85


def _asr_cues_are_same_speech(new_text, old_text, overlap_frac=None):
    """True only if two overlapping-in-time cues are actually the same
    utterance re-transcribed (a sentence straddling a chunk boundary, or a
    chunk redone after an interrupted run) rather than two different
    sentences that merely landed in overlapping windows.

    Two independent extractions of the same audio (this happens on purpose
    -- see _ASR_CHUNK_OVERLAP_SEC) can disagree slightly on where a segment
    starts/ends, especially right after a quieter or fast reply from a
    second speaker. When that happens, a genuinely new sentence can overlap
    an existing cue's timestamps by more than half its span while
    containing completely different words. Requiring the text to actually
    match (not just the timing) is what stops that new sentence from being
    silently discarded as a "duplicate" of the old one.

    Short exclamations break that logic, though: "No!" trivially "matches"
    any other "No!" by text no matter which two utterances they actually
    are, so a genuine second "No!" spoken shortly after the first looks
    identical to a re-transcription of the same one. For text at or under
    _ASR_SHORT_TEXT_CHARS, text matching alone is not enough evidence --
    also require the two cues' timestamps to overlap almost completely
    (>= _ASR_SHORT_TEXT_OVERLAP_MIN of the shorter span), which a
    re-transcription of the same audio will do and two distinct spoken
    instants generally won't. overlap_frac is that fraction, precomputed
    by the caller (which already has both cues' spans on hand).
    """
    a = (new_text or "").strip().lower()
    b = (old_text or "").strip().lower()
    if not a or not b:
        return False
    text_matches = False
    if a in b or b in a:
        text_matches = True
    elif difflib.SequenceMatcher(None, a, b).ratio() >= _ASR_DUP_TEXT_SIMILARITY_MIN:
        text_matches = True
    if not text_matches:
        return False
    if len(a) <= _ASR_SHORT_TEXT_CHARS or len(b) <= _ASR_SHORT_TEXT_CHARS:
        is_dup = overlap_frac is not None and overlap_frac >= _ASR_SHORT_TEXT_OVERLAP_MIN
        log.info(
            "[LIVE ASR SHORT-TEXT] %r vs %r: overlap=%s min=%.2f -> %s",
            new_text, old_text,
            ("n/a" if overlap_frac is None else f"{overlap_frac:.2f}"),
            _ASR_SHORT_TEXT_OVERLAP_MIN,
            "treated as duplicate (dropped)" if is_dup else "kept as distinct cue")
        return is_dup
    return True


def _asr_merge_cues(cues, new_cues):
    """Add new_cues ([start, end, text] lists) into `cues` in place. A new
    cue that mostly overlaps one already there AND reads as the same speech
    (see _asr_cues_are_same_speech) is a duplicate take -- a sentence
    straddling a chunk boundary or the wrap-around point, or a chunk redone
    after an interrupted run -- so it is not added as a second cue; if the
    new take is the more complete one (noticeably longer, e.g. a whole
    sentence replacing a cut-off fragment) it replaces the old one instead.
    A new cue that overlaps in time but does NOT match the old cue's text is
    a second, distinct utterance (e.g. a quick reply that landed inside the
    first speaker's mistimed window) and is kept as its own cue rather than
    dropped. Returns how many brand-new cues were added."""
    added = 0
    for s, e, t in new_cues:
        span = max(0.001, e - s)
        dup_idx = None
        for i, (cs, ce, ct) in enumerate(cues):
            overlap = min(e, ce) - max(s, cs)
            shorter_span = min(span, max(0.001, ce - cs))
            if overlap > 0.5 * shorter_span:
                overlap_frac = overlap / shorter_span
                if _asr_cues_are_same_speech(t, ct, overlap_frac=overlap_frac):
                    dup_idx = i
                    break
                log.info(
                    "[LIVE ASR MERGE] overlapping cues kept as distinct speech: "
                    "existing=%.2f-%.2fs %r new=%.2f-%.2fs %r",
                    cs, ce, ct, s, e, t)
        if dup_idx is None:
            cues.append([s, e, t])
            added += 1
        else:
            cs, ce, _ct = cues[dup_idx]
            if span > (ce - cs) + 0.25:
                cues[dup_idx] = [s, e, t]
    cues.sort(key=lambda c: c[0])
    return added


def _asr_probe_media(file_path, timeout=30):
    """One ffmpeg header read -> {"duration": float|None, "has_audio": bool},
    or None if the file couldn't be read at all."""
    import re
    try:
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
            )
        with _fast_disk_io_slot(f"asr_probe({os.path.basename(file_path)})"):
            result = subprocess.run(
                [_resolve_ffmpeg_path(), "-nostdin", "-hide_banner", "-i", file_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL, **popen_kwargs
            )
    except Exception as e:
        log.warning("[ASR] probe failed for %s: %s", os.path.basename(file_path), e)
        return None
    err = result.stderr or ""
    if "Input #0" not in err:
        return None
    duration = None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        h, mi, sec = m.groups()
        total = int(h) * 3600 + int(mi) * 60 + float(sec)
        duration = total if total > 0 else None
    return {"duration": duration, "has_audio": _stderr_has_audio_stream(err)}


# How far before the real target to land with the fast (input-side) seek,
# before switching to accurate (output-side) seeking for the remainder --
# see _asr_extract_chunk's docstring for why this two-stage approach exists.
# Old/loosely-indexed containers (a plain "-ss before -i" fast seek is only
# keyframe/sync-point accurate, not sample-accurate) can be off by several
# seconds on a single-stage fast seek -- every cue built from that chunk
# then inherits the same offset, since every cue's absolute time is
# `chunk_start_sec + seg.start` and that arithmetic assumes the extracted
# audio really began at chunk_start_sec. 15s comfortably covers the keyframe
# intervals seen on older AVI rips; the accurate second-stage seek is cheap
# (well under the whisper transcription time for the chunk) regardless of
# how large this margin is, so there's no real cost to being generous here.
_ASR_ACCURATE_SEEK_MARGIN_SEC = 15.0


def _asr_extract_chunk(file_path, start_sec, length_sec, out_wav_path, timeout=120,
                        accurate_seek_margin_sec=None, audio_filter=None):
    """Extract mono 16 kHz PCM for just [start, start+length] of the file.
    Returns "ok", "empty" (ffmpeg succeeded but there was no audio in that
    span -- e.g. the tail past where the audio actually ends), or "failed"
    (ffmpeg error/timeout -- transient, worth retrying).

    Two-stage ("fast-then-accurate") seek: a first -ss BEFORE -i does a
    cheap demuxer-level seek to comfortably before the real target (fast,
    but only keyframe/sync-point accurate -- can land several seconds off
    on a loosely-indexed container), then a second -ss AFTER -i decodes
    forward from there sample-accurately to close the remaining gap. A
    single-stage "-ss before -i" (the old approach) is fast but every cue
    computed from its output silently inherits however far off that one
    seek landed -- on affected files this showed up as captions running a
    consistent several-to-teens-of-seconds ahead of (or behind) the actual
    dialogue, despite the ASR pass itself transcribing correctly. The
    accurate second stage only has to decode `accurate_seek_margin_sec`
    worth of audio, not the whole file up to that point, so this stays
    cheap even deep into a long file.

    `accurate_seek_margin_sec` defaults to _ASR_ACCURATE_SEEK_MARGIN_SEC
    (the disk-cached path's value -- generous, since that pass runs once
    and its .srt is kept). LiveCaptionSession passes a smaller value: this
    decode is a FIXED tax paid on every single live chunk (not just the
    first), competing for the same CPU that's simultaneously decoding
    video for actual playback -- see the BUG FIX note on
    _ASR_LIVE_ACCURATE_SEEK_MARGIN_SEC for why shaving it there matters
    more than it does for a background one-shot job.
    """
    if accurate_seek_margin_sec is None:
        accurate_seek_margin_sec = _ASR_ACCURATE_SEEK_MARGIN_SEC
    start_sec = max(0.0, start_sec)
    coarse_sec = max(0.0, start_sec - accurate_seek_margin_sec)
    fine_sec = start_sec - coarse_sec
    try:
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
            )
        with _fast_disk_io_slot(f"asr_extract({os.path.basename(file_path)})"):
            result = subprocess.run(
                [_resolve_ffmpeg_path(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", f"{coarse_sec:.3f}", "-i", file_path,
                 "-ss", f"{fine_sec:.3f}",
                 "-t", f"{max(0.1, length_sec):.3f}",
                 "-vn", "-ac", "1", "-ar", "16000",
                 *(["-af", audio_filter] if audio_filter else []),
                 "-f", "wav", out_wav_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL, **popen_kwargs
            )
    except subprocess.TimeoutExpired:
        return "failed"
    except Exception as e:
        log.warning("[ASR] chunk extract error for %s: %s", os.path.basename(file_path), e)
        return "failed"
    try:
        size = os.path.getsize(out_wav_path) if os.path.isfile(out_wav_path) else 0
    except OSError:
        size = 0
    if size > 44:
        return "ok"
    return "empty" if result.returncode == 0 else "failed"


def _asr_cleanup_orphan_wavs(out_dir, max_age_sec=3600):
    """Remove temp wavs a hard kill left behind (only our own asr_*.wav)."""
    try:
        now = time.time()
        for name in os.listdir(out_dir):
            if name.startswith("asr_") and name.endswith(".wav"):
                p = os.path.join(out_dir, name)
                try:
                    if now - os.path.getmtime(p) > max_age_sec:
                        os.remove(p)
                except OSError:
                    pass
    except OSError:
        pass


def generate_captions_via_asr_chunked(file_path, start_sec=0.0, language=None,
                                      should_stop=None, timeout_extract=120):
    """Last-resort caption source: transcribe a plain video/audio file with
    the local faster-whisper model, in chunks, and return (srt_path, reason).

    Why chunked: the old version decoded and transcribed the WHOLE file
    before a single cue existed, so a feature-length title had no captions
    for many minutes. Here the file is transcribed in ~5 minute chunks
    (the first only 60 s) starting at `start_sec` -- the playback position --
    and running to the end, then wrapping around to fill 0 -> start_sec.
    After every chunk the .srt is rewritten atomically, and resolve_caption_
    source() reports the growing file (kind "asr_partial"), so the on-screen
    overlay picks up new cues as they land instead of waiting for the end.

    Resume: alongside the .srt is a small .asr.json record of which time
    ranges are done. If the app is closed mid-job (or should_stop fires), the
    next call for the same unchanged file skips every finished range and
    continues -- it never restarts from scratch. A changed file (mtime/size)
    discards the old partial.

    Return contract: srt_path is set ONLY when the whole file is done.
    Anything else returns (None, reason) with the partial progress kept on
    disk; see the ASR_REASON_* notes above for which reasons are permanent.

    should_stop: optional zero-arg callable, polled between chunks and
    while a chunk is being transcribed. When it returns True the job stops
    promptly (a chunk in flight is discarded, finished ones are kept) and
    returns (None, ASR_REASON_INTERRUPTED).

    Scope: plain file paths only. Real discs/ISOs opened as dvdnav:// have
    no single audio file to decode -- callers don't route those here.

    Call inside `with _asr_slot(...)` from a background worker so the
    one-job-at-a-time cap and live-playback yield apply to the whole job.
    """
    def _stopped():
        try:
            return bool(should_stop and should_stop())
        except Exception:
            return False

    if not file_path or not _cached_path_exists(file_path):
        return None, ASR_REASON_EXTRACT_FAILED
    try:
        st = os.stat(file_path)
    except OSError:
        return None, ASR_REASON_EXTRACT_FAILED
    srt_path, meta_path = _asr_paths(file_path)
    base_name = os.path.basename(file_path)

    # A finished transcript for this exact file needs no model at all.
    state = _asr_read_valid(file_path, st.st_mtime, st.st_size)
    if state and state["meta"].get("complete"):
        if state["srt_exists"]:
            return srt_path, ASR_REASON_SUCCESS
        return None, ASR_REASON_NO_SPEECH

    model = _get_whisper_model()
    if model is None:
        return None, ASR_REASON_UNAVAILABLE

    probe = _asr_probe_media(file_path)
    if probe is None or not probe["duration"]:
        return None, ASR_REASON_EXTRACT_FAILED
    if not probe["has_audio"]:
        return None, ASR_REASON_NO_AUDIO
    duration = probe["duration"]

    if state is None:
        _asr_discard_state(file_path)   # stale record for an older version of this file
        done, cues, audio_chunks = [], [], 0
    else:
        meta = state["meta"]
        done = _asr_merge_ranges(meta.get("done") or [])
        audio_chunks = int(meta.get("audio_chunks", 0) or 0)
        cues = []
        if state["srt_exists"]:
            cues = [[s / 1000.0, e / 1000.0, t] for s, e, t in parse_caption_cues(srt_path)]
        log.info("[ASR] resuming %s: %d range(s) already done, %d cues",
                 base_name, len(done), len(cues))

    lang = language if language is not None else _ASR_LANGUAGE
    out_dir = resolve_captions_dir()
    _asr_cleanup_orphan_wavs(out_dir)

    def _save(complete):
        ok = True
        if cues:
            ok = _atomic_write_text(srt_path, _asr_serialize_srt(cues))
        meta_out = {
            "version": _ASR_META_VERSION, "mtime": st.st_mtime, "size": st.st_size,
            "duration": duration, "model": _ASR_MODEL_SIZE, "done": done,
            "cue_count": len(cues), "audio_chunks": audio_chunks, "complete": bool(complete),
        }
        return ok and _atomic_write_text(meta_path, json.dumps(meta_out))

    plan = _asr_plan_chunks(duration, start_sec, done)
    total = len(plan)
    import tempfile
    for idx, (a, b) in enumerate(plan, 1):
        if _stopped():
            return None, ASR_REASON_INTERRUPTED
        _yield_to_live_playback(f"asr_chunk({base_name})")

        at_file_end = b >= duration - 0.5
        audio_end = duration if at_file_end else min(duration, b + _ASR_CHUNK_OVERLAP_SEC)
        wav_fd, wav_path = tempfile.mkstemp(prefix="asr_", suffix=".wav", dir=out_dir)
        os.close(wav_fd)
        t0 = time.monotonic()
        try:
            status = _asr_extract_chunk(file_path, a, audio_end - a, wav_path, timeout=timeout_extract)
            new_cues = []
            if status == "failed":
                log.info("[ASR] extract failed for %s at %.0fs", base_name, a)
                return None, ASR_REASON_EXTRACT_FAILED
            if status == "ok":
                audio_chunks += 1
                try:
                    segments = _asr_transcribe_chunk(model, wav_path, lang, _ASR_BEAM_SIZE,
                                                     initial_prompt=_asr_prompt_for(file_path),
                                                     should_stop=_stopped)
                    if _stopped():
                        return None, ASR_REASON_INTERRUPTED
                    new_cues, _drops = _asr_filter_cues(segments, a, b, at_file_end)
                except Exception as e:
                    log.warning("[ASR] transcription failed for %s: %s", base_name, e)
                    return None, ASR_REASON_TRANSCRIBE_FAILED
            # status == "empty": no audio in this span (past the real end of
            # the audio) -- counts as done, contributes no cues.
        finally:
            try:
                if os.path.isfile(wav_path):
                    os.remove(wav_path)
            except OSError:
                pass

        _asr_merge_cues(cues, new_cues)
        done = _asr_merge_ranges(done + [[a, b]])
        if not _save(complete=False):
            return None, ASR_REASON_TRANSCRIBE_FAILED
        log.info("[ASR] %s chunk %d/%d (%.0f-%.0fs): +%d cues (%d total) in %.1fs",
                 base_name, idx, total, a, b, len(new_cues), len(cues), time.monotonic() - t0)

    if audio_chunks == 0:
        # Every span came back empty -- the audio stream is unreadable, which
        # is not the same as "no speech". Leave it retryable.
        return None, ASR_REASON_EXTRACT_FAILED
    if not _save(complete=True):
        return None, ASR_REASON_TRANSCRIBE_FAILED
    if not cues:
        log.info("[ASR] no speech detected in %s", base_name)
        return None, ASR_REASON_NO_SPEECH
    log.info("[ASR] captions generated for %s (%d cues)", base_name, len(cues))
    return srt_path, ASR_REASON_SUCCESS


# ---------------------------------------------------------------------------
# Live, in-memory-only ASR captions (TV-station / visualizer overlay only).
#
# LiveCaptionSession transcribes forward from the current playback position in
# small chunks and keeps the cues in a plain in-memory list; nothing is written
# to disk. (The DVD player's captions don't use this -- they go through
# generate_captions_via_asr_chunked above.)
#
# Timing: everything is measured against the PLAYER'S OWN clock -- the
# position_fn the app passes in (VLC's get_time(), the same clock the overlay
# compares cue times against). A wall-clock estimate anchored at session start
# is only the fallback for when that clock is missing or not yet trustworthy
# (VLC reports ~0 until a seek lands). If transcription ever falls behind
# playback (slow chunk, CPU contention) the session skips ahead to the current
# position instead of grinding through audio that has already been heard.
# ---------------------------------------------------------------------------

# The overlay stays blank until the session is this far ahead of playback...
LIVE_CAPTION_LEAD_SEC = 5.0
# ...or this long after tune-in, whichever comes first (checked between chunks,
# so a slow chunk can overshoot it by up to one small chunk's time).
LIVE_CAPTION_READY_DEADLINE_SEC = 10.0
# Chunk length once comfortably ahead, and the short chunk used while building
# the initial lead or recovering from a stall (bounds worst-case latency).
# Long chunks (this LIVE_CAPTION_CHUNK_SEC=30s) mean fewer chunk *boundaries*
# per minute of audio -- and boundaries are exactly where the merge logic
# (_asr_merge_cues / _asr_cues_are_same_speech) has to guess whether an
# overlapping cue is a duplicate re-transcription or a genuinely new line,
# which is where both the dropped-line and duplicate-line bugs live. Fewer
# boundaries, fewer chances for that guess to go wrong. But a 30s chunk's
# cues only become visible to the overlay all at once, the moment the whole
# chunk finishes transcribing -- risking one this long before there's a
# comfortable lead banked would mean a long, avoidable stretch of dead air
# on tune-in. So a session starts on short _LIVE_CAPTION_STARTUP_CHUNK_SEC
# chunks (gets *something* on screen fast) and only moves up to full
# LIVE_CAPTION_CHUNK_SEC chunks once it has measured (from its own recent
# chunks, not a guess) that it's comfortably ahead enough for a chunk that
# size to finish before its audio is due on screen -- see
# _live_caption_use_big_chunk() in _run_locked. If it ever falls behind
# again (a stall, a dense-dialogue slowdown), it drops back to short chunks
# to rebuild lead, then climbs back to long ones once it's earned it again.
LIVE_CAPTION_CHUNK_SEC = 30.0
_LIVE_CAPTION_STARTUP_CHUNK_SEC = 10.0
# The very FIRST chunk of a tune-in is shorter still. It has to be extracted +
# transcribed while playback keeps moving, so a 10s first chunk (~4.5s of compute
# measured in the logs) lands after most of its own audio has already played --
# real logs showed every cue in it already 5-10s stale on arrival. 5s finishes
# in roughly half the time, so its text lands while still (mostly) ahead of
# playback. Only used for an attached (actually playing) session's first chunk.
_LIVE_CAPTION_FIRST_CHUNK_SEC = 5.0
# Every live chunk also transcribes extra audio PAST its end (_ASR_CHUNK_OVERLAP_SEC,
# 8s) so a sentence straddling the boundary isn't cut off -- but cues that start in
# that tail are thrown away (the next chunk redoes them). On a 5s first chunk that
# meant transcribing 13s of audio and discarding most of the extra. For the short
# startup chunks, where latency matters most, use a smaller tail; long chunks keep
# the full 8s (the tail is a small fraction of their length there).
_LIVE_CAPTION_FIRST_CHUNK_OVERLAP_SEC = 3.0
_LIVE_CAPTION_STARTUP_CHUNK_OVERLAP_SEC = 5.0
# The first chunk starts this far AHEAD of playback, not at it. Extract + transcribe
# takes ~3s, during which playback moves on, so a chunk that starts at "now" lands
# with its cues already expired (measured: 2-6s stale on arrival). Starting ~3s
# ahead means its cues land right as they're due -- the first ~3s of dialogue after
# tune-in can't be captioned in time either way.
_LIVE_CAPTION_FIRST_CHUNK_AHEAD_SEC = 3.0
# --- Live session vs. next-file prefetch fairness -------------------------
# There is ONE transcription slot (_asr_slot). A next-file prefetch session
# running 30s chunks can hold it for 20+ seconds on dense dialogue (measured:
# 22s), and the live session's next chunk then waits behind it and lands
# seconds late -- that is missing text on screen. So a prefetch session:
#   * only starts a chunk while every attached (actually playing) session is
#     comfortably ahead (>= _LIVE_CAPTION_PREFETCH_MIN_LEAD_SEC) and idle, and
#   * uses short chunks so it can never hold the slot for long.
_LIVE_CAPTION_PREFETCH_MIN_LEAD_SEC = 15.0
_LIVE_CAPTION_PREFETCH_CHUNK_SEC = 10.0
_LIVE_CAPTION_PREFETCH_MAX_WAIT_SEC = 120.0
_LIVE_ATTACHED_SESSIONS = set()


def _prefetch_may_run():
    """True when no attached live session needs the transcription slot."""
    for sess in list(_LIVE_ATTACHED_SESSIONS):
        if sess._stopped():
            _LIVE_ATTACHED_SESSIONS.discard(sess)
            continue
        if sess._busy or sess._last_lead < _LIVE_CAPTION_PREFETCH_MIN_LEAD_SEC:
            return False
    return True
# Extra cushion added on top of the *measured* time the session's own
# chunks have been taking, before trusting that a full LIVE_CAPTION_CHUNK_SEC
# chunk will finish before playback needs its text -- guards against the
# next chunk being slower than the last few (denser dialogue, a momentary
# CPU spike) rather than betting the exact average will hold.
_LIVE_CAPTION_BIG_CHUNK_SAFETY_MARGIN_SEC = 3.0
# Conservative up-front guess for how long a full LIVE_CAPTION_CHUNK_SEC
# chunk will take, used only until the session has actually measured one
# of its own chunks (see self._chunk_rtf_ema) -- deliberately on the high
# side (real production timings on 8s chunks ranged as high as 14.4s on
# dense dialogue; 30s of denser content could run well past that) so nothing
# gambles on an untested chunk size before there's real data to trust.
_LIVE_CAPTION_BIG_CHUNK_DEFAULT_ESTIMATE_SEC = 15.0
# Don't transcribe further ahead of playback than this (idle instead).
LIVE_CAPTION_MAX_LEAD_SEC = 90.0
_LIVE_CAPTION_PACE_POLL_SEC = 1.0
# If playback has passed our position by more than SKIP_BEHIND, give up on
# transcribing the untranscribed stretch in between and resume RESUME_AHEAD
# seconds in front of playback instead (enough for the next chunk to land in
# time). This is a deliberate, PERMANENT loss of whatever dialogue fell in
# that stretch -- it is never transcribed, so it can never appear -- so the
# threshold has to be well above the lag a single slow chunk can produce on
# its own, or it fires on ordinary transcription hiccups and eats real,
# audible speech for no reason. Logged first-chunk times up to ~12-17s
# (system load, model warm-up, a busy CPU moment) were measured pushing lead
# to -3s to -7s on nothing more than a normal 5-8s chunk -- comfortably past
# the old 1.0s threshold, so those chunks' dialogue was being discarded
# outright even though the session was never more than a few seconds off.
# This diagnosed the "same speaker's lines cut in and out" symptom: it
# wasn't the speech going undetected, it was already-heard-fine audio being
# thrown away before it was ever given the chance to transcribe. 15s gives
# real transient slowdowns room to catch up on their own (the small-chunk
# pacing below already biases toward that once lead < LIVE_CAPTION_LEAD_SEC)
# while still catching genuine fell-way-behind cases (tuning into a show
# already deep in, a multi-minute stall) that can never be shown in sync
# again regardless.
_LIVE_CAPTION_SKIP_BEHIND_SEC = 15.0
_LIVE_CAPTION_RESUME_AHEAD_SEC = 2.0
# Cues are dropped this long after they've been shown; hard cap as a safety net.
_LIVE_CAPTION_KEEP_PAST_SEC = 20.0
_LIVE_CAPTION_MAX_CUES = 500
# Consecutive extract/transcribe failures tolerated before the session gives up.
_LIVE_CAPTION_MAX_FAILURES = 3
# Live-only speed choices (the disk-cached path keeps _ASR_BEAM_SIZE and the
# larger seek margin, since it runs once in the background and is kept).
_ASR_LIVE_BEAM_SIZE = 3
_ASR_LIVE_ACCURATE_SEEK_MARGIN_SEC = 4.0
# A player-clock reading is trusted once it has advanced in step with real time
# between two polls at least this far apart (within this tolerance).
_LIVE_CAPTION_CLOCK_MIN_DT = 0.75
_LIVE_CAPTION_CLOCK_TOLERANCE = 0.4


class LiveCaptionSession:
    """Transcribes `file_path` forward from `start_sec` on a background thread.

    Call `.start()` once, `.stop()` on channel change, and read `.ready` /
    `.snapshot()` from any thread. `cues` is only mutated under `.lock`.
    `.ready` flips True once the session is LIVE_CAPTION_LEAD_SEC ahead of
    playback (or LIVE_CAPTION_READY_DEADLINE_SEC after tune-in with at least
    one chunk done); before that the overlay should show nothing.

    position_fn: optional zero-arg callable returning the player's current
    position in file seconds (or None). See the module comment above.
    """

    def __init__(self, file_path, start_sec=0.0, language=None, position_fn=None):
        self.file_path = file_path
        self.start_sec = max(0.0, float(start_sec or 0.0))
        self.language = language if language is not None else _ASR_LANGUAGE
        self.position_fn = position_fn
        self.cues = []          # [[start_sec, end_sec, text], ...], absolute file time, sorted
        self.lock = threading.Lock()
        self.ready = False
        self.status = None      # ASR_REASON_* once the job ends/gives up, else None
        self._stop = threading.Event()
        self._thread = None
        self._created = time.monotonic()
        # Wall-clock estimate of playback position: anchor_pos at anchor_mono.
        self._anchor_mono = self._created
        self._anchor_pos = self.start_sec
        self._clock_probe = None    # (monotonic, position) of the last spaced-out poll
        self._clock_trusted = False
        # True once this session is tied to a file that's actually playing
        # (position_fn given at construction, or attach_playback() called
        # later for a prefetched session once its file goes live). Before
        # that there is no real playback to be "behind" or "ahead" of --
        # see _run_locked's lead calculation and _trim_cues.
        self._attached = position_fn is not None
        # One-shot: set by attach_playback so the loop below doesn't mistake
        # a prefetch session's legitimately large built-up lead, right at
        # the moment it's promoted to real playback, for a backward seek.
        self._just_attached = False
        # Rolling estimate of seconds-of-compute per second-of-audio,
        # updated after each completed chunk (see _run_locked) -- used to
        # decide when there's enough measured throughput to safely move up
        # to LIVE_CAPTION_CHUNK_SEC chunks. None until the session has a
        # real measurement to go on (the very first chunk is excluded from
        # ever updating this -- see _run_locked -- since its timing is
        # confounded by one-time model warm-up, not steady-state speed).
        self._chunk_rtf_ema = None
        # Fairness bookkeeping read by _prefetch_may_run() (see above).
        self._busy = False              # True from "about to take the ASR slot" to "chunk done"
        self._last_lead = 0.0           # lead over playback at the top of the last loop pass

    def start(self):
        if self._attached:
            _LIVE_ATTACHED_SESSIONS.add(self)
        self._thread = threading.Thread(target=self._run, daemon=True, name="LiveCaptionASR")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def snapshot(self):
        """Cues in MILLISECONDS ([start_ms, end_ms, text]), the unit the overlay
        and parse_caption_cues use; internally they are kept in seconds."""
        with self.lock:
            return [[s * 1000.0, e * 1000.0, t] for s, e, t in self.cues]

    def attach_playback(self, position_fn, file_pos=0.0):
        """Give a session that was started ahead of time (next-file prefetch) the
        player clock once its file actually goes live, re-anchoring the
        wall-clock estimate to `file_pos`."""
        self.position_fn = position_fn
        self._anchor_mono = time.monotonic()
        self._anchor_pos = max(0.0, float(file_pos or 0.0))
        self._clock_probe = None
        self._clock_trusted = False
        self._attached = True
        self._just_attached = True
        _LIVE_ATTACHED_SESSIONS.add(self)

    def _stopped(self):
        return self._stop.is_set()

    def _playback_sec(self):
        """Current playback position in file seconds. Uses position_fn once its
        readings advance in step with real time; otherwise the wall-clock
        estimate (which also covers VLC reporting ~0 before a seek lands)."""
        mono = time.monotonic()
        est = self._anchor_pos + (mono - self._anchor_mono)
        fn = self.position_fn
        if fn is None:
            return est
        try:
            p = fn()
        except Exception:
            p = None
        if p is None or p < 0:
            return est
        if self._clock_probe is None:
            self._clock_probe = (mono, p)
        else:
            probe_mono, probe_p = self._clock_probe
            dt = mono - probe_mono
            if dt >= _LIVE_CAPTION_CLOCK_MIN_DT:
                self._clock_trusted = abs((p - probe_p) - dt) <= _LIVE_CAPTION_CLOCK_TOLERANCE
                self._clock_probe = (mono, p)
        if self._clock_trusted:
            self._anchor_mono, self._anchor_pos = mono, p
            return p
        return est

    def _trim_cues(self):
        if not self._attached:
            # Prefetching ahead of a file that isn't live yet -- nothing has
            # been shown, so nothing is "stale". Just keep memory bounded.
            with self.lock:
                if len(self.cues) > _LIVE_CAPTION_MAX_CUES:
                    del self.cues[:len(self.cues) - _LIVE_CAPTION_MAX_CUES]
            return
        cutoff = self._playback_sec() - _LIVE_CAPTION_KEEP_PAST_SEC
        with self.lock:
            self.cues[:] = [c for c in self.cues if c[1] >= cutoff]
            if len(self.cues) > _LIVE_CAPTION_MAX_CUES:
                del self.cues[:len(self.cues) - _LIVE_CAPTION_MAX_CUES]

    def _run(self):
        try:
            self._run_locked()
        except Exception as e:
            log.warning("[LIVE ASR] session failed for %s: %s",
                        os.path.basename(self.file_path), e)
            self.status = ASR_REASON_TRANSCRIBE_FAILED
        finally:
            _LIVE_ATTACHED_SESSIONS.discard(self)

    def _run_locked(self):
        import tempfile

        if self._stopped():
            return
        model = _get_whisper_model()
        if model is None:
            self.status = ASR_REASON_UNAVAILABLE
            return
        probe = _asr_probe_media(self.file_path)
        if probe is None or not probe["duration"]:
            self.status = ASR_REASON_EXTRACT_FAILED
            return
        if not probe["has_audio"]:
            self.status = ASR_REASON_NO_AUDIO
            return
        duration = probe["duration"]
        base_name = os.path.basename(self.file_path)
        prompt = _asr_prompt_for(self.file_path)

        pos = min(self.start_sec, duration)
        chunks_done = 0
        failures = 0
        while not self._stopped() and pos < duration:
            _yield_to_live_playback(f"live_caption({base_name})")

            if not self._attached:
                # Prefetching ahead of a file that isn't live yet -- there is
                # no real playback to be behind or ahead of, so just keep
                # transcribing forward (the sentinel below reads as "plenty
                # of lead" for the ready-flag and small-chunk checks further
                # down). Real lead bookkeeping resumes once attach_playback()
                # ties this session to the file that's actually playing.
                now, lead = None, LIVE_CAPTION_MAX_LEAD_SEC
            else:
                now = self._playback_sec()
                lead = pos - now
                just_attached, self._just_attached = self._just_attached, False
                # Only treat the session as genuinely behind once it has done
                # at least one chunk of real work -- on the very first chunk,
                # `lead` reflects startup latency (model load, or waiting for
                # the single ASR job slot to free up), not a throughput
                # problem, and skipping ahead here would discard speech
                # nothing has even tried to transcribe yet.
                if chunks_done > 0 and lead < -_LIVE_CAPTION_SKIP_BEHIND_SEC:
                    # Behind playback: the audio before `now` has already been heard.
                    new_pos = min(duration, now + _LIVE_CAPTION_RESUME_AHEAD_SEC)
                    log.info("[LIVE ASR] %s behind playback by %.1fs -- skipping ahead to %.1fs",
                             base_name, -lead, new_pos)
                    pos = new_pos
                    if pos >= duration:
                        break
                    lead = pos - now
                elif chunks_done > 0 and not just_attached and lead > LIVE_CAPTION_MAX_LEAD_SEC + 30.0:
                    pos, lead = now, 0.0   # playback jumped backward -- restart from it
                    # (a big lead right after attach_playback() is a prefetch
                    # session's legitimate head start, not a backward seek --
                    # just_attached suppresses this check for that one check.)

            if chunks_done == 0 and self._attached and now is not None:
                _first_pos = min(duration, now + _LIVE_CAPTION_FIRST_CHUNK_AHEAD_SEC)
                if pos < _first_pos:
                    log.info("[LIVE ASR] %s first chunk was set to start %.1fs %s playback -- "
                             "starting at %.1fs (playback %.1fs + %.1fs ahead) so its cues land in time",
                             base_name, abs(lead), "behind" if lead < 0 else "ahead of",
                             _first_pos, now, _LIVE_CAPTION_FIRST_CHUNK_AHEAD_SEC)
                    pos = _first_pos
                    lead = pos - now
                    if pos >= duration:
                        break

            self._last_lead = lead if self._attached else LIVE_CAPTION_MAX_LEAD_SEC

            if not self.ready and (
                    chunks_done > 0
                    or lead >= LIVE_CAPTION_LEAD_SEC
                    or (chunks_done > 0
                        and time.monotonic() - self._created >= LIVE_CAPTION_READY_DEADLINE_SEC)):
                # chunks_done > 0: go live the moment the first chunk has landed.
                # Waiting for a 5s lead (or the 10s deadline, which is only checked
                # between chunks) held captions back ~10-15s after every tune-in even
                # though cues that arrive in time display correctly on their own.
                self.ready = True
                log.info("[LIVE ASR READY] %s captions live %.1fs after session start "
                         "(chunks_done=%d lead=%s)", base_name,
                         time.monotonic() - self._created, chunks_done,
                         ("n/a" if now is None else f"{lead:.1f}s"))
            if self.ready and self._attached:
                while not self._stopped() and lead > LIVE_CAPTION_MAX_LEAD_SEC:
                    self._stop.wait(_LIVE_CAPTION_PACE_POLL_SEC)
                    lead = pos - self._playback_sec()
                if self._stopped():
                    break

            small = (not self.ready) or lead < LIVE_CAPTION_LEAD_SEC
            # Chunk-size decision, independent of the display-readiness
            # decision just above: even once captions are showing, don't
            # jump to a full LIVE_CAPTION_CHUNK_SEC chunk until the
            # session's own measured throughput says one will finish with
            # room to spare before playback needs it. est_big_chunk_sec is
            # that measurement (or the conservative default before any
            # chunk has been measured); use_big_chunk is the actual
            # go/no-go for *this* chunk.
            est_big_chunk_sec = (self._chunk_rtf_ema * LIVE_CAPTION_CHUNK_SEC
                                  if self._chunk_rtf_ema is not None
                                  else _LIVE_CAPTION_BIG_CHUNK_DEFAULT_ESTIMATE_SEC)
            use_big_chunk = (not small) and lead >= (
                est_big_chunk_sec + _LIVE_CAPTION_BIG_CHUNK_SAFETY_MARGIN_SEC)
            _target_len = LIVE_CAPTION_CHUNK_SEC if use_big_chunk else _LIVE_CAPTION_STARTUP_CHUNK_SEC
            if chunks_done == 0 and self._attached:
                _target_len = _LIVE_CAPTION_FIRST_CHUNK_SEC
            elif not self._attached:
                _target_len = _LIVE_CAPTION_PREFETCH_CHUNK_SEC   # never hog the slot
            length = min(_target_len, duration - pos)
            at_file_end = (pos + length) >= duration - 0.5
            if chunks_done == 0 and self._attached:
                _overlap = _LIVE_CAPTION_FIRST_CHUNK_OVERLAP_SEC
            elif not self._attached or not use_big_chunk:
                _overlap = _LIVE_CAPTION_STARTUP_CHUNK_OVERLAP_SEC
            else:
                _overlap = _ASR_CHUNK_OVERLAP_SEC
            audio_end = duration if at_file_end else min(duration, pos + length + _overlap)

            if not self._attached:
                # Next-file prefetch: wait until no playing session needs the slot.
                _pf_deadline = time.monotonic() + _LIVE_CAPTION_PREFETCH_MAX_WAIT_SEC
                while (not self._stopped() and not _prefetch_may_run()
                       and time.monotonic() < _pf_deadline):
                    self._stop.wait(0.25)
                if self._stopped():
                    break
            self._busy = True
            t0 = time.monotonic()
            _slot_t0 = t0   # reset once the slot is actually held
            took = None  # set once actual work happens; guards the EMA update below
            wav_fd, wav_path = tempfile.mkstemp(prefix="live_asr_", suffix=".wav")
            os.close(wav_fd)
            try:
                # The ASR job slot is held only for this one chunk's actual
                # extract+transcribe work -- not for the idle pacing wait
                # above. A session that's comfortably ahead and just waiting
                # for playback to need more shouldn't keep another session
                # (e.g. a next-file prefetch) from using that CPU meanwhile.
                with _asr_slot(f"live_caption({base_name})"):
                    _slot_t0 = time.monotonic()
                    status = _asr_extract_chunk(self.file_path, pos, audio_end - pos, wav_path,
                                                accurate_seek_margin_sec=_ASR_LIVE_ACCURATE_SEEK_MARGIN_SEC,
                                                audio_filter=_ASR_LIVE_AUDIO_FILTER)
                    if status == "failed":
                        failures += 1
                        if failures >= _LIVE_CAPTION_MAX_FAILURES:
                            self.status = ASR_REASON_EXTRACT_FAILED
                            return
                        pos += length
                        continue
                    if status == "ok" and not self._stopped():
                        try:
                            segments = _asr_transcribe_chunk(
                                model, wav_path, self.language, _ASR_LIVE_BEAM_SIZE,
                                initial_prompt=prompt, temperature=0.0, should_stop=self._stopped)
                        except Exception as e:
                            log.warning("[LIVE ASR] transcription failed for %s: %s", base_name, e)
                            failures += 1
                            if failures >= _LIVE_CAPTION_MAX_FAILURES:
                                self.status = ASR_REASON_TRANSCRIBE_FAILED
                                return
                            pos += length
                            continue
                        if self._stopped():
                            return
                        failures = 0
                        new_cues, drops = _asr_filter_cues(segments, pos, pos + length, at_file_end)
                        if new_cues:
                            with self.lock:
                                _asr_merge_cues(self.cues, new_cues)
                            # Diagnostic for the "flashes then gets overtaken" symptom:
                            # a cue's start/end timestamps are exact (they come straight
                            # from Whisper's own word timing), but they're only checked
                            # against the on-screen clock once this whole chunk has
                            # finished transcribing and landed here. If real playback has
                            # already moved past a cue's start by the time it arrives,
                            # that cue's on-screen window is already partly or fully gone
                            # -- this measures exactly how much, in real seconds, per cue.
                            if self._attached:
                                arrival = self._playback_sec()
                                stale = [max(0.0, arrival - c[0]) for c in new_cues]
                                log.info(
                                    "[LIVE ASR STALE] %s %d cue(s) landed %.1fs into playback; "
                                    "already-elapsed-on-arrival min=%.1fs max=%.1fs avg=%.1fs",
                                    base_name, len(new_cues), arrival,
                                    min(stale), max(stale), sum(stale) / len(stale))
                        # took = compute time only. Time spent WAITING for the slot
                        # (behind another session) is reported separately so it can't
                        # inflate the throughput estimate that picks chunk sizes.
                        took = time.monotonic() - _slot_t0
                        _slot_wait = _slot_t0 - t0
                        log.info(
                            "[LIVE ASR] %s pos=%.1fs len=%.1fs audio=%.1fs took=%.1fs wait=%.1fs lead=%s ready=%s "
                            "chunk=%s(est_30s=%.1fs) vad=%s islands/%.1fs speech cues=+%d "
                            "dropped(next=%d empty=%d not_speech=%d)",
                            base_name, pos, length, (audio_end - pos), took, _slot_wait,
                            ("n/a" if now is None else f"{lead:.1f}s"), self.ready,
                            ("BIG" if use_big_chunk else ("first" if (chunks_done == 0 and self._attached) else "startup")), est_big_chunk_sec,
                            getattr(segments, "vad_islands", "?"), getattr(segments, "vad_speech_sec", 0.0),
                            len(new_cues), drops["next_chunk"], drops["empty"], drops["not_speech"])
            finally:
                self._busy = False
                try:
                    if os.path.isfile(wav_path):
                        os.remove(wav_path)
                except OSError:
                    pass

            # Fold this chunk's throughput into the rolling estimate used
            # to decide when it's safe to move up to LIVE_CAPTION_CHUNK_SEC
            # chunks -- but not from the very first chunk (chunks_done==0
            # here, before the increment below), whose timing is skewed by
            # one-time model warm-up rather than steady-state speed (see
            # the _LIVE_CAPTION_SKIP_BEHIND_SEC comment above for the same
            # 12-17s warm-up effect measured elsewhere). An EMA (rather
            # than a running average) so the estimate can still track a
            # real, sustained slowdown instead of being permanently
            # anchored to however fast the first few chunks happened to be.
            if took is not None and chunks_done > 0 and length > 0:
                _measured_ratio = took / length
                self._chunk_rtf_ema = (
                    _measured_ratio if self._chunk_rtf_ema is None
                    else 0.4 * _measured_ratio + 0.6 * self._chunk_rtf_ema)

            chunks_done += 1
            pos += length
            self._trim_cues()

        if not self._stopped():
            self.status = ASR_REASON_SUCCESS


class NoAudioStreamError(Exception):
    def __init__(self, file_path, reason="no audio track"):
        super().__init__(file_path)
        self.file_path = file_path
        self.reason = reason


class UnplayableMediaError(Exception):
    def __init__(self, file_path, reason="confirmed decode failure"):
        super().__init__(file_path)
        self.file_path = file_path
        self.reason = reason


class LoudnessProbeTimeoutError(Exception):
    def __init__(self, file_path):
        super().__init__(file_path)
        self.file_path = file_path


# ---------------------------------------------------------------------------
# check_media_truly_ended() -- shared debounce + duration-proximity guard
# against premature/spurious end-of-media reads.
#
# Shared "has this file actually finished?" check, usable by any VLC-backed
# engine (main TV playback today; DVD/other engines later) instead of each
# one duplicating the same debounce/proximity logic. media.py never imports
# vlc, so this function takes a plain state string plus the two numbers it
# actually needs (position/length) rather than a vlc.State or a player
# handle -- see VLCEngineWrapper.get_current_time() and
# .get_playback_state() in retro_tv_emulator.py for the two thin reads that
# feed it.
#
# Why this exists (the bug it fixes): VLC can report State.Ended/Stopped for
# a single frame as a momentary glitch (very common right around a channel-
# switch/guide-preview transition), and separately can report a terminal
# state spuriously while the file is still genuinely mid-playback. Trusting
# either blip immediately triggers a full reload/advance -- which is what
# shows up on screen as a brief flash to static and back ("the flicker").
# Two independent guards, applied in order, are what actually suppress that:
#   1. Debounce -- require the terminal state to persist across a short
#      real-time window (debounce_ms) before it's trusted at all, so a
#      single-frame glitch resolves itself with no action taken.
#   2. Duration-proximity cross-check -- once the debounce passes, compare
#      the engine's OWN reported position against its OWN reported length
#      (not the scheduler's separate duration estimate) and only treat the
#      terminal state as a real finish if playback is actually near the end.
#      If the length isn't known yet, there's nothing to cross-check against,
#      so this step is skipped and the debounce-only result stands.
# A real State.Error is treated as reliable on its own (no proximity check
# makes sense for a decode failure), but still waits out min_play_time_ms so
# a file doesn't get marked broken before VLC has even had a chance to open
# it.
# ---------------------------------------------------------------------------
def check_media_truly_ended(
    state,
    pos_ms,
    length_ms,
    now_ms,
    last_play_started_at_ms,
    tracking,
    min_play_time_ms=1500,
    debounce_ms=200,
    proximity_ms=2000,
):
    """Decide whether a terminal engine read really means the current file
    has finished, vs. a spurious mid-playback state glitch.

    Args:
        state: one of "playing", "paused", "ended", "stopped", "error",
            "opening", "buffering", "other" -- see
            VLCEngineWrapper.get_playback_state().
        pos_ms: current playback position in ms, or None if unknown -- see
            VLCEngineWrapper.get_current_time().
        length_ms: known media length in ms, or None if not yet known.
        now_ms: current tick clock (e.g. pygame.time.get_ticks()); just
            needs to be monotonic and in the same units as
            last_play_started_at_ms.
        last_play_started_at_ms: tick time this file started playing.
        tracking: dict the CALLER owns and persists across calls for this
            one engine/player (e.g. VLCEngineWrapper._media_end_tracking) --
            mutated in place. A fresh {} is a valid starting point. Keys:
              _media_end_check_start_time -- tick when the current terminal-
                  state streak began; 0 while not currently debouncing.
              _media_end_confirmed -- True once the current streak has been
                  confirmed a real finish (lets a caller polling every frame
                  tell "just confirmed this frame" apart from "already
                  handled").
              _media_end_debounce_time -- the debounce_ms value in effect,
                  recorded for inspection/testing.
        min_play_time_ms: a file must have been playing at least this long
            before ANY terminal read (including Error) is trusted -- guards
            against acting on a state read from before playback genuinely
            started.
        debounce_ms: how long an Ended/Stopped read must persist, in real
            time, before it's cross-checked at all.
        proximity_ms: how close to the end (in ms) playback position must be
            for a debounced Ended/Stopped read to be trusted as a real
            finish. If length or position isn't known yet, proximity can't
            be evaluated and is treated as NOT near the end -- unknown is
            never assumed to mean "about to finish". In that case a finish
            can still be confirmed, but only via the separate stuck-streak
            fallback (a terminal read held continuously for well over a
            couple of debounce/retry cycles), not from min_play_time_ms
            alone.

    Returns:
        (is_finished, reason) -- reason is "ended", "error", or None.
        "stalled" is deliberately never returned here: Opening/Buffering
        stall detection is a separate, much-longer-timeout watchdog that
        stays with the caller (this function only clears the debounce for
        those states so a later, unrelated terminal read starts its own
        fresh window).
    """
    tracking["_media_end_debounce_time"] = debounce_ms

    if state == "error":
        tracking["_media_end_check_start_time"] = 0
        if now_ms - last_play_started_at_ms > min_play_time_ms:
            tracking["_media_end_confirmed"] = True
            return True, "error"
        tracking["_media_end_confirmed"] = False
        return False, None

    if state in ("ended", "stopped"):
        _start = tracking.get("_media_end_check_start_time", 0)
        # _media_end_glitch_since_ms tracks how long the engine has been
        # continuously sitting in a terminal (ended/stopped) read, INCLUDING
        # across the "not near end -> reset and retry" branch below. That
        # branch's own reset of _media_end_check_start_time used to also
        # wipe this history, so a file whose recorded/probed duration is
        # simply wrong (longer than the file actually plays -- exactly what
        # the commercial-pool duration correction exists to fix) could never
        # be trusted as finished: every debounce window would see "not near
        # the (wrong) known length" and reset, forever, even though the
        # underlying engine event is a perfectly real, one-time Ended read
        # with nothing left to decode. That silently starved the fast
        # internal-advance path (change_channel's internal_advance flag,
        # which suppresses the transition burst) of ever firing for such
        # files, leaving only the much slower wall-clock schedule-vs-loaded
        # -file mismatch check to eventually notice and reload -- a real,
        # visible black/frozen gap between this file ending and the next
        # one starting.
        if not _start:
            tracking["_media_end_check_start_time"] = now_ms
            tracking.setdefault("_media_end_glitch_since_ms", now_ms)
            tracking["_media_end_confirmed"] = False
            return False, None
        if now_ms - _start >= debounce_ms:
            if now_ms - last_play_started_at_ms > min_play_time_ms:
                # Default to False, not True, when length/position aren't
                # known yet. "Unknown" must never be treated as "near the
                # end" -- it means we can't verify either way, so the ONLY
                # thing allowed to confirm a finish in that case is the
                # _stuck_ms persistence fallback below, which requires the
                # terminal state to hold for a real stretch of wall-clock
                # time rather than a single min_play_time_ms window.
                #
                # This matters most for length-unknown loads: length_ms is
                # populated by an async VLC LengthChanged event (see the
                # engine's _on_length_changed), and is explicitly reset to
                # "unknown" immediately before every play() call. A load
                # that gets superseded/retried faster than that event can
                # land -- e.g. under heavy concurrent thread/disk load --
                # would otherwise have "near end" default to True and get
                # confirmed "finished" the instant min_play_time_ms elapses,
                # regardless of the file's real length or position. That
                # false confirmation then triggers an immediate advance to
                # the next file, which resets length again and repeats,
                # producing a self-sustaining loop that "finishes" a new
                # file every ~min_play_time_ms with nothing ever actually
                # playing. Requiring genuine position/length data (or a
                # sustained multi-second stuck streak) for a confirmed
                # finish breaks that loop at the source.
                _near_end = False
                if length_ms and length_ms > 0 and pos_ms is not None and pos_ms >= 0:
                    _near_end = pos_ms >= (length_ms - proximity_ms)
                _glitch_since = tracking.get("_media_end_glitch_since_ms") or now_ms
                _stuck_ms = now_ms - _glitch_since
                if _near_end or _stuck_ms >= max(1500, debounce_ms + proximity_ms):
                    # Either genuinely near the known length, OR the engine
                    # has now reported this same terminal state continuously
                    # for long enough (well past a couple of debounce/retry
                    # cycles) that it can no longer be a one-off glitch --
                    # trust the real engine event over a stale/optimistic
                    # recorded duration rather than looping on it forever.
                    tracking["_media_end_confirmed"] = True
                    tracking["_media_end_glitch_since_ms"] = 0
                    return True, "ended"
                # Debounce passed but position isn't actually near the end,
                # and we haven't been stuck here long enough yet to rule out
                # a transient glitch -- reset the debounce (but NOT the
                # glitch-since timer) and give the engine another window to
                # self-recover.
                tracking["_media_end_check_start_time"] = 0
                tracking["_media_end_confirmed"] = False
                return False, None
        tracking["_media_end_confirmed"] = False
        return False, None

    # Opening/Buffering/Playing/Paused/other -- healthy, or a separate
    # watchdog's concern (stall timeout on Opening/Buffering). Either way,
    # clear any in-progress debounce (and the glitch-since timer added
    # above) so a later, unrelated terminal read starts its own fresh
    # window instead of inheriting this one.
    tracking["_media_end_check_start_time"] = 0
    tracking["_media_end_glitch_since_ms"] = 0
    tracking["_media_end_confirmed"] = False
    return False, None


LOUDNESS_GAIN_CEILING = 10.0
# LOUDNESS_GAIN_FLOOR (-20dB) is the absolute last-resort floor, used ONLY
# where a cut is being driven by true-peak safety (see _peak_floor in
# get_media_loudness_gain/get_pair_loudness_gain, and the peak-capped path
# in _converge_clamped_gain below) -- i.e. the file would otherwise clip
# against TRUE_PEAK_CEILING_DBTP. This is the actual safety "ceiling of
# loudness" and is never loosened by LOUDNESS_GAIN_FLOOR_STANDARD below.
LOUDNESS_GAIN_FLOOR = 0.1
# LOUDNESS_GAIN_FLOOR_STANDARD (-6dB) is the floor for an ordinary
# LUFS-driven cut -- a file that simply measured louder than TARGET_LUFS,
# with no true-peak risk involved. Deliberately looser than
# LOUDNESS_GAIN_FLOOR: between a file ending up a little quieter than
# target and a little louder, the app is meant to lean loud -- a file that
# measures very hot no longer gets attenuated all the way down to
# TARGET_LUFS, only down to this floor. True-peak safety is a completely
# separate clamp (see LOUDNESS_GAIN_FLOOR above) and is NOT affected by
# this constant, so raising/loosening this floor never lets a file exceed
# TRUE_PEAK_CEILING_DBTP. Tune this number up (closer to 1.0) to lean even
# louder, or down (closer to LOUDNESS_GAIN_FLOOR) to normalize harder --
# it's the one knob for that trade-off.
LOUDNESS_GAIN_FLOOR_STANDARD = 0.5

# Music-only attenuation floor (-20dB). Music is mastered far hotter than
# TV/movie audio (typically -8 to -15 LUFS integrated, vs MUSIC_TARGET_LUFS
# of -24), so the ordinary -6dB floor above pinned essentially every track
# at x0.50 -- i.e. nothing was normalized, and tracks landed anywhere from
# ~3 to ~7dB over target depending on how hot their master was. This lets
# a loud track actually be pulled down to target. Only cuts are affected;
# boosts, the true-peak clamp, and every non-music path are unchanged.
MUSIC_GAIN_FLOOR = LOUDNESS_GAIN_FLOOR
# Probing always clamps against this fixed, symmetric ceiling (+-20dB)
# regardless of the Loudness Scan toggle -- that toggle only controls
# whether the resulting gain gets applied at playback time (see
# set_volume in retro_tv_emulator.py), not how probing/prep computes it.

# Quiet, hot-peaked clips (commercials especially) often get their boost
# capped short by true-peak headroom -- sometimes floored all the way back
# to gain=1.0, i.e. no boost applied at all (see _peak_floor in
# get_media_loudness_gain/get_pair_loudness_gain below). Meanwhile clips
# that started out loud attenuate down to TARGET_LUFS cleanly, since
# cutting gain never runs into the peak ceiling. Back to back, that reads
# as an audible quiet/loud/quiet/loud alternation even though every clip
# individually "reached" its best achievable level.
#
# LOUDNESS_CONVERGENCE_DB narrows that gap from both sides: a peak-capped
# boost gets rescued a little further (a bit more true-peak risk, in
# exchange for not being left at its original quiet level), and a clean
# attenuation gets pulled down a little further than bare target. Neither
# side reaches the other's level exactly, but they land closer together
# instead of sitting at opposite edges of the target loudness.
LOUDNESS_CONVERGENCE_DB = 1.5


def _converge_clamped_gain(gain, clamped_gain, peak_capped, ceiling,
                           attenuation_floor=None):
    """Nudge an already target/peak-clamped gain toward LOUDNESS_CONVERGENCE_DB
    of extra convergence -- see the constant's comment above for why."""
    _factor = 10 ** (LOUDNESS_CONVERGENCE_DB / 20.0)
    if gain > 1.0 and peak_capped:
        return min(clamped_gain * _factor, ceiling)
    if gain < 1.0:
        # Same "lean loud" floor as the primary clamp above -- this nudge
        # only ever pulls an ordinary attenuation a little further down for
        # consistency between clips, it never has anything to do with
        # true-peak safety, so it must respect LOUDNESS_GAIN_FLOOR_STANDARD,
        # not the true-peak emergency floor.
        _floor = LOUDNESS_GAIN_FLOOR_STANDARD if attenuation_floor is None else attenuation_floor
        return max(_floor, clamped_gain / _factor)
    return clamped_gain

# ATSC A/85 reference level.  Keep the true-peak ceiling below 0 dBTP so
# boosted quiet material cannot clip during playback or transcoding.
TRUE_PEAK_CEILING_DBTP = -2.0

# Raised +3.5dB from the ATSC A/85 reference (-24.0) on 2026-09-10: at -24,
# correctly-targeted content required ~100% system volume to be comfortably
# audible. -20.5 was picked to land that same comfort level at ~65% system
# volume instead (100%/65% is a ~3.7dB gap; rounded down slightly here to
# stay conservative). This raises the floor for every future probe on every
# channel -- attenuated (too-loud) content still gets pulled down to this
# new target, and TRUE_PEAK_CEILING_DBTP above is untouched, so boosted
# (too-quiet) content still can't clip on the way up. One shared constant,
# so TV/DVD (via VLC's live gain) and the pygame-backed paths (music
# visualizer, ch03 game music, via get_media_loudness_gain/
# get_pair_loudness_gain) all move together.
TARGET_LUFS = -20.5

# Music (ch03 menu music, visualizer songs) is mastered louder and with far
# less dynamic range than TV/movie content, so measuring it against the
# same -20.5 target as video pulls it down further than it needs to be for
# a background/ambient role, and leaves less headroom before the shared
# TRUE_PEAK_CEILING_DBTP clamp kicks in. Kept 3.5dB under TARGET_LUFS
# rather than matching typical streaming-music targets (~-14 LUFS) -- this
# is still meant to sit under other audio (menu narration, visualizer as
# background), not stand out at full program loudness.
# Raised from -24.0 to -18.5 together with MUSIC_GAIN_FLOOR below. In practice
# the old -24 target was never reached: hot music masters (-10 to -15 LUFS)
# needed 8-13dB of cut but the -6dB floor stopped every track at x0.50, so
# music actually played at an average of about -18 LUFS with a ~4.5dB
# spread between tracks. Lowering the floor alone would have normalized
# everything to -25.5 LUFS (about 7dB quieter than what was actually being
# heard). -18.5 keeps the overall music level close to where it really was
# (attenuated tracks land ~1.5dB under this via _converge_clamped_gain,
# i.e. about -20 LUFS) while making every track land at the SAME level.
# Tune by ear: 1dB here is roughly 2% of the music volume bar.
MUSIC_TARGET_LUFS = -18.5

# Shared dynamics-compressor tuning -- single source of truth for every
# audio path that's supposed to get this processing. VLC (TV stations +
# DVD) reads these via retro_tv_emulator.py's _compressor_filter_args()
# (imported from here, not redefined there) to build its live libvlc
# `compressor` filter args. Paths that can't attach a live VLC filter --
# ch03 game-channel menu music, the music visualizer, both pygame-backed
# -- get an equivalent offline render via bake_compressor_pass() below,
# using ffmpeg's `acompressor` with these exact same numbers. One set of
# constants so every channel that touches loudness processing is
# processed the same amount, instead of some getting compression and
# others (silently) not.
#
# Loosened from the original VLC-default-derived values (threshold -11.0,
# ratio 4.0, knee 5.0, makeup 7.0). Those defaults treated normal program
# loudness as something to squash: -11dB is close enough to typical
# dialog/music level that sustained, already-correctly-gained content
# (not just rare loud surprises) was getting compressed continuously, and
# a flat +7dB makeup gain couldn't fully undo that much sustained
# reduction on hot material -- net quieter than the single static
# per-file gain alone would have produced. Raising the threshold and
# softening the ratio means the filter only reacts to genuine surprises
# (a hot cold-open, an unexpectedly loud commercial) instead of
# continuously working on material that was already correctly leveled by
# the per-file gain probe.
COMPRESSOR_ATTACK_MS = 25.0
COMPRESSOR_RELEASE_MS = 800.0
COMPRESSOR_THRESHOLD_DB = -18.0   # was -11.0 -- only catch real surprises
COMPRESSOR_RATIO = 2.0            # was 4.0 -- gentle squeeze, not a squash
COMPRESSOR_KNEE_DB = 6.0          # was 5.0 -- smoother in/out of compression
COMPRESSOR_MAKEUP_DB = 4.0        # was 7.0 -- less reduction needs less makeup

# Bump to force every pygame-backed path to re-bake (parameter/methodology
# change) even though the source files themselves haven't changed --
# same pattern as GAP_SCAN_VERSION elsewhere in this file.
COMPRESSOR_BAKE_VERSION = 1


_CORRUPT_MEDIA_SIGNATURES = (
    "invalid data found when processing input",
    "moov atom not found",
    "error while decoding stream",
    "could not find codec parameters",
    "invalid nal unit size",
    "header missing",
    "truncated file",
    "corrupt input packet",
    "eof while searching for stream header",
)


def _stderr_indicates_corrupt(stderr_text):
    if not stderr_text:
        return False
    _lower = stderr_text.lower()
    return any(sig in _lower for sig in _CORRUPT_MEDIA_SIGNATURES)


def _stderr_has_audio_stream(stderr_text):
    if not stderr_text:
        return False
    import re as _re
    return bool(_re.search(r"Stream #\d+:\d+[^\n]*:\s*Audio:", stderr_text))


def _probe_duration_seconds_fast(file_path):
    if not file_path or not _cached_path_exists(file_path):
        return None
    try:
        import subprocess, re
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
            )
        with _fast_disk_io_slot(f"ffprobe_duration({os.path.basename(file_path)})"):
            result = subprocess.run(
                [_resolve_ffmpeg_path(), "-nostdin", "-hide_banner", "-i", file_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, stdin=subprocess.DEVNULL,
                **popen_kwargs
            )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        if not m:
            return None
        h, mi, s = m.groups()
        total = int(h) * 3600 + int(mi) * 60 + float(s)
        return total if total > 0 else None
    except Exception:
        return None


def _lufs_window_ffmpeg(file_path, target_lufs, start_sec=None, length_sec=None, timeout=90,
                         semaphore=None):
    import subprocess, re, json
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
        )
    cmd = [_resolve_ffmpeg_path(), "-nostdin"]
    if start_sec is not None:
        cmd += ["-ss", f"{max(0.0, start_sec):.2f}"]
    cmd += ["-i", file_path, "-vn"]
    if length_sec is not None:
        cmd += ["-t", f"{length_sec:.2f}"]
    cmd += ["-af", f"loudnorm=I={target_lufs}:TP={TRUE_PEAK_CEILING_DBTP}:LRA=11:print_format=json",
            "-f", "null", "-"]
    _probe_t0 = time.time()
    try:
        _yield_to_live_playback("loudness probe")
        with (semaphore or _LOUDNESS_PROBE_SEMAPHORE):
            # Start the drive-latency clock HERE, after the live-playback
            # yield and after winning a probe slot, so time spent queued
            # behind other loudness probes is not counted. The elapsed time
            # is later normalized by length_sec (see
            # _record_drive_op_latency): a window can cover up to 900 s of
            # audio, so its raw run time is not a drive-speed measurement.
            _probe_t0 = time.time()
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL,
                **popen_kwargs
            )
    except subprocess.TimeoutExpired:
        _elapsed = time.time() - _probe_t0
        _media_warning(
            "loudness_window_timeout",
            "[LOUDNESS PROBE WINDOW] Timeout after %.1fs (limit %ss) "
            "at start_sec=%s: %s",
            _elapsed, timeout, start_sec, os.path.basename(file_path),
        )
        # A timeout is itself the strongest possible "this drive is
        # struggling" signal -- feed the full elapsed wait in, not just
        # successful reads, so a drive that's failing to finish reads at
        # all gets flagged as degraded rather than just never contributing
        # samples.
        _record_drive_op_latency(file_path, _elapsed * 1000.0, audio_sec=length_sec)
        return None, None, None, "", True
    _record_drive_op_latency(file_path, (time.time() - _probe_t0) * 1000.0, audio_sec=length_sec)
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", result.stderr)
    if not match:
        return None, None, None, result.stderr, False
    try:
        stats = json.loads(match.group(0))
        _measured_i = float(stats.get("input_i", 0))
        try:
            _measured_tp = float(stats.get("input_tp"))
        except (TypeError, ValueError):
            _measured_tp = None
        # loudnorm's measurement pass already computes Loudness Range as
        # part of the exact same analysis that gives us input_i/input_tp --
        # this is a free field on the JSON blob we're already parsing, not
        # a second ffmpeg call. A high LRA (wide gap between the loud and
        # quiet parts of this window) is a cheap proxy for "this file
        # likely has short-term moments well above its own integrated
        # average" -- it is NOT a direct measurement of true short-term
        # (3s) loudness, just a free signal worth surfacing instead of
        # discarding.
        try:
            _measured_lra = float(stats.get("input_lra"))
        except (TypeError, ValueError):
            _measured_lra = None
        return _measured_i, _measured_tp, _measured_lra, result.stderr, False
    except Exception:
        return None, None, None, result.stderr, False


def _combine_lufs_readings(readings):
    import math
    if not readings:
        return None
    powers = [10 ** (r / 10.0) for r in readings]
    avg_power = sum(powers) / len(powers)
    return 10 * math.log10(avg_power) if avg_power > 0 else -70.0


def _combine_true_peak_readings(tp_readings):
    if not tp_readings:
        return None
    return max(tp_readings)


# Mid-show scan brackets. Each entry is
# (duration_ceiling_seconds, scan_window_seconds); the first bracket whose
# ceiling the file's duration falls under is used. "None" ceiling = whole
# show, scanned once (a second identical full-file scan would be pure
# waste, so the dual-window compare below only applies to the windowed
# brackets). A window shorter than the file's duration is positioned away
# from 0, so scans land in normal program content instead of cold opens /
# logos / silence.
LOUDNESS_SCAN_BRACKETS = [
    (900.0, None),      # under 15 min -> scan the whole show (single pass)
    (1800.0, 300.0),    # 15-30 min -> 5 min windows
    (3600.0, 600.0),    # 30-60 min -> 10 min windows
    (None, 900.0),       # over 60 min -> 15 min windows
]

# If the two windowed scans disagree by more than this many LU, they're
# not just noise -- treat it as a real split and bring in a 3rd scan (see
# _lufs_middle_scan_window) rather than immediately picking a side. Within
# this threshold, the two readings are close enough to just power-average
# normally with no 3rd scan needed.
LOUDNESS_DUAL_SCAN_LEAN_QUIET_THRESHOLD_LU = 3.0

# A Loudness Range this wide on a single window is a free-riding proxy
# (see _lufs_window_ffmpeg's input_lra extraction) for "this file likely
# has short-term moments well above its own average" -- not a direct
# short-term-loudness measurement, just a flag worth recording alongside
# the gain so it's visible in logs/cache rather than silently discarded.
LOUDNESS_WIDE_LRA_FLAG_LU = 15.0


def _lufs_scan_window(duration_sec):
    """Return (start_sec, length_sec) for a single-scan bracket (the
    "whole show" under-15-min case), per LOUDNESS_SCAN_BRACKETS."""
    return 0.0, duration_sec


def _lufs_dual_scan_windows(duration_sec, window_length):
    """Return two (start_sec, length_sec) windows for the windowed
    brackets: one centered around the first-third mark, one around the
    last-third mark, so the two scans sample different parts of the file
    rather than the same spot twice."""
    length = min(window_length, duration_sec)
    max_start = max(0.0, duration_sec - length)

    center_a = duration_sec / 3.0
    center_b = duration_sec * 2.0 / 3.0
    start_a = max(0.0, min(center_a - length / 2.0, max_start))
    start_b = max(0.0, min(center_b - length / 2.0, max_start))
    return (start_a, length), (start_b, length)


def _lufs_middle_scan_window(duration_sec, window_length):
    """Return a single (start_sec, length_sec) window centered on the
    file's midpoint -- the tie-breaker 3rd scan used only when the two
    _lufs_dual_scan_windows readings disagree by more than
    LOUDNESS_DUAL_SCAN_LEAN_QUIET_THRESHOLD_LU. A 3rd sample from a
    different part of the file (rather than just re-measuring one of the
    first two) gives the reconciliation something genuinely new to check
    the disagreement against."""
    length = min(window_length, duration_sec)
    max_start = max(0.0, duration_sec - length)
    center = duration_sec / 2.0
    start = max(0.0, min(center - length / 2.0, max_start))
    return start, length


def _reconcile_lufs_readings(pass_readings):
    """Reconcile 2 or 3 window readings into one file-level LUFS basis.

    2 readings, close together: power-average as usual.
    2 readings, far apart: lean quiet (a louder single spot is more likely
    an atypical moment -- action beat, music swell -- than a shift in the
    file's overall conversational level), but the caller only reaches this
    with exactly 2 readings if the 3rd tie-breaker scan itself produced
    nothing usable (timeout/no stats), so leaning quiet is still the best
    available fallback.
    3 readings (the tie-breaker fired and got a usable 3rd window): look
    for any pair that agrees within the threshold and power-average that
    pair -- majority agreement beats an isolated outlier. If all three
    disagree with each other, fall back to the quietest of the three,
    extending the same lean-quiet philosophy rather than guessing.
    """
    if len(pass_readings) >= 3:
        r1, r2, r3 = pass_readings[0], pass_readings[1], pass_readings[2]
        pairs = [(r1, r2), (r1, r3), (r2, r3)]
        for a, b in pairs:
            if abs(a - b) <= LOUDNESS_DUAL_SCAN_LEAN_QUIET_THRESHOLD_LU:
                return _combine_lufs_readings([a, b])
        return min(r1, r2, r3)
    if len(pass_readings) == 2:
        r1, r2 = pass_readings
        if abs(r1 - r2) > LOUDNESS_DUAL_SCAN_LEAN_QUIET_THRESHOLD_LU:
            return min(r1, r2)
        return _combine_lufs_readings(pass_readings)
    if len(pass_readings) == 1:
        return pass_readings[0]
    return None


def _sample_lufs_readings(file_path, target_lufs, semaphore=None, chunk_timeout=180):
    """Measure a file, returning one consistent file-level loudness basis.

    Short files (under the first LOUDNESS_SCAN_BRACKETS ceiling) get a
    single full-file scan -- there's nothing to compare against.

    Longer files get two independent scans at different points in the
    runtime (first-third and last-third), each windowed per
    LOUDNESS_SCAN_BRACKETS so total scan time still stays capped
    regardless of the file's length. If the two disagree by more than
    LOUDNESS_DUAL_SCAN_LEAN_QUIET_THRESHOLD_LU, a 3rd tie-breaker scan at
    the file's midpoint (_lufs_middle_scan_window) is added before
    reconciling -- this only happens on the disagreement path, so the
    common case (2 windows that already agree) costs nothing extra.

    chunk_timeout: per-window ffmpeg timeout in seconds, 180 by default.
    Callers on a real-time budget (a boot-time blocking scan the loading
    screen is waiting on, e.g.) can pass a much shorter value -- a window
    that doesn't finish in time is just treated the same as any other
    timed-out window (any_timeout/no reading), not an error, so the
    caller's own existing fallback-gain handling covers it unchanged.
    """
    duration_sec = _probe_duration_seconds_fast(file_path)
    readings = []
    tp_readings = []
    lra_readings = []
    stderr_for_stream_checks = ""
    any_timeout = False

    if duration_sec:
        bracket_window = None
        for ceiling, window in LOUDNESS_SCAN_BRACKETS:
            if ceiling is None or duration_sec < ceiling:
                bracket_window = window
                break

        if bracket_window is None:
            # Whole-show bracket: one pass, nothing to compare.
            start, length = _lufs_scan_window(duration_sec)
            m_i, m_tp, m_lra, chunk_stderr, timed_out = _lufs_window_ffmpeg(
                file_path, target_lufs, start_sec=float(start),
                length_sec=max(1.0, length), timeout=chunk_timeout,
                semaphore=semaphore)
            if chunk_stderr:
                stderr_for_stream_checks = chunk_stderr
            any_timeout = timed_out
            if m_i is not None:
                readings.append(m_i)
            if m_tp is not None:
                tp_readings.append(m_tp)
            if m_lra is not None:
                lra_readings.append(m_lra)
        else:
            # Windowed bracket: two scans at different points, reconciled.
            # A 3rd (middle) scan is only added if the first two disagree.
            (start_a, len_a), (start_b, len_b) = _lufs_dual_scan_windows(
                duration_sec, bracket_window)
            windows = [(start_a, len_a), (start_b, len_b)]
            pass_readings = []
            timeouts = []
            for start, length in windows:
                m_i, m_tp, m_lra, chunk_stderr, timed_out = _lufs_window_ffmpeg(
                    file_path, target_lufs, start_sec=float(start),
                    length_sec=max(1.0, length), timeout=chunk_timeout,
                    semaphore=semaphore)
                if not stderr_for_stream_checks and chunk_stderr:
                    stderr_for_stream_checks = chunk_stderr
                timeouts.append(timed_out)
                if m_i is not None:
                    pass_readings.append(m_i)
                if m_tp is not None:
                    tp_readings.append(m_tp)
                if m_lra is not None:
                    lra_readings.append(m_lra)

            if (len(pass_readings) == 2
                    and abs(pass_readings[0] - pass_readings[1])
                    > LOUDNESS_DUAL_SCAN_LEAN_QUIET_THRESHOLD_LU):
                start_c, len_c = _lufs_middle_scan_window(duration_sec, bracket_window)
                m_i, m_tp, m_lra, chunk_stderr, timed_out = _lufs_window_ffmpeg(
                    file_path, target_lufs, start_sec=float(start_c),
                    length_sec=max(1.0, len_c), timeout=chunk_timeout,
                    semaphore=semaphore)
                timeouts.append(timed_out)
                if m_i is not None:
                    pass_readings.append(m_i)
                if m_tp is not None:
                    tp_readings.append(m_tp)
                if m_lra is not None:
                    lra_readings.append(m_lra)

            any_timeout = all(timeouts) if timeouts else False
            _basis = _reconcile_lufs_readings(pass_readings)
            if _basis is not None:
                readings.append(_basis)
            # else: no scan produced a reading; readings stays empty
    else:
        m_i, m_tp, m_lra, stderr_for_stream_checks, timed_out = _lufs_window_ffmpeg(
            file_path, target_lufs, start_sec=None, length_sec=None, timeout=chunk_timeout,
            semaphore=semaphore)
        any_timeout = timed_out
        if m_i is not None:
            readings.append(m_i)
        if m_tp is not None:
            tp_readings.append(m_tp)
        if m_lra is not None:
            lra_readings.append(m_lra)

    return readings, tp_readings, lra_readings, stderr_for_stream_checks, any_timeout


def get_media_loudness_gain(file_path, target_lufs=TARGET_LUFS,
                             raise_on_no_audio=False,
                             treat_fallback_as_unknown=False,
                             raise_on_unplayable=False,
                             raise_on_timeout=False,
                             shortfall_out=None,
                             db=None,
                             semaphore=None,
                             chunk_timeout=180,
                             attenuation_floor=None):
    _fallback = None if treat_fallback_as_unknown else 1.0
    _ceiling = LOUDNESS_GAIN_CEILING
    _atten_floor = LOUDNESS_GAIN_FLOOR_STANDARD if attenuation_floor is None else attenuation_floor

    if not file_path or not _cached_path_exists(file_path):
        return _fallback

    norm_p = os.path.normpath(os.path.abspath(file_path))
    with _loudness_inflight_lock:
        if norm_p in _loudness_inflight:
            return _fallback
        _loudness_inflight.add(norm_p)

    _loud_t0 = time.time()
    try:
        import subprocess, re, json
        readings, tp_readings, lra_readings, stderr_for_stream_checks, any_timeout = _sample_lufs_readings(
            file_path, target_lufs, semaphore=semaphore, chunk_timeout=chunk_timeout)

        if not readings:
            if any_timeout:
                _media_warning(
                    "loudness_timeout",
                    "[LOUDNESS PROBE] Timed out measuring %s",
                    os.path.basename(file_path),
                )
                if raise_on_timeout:
                    raise LoudnessProbeTimeoutError(file_path)
                return _fallback
            if raise_on_unplayable and _stderr_indicates_corrupt(stderr_for_stream_checks):
                _corrupt_reason = "confirmed decode failure"
                for _sig in _CORRUPT_MEDIA_SIGNATURES:
                    if _sig in stderr_for_stream_checks.lower():
                        _corrupt_reason = _sig
                        break
                _media_warning(
                    "playability_unplayable",
                    "[PLAYABILITY PROBE] Confirmed unplayable (%s) in %s",
                    _corrupt_reason, os.path.basename(file_path),
                )
                raise UnplayableMediaError(file_path, reason=_corrupt_reason)
            if not _stderr_has_audio_stream(stderr_for_stream_checks):
                _media_warning(
                    "loudness_no_audio",
                    "[LOUDNESS PROBE] No audio stream in %s",
                    os.path.basename(file_path),
                )
                if raise_on_no_audio:
                    raise NoAudioStreamError(file_path)
                return _fallback
            _media_warning(
                "loudness_no_stats",
                "[LOUDNESS PROBE] No loudnorm stats returned for %s",
                os.path.basename(file_path),
            )
            return _fallback

        measured_i = _combine_lufs_readings(readings)
        if measured_i <= -70.0:
            _media_warning(
                "loudness_silent",
                "[LOUDNESS PROBE] Effectively silent (%.1f LUFS) in %s",
                measured_i, os.path.basename(file_path),
            )
            if raise_on_no_audio:
                raise NoAudioStreamError(file_path, reason=f"measured as silent ({measured_i:.1f} LUFS)")
            return _fallback
        if measured_i > 0.0:
            _media_warning(
                "loudness_unreliable",
                "[LOUDNESS PROBE] Unreliable reading (%.1f LUFS) for %s",
                measured_i, os.path.basename(file_path),
            )
            return _fallback
        gain_db = target_lufs - measured_i
        gain = 10 ** (gain_db / 20.0)
        _clamped_gain = max(_atten_floor, min(gain, _ceiling))
        combined_tp = _combine_true_peak_readings(tp_readings)
        _peak_capped = False
        if combined_tp is not None:
            _peak_headroom_db = TRUE_PEAK_CEILING_DBTP - combined_tp
            _peak_gain_cap = 10 ** (_peak_headroom_db / 20.0)
            if _peak_gain_cap < _clamped_gain:
                _peak_floor = 1.0 if gain > 1.0 else LOUDNESS_GAIN_FLOOR
                _clamped_gain = max(_peak_floor, _peak_gain_cap)
                _peak_capped = True
        _clamped_gain = _converge_clamped_gain(
            gain, _clamped_gain, _peak_capped, _ceiling, attenuation_floor=_atten_floor)
        _max_lra = max(lra_readings) if lra_readings else None
        _wide_lra_flag = _max_lra is not None and _max_lra >= LOUDNESS_WIDE_LRA_FLAG_LU
        if _wide_lra_flag:
            _media_warning(
                "loudness_wide_lra",
                "[LOUDNESS PROBE] %s: wide loudness range (%.1f LU) on the "
                "scanned window(s) -- integrated/true-peak targets are met, "
                "but this is a signal (not a measurement) that a short-term "
                "moment could still land well above -20 LUFS/3s",
                os.path.basename(file_path), _max_lra,
            )
        if shortfall_out is not None:
            shortfall_out.update({
                "measured_lufs": measured_i,
                "achieved_lufs": measured_i + (20 * math.log10(_clamped_gain)),
                "peak_capped": _peak_capped,
                "clamped_gain": _clamped_gain,
                "measured_lra": _max_lra,
                "wide_lra_flag": _wide_lra_flag,
            })
        if gain != _clamped_gain:
            _achieved_lufs = measured_i + (20 * math.log10(_clamped_gain))
            _shortfall_db = target_lufs - _achieved_lufs
            _side = "BOOST" if gain > 1.0 else "ATTENUATION"
            _reason = "TRUE PEAK" if _peak_capped else "LUFS"
            if shortfall_out is not None and (
                _side == "BOOST" or (_side == "ATTENUATION" and _reason == "TRUE PEAK")
            ):
                shortfall_out.update({
                    "reason": _reason,
                    "side": _side,
                    "shortfall_db": _shortfall_db,
                    "needed_gain": gain,
                    "clamped_gain": _clamped_gain,
                })
        return _clamped_gain
    except (NoAudioStreamError, UnplayableMediaError, LoudnessProbeTimeoutError):
        raise
    except FileNotFoundError:
        _media_warning(
            "ffmpeg_missing_loudness",
            "[LOUDNESS PROBE] ffmpeg not found (checked bundled locations and PATH) -- "
            "loudness normalization is disabled until it is available.",
        )
        return _fallback
    except subprocess.TimeoutExpired:
        _media_warning(
            "loudness_timeout",
            "[LOUDNESS PROBE] Timed out measuring %s",
            os.path.basename(file_path),
        )
        return _fallback
    except Exception as e:
        _media_warning(
            "loudness_error",
            "[LOUDNESS PROBE ERROR] %s: %s",
            os.path.basename(file_path), e,
        )
        return _fallback
    finally:
        with _loudness_inflight_lock:
            _loudness_inflight.discard(norm_p)


def get_music_loudness_gain(file_path, raise_on_no_audio=False,
                             treat_fallback_as_unknown=False,
                             raise_on_unplayable=False,
                             raise_on_timeout=False,
                             shortfall_out=None,
                             db=None):
    """Music counterpart to get_media_loudness_gain -- same measurement
    pipeline, but targets MUSIC_TARGET_LUFS instead of TARGET_LUFS and runs
    against _MUSIC_LOUDNESS_PROBE_SEMAPHORE instead of the shared video
    one, so a dedicated music-scan thread (retro_tv_emulator.py's
    _music_precache_worker) never has to wait behind whatever long movie/
    show scan the main probe pass is in the middle of. Both semaphores are
    real, separate BoundedSemaphores -- a music scan and a video scan can
    genuinely run at the same time, not just get interleaved on one.
    """
    return get_media_loudness_gain(
        file_path, target_lufs=MUSIC_TARGET_LUFS,
        raise_on_no_audio=raise_on_no_audio,
        treat_fallback_as_unknown=treat_fallback_as_unknown,
        raise_on_unplayable=raise_on_unplayable,
        raise_on_timeout=raise_on_timeout,
        shortfall_out=shortfall_out,
        db=db,
        semaphore=_MUSIC_LOUDNESS_PROBE_SEMAPHORE,
        attenuation_floor=MUSIC_GAIN_FLOOR,
    )


def get_pair_loudness_gain(path1, path2, target_lufs=TARGET_LUFS,
                            raise_on_no_audio=False,
                            treat_fallback_as_unknown=False,
                            raise_on_unplayable=False,
                            raise_on_timeout=False,
                            shortfall_out=None,
                            db=None):
    _fallback = None if treat_fallback_as_unknown else 1.0
    _ceiling = LOUDNESS_GAIN_CEILING

    if not path1 or not path2 or not _cached_path_exists(path1) or not _cached_path_exists(path2):
        return _fallback

    norm_p1 = os.path.normpath(os.path.abspath(path1))
    norm_p2 = os.path.normpath(os.path.abspath(path2))
    with _loudness_inflight_lock:
        if norm_p1 in _loudness_inflight or norm_p2 in _loudness_inflight:
            return _fallback
        _loudness_inflight.add(norm_p1)
        _loudness_inflight.add(norm_p2)

    try:
        import subprocess
        readings = []
        tp_readings = []
        lra_readings = []
        stderr_for_stream_checks = ""
        any_timeout = False
        for _fp in (path1, path2):
            _r, _tp_r, _lra_r, _stderr, _timed_out = _sample_lufs_readings(_fp, target_lufs)
            readings.extend(_r)
            tp_readings.extend(_tp_r)
            lra_readings.extend(_lra_r)
            if not stderr_for_stream_checks and _stderr:
                stderr_for_stream_checks = _stderr
            any_timeout = any_timeout or _timed_out
            if not _r and _stderr and _stderr_indicates_corrupt(_stderr) and raise_on_unplayable:
                _media_warning(
                    "pair_unplayable",
                    "[PLAYABILITY PROBE] Confirmed unplayable in pair half: %s",
                    os.path.basename(_fp),
                )
                raise UnplayableMediaError(_fp, reason="confirmed decode failure")

        if not readings:
            if any_timeout:
                _media_warning(
                    "pair_loudness_timeout",
                    "[LOUDNESS PROBE] Timed out measuring pair: %s + %s",
                    os.path.basename(path1), os.path.basename(path2),
                )
                if raise_on_timeout:
                    raise LoudnessProbeTimeoutError(path1)
                return _fallback
            if raise_on_unplayable and _stderr_indicates_corrupt(stderr_for_stream_checks):
                raise UnplayableMediaError(path1, reason="confirmed decode failure")
            if not _stderr_has_audio_stream(stderr_for_stream_checks):
                _media_warning(
                    "pair_no_audio",
                    "[LOUDNESS PROBE] No audio stream in pair: %s + %s",
                    os.path.basename(path1), os.path.basename(path2),
                )
                if raise_on_no_audio:
                    raise NoAudioStreamError(path1)
                return _fallback
            _media_warning(
                "pair_no_stats",
                "[LOUDNESS PROBE] No loudnorm stats returned for pair: %s + %s",
                os.path.basename(path1), os.path.basename(path2),
            )
            return _fallback

        measured_i = _combine_lufs_readings(readings)
        if measured_i <= -70.0:
            _media_warning(
                "pair_silent",
                "[LOUDNESS PROBE] Effectively silent (%.1f LUFS) in pair: %s + %s",
                measured_i, os.path.basename(path1), os.path.basename(path2),
            )
            if raise_on_no_audio:
                raise NoAudioStreamError(path1, reason=f"measured as silent ({measured_i:.1f} LUFS)")
            return _fallback
        if measured_i > 0.0:
            _media_warning(
                "pair_unreliable",
                "[LOUDNESS PROBE] Unreliable reading (%.1f LUFS) for pair: %s + %s",
                measured_i, os.path.basename(path1), os.path.basename(path2),
            )
            return _fallback
        gain_db = target_lufs - measured_i
        gain = 10 ** (gain_db / 20.0)
        _clamped_gain = max(LOUDNESS_GAIN_FLOOR_STANDARD, min(gain, _ceiling))
        combined_tp = _combine_true_peak_readings(tp_readings)
        _peak_capped = False
        if combined_tp is not None:
            _peak_headroom_db = TRUE_PEAK_CEILING_DBTP - combined_tp
            _peak_gain_cap = 10 ** (_peak_headroom_db / 20.0)
            if _peak_gain_cap < _clamped_gain:
                _peak_floor = 1.0 if gain > 1.0 else LOUDNESS_GAIN_FLOOR
                _clamped_gain = max(_peak_floor, _peak_gain_cap)
                _peak_capped = True
        _clamped_gain = _converge_clamped_gain(gain, _clamped_gain, _peak_capped, _ceiling)
        _max_lra = max(lra_readings) if lra_readings else None
        if _max_lra is not None and _max_lra >= LOUDNESS_WIDE_LRA_FLAG_LU:
            _media_warning(
                "pair_wide_lra",
                "[LOUDNESS PROBE] pair %s + %s: wide loudness range (%.1f LU) "
                "on the scanned window(s) -- signal only, not a short-term measurement",
                os.path.basename(path1), os.path.basename(path2), _max_lra,
            )
        if gain != _clamped_gain:
            _achieved_lufs = measured_i + (20 * math.log10(_clamped_gain))
            _shortfall_db = target_lufs - _achieved_lufs
            _side = "BOOST" if gain > 1.0 else "ATTENUATION"
            _reason = "TRUE PEAK" if _peak_capped else "LUFS"
            if shortfall_out is not None and (
                _side == "BOOST" or (_side == "ATTENUATION" and _reason == "TRUE PEAK")
            ):
                shortfall_out.update({
                    "reason": _reason,
                    "side": _side,
                    "shortfall_db": _shortfall_db,
                    "needed_gain": gain,
                    "clamped_gain": _clamped_gain,
                })
        return _clamped_gain
    except (NoAudioStreamError, UnplayableMediaError, LoudnessProbeTimeoutError):
        raise
    except FileNotFoundError:
        _media_warning(
            "ffmpeg_missing_loudness",
            "[LOUDNESS PROBE] ffmpeg not found (checked bundled locations and PATH) -- "
            "loudness normalization is disabled until it is available.",
        )
        return _fallback
    except subprocess.TimeoutExpired:
        _media_warning(
            "pair_loudness_timeout",
            "[LOUDNESS PROBE] Timed out measuring pair: %s + %s",
            os.path.basename(path1), os.path.basename(path2),
        )
        return _fallback
    except Exception as e:
        _media_warning(
            "pair_loudness_error",
            "[LOUDNESS PROBE ERROR] pair %s + %s: %s",
            os.path.basename(path1), os.path.basename(path2), e,
        )
        return _fallback
    finally:
        with _loudness_inflight_lock:
            _loudness_inflight.discard(norm_p1)
            _loudness_inflight.discard(norm_p2)



LOUDNORM_RENORM_LRA = 11.0
LOUDNORM_RENORM_LRA_AGGRESSIVE = 7.0

RENORM_DYNAMIC_LRA = 6.0

RENORM_DYNAMIC_LRA_AGGRESSIVE = 4.0

RENORM_DYNAMIC_MAX_DURATION_SECONDS = 600

RENORM_SHORTFALL_THRESHOLD_DB = 3.0

RENORM_PEAK_SHORTFALL_THRESHOLD_DB = 0.5

RENORM_VERIFY_TOLERANCE_DB = 1.0


class _FFmpegAborted(Exception):
    """Raised by callers of _run_ffmpeg_interruptible() when should_abort()
    started returning True mid-run (e.g. live playback began) and the
    ffmpeg process was terminated early. Kept distinct from a genuine
    failure/timeout so callers can quietly retry the file later instead
    of logging it as broken or marking it permanently done."""
    pass


def _run_ffmpeg_interruptible(cmd, timeout, should_abort=None, poll_interval=0.4):
    """Like subprocess.run(), but polls should_abort() (if given) roughly
    every poll_interval seconds while ffmpeg runs and terminates the
    process early the instant it starts returning True -- so a long
    background scan/renorm pass can back off promptly the moment live
    playback starts, instead of always running its current file/decode to
    completion regardless of what's on screen.

    Returns (result, aborted). On a normal finish, result behaves like a
    subprocess.CompletedProcess (.returncode/.stdout/.stderr) and aborted
    is False. If should_abort() fired, result is None and aborted is
    True. A real timeout still raises subprocess.TimeoutExpired, exactly
    like subprocess.run() would, so existing except blocks keep working
    unchanged.
    """
    import subprocess, collections
    _FFResult = collections.namedtuple("_FFResult", ["returncode", "stdout", "stderr"])
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
        )
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, **popen_kwargs
    )
    _start = time.monotonic()
    while True:
        try:
            out, err = proc.communicate(timeout=poll_interval)
            return _FFResult(proc.returncode, out, err), False
        except subprocess.TimeoutExpired:
            if should_abort is not None and should_abort():
                proc.terminate()
                try:
                    proc.communicate(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                return None, True
            if (time.monotonic() - _start) >= timeout:
                proc.kill()
                try:
                    proc.communicate(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.communicate()
                raise subprocess.TimeoutExpired(cmd, timeout)


def _run_ffmpeg_loudnorm_json(file_path, target_lufs, target_tp, lra, timeout,
                               measured=None, should_abort=None):
    import re, json as _json
    af = (f"loudnorm=I={target_lufs}:TP={target_tp}:LRA={lra}:"
          f"print_format=json")
    if measured:
        af += (f":measured_I={measured['input_i']}"
               f":measured_TP={measured['input_tp']}"
               f":measured_LRA={measured['input_lra']}"
               f":measured_thresh={measured['input_thresh']}"
               f":offset={measured.get('target_offset', 0.0)}"
               f":linear=true")
    result, aborted = _run_ffmpeg_interruptible(
        [_resolve_ffmpeg_path(), "-nostdin", "-hide_banner",
         "-i", file_path, "-vn", "-af", af, "-f", "null", "-"],
        timeout, should_abort=should_abort,
    )
    if aborted:
        raise _FFmpegAborted(
            f"loudnorm measure pass aborted for {os.path.basename(file_path)}")
    matches = re.findall(r"\{[^{}]*\}", result.stderr, re.DOTALL)
    if not matches:
        return None
    try:
        return _json.loads(matches[-1])
    except Exception:
        return None


def _compressor_bake_cache_path(file_path):
    """Return the on-disk path a baked (pre-compressed) copy of file_path
    would live at, without checking whether it exists yet. Keyed on a
    hash of the absolute path (so two identically-named files in
    different folders can't collide, same reasoning as
    renormalize_file_two_pass's tmp_path) plus COMPRESSOR_BAKE_VERSION
    (so a tuning change mints a fresh filename instead of silently
    reusing a stale render)."""
    import hashlib as _hashlib
    file_path = os.path.abspath(file_path)
    stem = os.path.splitext(os.path.basename(file_path))[0]
    _path_key = _hashlib.sha1(file_path.encode("utf-8", "ignore")).hexdigest()[:16]
    _cache_dir = os.path.join(resolve_loudness_dir(), "compressor_cache")
    try:
        os.makedirs(_cache_dir, exist_ok=True)
    except OSError as e:
        log.warning("[COMPRESSOR BAKE] could not create cache dir %s: %s", _cache_dir, e)
    return os.path.join(
        _cache_dir, f"{_path_key}__{stem}.v{COMPRESSOR_BAKE_VERSION}.compressed.wav",
    )


def get_cached_compressor_bake(file_path):
    """Return the baked (pre-compressed) copy of file_path if one already
    exists on disk for the current COMPRESSOR_BAKE_VERSION, else None.
    Never blocks on ffmpeg -- callers use this to decide whether to play
    the cached compressed render right now or fall back to the raw file
    (with just the existing static per-file gain) while a background bake
    (bake_compressor_pass) catches up -- the exact same "unscanned
    default until the background pass lands, then hot-patch" shape the
    loudness gain probe already uses elsewhere in this module.
    """
    if not file_path or not _cached_path_exists(file_path):
        return None
    _out_path = _compressor_bake_cache_path(file_path)
    if _cached_path_exists(_out_path):
        return _out_path
    return None


_compressor_bake_inflight_lock = threading.Lock()
_compressor_bake_inflight = set()


def bake_compressor_pass(file_path, timeout=180, should_abort=None):
    """Render a copy of file_path through the same dynamics-compressor
    curve VLC applies live to TV-station/DVD playback (COMPRESSOR_*
    above, kept in exact lockstep with retro_tv_emulator.py's
    _compressor_filter_args() -- both read from the same constants
    defined in this file), so pygame-backed audio paths that can't attach
    a live libvlc filter -- ch03 game-channel menu music, the music
    visualizer -- end up processed the same amount instead of playing
    uncompressed while every VLC-driven channel isn't.

    ffmpeg's `acompressor` filter is a different implementation than
    libvlc's `compressor` module (different codebase entirely), so this
    is a matched-parameter equivalent, not a bit-identical render -- but
    it's driven by the exact same threshold/ratio/attack/release/knee/
    makeup numbers, which is what actually matters for "does this get
    the same amount of processing", not internal algorithm details
    nobody can hear the difference of.

    Gated on the same "Loudness Scan" toggle as everything else in this
    feature -- callers check loudness_scan_enabled before calling this,
    same as get_media_loudness_gain's callers already do.

    Returns the output path on success, None on any failure. A failure
    here should never block or fail playback -- callers just keep using
    the raw file (with the existing static gain still applied) and can
    retry the bake later, exactly like a failed loudness probe already
    does.
    """
    if not file_path or not _cached_path_exists(file_path):
        return None
    norm_p = os.path.normpath(os.path.abspath(file_path))
    with _compressor_bake_inflight_lock:
        if norm_p in _compressor_bake_inflight:
            return None
        _compressor_bake_inflight.add(norm_p)
    try:
        out_path = _compressor_bake_cache_path(norm_p)
        if _cached_path_exists(out_path):
            return out_path
        tmp_path = out_path + ".tmp"
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        # acompressor's threshold/makeup are linear amplitude (0-1 / 1-64),
        # not dB -- convert from the same dB numbers VLC's compressor
        # filter takes, so COMPRESSOR_THRESHOLD_DB/COMPRESSOR_MAKEUP_DB
        # stay the one shared source of truth for both engines.
        _threshold_lin = 10 ** (COMPRESSOR_THRESHOLD_DB / 20.0)
        _makeup_lin = 10 ** (COMPRESSOR_MAKEUP_DB / 20.0)
        af = (
            f"acompressor=threshold={_threshold_lin:.6f}:"
            f"ratio={COMPRESSOR_RATIO}:"
            f"attack={COMPRESSOR_ATTACK_MS}:"
            f"release={COMPRESSOR_RELEASE_MS}:"
            f"knee={COMPRESSOR_KNEE_DB}:"
            f"makeup={_makeup_lin:.6f}"
        )
        with _LOUDNESS_PROBE_SEMAPHORE:
            # -f wav is required here, not cosmetic: ffmpeg picks its output
            # muxer from the OUTPUT FILENAME'S extension, and tmp_path ends
            # in ".tmp" (the real ".wav" is in the middle of the name, from
            # out_path + ".tmp" above) -- so without an explicit -f, ffmpeg
            # can't tell what container to write and fails outright ("Unable
            # to choose an output format for '...wav.tmp'"). That failure
            # was silent to playback (callers fall back to the raw,
            # uncompressed file on any bake failure -- see this function's
            # docstring) but meant the bake never actually succeeded, ever,
            # for any track: pygame-backed audio (ch03 game-channel music,
            # the music visualizer) never got the same dynamics-compressor
            # safety net TV/DVD playback gets live via VLC, and just played
            # the source's full uncontrolled dynamics instead. Forcing the
            # container format explicitly makes ffmpeg write real WAV data
            # regardless of what the temp filename's extension looks like.
            result, aborted = _run_ffmpeg_interruptible(
                [_resolve_ffmpeg_path(), "-nostdin", "-hide_banner", "-y",
                 "-i", norm_p, "-vn", "-af", af, "-f", "wav", tmp_path],
                timeout, should_abort=should_abort,
            )
        if aborted:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return None
        if (result.returncode != 0 or not os.path.exists(tmp_path)
                or os.path.getsize(tmp_path) == 0):
            # The exit code alone ("ffmpeg exit 4294967274" -- Windows'
            # unsigned view of a negative return code) has never been
            # enough to actually diagnose why this fails: the raw file
            # keeps playing uncompressed (only the flat loudness gain
            # applied, no peak/dynamics control) every time it's picked,
            # with nothing but that bare number to go on. Log ffmpeg's own
            # stderr tail too, so a repeat of this is finally debuggable
            # instead of just a recurring mystery number in the log.
            _stderr_tail = (getattr(result, "stderr", "") or "").strip().splitlines()[-6:]
            _media_warning(
                "compressor_bake_failed",
                "[COMPRESSOR BAKE] failed for %s (ffmpeg exit %s): %s",
                os.path.basename(norm_p), getattr(result, "returncode", "?"),
                " | ".join(_stderr_tail) if _stderr_tail else "(no stderr captured)",
            )
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return None
        os.replace(tmp_path, out_path)
        print(f"[COMPRESSOR BAKE] {os.path.basename(norm_p)} -> "
              f"{os.path.basename(out_path)}")
        return out_path
    except Exception as e:
        _media_warning(
            "compressor_bake_error",
            "[COMPRESSOR BAKE ERROR] %s: %s", os.path.basename(file_path), e,
        )
        return None
    finally:
        with _compressor_bake_inflight_lock:
            _compressor_bake_inflight.discard(norm_p)


def renormalize_file_two_pass(file_path, target_lufs=TARGET_LUFS,
                               target_tp_dbtp=None, lra=None, timeout=None,
                               dynamic=False, should_abort=None):
    if target_tp_dbtp is None:
        target_tp_dbtp = TRUE_PEAK_CEILING_DBTP
    if lra is None:
        lra = RENORM_DYNAMIC_LRA if dynamic else LOUDNORM_RENORM_LRA

    _result = {"status": "failed", "pre_lufs": None, "post_lufs": None,
               "shortfall_db": None, "error": None}

    if not file_path or not _cached_path_exists(file_path):
        _result["error"] = "file not found"
        return _result

    file_path = os.path.abspath(file_path)
    stem, ext = os.path.splitext(os.path.basename(file_path))
    # Work files live in the program's own loudness/ folder, never next to
    # the source media (see resolve_loudness_dir). The path is folded into
    # a short hash so two different shows with an identically-named
    # episode (e.g. "S01E01.mp4") can never collide in the shared folder.
    import hashlib as _hashlib
    _path_key = _hashlib.sha1(file_path.encode("utf-8", "ignore")).hexdigest()[:16]
    loudness_dir = resolve_loudness_dir()
    tmp_path = os.path.join(loudness_dir, f"{_path_key}__{stem}.renorm_tmp{ext}")
    backup_path = os.path.join(loudness_dir, f"{_path_key}__{stem}.pre_renorm_backup{ext}")

    if timeout is None:
        _dur = _probe_duration_seconds_fast(file_path)
        timeout = max(600, int((_dur or 3600) * 1.5))

    # Renorm work competes for the same 2 global loudness-analysis
    # slots as the regular probe pass, so it can never run as an
    # invisible 3rd concurrent ffmpeg process.
    with _LOUDNESS_PROBE_SEMAPHORE:
        try:
            measured = _run_ffmpeg_loudnorm_json(
                file_path, target_lufs, target_tp_dbtp, lra, timeout,
                should_abort=should_abort)
            if not measured or "input_i" not in measured:
                _result["status"] = "no_audio"
                _result["error"] = "loudnorm measure pass produced no stats"
                _media_warning(
                    "renorm_no_stats",
                    "[RENORM] %s: measure pass produced no usable stats -- "
                    "leaving file untouched",
                    os.path.basename(file_path),
                )
                return _result
            try:
                _result["pre_lufs"] = float(measured["input_i"])
            except (TypeError, ValueError):
                pass

            _linear_str = "false" if dynamic else "true"
            af = (f"loudnorm=I={target_lufs}:TP={target_tp_dbtp}:LRA={lra}:"
                  f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
                  f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
                  f"offset={measured.get('target_offset', 0.0)}:linear={_linear_str}:print_format=json")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)  # Remove an existing temporary output first.
            apply_result, _apply_aborted = _run_ffmpeg_interruptible(
                [_resolve_ffmpeg_path(), "-nostdin", "-hide_banner", "-y",
                 "-i", file_path, "-map", "0",
                 "-c:v", "copy", "-c:s", "copy",
                 "-af", af, "-c:a", "aac", "-b:a", "256k",
                 tmp_path],
                timeout, should_abort=should_abort,
            )
            if _apply_aborted:
                raise _FFmpegAborted(
                    f"apply pass aborted for {os.path.basename(file_path)}")
            if apply_result.returncode != 0 or not os.path.exists(tmp_path) \
                    or os.path.getsize(tmp_path) == 0:
                _result["error"] = (f"ffmpeg apply pass exited "
                                     f"{apply_result.returncode}")
                _media_warning(
                    "renorm_apply_failed",
                    "[RENORM] %s: apply pass failed (%s) -- "
                    "leaving original untouched",
                    os.path.basename(file_path), _result["error"],
                )
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                return _result

            verify = _run_ffmpeg_loudnorm_json(
                tmp_path, target_lufs, target_tp_dbtp, lra, min(timeout, 300),
                should_abort=should_abort)
            _post_lufs = None
            if verify and "input_i" in verify:
                try:
                    _post_lufs = float(verify["input_i"])
                except (TypeError, ValueError):
                    _post_lufs = None
            _result["post_lufs"] = _post_lufs

            # The file swap below must never leave a window where file_path
            # doesn't exist on disk. This app's own quit path calls
            # os._exit(0) with no grace period for in-flight background
            # work (deliberately, to avoid a worse shutdown-hang bug --
            # see the pygame.QUIT handler), so a renorm running at the
            # exact moment the user quits could hit any gap between
            # "original removed" and "new file in place" and leave the
            # user's video file missing from its expected location.
            #
            # Preserve the backup with a plain (non-atomic, but
            # non-destructive) copy first -- losing/partially-writing the
            # disposable backup is harmless -- then do the one and only
            # operation that actually touches file_path as a single atomic
            # os.replace(). file_path is therefore always either the
            # original or the new file, never neither.
            if not os.path.exists(backup_path):
                shutil.copy2(file_path, backup_path)
            # os.replace() is atomic, but only within a single volume --
            # cross-drive (e.g. the loudness work folder on C:, the media
            # library on D:) it raises WinError 17 "cannot move the file
            # to a different disk drive" for any library that doesn't
            # happen to live on the same drive as the app. Stage a same-
            # drive copy right next to the real destination first -- that
            # copy touches a brand-new filename, not file_path itself, so
            # it can't leave file_path half-written -- then do the actual
            # atomic swap from there. file_path is still always either the
            # untouched original or the fully-written new file, never
            # neither; only the source of the final os.replace() changes
            # when the two paths aren't on one drive.
            if os.path.splitdrive(tmp_path)[0].lower() == os.path.splitdrive(file_path)[0].lower():
                os.replace(tmp_path, file_path)
            else:
                _same_drive_tmp = file_path + ".renorm_tmp_swap"
                shutil.copy2(tmp_path, _same_drive_tmp)
                try:
                    os.replace(_same_drive_tmp, file_path)
                finally:
                    # Clean up BOTH staging artifacts, not just the C:-side
                    # source. If os.replace() above raised (e.g. another
                    # I/O hiccup on the same flaky drive), _same_drive_tmp
                    # would otherwise be left sitting next to file_path
                    # forever -- an orphaned ".renorm_tmp_swap" file that
                    # nothing else in this codebase ever looks for or
                    # cleans up on its own.
                    for _leftover in (tmp_path, _same_drive_tmp):
                        try:
                            if os.path.exists(_leftover):
                                os.remove(_leftover)
                        except OSError:
                            pass

            if _post_lufs is not None and abs(_post_lufs - target_lufs) <= RENORM_VERIFY_TOLERANCE_DB:
                _result["status"] = "fixed"
                # Verified good -- nothing in this codebase ever reads the
                # backup back to restore it, so keeping it around forever
                # serves no purpose. Clean it up now rather than let it
                # accumulate in the loudness folder indefinitely.
                try:
                    os.remove(backup_path)
                except OSError:
                    pass
            else:
                _result["status"] = "still_short"
                if _post_lufs is not None:
                    _result["shortfall_db"] = abs(target_lufs - _post_lufs)
                    _media_warning(
                        "renorm_still_short",
                        "[RENORM] %s: re-encoded but still short -- lands at "
                        "~%.1f LUFS (%.1fdB short of %.1f)",
                        os.path.basename(file_path), _post_lufs,
                        _result["shortfall_db"], target_lufs,
                    )
                else:
                    _media_warning(
                        "renorm_verify_failed",
                        "[RENORM] %s: re-encoded successfully but verification "
                        "re-measure failed -- file replaced, shortfall unknown",
                        os.path.basename(file_path),
                    )
                # Keep the pre-renorm backup as a manual rollback option. It
                # stays isolated in the loudness folder rather than the media
                # folder.
            return _result
        except _FFmpegAborted:
            # Live playback started mid-pass -- not a failure, just not a
            # good time. Nothing has touched the original file yet (the
            # atomic swap only happens after both apply and verify
            # succeed), so it's safe to just bail out quietly; the
            # sweep's own retry loop will pick this file back up once
            # playback ends.
            _result["status"] = "interrupted"
            _result["error"] = "live playback started"
            return _result
        except subprocess.TimeoutExpired:
            _result["error"] = "timed out"
            _media_warning(
                "renorm_timeout",
                "[RENORM] %s: timed out -- leaving original untouched",
                os.path.basename(file_path),
            )
            return _result
        except Exception as e:
            _result["error"] = str(e)
            _media_warning(
                "renorm_error",
                "[RENORM ERROR] %s: %s",
                os.path.basename(file_path), e,
            )
            return _result
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


class RetroDatabase:
    def __init__(self, detected_aspect_ratio=None):
        self.base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        self.config_path = os.path.join(self.base_dir, "retro_config.json")
        self._detected_aspect_ratio = detected_aspect_ratio

        self.playback_active_check = None
        
        self.themes = {
            "DefaultBlueGreen": {"ui": (0, 255, 128), "bg": (10, 15, 30), "text": (255, 255, 255)},
            "Classic Amber": {"ui": (255, 170, 0), "bg": (15, 10, 0), "text": (240, 220, 180)}
        }
        
        self.config = {}
        self.channels_db = {}
        self.audio_files = [] 
        self.ready_flag = False
        self._save_lock = threading.Lock()
        self._save_request_lock = threading.Lock()
        self._save_generation = 0

        # --- Server/User networking: independent channels source ---
        # channels_path starts out equal to config_path (normal, non-server
        # behavior: config and channels live in the same file). "Make Active"
        # (server slot flow) repoints channels_path at a slot's own file
        # without ever moving config_path. config_path always stays the
        # single source of truth for db.config.
        self.channels_path = self.config_path
        # Cache of whatever "channels" data belongs to config_path itself,
        # kept in sync independently of which path channels_db is currently
        # backed by. This is what lets a config-only save (while a server
        # slot is active elsewhere) write retro_config.json without
        # clobbering the main project's own channel data with whatever
        # slot's data happens to be in memory right now.
        self._main_channels_cache = {}
        self._channels_lock = threading.Lock()
        self._channels_save_generation = 0

        # --- Persistence split: durable vs. runtime state ---
        # playback_anchor is, by a wide margin, the single highest-frequency
        # write in the whole app -- it's rewritten on every confirmed play
        # across every code path (_confirm_playback_started is the sole
        # choke point per fix #5), which means every channel change, every
        # commercial cut, and every watchdog in-place resume. Before this
        # split, that single small dict change triggered save_settings_async(),
        # which deep-copies and rewrites the ENTIRE combined config+channels
        # file -- every channel's full schedule, rotation state, duration
        # cache, and broken_files list -- just to persist one anchor. On a
        # slow/failing drive that's a large, fragile disk write sitting
        # directly in the hot path of ordinary playback confirmation.
        # runtime_path holds ONLY per-channel playback_anchor data, written
        # independently, under its own lock/generation, so a routine anchor
        # update never has to serialize or write anything durable (schedules,
        # rotation, broken_files, settings), and a durable save is never
        # slowed down by runtime churn either. The two writers can never
        # race on the same physical file, by construction.
        self.runtime_path = os.path.join(self.base_dir, "playback_runtime.json")
        self._runtime_lock = threading.Lock()
        self._runtime_save_generation = 0


        # A User temporarily overlays the locally-owned channel records with
        # the schedule received from a selected Server.  Keep that overlay
        # completely separate from the normal save/load path so a remote
        # snapshot can never be persisted over the user's own channels.
        self._remote_schedule_lock = threading.RLock()
        self._remote_schedule_active = False
        self._remote_local_channels_snapshot = None
        self._remote_locked_channels = set()
        # Server-imposed playback settings (screen mode); empty when no Server.
        self.remote_playback = {}

        threading.Thread(target=self.load_settings_async, daemon=True).start()

    # ------------------------------------------------------------------
    # Config / channels loading — split per SERVER_FEATURE_PLAN_1_.md §2.3.
    #
    # `_apply_config_defaults` and `_build_channels_db` are pure helpers
    # (raw dict in, filled dict out) so both the normal startup load *and*
    # a later "Make Active" channels-only reload can reuse the exact same
    # migration/default logic without duplicating it.
    # ------------------------------------------------------------------

    def _apply_config_defaults(self, raw_config):
        """Fill in defaults/migrations on top of a raw 'config' dict loaded
        from disk. Does not touch channels_db or any file on disk."""
        config = dict(raw_config)
        config["is_muted"] = False
        # Fill missing settings without overwriting saved choices.
        if "screen_mode" not in config:
            config["screen_mode"] = "normal"
        # DVD player gets its own independent Aspect Ratio /
        # Screen Mode pair (set from the Games tab's DVD
        # sub-menu) so switching it never touches -- and is
        # never touched by -- the main Video tab's settings
        # above, which apply to every other content type.
        if "dvd_aspect_ratio" not in config:
            config["dvd_aspect_ratio"] = "16:9"
        if "dvd_screen_mode" not in config:
            config["dvd_screen_mode"] = "normal"
        # Launched Games (libretro) get the same independent Aspect Ratio /
        # Screen Mode pair as DVD, set from the Games tab's MENU sub-menu.
        # Unlike DVD's pair (which still needs a boot-lock pass), this one
        # is force-locked to "4:3" below on real 4:3 hardware from the start.
        if "game_aspect_ratio" not in config:
            config["game_aspect_ratio"] = "16:9"
        if "game_screen_mode" not in config:
            config["game_screen_mode"] = "normal"
        if "pixel_orbiter_enabled" not in config:
            config["pixel_orbiter_enabled"] = True
        # --- Legacy Bezel-forced-4:3 migration -----------------------
        # Bezel (fake_43_border_enabled) used to force aspect_ratio /
        # dvd_aspect_ratio / game_aspect_ratio to "4:3" the moment it
        # turned on, stashing the real pre-Bezel value in
        # _aspect_ratio_before_bezel / _dvd_aspect_ratio_before_bezel /
        # _game_aspect_ratio_before_bezel so it could be put back once
        # Bezel turned off again (see _restore_aspect_ratio_after_bezel_
        # off). That forcing was removed -- Bezel's own per-axis mapping
        # (_bezel_composite_geometry) already fits whatever aspect the
        # content actually is edge-to-edge into the cutout, so pre-boxing
        # to 4:3 upstream only pillarboxed the picture before Bezel ever
        # saw it, then stretched those baked-in black bars into the
        # cutout along with the picture.
        #
        # But a save made while Bezel was already on, from before that
        # fix, still has its real values sitting forced at "4:3" with the
        # stash keys present -- and _restore_aspect_ratio_after_bezel_off
        # only ever runs on a Bezel ON->OFF toggle, so a save where Bezel
        # has stayed continuously on since before the fix landed never
        # gets it back on its own; the person would have to know to
        # toggle Bezel off and back on themselves. Restoring here instead
        # -- on every load, unconditionally, regardless of Bezel's
        # current on/off state -- means it self-heals the moment the app
        # starts, without that manual step. Safe to run every time: once
        # the stash keys are gone this is a no-op on every later load,
        # and if _detected_aspect_ratio is genuinely "4:3" (real 4:3
        # hardware) the forced-back-to-"4:3" pass just below overwrites
        # whatever this restores anyway, so it can never fight that lock.
        for _live_key, _stash_key in (
            ("aspect_ratio", "_aspect_ratio_before_bezel"),
            ("dvd_aspect_ratio", "_dvd_aspect_ratio_before_bezel"),
            ("game_aspect_ratio", "_game_aspect_ratio_before_bezel"),
        ):
            if _stash_key in config:
                config[_live_key] = config.pop(_stash_key)
        if not config.get("_static_color_migrated"):
            if config.get("static_noise_color") == "white":
                config["static_noise_color"] = "grey"
            config["_static_color_migrated"] = True
        if self._detected_aspect_ratio is not None:
            if self._detected_aspect_ratio == "4:3":
                # Real 4:3 (CRT) hardware always wins -- the
                # simulated 4:3 preview and Bezel are both
                # irrelevant on it (see _full_tv_border_geometry
                # and _package_pillarbox_active), so there is
                # nothing meaningful to restore here.
                config["aspect_ratio"] = "4:3"
                # Launched Games' own Aspect Ratio locks the same way --
                # see get_games_sub_menu_rows / the GAMES_SUB_MENU
                # "game_aspect_ratio" row handler, which greys the row
                # out and no-ops Select while this is true.
                config["game_aspect_ratio"] = "4:3"
                # DVD player's own Aspect Ratio locks the same way -- real
                # 4:3 hardware can't show a 16:9 DVD picture any more than
                # it can a 16:9 game or the main Video tab content, so this
                # closes the gap where dvd_aspect_ratio could still be left
                # at (or switched to) "16:9" on a 4:3 boot. See the DVD
                # sub-menu's "dvd_aspect_ratio" row handler/UI, which needs
                # the same greyed-out/no-op treatment as game_aspect_ratio
                # above for this to be enforced end-to-end.
                config["dvd_aspect_ratio"] = "4:3"
            else:
                # Ordinary (16:9) hardware: restore whatever
                # Aspect Ratio / Bezel preview was last chosen.
                # setdefault so a genuinely first-ever run (no
                # saved value at all) still starts at the
                # hardware's own aspect ratio.
                config.setdefault("aspect_ratio", self._detected_aspect_ratio)
        return config

    def _build_channels_db(self, raw_channels):
        """Build a full channels_db dict (all 40 channel slots, every field
        defaulted) from a raw 'channels' dict loaded from disk. `raw_channels`
        may be partial or empty -- an empty dict yields the same fresh-install
        defaults a brand new main project or a brand new server slot gets."""
        channels_db = {}
        _VIS_STYLE_MIGRATIONS = {
            "KaleidoscopeEdge": "PrismShards",
            "KaleidoscopeBloom": "LiquidChrome",
            "StainedGlass": "LiquidChrome",
            "RibbonWave": "PulseRings",
            "ChromaWave": "WarpTunnel",
            "AuroraPeaks": "GlowBraid",
        }
        _SCHED_MODE_MIGRATIONS = {
            "random":       "random_slots",
            "round_robin":  "one_slot",
            "two_episodes": "two_slots",
        }
        for i in range(5, 45):
            ch_str = str(i).zfill(2)
            incoming_profile = raw_channels.get(ch_str, {})

            saved_schedules = incoming_profile.get("schedules", {})

            _raw_vis_style = incoming_profile.get("visualizer_style", "Random")
            _vis_style = _VIS_STYLE_MIGRATIONS.get(_raw_vis_style, _raw_vis_style)

            _explicit_block_mode = incoming_profile.get("block_mode")
            if _explicit_block_mode in ("full_day", "segmented", "six_block"):
                _block_mode = _explicit_block_mode
            else:
                _six_keys = ("12-4am", "4-8am", "8-12pm", "12-4pm", "4-8pm", "8-12am")
                _seg_keys = ("Morning", "Evening", "Night")
                _has_six = any(saved_schedules.get(k) for k in _six_keys)
                _has_seg = any(saved_schedules.get(k) for k in _seg_keys)
                _has_fullday = bool(saved_schedules.get("Full Day"))
                if _has_six and not _has_fullday:
                    _block_mode = "six_block"
                elif _has_seg and not _has_fullday:
                    _block_mode = "segmented"
                else:
                    _block_mode = "full_day"

            channels_db[ch_str] = {
                "active":            incoming_profile.get("active", True if ch_str == "05" else False),
                "is_visualizer":     incoming_profile.get("is_visualizer", False),
                "visualizer_style":  _vis_style,
                "name":              incoming_profile.get("name", f"STATION {ch_str}"),

                "scheduling_mode": _SCHED_MODE_MIGRATIONS.get(
                    incoming_profile.get("scheduling_mode", "random_slots"),
                    incoming_profile.get("scheduling_mode", "random_slots"),
                ),

                "episode_order_mode": incoming_profile.get("episode_order_mode", "sequential"),

                "block_mode": _block_mode,

                "pair_threshold_minutes": incoming_profile.get("pair_threshold_minutes", 15),

                "commercials_enabled":   incoming_profile.get("commercials_enabled", False),
                "commercial_placement":  incoming_profile.get("commercial_placement", "interrupt_half_hour"),
                "intros_enabled":        incoming_profile.get("intros_enabled", False),
                "outros_enabled":        incoming_profile.get("outros_enabled", False),

                "schedules": {
                    "Morning":      saved_schedules.get("Morning",      []),
                    "Evening":      saved_schedules.get("Evening",      []),
                    "Night":        saved_schedules.get("Night",        []),
                    "Full Day":     saved_schedules.get("Full Day",     []),
                    # 4-HOUR ("six_block") buckets — see SIX_HOUR_BLOCKS.
                    "12-4am":       saved_schedules.get("12-4am",       []),
                    "4-8am":        saved_schedules.get("4-8am",        []),
                    "8-12pm":       saved_schedules.get("8-12pm",       []),
                    "12-4pm":       saved_schedules.get("12-4pm",       []),
                    "4-8pm":        saved_schedules.get("4-8pm",        []),
                    "8-12am":       saved_schedules.get("8-12am",       []),
                    "Commercials":  saved_schedules.get("Commercials",  []),
                    "Intros":       saved_schedules.get("Intros",       []),
                    "Outros":       saved_schedules.get("Outros",       []),
                    "Music Tracks": saved_schedules.get("Music Tracks", []),
                    "Marathon":     saved_schedules.get("Marathon",     []),
                },

                "holiday_schedules": {
                    "halloween":    incoming_profile.get("holiday_schedules", {}).get("halloween",    []),
                    "christmas":    incoming_profile.get("holiday_schedules", {}).get("christmas",    []),
                    "valentine":    incoming_profile.get("holiday_schedules", {}).get("valentine",    []),
                    "thanksgiving": incoming_profile.get("holiday_schedules", {}).get("thanksgiving", []),
                    "new_year":     incoming_profile.get("holiday_schedules", {}).get("new_year",     []),
                },

                "playback_log": incoming_profile.get("playback_log", {}),

                "playback_anchor": incoming_profile.get("playback_anchor", {}),

                "marathon_anchor_epoch": incoming_profile.get("marathon_anchor_epoch"),
            }
        return channels_db

    def _start_resume_probe(self, channels_db):
        """RETIRED (see notes below) -- kept as a documented no-op so every
        call site (normal boot, fresh install, server-slot reload,
        remote-schedule disconnect) doesn't need to be touched.

        This used to spin up a background thread that walked every channel's
        schedules AND holiday_schedules looking for the -1 'unprobed'
        duration placeholder and filling it in. Its per-file gate,
        _wait_out_active_playback(), blocked in a `while True: sleep(0.5)`
        loop until NOTHING anywhere was playing video -- on a 24/7 channel
        sim that condition is essentially never true, so in practice this
        thread got stuck waiting the first time it hit an unprobed file
        while anything was on, and the rest of that channel's -1 entries
        (and everything after it) were silently never reached. Confirmed
        in the field: 7 files stuck at -1 on one channel, 10 tracks stuck
        on another, no error ever logged for any of them.

        retro_tv_emulator.py's _probe_upcoming_media() already does this
        job, correctly: it primes the near-term window synchronously
        before the first frame renders, then runs an always-on background
        pass (_gather_deep_backlog_queue) that walks the rest of every
        channel's current schedule on a loop, throttled down -- not
        stopped -- while something's playing. It reads db.channels_db
        fresh on every pass, so a server-slot switch or remote-schedule
        disconnect needs no separate kickoff the way this method used to
        provide; the very next pass already sees whatever channels_db is
        now current.

        The one thing this method covered that the ambient scanner
        doesn't is holiday_schedules -- not in use on any channel here,
        so that gap is moot. If holiday schedules are added later, the
        ambient scanner's coverage should be extended to include them
        instead of reviving this thread.
        """
        return

    def _load_channels_from_path(self, path):
        """Load channels_db from `path`, independent of whatever config_path
        is doing. Used by load_settings_async at startup (when channels_path
        already differs from config_path -- not expected via the current UI,
        but handled defensively) and by reload_channels_from() any time
        'Make Active' or 'leave server mode' repoints channels_path."""
        raw_channels = {}
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                raw_channels = data.get("channels", {})
            except Exception as load_err:
                _media_warning(
                    "channels_load_error",
                    "Channel source read failed path=%s: %s",
                    path, load_err,
                )
                raw_channels = {}
        elif path != self.config_path:
            log.error("Active channel source is missing; using defaults: %s", path)
        self.channels_db = self._build_channels_db(raw_channels)
        self._start_resume_probe(self.channels_db)

    def reload_channels_from(self, path):
        """Core 'Make Active' / 'leave server mode' mechanic (see
        SERVER_FEATURE_PLAN_1_.md §2.3): flush whatever's currently in
        channels_db back to wherever channels_path currently points (the
        main project, or a different server slot if one was already
        active), then repoint channels_path at `path` and reload
        channels_db from there. db.config and config_path are never
        touched by this call."""
        try:
            target_path = os.fspath(path)
        except (TypeError, ValueError):
            log.error("Channel redirect rejected invalid path: %r", path)
            return False

        old_path = self.channels_path
        self.save_channels(force=True)  # flush to the OLD path first
        self.channels_path = target_path
        self._load_channels_from_path(target_path)
        log.info(
            "Channel source redirected old=%s new=%s exists=%s",
            old_path, target_path, os.path.exists(target_path),
        )
        return True

    @staticmethod
    def _remote_channel_has_content(channel):
        """Return whether a remote channel actually owns scheduled content.

        Server slot files contain a full, defaulted channel shape after they
        have been loaded once.  Presence of a channel key, or an ``active``
        flag, therefore is not enough to lock it on the User side.  A channel
        is server-owned when one of its broadcast/visualizer/holiday
        schedules contains at least one item.
        """
        if not isinstance(channel, dict):
            return False
        schedules = channel.get("schedules", {})
        if isinstance(schedules, dict) and any(
                isinstance(items, (list, tuple)) and len(items) > 0
                for items in schedules.values()):
            return True
        holidays = channel.get("holiday_schedules", {})
        if isinstance(holidays, dict) and any(
                isinstance(items, (list, tuple)) and len(items) > 0
                for items in holidays.values()):
            return True
        return False

    def _fold_live_unlocked_into_snapshot_locked(self):
        """Caller holds _remote_schedule_lock. While a Server is connected,
        channels the Server does NOT own stay editable. Copy their live
        records back into the saved local snapshot so a channel the User
        creates mid-session is saved, survives the next remote update, and
        is restored on disconnect."""
        snap = self._remote_local_channels_snapshot
        if not self._remote_schedule_active or snap is None:
            return
        for key, rec in list(self.channels_db.items()):
            if key in self._remote_locked_channels or not isinstance(rec, dict):
                continue
            clean = copy.deepcopy(rec)
            clean.pop("server_locked", None)
            snap[key] = clean

    def apply_remote_schedule(self, channels, playback=None):
        """Apply a live Server schedule without destroying local ownership.

        The first remote snapshot captures the exact local channel state.
        Only channels that contain scheduled content in the Server payload are
        overlaid and marked ``server_locked``.  Empty/unmentioned channels
        continue to expose the User's local records.  Subsequent snapshots
        replace the overlay, so server-side additions and removals are
        reflected live while the saved local snapshot remains untouched.
        """
        if not isinstance(channels, dict):
            return False

        normalized = {}
        for key, value in channels.items():
            try:
                channel_num = int(str(key))
            except (TypeError, ValueError):
                continue
            if 5 <= channel_num <= 44 and isinstance(value, dict):
                normalized[str(channel_num).zfill(2)] = value

        with self._remote_schedule_lock:
            if not self._remote_schedule_active:
                self._remote_local_channels_snapshot = copy.deepcopy(self.channels_db)
                self._remote_schedule_active = True

            self._fold_live_unlocked_into_snapshot_locked()
            local_channels = self._remote_local_channels_snapshot or {}
            locked = {
                channel_key for channel_key, channel in normalized.items()
                if self._remote_channel_has_content(channel)
            }
            remote_channels = self._build_channels_db(normalized)
            merged = {}

            for channel_num in range(5, 45):
                channel_key = str(channel_num).zfill(2)
                if channel_key in locked:
                    merged[channel_key] = copy.deepcopy(remote_channels[channel_key])
                    merged[channel_key]["server_locked"] = True
                else:
                    # Preserve the exact pre-connect local record, including
                    # a deliberate local ``active: False`` choice.
                    local_record = local_channels.get(channel_key)
                    if local_record is None:
                        local_record = self._build_channels_db({}).get(channel_key, {})
                    merged[channel_key] = copy.deepcopy(local_record)
                    merged[channel_key].pop("server_locked", None)

            self.channels_db = merged
            self._remote_locked_channels = locked
            _mode = str((playback or {}).get("screen_mode", "")).strip().lower() if isinstance(playback, dict) else ""
            self.remote_playback = ({"screen_mode": _mode}
                                    if _mode in ("normal", "stretch", "crop", "panscan") else {})
        return True

    def remote_screen_mode_for(self, ch_key):
        """Server's screen mode if ch_key is a Server-streamed channel, else None
        (your own channels always use your own setting)."""
        try:
            mode = self.remote_playback.get("screen_mode") if self._remote_schedule_active else None
            if mode and str(ch_key).zfill(2) in self._remote_locked_channels:
                return mode
        except Exception:
            pass
        return None

    def disconnect_remote_schedule(self):
        """Restore the exact local channel state captured at connect time."""
        with self._remote_schedule_lock:
            if not self._remote_schedule_active:
                return False
            self._fold_live_unlocked_into_snapshot_locked()
            restored = copy.deepcopy(self._remote_local_channels_snapshot or {})
            self.channels_db = restored
            self._remote_schedule_active = False
            self._remote_local_channels_snapshot = None
            self._remote_locked_channels = set()
            self.remote_playback = {}
        self._start_resume_probe(self.channels_db)
        return True

    # ------------------------------------------------------------------
    # Server slot management (SERVER_FEATURE_PLAN_1_.md §2.2, §6.1) —
    # Build Order Step 1. Slot files are channels-only ({"schema_version":
    # ..., "channels": {...}}); the slot_id -> display name mapping lives
    # in db.config["server_slots"] instead, per plan §6.1, since config is
    # never redirected and this list is small. ui.py / retro_tv_emulator.py
    # already call these methods for the Manage Servers screen.
    # ------------------------------------------------------------------

    SERVER_SLOT_IDS = tuple(f"server{i}" for i in range(1, 7))
    SERVER_SLOT_SCHEMA_VERSION = 1

    def get_server_slot_path(self, slot_id):
        """Resolve a slot_id ('server1'..'server6') to its on-disk file:
        <base_dir>/server/.json/serverN/retro.config.json."""
        return os.path.join(self.base_dir, "server", ".json", slot_id, "retro.config.json")

    def list_server_slots(self):
        """Return [(slot_id, name), ...] for every currently-named slot,
        in db.config['server_slots'] insertion order."""
        server_slots = self.config.get("server_slots", {})
        return [(slot_id, name) for slot_id, name in server_slots.items()]

    def next_free_server_slot(self):
        """First unused slot id (1-6), or None if all 6 are already taken."""
        used = set(self.config.get("server_slots", {}).keys())
        for slot_id in self.SERVER_SLOT_IDS:
            if slot_id not in used:
                return slot_id
        return None

    def create_server_slot(self, name):
        """Create a new named server slot: pick the next free slot, write
        its (empty, schema-stamped) channels-only file, record the
        name -> slot mapping in config, and save config. Returns the new
        slot_id, or None if all 6 slots are already in use or the file
        couldn't be written."""
        slot_id = self.next_free_server_slot()
        if slot_id is None:
            return None
        path = self.get_server_slot_path(slot_id)
        try:
            dir_name = os.path.dirname(path)
            os.makedirs(dir_name, exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(
                    {"schema_version": self.SERVER_SLOT_SCHEMA_VERSION, "channels": {}},
                    f, indent=4,
                )
            os.replace(tmp_path, path)
        except Exception as create_err:
            log.error("Slot file create failed path=%s: %s", path, create_err)
            return None
        server_slots = dict(self.config.get("server_slots", {}))
        server_slots[slot_id] = name
        self.config["server_slots"] = server_slots
        self.save_config()
        return slot_id

    def rename_server_slot(self, slot_id, new_name):
        """Edit a slot's display label only -- never touches its folder or
        file. Returns True if the slot existed and was renamed."""
        server_slots = dict(self.config.get("server_slots", {}))
        if slot_id not in server_slots:
            return False
        server_slots[slot_id] = new_name
        self.config["server_slots"] = server_slots
        self.save_config()
        return True

    def delete_server_slot(self, slot_id):
        """Remove a named slot. If it's the currently-active-for-edit slot,
        redirect channels_db back to the main project first (via
        reload_channels_from) so nothing's left pointed at a file about to
        disappear, then drop it from config and delete its folder (with a
        safety check that the delete path is actually inside this
        project's own server/.json folder). Returns True if the slot
        existed and was deleted."""
        server_slots = dict(self.config.get("server_slots", {}))
        if slot_id not in server_slots:
            return False

        slot_path = self.get_server_slot_path(slot_id)
        if self.channels_path == slot_path:
            self.reload_channels_from(self.config_path)
            self.config["active_server_slot"] = None

        del server_slots[slot_id]
        self.config["server_slots"] = server_slots
        self.save_config()

        slot_dir = os.path.dirname(slot_path)
        server_root = os.path.join(self.base_dir, "server", ".json")
        try:
            if os.path.isdir(slot_dir) and os.path.commonpath(
                [os.path.abspath(slot_dir), os.path.abspath(server_root)]
            ) == os.path.abspath(server_root):
                shutil.rmtree(slot_dir)
        except Exception as delete_err:
            _media_warning(
                "server_slot_delete_error",
                "[DB ERR] Could not delete server slot folder %s: %s",
                slot_dir, delete_err,
            )
        return True

    # ------------------------------------------------------------------
    # "Make Active" / "leave server mode" (Build Order Step 2, plan §2.3,
    # §3b). db.config["active_server_slot"] (a slot_id, or None) tracks
    # which slot -- if any -- is currently active-for-edit. It's purely a
    # bookkeeping/UI flag so the Manage Servers screen can show which slot
    # channels_db currently points to; the actual redirect is Step 0's
    # reload_channels_from, which already flushes the old path before
    # switching (so making Slot B active while Slot A is active flushes A
    # first, satisfying the single-active-slot rule).
    # ------------------------------------------------------------------

    def make_server_slot_active(self, slot_id):
        """'Make Active': flush whatever's currently in channels_db back to
        wherever channels_path currently points (main project, or a
        different slot if one was already active), then redirect
        channels_db to slot_id's own file. Returns True on success, False
        if slot_id isn't a real (named) slot."""
        server_slots = self.config.get("server_slots", {})
        if slot_id not in server_slots:
            return False
        self.reload_channels_from(self.get_server_slot_path(slot_id))
        self.config["active_server_slot"] = slot_id
        self.save_config()
        return True

    def leave_server_slot(self):
        """'Leave this mode' (switch role card to User, or turn
        server_option off): redirect channels_db back to the main
        project's own channel data. Safe to call even when no slot is
        currently active (no-op in that case)."""
        if self.config.get("active_server_slot") is None and self.channels_path == self.config_path:
            return
        self.reload_channels_from(self.config_path)
        self.config["active_server_slot"] = None
        self.save_config()

    def load_settings_async(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    data = json.load(f)
                self.config = self._apply_config_defaults(data.get("config", {}))
                raw_channels = data.get("channels", {})

                # The main project file's own channels are always cached
                # here regardless of which path channels_db is currently
                # backed by, so a config-only save while a server slot is
                # active can't accidentally clobber the main project's real
                # channel data with whatever slot happens to be in memory.
                self._main_channels_cache = raw_channels

                if self.channels_path == self.config_path:
                    self.channels_db = self._build_channels_db(raw_channels)
                    self._load_playback_anchors()
                    self.ready_flag = True
                    self._start_resume_probe(self.channels_db)
                else:
                    # channels_path was already redirected to a server slot
                    # before this load ran (not expected from the current
                    # UI flow -- slot selection isn't persisted across a
                    # restart today -- but handled defensively rather than
                    # silently ignored).
                    self.ready_flag = True
                    self._load_channels_from_path(self.channels_path)

                return
            except Exception as load_err:
                _media_warning(
                    "settings_load_error",
                    "[DB ERROR] Settings reload recovery pass triggered: %s",
                    load_err,
                )
                self._recovered_from_load_error = True
                try:
                    if os.path.exists(self.config_path):
                        backup_path = self.config_path + f".corrupt-{int(time.time())}.bak"
                        shutil.copy2(self.config_path, backup_path)
                except Exception as backup_err:
                    _media_warning(
                        "settings_backup_error",
                        "[DB ERROR] Could not back up unparsed settings file: %s",
                        backup_err,
                    )

        self.config = {
            "global_volume": 70,
            "current_theme": "DefaultBlueGreen",
            "show_controls_on_launch": True,
            "tv_guide_enabled": True,
            "start_on_boot": False,
            "kiosk_mode_enabled": False,
            "server_mode_enabled": False,
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
            "transition_type": "black",
            "letterbox_color": "black",
            "static_noise_color": "white",
            "_static_color_migrated": True,
            "volume_bar_style": "1",
            "aspect_ratio": self._detected_aspect_ratio or "16:9",
            "fake_43_border_enabled": False,
            "bezel_style": 1,
            "fake_43_scanlines_enabled": False,
            "fake_static_noise_enabled": False,
            "loudness_scan_enabled": True,
            "screen_mode": "normal",
            # DVD uses independent aspect-ratio and screen-mode settings.
            "dvd_aspect_ratio": "16:9",
            "dvd_screen_mode": "normal",
            "pixel_orbiter_enabled": True,
            # "Don't show this message again" checkboxes for the five
            # burn-in-family Yes/No warnings (switching a music/visualizer
            # channel off Random to a fixed style, per-console/handheld TV
            # bezel, full-program bezel, turning off Pixel Orbiter, turning
            # off the Games-tab Screen Saver). Off by default -- each
            # warning shows every time it's triggered until the user
            # explicitly checks its box.
            "burn_in_protection_skip_confirm": False,
            "console_bezel_skip_confirm": False,
            "program_bezel_skip_confirm": False,
            "pixel_orbiter_skip_confirm": False,
            "ch03_screensaver_skip_confirm": False,
            "screen_offset_x": 0,
            "screen_offset_y": 0,
            "screen_edge_left": 0,
            "screen_edge_right": 0,
            "screen_edge_top": 0,
            "screen_edge_bottom": 0,
            "remote_bindings": {},
        }

        # Fresh install (no file at config_path yet): build default channels
        # through the same helper a server slot uses, so "empty raw_channels"
        # always yields identical results everywhere in the app.
        self._main_channels_cache = {}
        self.channels_db = self._build_channels_db({})
        if not getattr(self, "_recovered_from_load_error", False):
            self.save_settings()
        self.ready_flag = True
        self._start_resume_probe(self.channels_db)

    def _next_save_generation(self):
        with self._save_request_lock:
            self._save_generation += 1
            return self._save_generation

    def _next_channels_save_generation(self):
        with self._save_request_lock:
            self._channels_save_generation += 1
            return self._channels_save_generation

    def _snapshot_config(self):
        """Return a stable copy of just the config tree."""
        for attempt in range(8):
            try:
                return copy.deepcopy(self.config)
            except RuntimeError as snapshot_err:
                if attempt == 7:
                    _media_warning(
                        "config_snapshot_retries_exhausted",
                        "[DB ERR] Config snapshot retries exhausted (%s); "
                        "skipping this write.",
                        snapshot_err,
                    )
                    return None
                time.sleep(0.01 * (attempt + 1))
            except Exception as snapshot_err:
                _media_warning(
                    "config_snapshot_error",
                    "[DB ERR] Config snapshot failed: %s; skipping this write.",
                    snapshot_err,
                )
                return None

    def _snapshot_channels(self):
        """Return a stable copy of just the channels tree."""
        for attempt in range(8):
            try:
                # While a Server is connected, channels_db is a temporary
                # remote overlay.  Any of the many existing save_settings()
                # call sites must continue saving the User's local records,
                # never the streamed records or the transient
                # server_locked flags.
                with self._remote_schedule_lock:
                    self._fold_live_unlocked_into_snapshot_locked()
                    source = (
                        self._remote_local_channels_snapshot
                        if self._remote_schedule_active
                        and self._remote_local_channels_snapshot is not None
                        else self.channels_db
                    )
                    return copy.deepcopy(source)
            except RuntimeError as snapshot_err:
                if attempt == 7:
                    _media_warning(
                        "channels_snapshot_retries_exhausted",
                        "[DB ERR] Channels snapshot retries exhausted (%s); "
                        "skipping this write.",
                        snapshot_err,
                    )
                    return None
                time.sleep(0.01 * (attempt + 1))
            except Exception as snapshot_err:
                _media_warning(
                    "channels_snapshot_error",
                    "[DB ERR] Channels snapshot failed: %s; skipping this write.",
                    snapshot_err,
                )
                return None

    def _snapshot_settings(self):
        """Return a stable copy of both mutable settings trees. Still used
        by save_settings()/save_settings_async(), which remain the
        'save everything' entry points every existing call site uses."""
        config_snapshot = self._snapshot_config()
        if config_snapshot is None:
            return None
        channels_snapshot = self._snapshot_channels()
        if channels_snapshot is None:
            return None
        return {"config": config_snapshot, "channels": channels_snapshot}

    def _snapshot_playback_anchors(self):
        """Return a stable copy of just the per-channel playback_anchor
        data -- the runtime half of the persistence split. Deliberately
        excludes everything else in channels_db (schedules, rotation,
        broken_files, durations): those are durable state and stay on the
        save_settings()/save_channels() path.

        Uses the same retry-on-RuntimeError pattern as _snapshot_channels
        (channels_db can be mutated by another thread mid-iteration), but
        the result here is tiny -- one small dict per channel that actually
        has an anchor -- so a retry is cheap even under real contention.
        """
        for attempt in range(8):
            try:
                with self._channels_lock:
                    return {
                        ch_key: copy.deepcopy(ch.get("playback_anchor"))
                        for ch_key, ch in self.channels_db.items()
                        if ch.get("playback_anchor")
                    }
            except RuntimeError as snapshot_err:
                if attempt == 7:
                    _media_warning(
                        "runtime_snapshot_retries_exhausted",
                        "[DB ERR] Playback-anchor snapshot retries exhausted (%s); "
                        "skipping this write.",
                        snapshot_err,
                    )
                    return None
                time.sleep(0.01 * (attempt + 1))
            except Exception as snapshot_err:
                _media_warning(
                    "runtime_snapshot_error",
                    "[DB ERR] Playback-anchor snapshot failed: %s; skipping this write.",
                    snapshot_err,
                )
                return None

    def _next_runtime_save_generation(self):
        with self._save_request_lock:
            self._runtime_save_generation += 1
            return self._runtime_save_generation

    def _write_runtime_snapshot(self, anchors_snapshot, generation):
        """Atomically write runtime_path unless this write is stale. Same
        atomic-write primitive as the durable saves (temp file + fsync +
        os.replace), but on its own lock/generation/file, so this can never
        race against or block on a durable save in progress, and vice versa.
        """
        try:
            with self._runtime_lock:
                with self._save_request_lock:
                    if generation != self._runtime_save_generation:
                        return
                tmp_path = self.runtime_path + ".tmp"
                self._atomic_write_json(tmp_path, self.runtime_path, {"anchors": anchors_snapshot})
        except Exception as save_err:
            log.error("Runtime (playback_anchor) write failed path=%s: %s", self.runtime_path, save_err)

    def save_playback_anchor(self, force=False):
        """Persist just the runtime playback_anchor data. This is the
        lightweight counterpart to save_settings_async() that
        _confirm_playback_started and the other pure-anchor call sites use
        instead of triggering a full durable save on every confirmed play.

        Async by default, same contract as save_settings_async -- callers
        that need the write to have landed before continuing (e.g. right
        before a deliberate shutdown) can pass force=True.
        """
        if not getattr(self, "ready_flag", False):
            return
        anchors_snapshot = self._snapshot_playback_anchors()
        if anchors_snapshot is None:
            return
        generation = self._next_runtime_save_generation()
        if force:
            self._write_runtime_snapshot(anchors_snapshot, generation)
        else:
            threading.Thread(
                target=self._write_runtime_snapshot,
                args=(anchors_snapshot, generation),
                daemon=True,
            ).start()

    def _load_playback_anchors(self):
        """Merge runtime_path's anchors back into an already-built
        channels_db. Purely informational/diagnostic at boot -- per the
        live-TV-only restart policy, a loaded anchor is never trusted to
        resume playback across a restart (see calculate_slotted_playback_
        state's cold-boot handling). It's still loaded because it's cheap,
        harmless, and useful for crash forensics (what was last confirmed
        playing before an unclean shutdown) -- and because the SAME anchor
        data still drives the legitimate same-session glitch-recovery path
        (_anchor_is_live_video) once fresh, real confirmations start
        arriving again in the new session.
        """
        if not os.path.exists(self.runtime_path):
            return
        try:
            with open(self.runtime_path, "r") as f:
                data = json.load(f)
            anchors = data.get("anchors", {})
            for ch_key, anchor in anchors.items():
                if ch_key in self.channels_db and isinstance(anchor, dict):
                    self.channels_db[ch_key]["playback_anchor"] = anchor
        except Exception as load_err:
            _media_warning(
                "runtime_load_error",
                "[DB ERR] Playback-anchor runtime load failed path=%s: %s (continuing "
                "without prior anchors -- never fatal, since anchors are never trusted "
                "to resume playback across a restart anyway)",
                self.runtime_path, load_err,
            )

    @staticmethod
    def _atomic_write_json(tmp_path, final_path, data):
        """Write `data` to `tmp_path` as JSON, force it to physical disk,
        then atomically rename it onto `final_path`.

        os.replace() alone only guarantees the *directory entry* swap is
        atomic -- it says nothing about whether the tmp file's bytes have
        actually left the OS/drive write-back cache. On a real power loss
        (as opposed to just a process crash), Windows can journal the
        rename before the cached file content is flushed, so a reboot can
        come back with a `final_path` that exists but is 0 bytes or
        truncated -- exactly the "looks like the config never existed"
        failure mode. flush()+fsync() before the rename closes that
        window: once fsync() returns, the tmp file's content is durable,
        so whichever file is on disk after a crash -- old or new -- is
        always a complete, valid one.
        """
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, final_path)

    def _write_config_snapshot(self, config_snapshot, generation):
        """Atomically write config_path unless this write is stale. Always
        writes retro_config.json as {"config": ..., "channels": ...}, using
        _main_channels_cache for the channels half so a config-only save
        never clobbers the main project's own channel data with whatever a
        currently-active server slot happens to hold in memory."""
        try:
            with self._save_lock:
                with self._save_request_lock:
                    if generation != self._save_generation:
                        return
                tmp_path = self.config_path + ".tmp"
                self._atomic_write_json(
                    tmp_path, self.config_path,
                    {"config": config_snapshot, "channels": self._main_channels_cache},
                )
        except Exception as save_err:
            log.error("Main config write failed path=%s: %s", self.config_path, save_err)

    def _write_channels_snapshot(self, channels_snapshot, path, generation):
        """Atomically write `path` (whichever file channels_path currently
        points at) unless this write is stale.

        - If `path` is config_path (normal, non-server case): update
          _main_channels_cache and write the *combined* retro_config.json
          via the same lock/tmp-file path _write_config_snapshot uses, so
          the two writers never race on the same physical file.
        - Otherwise (`path` is a server slot's own file): write a
          channels-only {"channels": ...} shape there instead -- slot files
          never need the full config shape (SERVER_FEATURE_PLAN_1_.md §2.2).
        """
        try:
            with self._channels_lock:
                with self._save_request_lock:
                    if generation != self._channels_save_generation:
                        return
                if path == self.config_path:
                    self._main_channels_cache = channels_snapshot
                    with self._save_lock:
                        tmp_path = self.config_path + ".tmp"
                        self._atomic_write_json(
                            tmp_path, self.config_path,
                            {"config": copy.deepcopy(self.config), "channels": channels_snapshot},
                        )
                else:
                    dir_name = os.path.dirname(path)
                    if dir_name:
                        os.makedirs(dir_name, exist_ok=True)
                    tmp_path = path + ".tmp"
                    self._atomic_write_json(tmp_path, path, {"channels": channels_snapshot})
        except Exception as save_err:
            log.error("Channel source write failed path=%s: %s", path, save_err)

    def save_config(self):
        """Save just db.config to config_path. Never touches channels_db or
        channels_path, so this is safe to call at any time regardless of
        whether a server slot is currently active."""
        if not getattr(self, "ready_flag", False):
            return
        config_snapshot = self._snapshot_config()
        if config_snapshot is None:
            return
        self._write_config_snapshot(config_snapshot, self._next_save_generation())

    def save_channels(self, force=False):
        """Save just db.channels_db to wherever channels_path currently
        points (main project file or an active server slot). Never touches
        db.config."""
        if not getattr(self, "ready_flag", False):
            return
        channels_snapshot = self._snapshot_channels()
        if channels_snapshot is None:
            return
        generation = self._next_channels_save_generation()
        path = self.channels_path
        if force:
            self._write_channels_snapshot(channels_snapshot, path, generation)
        else:
            threading.Thread(
                target=self._write_channels_snapshot,
                args=(channels_snapshot, path, generation),
                daemon=True,
            ).start()

    def save_settings(self):
        """Save everything -- config AND channels. Kept as the existing
        'save it all' entry point so the ~190 existing call sites across
        the app that just want a full save keep working unmodified.

        Normal (non-server) case: channels_path == config_path, so this
        does exactly one combined atomic write, same as before this
        refactor -- no extra disk I/O just because the internals are now
        split. Only when a server slot is active (channels_path points
        elsewhere) does this become two separate writes, because two
        different files genuinely need updating."""
        if not getattr(self, "ready_flag", False):
            return
        if self.channels_path == self.config_path:
            snapshot = self._snapshot_settings()
            if snapshot is None:
                return
            self._main_channels_cache = snapshot["channels"]
            self._write_config_snapshot(snapshot["config"], self._next_save_generation())
        else:
            self.save_config()
            self.save_channels(force=True)

    def save_settings_async(self, force=False):
        """Save everything, async by default. Same contract as before this
        refactor -- callers don't need to know or care that config and
        channels now travel through independent paths internally."""
        if not getattr(self, "ready_flag", False):
            return
        if self.channels_path == self.config_path:
            snapshot = self._snapshot_settings()
            if snapshot is None:
                return
            self._main_channels_cache = snapshot["channels"]
            generation = self._next_save_generation()
            if force:
                self._write_config_snapshot(snapshot["config"], generation)
            else:
                threading.Thread(
                    target=self._write_config_snapshot,
                    args=(snapshot["config"], generation),
                    daemon=True,
                ).start()
        else:
            self.save_config()
            self.save_channels(force=force)

    def factory_reset(self):

        # Preserve flags and settings that should survive a reset
        game_ch_flag   = self.config.get("game_channel_enabled", True)
        guide_ch_flag  = self.config.get("tv_guide_enabled", True)
        theme_name     = self.config.get("current_theme", "DefaultBlueGreen")
        theme_bg_color = self.config.get("theme_bg_color", "Blue")
        theme_ui_color = self.config.get("theme_ui_color", "Green")
        theme_bg_hue   = self.config.get("theme_bg_hue", 220)
        theme_ui_hue   = self.config.get("theme_ui_hue", 140)
        menu_opacity   = self.config.get("menu_opacity", 50)
        transition_type = self.config.get("transition_type", "black")
        remote_bindings = self.config.get("remote_bindings", {})

        # This reset (System menu's "Reset All Settings", which drives the
        # user-facing "Clear All Media" action) is scoped to the TV
        # Station/Visualizer side of the app -- the channels_db slots
        # below (05-44) and the system/display settings dict. The Games
        # channel (03) is a completely separate subsystem: console
        # selection/enablement, ROM directories, BIOS assignments, splash
        # art, scroll style, and per-console control profiles all live in
        # this same self.config dict but must survive untouched, same as
        # game_ch_flag/guide_ch_flag above -- this reset must never turn
        # off a console or undo a profile choice. ROMs, cores, BIOS files,
        # and Game-channel Music Tracks on disk are untouched either way
        # (nothing here deletes files), but without preserving these keys
        # the config reassignment below would still silently drop which
        # consoles are enabled and which profile/core/BIOS/splash each
        # one is set to.
        consoles_enabled      = self.config.get("consoles_enabled", {})
        console_rom_dir       = self.config.get("console_rom_dir", {})
        active_bios           = self.config.get("active_bios", {})
        console_profiles      = self.config.get("console_profiles", {})
        all_profiles_sync     = self.config.get("all_profiles_sync", False)
        all_profiles_sync_tgt = self.config.get("all_profiles_sync_target", 1)
        console_scroll_style  = self.config.get("console_scroll_style", "carousel")
        console_splash_enabled = self.config.get("console_splash_enabled", {})
        console_splash_path   = self.config.get("console_splash_path", {})
        dvd_splash_enabled    = self.config.get("dvd_splash_enabled", False)
        dvd_splash_path       = self.config.get("dvd_splash_path", "")

        # Reset all system/display settings to factory defaults
        self.config = {
            "global_volume": 70,
            "current_theme": theme_name,
            "show_controls_on_launch": True,
            "tv_guide_enabled": guide_ch_flag,
            "game_channel_enabled": game_ch_flag,
            "start_on_boot": False,
            "kiosk_mode_enabled": False,
            "server_mode_enabled": False,
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
            "fake_43_border_enabled": False,
            "bezel_style": 1,
            "fake_43_scanlines_enabled": False,
            "fake_static_noise_enabled": False,
            "loudness_scan_enabled": True,
            "screen_mode": "normal",
            # DVD uses independent aspect-ratio and screen-mode settings.
            "dvd_aspect_ratio": "16:9",
            "dvd_screen_mode": "normal",
            "pixel_orbiter_enabled": True,
            # "Don't show this message again" checkboxes for the five
            # burn-in-family Yes/No warnings (switching a music/visualizer
            # channel off Random to a fixed style, per-console/handheld TV
            # bezel, full-program bezel, turning off Pixel Orbiter, turning
            # off the Games-tab Screen Saver). Off by default -- each
            # warning shows every time it's triggered until the user
            # explicitly checks its box.
            "burn_in_protection_skip_confirm": False,
            "console_bezel_skip_confirm": False,
            "program_bezel_skip_confirm": False,
            "pixel_orbiter_skip_confirm": False,
            "ch03_screensaver_skip_confirm": False,
            "screen_offset_x": 0,
            "screen_offset_y": 0,
            "screen_edge_left": 0,
            "screen_edge_right": 0,
            "screen_edge_top": 0,
            "screen_edge_bottom": 0,
            "remote_bindings": remote_bindings,
            # Games-channel/console state -- carried forward untouched;
            # see the note above factory_reset's preserve-block.
            "consoles_enabled": consoles_enabled,
            "console_rom_dir": console_rom_dir,
            "active_bios": active_bios,
            "console_profiles": console_profiles,
            "all_profiles_sync": all_profiles_sync,
            "all_profiles_sync_target": all_profiles_sync_tgt,
            "console_scroll_style": console_scroll_style,
            "console_splash_enabled": console_splash_enabled,
            "console_splash_path": console_splash_path,
            "dvd_splash_enabled": dvd_splash_enabled,
            "dvd_splash_path": dvd_splash_path,
        }

        # Reset every TV Station/Visualizer channel slot (05-44) all the
        # way back to factory-blank -- identity (active/is_visualizer/
        # visualizer_style/name) included, not just schedule content.
        # _build_channels_db({}) is the same helper a fresh install / new
        # server slot uses, so an empty raw_channels dict here yields
        # identical from-scratch defaults (channel 05 active, every other
        # channel off, no channel left marked as a Visualizer) instead of
        # hand-listing the same fields. channels_db only ever covers
        # channels 05-44 -- the Games channel (03) and its ROMs/cores/
        # BIOS/profiles are a separate subsystem entirely and were never
        # part of this dict, so this line was never the source of any
        # Games-channel/console reset behavior.
        self.channels_db = self._build_channels_db({})

        self.save_settings()



