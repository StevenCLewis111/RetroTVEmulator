"""Keeps the logs folder from growing without limit.

Owns: removing old rotated app logs and stale scratch files, clearing what earlier updates left behind, and writing one line that says what is taking the space.
Does not own: the live log files themselves (app_log.txt, crash logs, vlc-debug logs), the dated settings copies in logs/config_backups (media.py keeps the newest few), or anything the owner's setup lives in (logs/controllers, logs/server).
"""

from __future__ import annotations

import logging
import os
import re
import time

log = logging.getLogger("retro_tv.log_manager")

try:
    import performance_governor as _perf_governor
except Exception:
    _perf_governor = None

# WARNING: the fallback numbers below must match LOGS_ROTATED_APP_LOGS_KEPT, LOGS_TEMP_FILE_MAX_AGE_DAYS in performance_governor.py; they are used only when that module fails to import.
LOGS_ROTATED_APP_LOGS_KEPT = _perf_governor.LOGS_ROTATED_APP_LOGS_KEPT if _perf_governor is not None else 2
LOGS_TEMP_FILE_MAX_AGE_DAYS = _perf_governor.LOGS_TEMP_FILE_MAX_AGE_DAYS if _perf_governor is not None else 7

_ROTATED_APP_LOG = re.compile(r"^app_log\d+\.txt$", re.IGNORECASE)


def _entry_size(path):
    """Bytes in a file, or in every file under a folder; 0 when unreadable."""
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
    except OSError:
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def folder_report(logs_dir, top=6):
    """One line: the logs folder's total size and its biggest entries, so a folder that grew can be explained from the log."""
    try:
        names = os.listdir(logs_dir)
    except OSError:
        return "[LOG FOLDER] %s cannot be read" % logs_dir
    sizes = sorted(((_entry_size(os.path.join(logs_dir, name)), name) for name in names), reverse=True)
    total = sum(size for size, _name in sizes)
    parts = ", ".join("%s %.1f MB" % (name, size / 1048576.0) for size, name in sizes[:top] if size)
    return "[LOG FOLDER] %.1f MB in %d entries: %s" % (total / 1048576.0, len(sizes), parts or "empty")


def prune_logs(logs_dir, now=None):
    """Removes rotated app logs beyond the newest LOGS_ROTATED_APP_LOGS_KEPT and files in logs/temp older than LOGS_TEMP_FILE_MAX_AGE_DAYS. Returns (files removed, bytes freed)."""
    now = time.time() if now is None else now
    removed, freed = 0, 0

    def remove(path):
        nonlocal removed, freed
        try:
            size = os.path.getsize(path)
            os.remove(path)
        except OSError:
            return
        removed += 1
        freed += size

    try:
        names = os.listdir(logs_dir)
    except OSError:
        return 0, 0
    rotated = []
    for name in names:
        full = os.path.join(logs_dir, name)
        if _ROTATED_APP_LOG.match(name) and os.path.isfile(full):
            try:
                rotated.append((os.path.getmtime(full), full))
            except OSError:
                pass
    rotated.sort(reverse=True)
    for _mtime, full in rotated[max(0, LOGS_ROTATED_APP_LOGS_KEPT):]:
        remove(full)
    temp_dir = os.path.join(logs_dir, "temp")
    cutoff = now - LOGS_TEMP_FILE_MAX_AGE_DAYS * 86400.0
    for root, _dirs, files in os.walk(temp_dir):
        for name in files:
            full = os.path.join(root, name)
            try:
                if os.path.getmtime(full) < cutoff:
                    remove(full)
            except OSError:
                pass
    if removed:
        log.info("[LOG FOLDER] removed %d old file(s), %.1f MB freed", removed, freed / 1048576.0)
    return removed, freed


def tidy(base_dir):
    """One pass for the whole folder: clears the update leftovers, prunes the logs, and writes the before and after report. Safe to run in a background thread; never raises."""
    logs_dir = os.path.join(os.path.abspath(base_dir), "logs")
    try:
        log.info(folder_report(logs_dir))
        try:
            import auto_updater
            auto_updater.prune_update_folder(base_dir)
        except Exception as update_err:
            log.warning("[LOG FOLDER] could not clear the update folder: %s", update_err)
        prune_logs(logs_dir)
        log.info(folder_report(logs_dir))
    except Exception as err:
        log.warning("[LOG FOLDER] tidy failed: %s", err)
