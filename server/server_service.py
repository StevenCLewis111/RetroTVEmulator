"""Headless server process for Retro TV named server slots.

This is intentionally separate from the pygame application.  It owns the
discovery announcer and schedule broadcaster so closing the TV window does not
take the server off the LAN.  The service watches the main config file, which
is atomically written by ``RetroDatabase``; it therefore needs no database,
pygame, VLC, or media imports.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import shlex
import subprocess
import sys
import threading
import time

import server_discovery
import server_sync
from server_diagnostics import breadcrumb as _server_breadcrumb
from server_tray import ServerTray


SERVICE_PID_FILENAME = ".retro_tv_server_service.pid"
AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_REG_NAME = "RetroTVServerService"
SERVICE_POLL_INTERVAL = 1.0
_VALID_SLOT = re.compile(r"^server[1-6]$")

log = logging.getLogger("retro_tv.server_service")


def _configure_logging(base_dir):
    """Give the detached process its own small rotating log."""
    if log.handlers:
        return
    try:
        path = os.path.join(base_dir, "server_service.log")
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1 * 1024 * 1024, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    except Exception:
        logging.basicConfig(level=logging.INFO)


def _pid_path(base_dir):
    return os.path.join(base_dir, SERVICE_PID_FILENAME)


def _pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        # os.kill(pid, 0) is a non-destructive existence check on Windows and
        # POSIX.  Permission errors still mean the process exists.
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def _read_pid(base_dir):
    try:
        with open(_pid_path(base_dir), "r", encoding="ascii") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _write_pid(base_dir):
    path = _pid_path(base_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii") as fh:
        fh.write(str(os.getpid()))
    os.replace(tmp, path)


def _remove_pid(base_dir):
    try:
        if _read_pid(base_dir) == os.getpid():
            os.remove(_pid_path(base_dir))
    except OSError:
        pass


def _config_path(base_dir):
    return os.path.join(base_dir, "retro_config.json")


def _read_runtime_config(base_dir):
    """Return ``(config, slots)`` from the stable main config snapshot.

    ``slots`` contains only named slots whose files currently exist.  A
    malformed or temporarily unavailable file returns ``(None, None)`` so the
    service can wait through a brief atomic-replace window instead of shutting
    down and needlessly removing autostart.
    """
    with open(_config_path(base_dir), "r", encoding="utf-8") as fh:
        data = json.load(fh)
    config = data.get("config", {})
    if not isinstance(config, dict):
        raise ValueError("config tree is not an object")
    names = config.get("server_slots", {})
    if not isinstance(names, dict):
        names = {}
    slots = {}
    for slot_id, name in names.items():
        slot_id = str(slot_id)
        if not _VALID_SLOT.fullmatch(slot_id) or not str(name).strip():
            continue
        path = os.path.join(
            base_dir, "server", ".json", slot_id, "retro.config.json")
        if os.path.isfile(path):
            slots[slot_id] = path
    return config, slots


def _write_server_mode(base_dir, enabled):
    """Change only the main config's server-mode flag, atomically."""
    path = _config_path(base_dir)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    config = data.setdefault("config", {})
    config["server_mode_enabled"] = bool(enabled)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4)
    os.replace(tmp, path)


def _service_command(base_dir):
    """Build the command used by both autostart and the live launcher."""
    if getattr(sys, "frozen", False):
        # A bundled build can route this flag before pygame initialization.
        return [sys.executable, "--server-service", "--base-dir", base_dir]
    executable = sys.executable
    if os.name == "nt" and os.path.basename(executable).lower() == "python.exe":
        # HKCU Run should not flash a console window in a source/dev install.
        pythonw = os.path.join(os.path.dirname(executable), "pythonw.exe")
        if os.path.isfile(pythonw):
            executable = pythonw
    return [executable, os.path.abspath(__file__),
            "--run", "--base-dir", base_dir]


def _command_for_registry(command):
    # Windows Run values are command-line strings, not argv arrays.
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return " ".join(shlex.quote(str(part)) for part in command)


def set_autostart(base_dir, enabled):
    """Install/remove only the server service's HKCU Run value.

    This never touches the app's Winlogon ``Shell`` value used by kiosk boot.
    """
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.CreateKey(
                winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH) as key:
            if enabled:
                winreg.SetValueEx(
                    key, AUTOSTART_REG_NAME, 0, winreg.REG_SZ,
                    _command_for_registry(_service_command(base_dir)))
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_REG_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as exc:
        _server_breadcrumb(
            log, logging.ERROR, f"server-autostart-registry:{base_dir}",
            "[SERVER DIAG] Server autostart registry update failed "
            "base_dir=%s: %s", base_dir, exc,
        )
        return False


