"""The last schedule and file map a server sent, saved on this (User) PC so the next start can air the server's channels, build the guide and warm the files before the server has answered, the way a regular station starts from its saved channel file."""
import json
import logging
import os
import re
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
    log.warning("[FAILURE] server_boot_cache: performance_governor did not import, so the built-in numbers are used: %s", _governor_error)

CACHE_FOLDER_NAME = "streamed_server_copies"
CACHE_VERSION = 1
# WARNING: the fallback number below must match SERVER_BOOT_CACHE_MIN_SECONDS_BETWEEN_SAVES in performance_governor.py; it is used only when that module fails to import.
MIN_SECONDS_BETWEEN_SAVES = _gov.SERVER_BOOT_CACHE_MIN_SECONDS_BETWEEN_SAVES if _gov is not None else 20.0

_write_lock = threading.Lock()
_last_write = {}


# ============================================================================
# SECTION: Where the saved copy lives and what it holds
# ============================================================================


def cache_path_for(server_root, name):
    """The cache file for one server this PC streams from: <server data folder>/server/streamed_server_copies/<server name>.json (the folder of the servers' own channel files; this PC's own copy of a server's channels, in the user folder, is left alone)."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name or "")).strip().rstrip(". ") or "server"
    return os.path.join(str(server_root), CACHE_FOLDER_NAME, safe + ".json")


def _slim_files(entries):
    """Only where each file lives on the server's shares; the facts about it are already kept in this PC's library."""
    slim = []
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("p") and entry.get("s"):
            slim.append({"p": entry["p"], "s": entry.get("s"), "r": entry.get("r")})
    return slim


# ============================================================================
# SECTION: Saving and loading
# ============================================================================


def save(path, picked, schedule_message=None, file_entries=None, force=False):
    """Writes the cache (merging with what is saved); False when skipped (too soon) or when nothing could be written."""
    now = time.monotonic()
    with _write_lock:
        # WARNING: owner 2026-11-13: a save that carries the schedule is never skipped; the file facts arrive a moment before it, and their save used to make this one wait out the limit, so a server that sends its schedule once never left a usable copy.
        if not force and schedule_message is None and now - _last_write.get(path, -1e9) < MIN_SECONDS_BETWEEN_SAVES:
            return False
        held = load(path, picked.get("name")) or {}
        data = {
            "version": CACHE_VERSION,
            "name": str(picked.get("name", "")),
            "host": str(picked.get("host", "")),
            "port": picked.get("port", 47532),
            "slot_id": str(picked.get("slot_id", "")),
            "saved_at": time.time(),
            "schedule": schedule_message if schedule_message is not None else held.get("schedule"),
            "files": _slim_files(file_entries) if file_entries is not None else held.get("files", []),
        }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, path)
            _last_write[path] = now
            return True
        except Exception as exc:
            log.warning("[SERVER CACHE] could not save %s: %s", path, exc)
            return False


def load(path, name=None):
    """The saved cache, or None when there is none, it is unreadable, from another build's format, or for another server."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        # WARNING: the normal end: this PC has not streamed from that server before, so there is no saved copy.
        return None
    except Exception as exc:
        log.warning("[SERVER CACHE] could not read %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return None
    if name is not None and str(data.get("name", "")).strip().lower() != str(name).strip().lower():
        return None
    return data


def usable(data):
    """True when the cache can air channels at once: it names a host and holds a schedule."""
    return bool(isinstance(data, dict) and data.get("host") and isinstance(data.get("schedule"), dict)
                and isinstance(data["schedule"].get("channels"), dict))
