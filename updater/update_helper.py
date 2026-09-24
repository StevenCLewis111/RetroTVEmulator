"""Detached file-swap helper for Retro TV Emulator updates.

This process must be separate because Windows keeps the running executable
locked.  It waits for the foreground process to exit, backs up every payload
target, copies the staged files, relaunches the app, and restores the backup if
the replacement cannot stay alive.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import tempfile
import time

# server_diagnostics.py may be a sibling of this file (server/) or one
# directory up (../server), depending on how the build is packaged.
def _add_server_path():
    if getattr(sys, "frozen", False):
        _base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        _base = os.path.dirname(os.path.abspath(__file__))
    for _candidate in (os.path.join(_base, "server"), os.path.join(_base, "..", "server")):
        if os.path.isdir(_candidate):
            sys.path.insert(0, _candidate)
            return
    sys.path.insert(0, os.path.join(_base, "..", "server"))


_add_server_path()
from server_diagnostics import breadcrumb as _server_breadcrumb

WAIT_FOR_PARENT_SECONDS = 120
HEALTH_CHECK_SECONDS = 15

log = logging.getLogger("retro_tv.update_helper")


def _configure_logging(install_dir):
    """Give the detached helper its own small rotating log."""
    if log.handlers:
        return
    try:
        path = os.path.join(install_dir, "update_helper.log")
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=256 * 1024, backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    except Exception:
        pass


def _read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, ProcessLookupError):
        return False


def _payload_files(staged_path):
    files = []
    for root, dirs, names in os.walk(staged_path):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(names):
            source = os.path.join(root, name)
            relative = os.path.relpath(source, staged_path)
            files.append((source, relative))
    return files


def _backup_targets(install_dir, files):
    backup_dir = tempfile.mkdtemp(prefix="retro-tv-rollback-")
    copied = []
    absent = []
    for _source, relative in files:
        target = os.path.join(install_dir, relative)
        if not os.path.isfile(target):
            absent.append(relative)
            continue
        backup = os.path.join(backup_dir, relative)
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        shutil.copy2(target, backup)
        copied.append(relative)
    return backup_dir, copied, absent


def _restore(install_dir, backup_dir, copied, absent):
    for relative in copied:
        backup = os.path.join(backup_dir, relative)
        target = os.path.join(install_dir, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(backup, target)
    for relative in absent:
        try:
            os.unlink(os.path.join(install_dir, relative))
        except FileNotFoundError:
            pass


def _apply_payload(install_dir, files):
    for source, relative in files:
        target = os.path.join(install_dir, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)


def apply_update(metadata_path):
    metadata = _read_json(metadata_path)
    install_dir = os.path.abspath(metadata["install_dir"])
    staged_path = os.path.abspath(metadata["staged_path"])
    parent_pid = int(metadata.get("parent_pid") or 0)
    command = metadata.get("relaunch_command")
    _configure_logging(install_dir)
    log.info("[UPDATE DIAG] Helper starting version=%s install_dir=%s",
             metadata.get("version", ""), install_dir)
    if not os.path.isdir(install_dir) or not os.path.isdir(staged_path):
        raise RuntimeError("Update install directory or staged payload is missing")
    if not isinstance(command, list) or not command:
        raise RuntimeError("Update relaunch command is missing")

    deadline = time.monotonic() + WAIT_FOR_PARENT_SECONDS
    while _pid_alive(parent_pid) and time.monotonic() < deadline:
        time.sleep(0.25)
    if _pid_alive(parent_pid):
        _server_breadcrumb(
            log, logging.ERROR, "update-parent-wait",
            "[UPDATE DIAG] Timed out waiting for pid=%s to exit", parent_pid,
            interval=0.0,
        )
        raise RuntimeError("Timed out waiting for the old application to exit")

    files = _payload_files(staged_path)
    if not files:
        raise RuntimeError("Staged update contains no files")
    backup_dir, copied, absent = _backup_targets(install_dir, files)
    try:
        _apply_payload(install_dir, files)
        process = subprocess.Popen(
            command,
            cwd=install_dir,
            close_fds=True,
            creationflags=(
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            ) if os.name == "nt" else 0,
        )
        # A spawn that immediately dies is usually a bad executable/path. Give
        # normal startup a little time, then restore the previous files.
        deadline = time.monotonic() + HEALTH_CHECK_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Updated application exited during startup")
            time.sleep(0.25)
    except Exception as exc:
        _server_breadcrumb(
            log, logging.ERROR, "update-apply-failed",
            "[UPDATE DIAG] Update apply failed, restoring backup: %s", exc,
            interval=0.0,
        )
        _restore(install_dir, backup_dir, copied, absent)
        try:
            subprocess.Popen(
                command,
                cwd=install_dir,
                close_fds=True,
                creationflags=(
                    getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                ) if os.name == "nt" else 0,
            )
        except Exception:
            pass
        raise
    finally:
        shutil.rmtree(backup_dir, ignore_errors=True)
        shutil.rmtree(os.path.dirname(staged_path), ignore_errors=True)
        try:
            os.unlink(metadata_path)
        except FileNotFoundError:
            pass
    log.info("[UPDATE DIAG] Helper finished successfully version=%s",
             metadata.get("version", ""))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", required=True, dest="metadata_path")
    args = parser.parse_args(argv)
    try:
        return apply_update(args.metadata_path)
    except Exception as exc:
        # apply_update already logs the underlying failure via breadcrumb;
        # this line is the terminal outcome for a run that had no console.
        try:
            log.error("[UPDATE DIAG] Helper exiting with failure: %s", exc)
        except Exception:
            pass
        print(f"[RETRO TV UPDATE] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())