def ensure_running(base_dir):
    """Start the detached service if it is not already the owner.

    Returns True when the service is running or was launched.  False means the
    live app should fall back to its in-process broadcaster (useful for a
    source tree with a missing service entry point).
    """
    base_dir = os.path.abspath(base_dir)
    os.makedirs(base_dir, exist_ok=True)
    pid = _read_pid(base_dir)
    if pid and _pid_alive(pid):
        set_autostart(base_dir, True)
        return True
    if pid:
        try:
            os.remove(_pid_path(base_dir))
        except OSError:
            pass
    command = _service_command(base_dir)
    if not getattr(sys, "frozen", False) and not os.path.isfile(command[1]):
        _server_breadcrumb(
            log, logging.ERROR, f"server-entry-point:{command[1]}",
            "[SERVER DIAG] Server service entry point is missing path=%s",
            command[1],
        )
        return False
    kwargs = {"cwd": base_dir, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    try:
        subprocess.Popen(command, **kwargs)
        set_autostart(base_dir, True)
        return True
    except Exception as exc:
        _server_breadcrumb(
            log, logging.ERROR, f"server-service-launch:{base_dir}",
            "[SERVER DIAG] Detached server service launch failed "
            "base_dir=%s: %s", base_dir, exc,
        )
        return False


def _open_server_folder(base_dir):
    path = os.path.join(base_dir, "server", ".json")
    try:
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        log.warning("[SERVER TRAY] could not open server folder: %s",
                    path, exc_info=True)


def run_service(base_dir):
    """Run until Server Mode is explicitly disabled or all slots disappear."""
    base_dir = os.path.abspath(base_dir)
    _configure_logging(base_dir)
    existing = _read_pid(base_dir)
    if existing and existing != os.getpid() and _pid_alive(existing):
        log.info("[SERVER SERVICE] already running as pid %s", existing)
        return 0

    try:
        _write_pid(base_dir)
    except OSError:
        log.exception("[SERVER SERVICE] could not claim pid file")
        return 1

    stop_event = threading.Event()
    def _stop_from_tray():
        try:
            _write_server_mode(base_dir, False)
        except Exception:
            log.exception("[SERVER TRAY] could not persist Server Mode off")
        finally:
            stop_event.set()

    tray = ServerTray(
        title="Retro TV Server",
        on_stop=_stop_from_tray,
        on_open_folder=lambda: _open_server_folder(base_dir),
    )
    announcer = None
    broadcaster = None
    device_id = server_discovery.get_or_create_device_id(base_dir)
    try:
        tray.start()
        log.info("[SERVER SERVICE] started (pid=%s)", os.getpid())
        bad_reads = 0
        while not stop_event.is_set():
            try:
                config, slots = _read_runtime_config(base_dir)
                if bad_reads:
                    log.info("[SERVER DIAG] Main config read recovered after %d "
                             "failed attempt(s)", bad_reads)
                bad_reads = 0
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                bad_reads += 1
                _server_breadcrumb(
                    log, logging.WARNING, f"service-config-read:{base_dir}",
                    "[SERVER DIAG] Server service config read failed "
                    "base_dir=%s: %s", base_dir, exc,
                )
                stop_event.wait(SERVICE_POLL_INTERVAL)
                continue

            enabled = (
                config.get("server_mode_enabled", False)
                and config.get("server_role") == "server"
                and bool(slots)
            )
            if not enabled:
                log.info("[SERVER SERVICE] stopping: Server Mode is off, "
                         "role changed, or no named slot files remain")
                break

            if announcer is None:
                def _current_slots():
                    try:
                        _unused_config, current = _read_runtime_config(base_dir)
                        return current
                    except Exception as exc:
                        _server_breadcrumb(
                            log, logging.WARNING, f"service-slot-refresh:{base_dir}",
                            "[SERVER DIAG] Server service slot refresh failed: %s",
                            exc,
                        )
                        return {}

                def _current_named_slots():
                    try:
                        current_config, current_paths = _read_runtime_config(base_dir)
                        return [
                            (slot_id, name)
                            for slot_id, name in
                            (current_config.get("server_slots", {}) or {}).items()
                            if _VALID_SLOT.fullmatch(str(slot_id))
                            and str(name).strip()
                            and os.path.isfile(current_paths.get(str(slot_id), ""))
                        ]
                    except Exception as exc:
                        _server_breadcrumb(
                            log, logging.WARNING, f"service-discovery-refresh:{base_dir}",
                            "[SERVER DIAG] Server service discovery refresh failed: %s",
                            exc,
                        )
                        return []

                announcer = server_discovery.ServerAnnouncer(
                    _current_named_slots,
                    device_id=device_id,
                )
                def _current_playback():
                    # The Server's own screen mode is imposed on every
                    # streamed channel a User plays.
                    try:
                        current_config, _unused_slots = _read_runtime_config(base_dir)
                        return {"screen_mode": current_config.get("screen_mode", "normal")}
                    except Exception:
                        return {}

                broadcaster = server_sync.ScheduleBroadcaster(
                    _current_slots, remote_id=device_id,
                    playback_provider=_current_playback)
                announcer.start()
                broadcaster.start()
                log.info("[SERVER SERVICE] discovery and schedule "
                         "broadcasting started for %d slot(s)", len(slots))

            stop_event.wait(SERVICE_POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("[SERVER SERVICE] interrupted")
    except Exception:
        log.exception("[SERVER SERVICE] fatal service error")
        return 1
    finally:
        if broadcaster is not None:
            try:
                broadcaster.stop()
            except Exception:
                log.exception("[SERVER SERVICE] broadcaster stop failed")
        if announcer is not None:
            try:
                announcer.stop()
            except Exception:
                log.exception("[SERVER SERVICE] announcer stop failed")
        tray.stop()
        _remove_pid(base_dir)
        # Explicit disable removes the Run value; a normal process exit from
        # a transient error leaves the next app launch able to reinstall it.
        try:
            config, slots = _read_runtime_config(base_dir)
            if (not config.get("server_mode_enabled", False)
                    or not slots
                    or config.get("server_role") != "server"):
                set_autostart(base_dir, False)
        except Exception:
            pass
        log.info("[SERVER SERVICE] stopped")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Retro TV headless server")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--base-dir", default=None)
    args = parser.parse_args(argv)
    # Falls back to the project root (this file's parent folder) rather than
    # its own folder, so a manual run without --base-dir still finds
    # retro_config.json and the pid file in the right place.
    base_dir = args.base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return run_service(base_dir)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ensure_running", "run_service", "set_autostart", "main"]