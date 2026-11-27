"""The one checklist of what must be known about a media file before it counts as fully scanned; the server scanner and the main app's bar and scan lanes all ask it."""

# ============================================================================
# SECTION: Checklist items
# ============================================================================

HEADER = "header"
BOX = "box"
GAIN = "gain"
BURNED = "burned"
# WARNING: the order below is the order the items are read in; the picture box and loudness wait for the header, and the burned-in verdict waits for the header too.
ITEMS = (HEADER, BOX, GAIN, BURNED)


# ============================================================================
# SECTION: What a file still lacks
# ============================================================================


def has_text_subtitles(entry, sidecar_found=False):
    """True when the file already has real caption text (a caption file beside it or a text subtitle track inside it), so its picture is never searched for burned-in captions."""
    if sidecar_found:
        return True
    for stream in (entry or {}).get("subtitle_streams") or []:
        if isinstance(stream, dict) and stream.get("is_text"):
            return True
    return False


def missing(entry, *, is_audio, box_version=None, gain_version=None, burned_verdict=None,
            want_gain=True, want_burned=True, sidecar_found=False):
    """The items of ITEMS the file still lacks, in reading order; entry is the library row (or {}), the versions are the newest picture-box detector and loudness scan versions (None skips that item)."""
    entry = entry or {}
    need = []
    header_done = bool(entry.get("header_probed_at") and entry.get("duration"))
    if not header_done:
        return [HEADER]
    if not is_audio and box_version is not None:
        if not (entry.get("picture_box") and (entry.get("picture_box_detector_version") or 0) >= box_version):
            need.append(BOX)
    if want_gain and gain_version is not None:
        if not (entry.get("gain") and entry.get("gain_scan_version") in (0, gain_version)):
            need.append(GAIN)
    if want_burned and not is_audio and burned_verdict is None and not has_text_subtitles(entry, sidecar_found):
        need.append(BURNED)
    return need
