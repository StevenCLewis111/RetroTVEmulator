"""Headless server process for Retro TV named server slots.

This is intentionally separate from the pygame application.  It owns the
discovery announcer and schedule broadcaster so closing the TV window does not
take the server off the LAN.  The service watches the main config file, which
is atomically written by ``RetroDatabase``; it therefore needs no database,
pygame, VLC, or media imports.

Owns: pid file, autostart registry value, tray, and the config-poll loop.
Does not own: discovery packets (server_discovery.py) or the schedule wire
protocol (server_sync.py).

WARNING: set_autostart() must only ever touch the HKCU Run value below. The
app's Winlogon Shell value used by kiosk boot must never be modified here.
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
import tempfile
import threading
import time

import server_captions
import server_clock
import server_discovery
import server_planner
import server_files
import server_scanner
import server_sync
from server_diagnostics import breadcrumb as _server_breadcrumb
from server_tray import ServerTray, icon_candidates


# ============================================================================
# SECTION: Constants & logging
# ============================================================================

SERVICE_PID_FILENAME = ".retro_tv_server_service.pid"
AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_REG_NAME = "RetroTVServerService"
try:
    import performance_governor as _gov
except Exception as _governor_error:
    _gov = None
    logging.getLogger("retro_tv.server_service").warning("[FAILURE] server_service: performance_governor did not import, so the built-in numbers are used: %s", _governor_error)


def _log_swallowed(where, error):
    """Writes the shared, counted [FAILURE] line through performance_governor (a plain warning when it did not import)."""
    if _gov is not None:
        return _gov.log_swallowed(where, error)
    logging.getLogger("retro_tv.server_service").warning("[FAILURE] %s: %s: %s", where, type(error).__name__, error)
    return None


# WARNING: the fallback values in this file must match the same names in performance_governor.py (the poll and relaunch values are SERVER_SERVICE_ there); they are used only when that module fails to import.
SERVICE_POLL_INTERVAL = _gov.SERVER_SERVICE_POLL_INTERVAL if _gov is not None else 1.0
SERVICE_RELAUNCH_COOLDOWN_S = _gov.SERVER_SERVICE_RELAUNCH_COOLDOWN_S if _gov is not None else 15.0
SERVER_HEARTBEAT_EVERY_S = _gov.SERVER_HEARTBEAT_EVERY_S if _gov is not None else 5.0
SERVER_HEARTBEAT_STALE_S = _gov.SERVER_HEARTBEAT_STALE_S if _gov is not None else 90.0
_MUTEX_HANDLES = []
_last_launch_time = 0.0
_VALID_SLOT = re.compile(r"^server[1-6]$")

log = logging.getLogger("retro_tv.server_service")


def _logs_dir(base_dir):
    """install folder/logs, made when missing (the pid file and the log live here)."""
    path = os.path.join(base_dir, "logs")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return base_dir
    return path


_service_log_handler = None


def _configure_logging(base_dir):
    """Give the detached process its own small rotating log, written by every module in the process (the caption scan, the sound and picture scan, the link to the Users, the tray), not only by this file."""
    # WARNING: owner logs 2026-11-24: the handler sat on this file's logger alone, so server_captions, server_scanner, server_sync and the others logged into nothing and the caption scan left no line at all; it now sits on the root logger of this process (the service process only; the program never calls this).
    global _service_log_handler
    if _service_log_handler is not None:
        return
    try:
        path = os.path.join(_logs_dir(base_dir), "server_service.log")
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1 * 1024 * 1024, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        _service_log_handler = handler
    except Exception:
        logging.basicConfig(level=logging.INFO)


# ============================================================================
# SECTION: PID file
# ============================================================================


def _pid_path(base_dir):
    return os.path.join(_logs_dir(base_dir), SERVICE_PID_FILENAME)


def _tidy_legacy_files(base_dir):
    """Older versions kept the pid file and the log in the install folder; move them into logs (never over a file already there, and a file another process still holds is simply left)."""
    for name in (SERVICE_PID_FILENAME, "server_service.log", "server_service.log.1", "server_service.log.2"):
        old = os.path.join(base_dir, name)
        new = os.path.join(_logs_dir(base_dir), name)
        try:
            if not os.path.isfile(old):
                continue
            if name == SERVICE_PID_FILENAME or os.path.exists(new):
                os.remove(old)
            else:
                os.replace(old, new)
        except OSError as caught_error:
            _log_swallowed("server_service: rotating the service log", caught_error)


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        # WARNING: the normal end: not a process number, so not running.
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        # WARNING: os.kill(pid, 0) is not a liveness test on Windows (signal 0 is CTRL_C_EVENT and always fails for a detached process), which made every check say "not running" and started a new server service every second until Windows ran out of memory. Ask the process itself.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        # WARNING: Windows codes and flags: SYNCHRONIZE | QUERY_LIMITED_INFORMATION.
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, 0, pid)
        if not handle:
            # WARNING: Windows codes and flags: access denied: the process exists.
            return kernel32.GetLastError() == 5
        try:
            # WARNING: Windows codes and flags: WAIT_TIMEOUT: still running.
            return kernel32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        # WARNING: the normal end: the process has ended (or the number is not a process here).
        return False


def _hold_mutex(name):
    """True when this process is the first to hold the named Windows mutex; it is held until the process ends."""
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, 1, name)
    _MUTEX_HANDLES.append(handle)
    # WARNING: Windows codes and flags: ERROR_ALREADY_EXISTS.
    return kernel32.GetLastError() != 183


COMPUTER_MUTEX_NAME = "Local\\RetroTVServerService_ThisComputer"


def _claim_single_instance(base_dir):
    """True for the only server service on this computer; a second copy, from this install folder or any other, gets False (Windows named mutexes, held until the process ends)."""
    if os.name != "nt":
        return True
    import hashlib
    folder_name = "Local\\RetroTVServerService_" + hashlib.sha1(os.path.normcase(base_dir).encode("utf-8")).hexdigest()[:16]
    if not _hold_mutex(folder_name):
        return False
    return _hold_mutex(COMPUTER_MUTEX_NAME)


def _read_pid(base_dir):
    try:
        with open(_pid_path(base_dir), "r", encoding="ascii") as fh:
            return int(fh.read().strip())
    except FileNotFoundError:
        # WARNING: the normal end: no service is running from this folder, so there is no pid file.
        return None
    except (OSError, ValueError) as caught_error:
        _log_swallowed("server_service: reading the service's pid file", caught_error)
        return None


def _write_pid(base_dir):
    path = _pid_path(base_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii") as fh:
        fh.write(str(os.getpid()))
    os.replace(tmp, path)


def _heartbeat_path(base_dir):
    return os.path.join(_logs_dir(base_dir), "server_heartbeat.txt")


def write_heartbeat(base_dir, now=None):
    """Marks 'the service loop is still turning' with the time, in a small file beside the pid file."""
    path = _heartbeat_path(base_dir)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="ascii") as fh:
            fh.write(repr(time.time() if now is None else float(now)))
        os.replace(tmp, path)
    except OSError as caught_error:
        _log_swallowed("server_service: writing the heartbeat", caught_error)


def heartbeat_age(base_dir, now=None):
    """Seconds since the service last wrote its heartbeat, or None when there is no readable heartbeat (an older service, or one that has not written yet)."""
    try:
        with open(_heartbeat_path(base_dir), "r", encoding="ascii") as fh:
            written = float(fh.read().strip())
    except FileNotFoundError:
        # WARNING: the normal end: the service has not written a heartbeat yet.
        return None
    except (OSError, ValueError) as caught_error:
        _log_swallowed("server_service: reading the heartbeat", caught_error)
        return None
    return max(0.0, (time.time() if now is None else float(now)) - written)


def _remove_pid(base_dir):
    try:
        if _read_pid(base_dir) == os.getpid():
            os.remove(_pid_path(base_dir))
    except OSError as caught_error:
        _log_swallowed("server_service: removing this service's pid file", caught_error)


# ============================================================================
# SECTION: Runtime config
# ============================================================================


def _config_path(base_dir):
    return os.path.join(base_dir, "retro_config.json")


_saved_mode_seen = {"mode": None}


def _saved_performance_mode(base_dir):
    """The performance mode (lite, standard or performance) saved in the program's settings on this PC, or None when it cannot be read."""
    # WARNING: owner 2026-11-26 (tray10: "server pc was and is set to performance mode"): the settings file is {"config": {...}, "channels": {...}} and the mode was looked for at its top level, where it never is, so the caption scan always fell back to the governor's default (standard). The mode is read from the config tree, through the same cached reader as the slots (the file holds every channel's lists and is parsed again only when it changed).
    try:
        import performance_governor
        config = _read_runtime_config(base_dir)[0]
        saved = config.get(performance_governor.MODE_CONFIG_KEY)
        mode = performance_governor.normalize_mode(saved) if saved else None
    except FileNotFoundError:
        # WARNING: the normal end: the program has not saved its settings on this PC yet, so the caption scan uses the governor's own mode.
        return None
    except Exception as mode_error:
        log.warning("[CAPTION SCAN] the performance mode could not be read from the program's settings: %s", mode_error)
        return None
    if mode != _saved_mode_seen["mode"]:
        _saved_mode_seen["mode"] = mode
        if mode:
            # WARNING: the tray's own governor follows the saved mode too, so everything in this process that asks the governor for the mode (the caption grades, thread counts) gets the same answer as the scan.
            performance_governor.set_mode(mode)
        log.info("[CAPTION SCAN] this PC's performance mode from the program's settings: %s", mode or "not saved (the governor's own is used)")
    return mode


def _project_root():
    """The project's root folder in a source copy (the one holding retro_tv_emulator.py): the folder above this script's folder, or above scripts when the script folders sit in scripts."""
    # WARNING: the same rule as media._project_root (a test compares them); it is kept here because the service finds its base folder from it before media can be imported.
    up = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.basename(up).lower() == "scripts":
        up = os.path.dirname(up)
    return up


def _folder_safe_name(name, fallback):
    """A server name as a folder name, by the program's own rule (media._folder_safe_name)."""
    import media as _name_media
    return _name_media._folder_safe_name(name, fallback)


def _server_data_dir(base_dir):
    """Where the server and user channel files are: media.server_data_dir_in_use, the program's own rule (main/data/.json, or an old place until the program has moved it); the service only reads."""
    import media as _data_media
    return _data_media.server_data_dir_in_use(base_dir)


def _slot_folder_name(names, slot_id):
    """WARNING: must give the same folder as RetroDatabase.server_folder_name in media.py (a test compares them)."""
    base = _folder_safe_name(names.get(slot_id), slot_id)
    for other_id, other_name in names.items():
        if other_id != slot_id and _folder_safe_name(other_name, other_id).lower() == base.lower() and other_id < slot_id:
            return "%s (%s)" % (base, slot_id)
    return base


_runtime_config_cache = {}


def _read_runtime_config(base_dir):
    """Return ``(config, slots)`` from the stable main config snapshot.

    ``slots`` contains only named slots whose files currently exist.

    Raises OSError / ValueError (including JSONDecodeError) when the file is
    missing or malformed. NOTE: run_service() treats that as transient and
    retries, so a brief atomic-replace window never shuts the service down or
    removes autostart.
    """
    path = _config_path(base_dir)
    stat = os.stat(path)
    stamp = (stat.st_mtime_ns, stat.st_size)
    held = _runtime_config_cache.get(path)
    if held is not None and held[0] == stamp:
        data = held[1]
    else:
        # WARNING: owner 2026-11-18: the settings file holds every Regular channel's file lists, so it is parsed again only when it changed on disk; it used to be parsed several times a second for as long as the service ran.
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _runtime_config_cache[path] = (stamp, data)
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
            _server_data_dir(base_dir), "server", _slot_folder_name(names, slot_id), "retro.config.json")
        if os.path.isfile(path):
            slots[slot_id] = path
    return config, slots


# ============================================================================
# SECTION: Autostart & launch
# ============================================================================


def _service_command(base_dir):
    """Build the command used by both autostart and the live launcher."""
    if getattr(sys, "frozen", False):
        # WARNING: A bundled build can route this flag before pygame initialization.
        return [sys.executable, "--server-service", "--base-dir", base_dir]
    executable = sys.executable
    if os.name == "nt" and os.path.basename(executable).lower() == "python.exe":
        # WARNING: HKCU Run should not flash a console window in a source/dev install.
        pythonw = os.path.join(os.path.dirname(executable), "pythonw.exe")
        if os.path.isfile(pythonw):
            executable = pythonw
    return [executable, os.path.abspath(__file__),
            "--run", "--base-dir", base_dir]


def _command_for_registry(command):
    # WARNING: Windows Run values are command-line strings, not argv arrays.
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return " ".join(shlex.quote(str(part)) for part in command)


LAUNCH_ON_BOOT_FILE = "server_launch_on_boot.json"


def launch_on_boot_enabled(base_dir):
    """True unless the owner unticked Launch on boot in the tray menu."""
    try:
        with open(os.path.join(base_dir, "logs", LAUNCH_ON_BOOT_FILE), "r", encoding="utf-8") as fh:
            return bool(json.load(fh).get("enabled", True))
    except (OSError, ValueError, AttributeError):
        return True


def set_launch_on_boot(base_dir, enabled):
    """Saves the tray menu's Launch on boot tick and puts the autostart value in or out to match; returns the saved state."""
    os.makedirs(os.path.join(base_dir, "logs"), exist_ok=True)
    with open(os.path.join(base_dir, "logs", LAUNCH_ON_BOOT_FILE), "w", encoding="utf-8") as fh:
        json.dump({"enabled": bool(enabled)}, fh)
    set_autostart(base_dir, bool(enabled))
    return bool(enabled)


def set_autostart(base_dir, enabled):
    """Install/remove only the server service's HKCU Run value.

    This never touches the app's Winlogon ``Shell`` value used by kiosk boot.
    """
    if os.name != "nt":
        return False
    # WARNING: owner 2026-11-17: with Launch on boot unticked in the tray menu the value is never installed, whoever asks.
    enabled = bool(enabled) and launch_on_boot_enabled(base_dir)
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
                    # WARNING: the normal end: there was no autostart value to remove.
                    pass
        return True
    except Exception as exc:
        _server_breadcrumb(
            log, logging.ERROR, f"server-autostart-registry:{base_dir}",
            "[SERVER DIAG] Server autostart registry update failed "
            "base_dir=%s: %s", base_dir, exc,
        )
        return False


# ============================================================================
# SECTION: Quit from the tray (the owner stopped the service; Server Mode stays on)
# ============================================================================

QUIT_MARKER_NAME = "server_quit_from_tray.json"
SAME_BOOT_TOLERANCE_S = _gov.SAME_BOOT_TOLERANCE_S if _gov is not None else 120


def stop_service(base_dir, wait_s=5.0):
    """Ends this install folder's server service process (never this one) so an update can replace the program file it has open; the next start of the app starts it again. Returns True when none is left running."""
    pid = _read_pid(base_dir)
    if not pid or pid == os.getpid() or not _pid_alive(pid):
        return True
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.kill(pid, 15)
    except Exception as exc:
        log.warning("[SERVER SERVICE] Could not stop pid=%s for the update: %s", pid, exc)
    deadline = time.monotonic() + wait_s
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    gone = not _pid_alive(pid)
    if gone:
        try:
            os.remove(_pid_path(base_dir))
        except OSError as caught_error:
            _log_swallowed("server_service: removing the ended service's pid file", caught_error)
    return gone


def _quit_marker_path(base_dir):
    return os.path.join(_logs_dir(base_dir), QUIT_MARKER_NAME)


def _boot_epoch():
    """Seconds since 1970 at which this computer started (from its uptime); 0 when it cannot be read."""
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            return int(time.time() - kernel32.GetTickCount64() / 1000.0)
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            return int(time.time() - float(fh.read().split()[0]))
    except Exception as caught_error:
        _log_swallowed("server_service: reading when this computer started", caught_error)
        return 0


def _app_is_open(base_dir):
    """True while the program is running: it keeps a running marker in logs/temp until a clean quit."""
    return os.path.isfile(os.path.join(_logs_dir(base_dir), "temp", "running.marker"))


def note_quit_from_tray(base_dir):
    """Remembers that the owner quit the service from the tray icon, and whether the program was open at the time."""
    try:
        with open(_quit_marker_path(base_dir), "w", encoding="utf-8") as fh:
            json.dump({"boot": _boot_epoch(), "app_open": _app_is_open(base_dir)}, fh)
    except OSError:
        log.warning("[SERVER TRAY] could not remember the quit", exc_info=True)


def clear_quit_marker(base_dir):
    try:
        os.remove(_quit_marker_path(base_dir))
    except FileNotFoundError:
        # WARNING: the normal end: there was no quit marker to clear.
        pass
    except OSError as caught_error:
        _log_swallowed("server_service: clearing the quit marker", caught_error)


def start_is_held(base_dir):
    """True when the owner quit the service from the tray while the program was open and this is still the same boot: the service then waits for the next boot. A quit made while the program was closed, or one from an earlier boot, does not hold anything (the marker is cleared), so opening the program starts the service again."""
    try:
        with open(_quit_marker_path(base_dir), "r", encoding="utf-8") as fh:
            marker = json.load(fh)
    except FileNotFoundError:
        # WARNING: the normal end: the server was not quit from the tray, so there is no quit marker.
        return False
    except (OSError, ValueError) as caught_error:
        _log_swallowed("server_service: reading the quit marker", caught_error)
        return False
    same_boot = abs(int(marker.get("boot", 0)) - _boot_epoch()) <= SAME_BOOT_TOLERANCE_S
    if marker.get("app_open") and same_boot:
        return True
    clear_quit_marker(base_dir)
    return False


def ensure_running(base_dir, force=False):
    """Start the detached service if it is not already the owner; force=True is an owner action in the program (turning Server Mode on, making a server) and ignores a quit from the tray.

    Returns True when the service is running or was launched.  False means the
    live app should fall back to its in-process broadcaster (useful for a
    source tree with a missing service entry point).
    """
    base_dir = os.path.abspath(base_dir)
    os.makedirs(base_dir, exist_ok=True)
    pid = _read_pid(base_dir)
    # WARNING: a forced start clears the tray-quit marker before anything else; a service about to exit (its server was just removed) is still alive here, and a marker left behind would hold every later automatic start for the rest of the boot.
    if force:
        clear_quit_marker(base_dir)
    if pid and _pid_alive(pid):
        # WARNING: a service whose loop stopped turning (a call that never returned) holds the port and the pid but serves nobody; it is ended and started again.
        age = heartbeat_age(base_dir)
        if age is not None and age > SERVER_HEARTBEAT_STALE_S and pid != os.getpid():
            log.warning("[SERVER SERVICE] the service (pid %s) has not written its heartbeat for %.0f s; ending it and starting it again", pid, age)
            if not stop_service(base_dir):
                return True
            pid = None
        else:
            set_autostart(base_dir, True)
            return True
    if not force and start_is_held(base_dir):
        # WARNING: returns True so the program does not start its own in-process server in place of the one the owner just quit.
        log.info("[SERVER SERVICE] not started: it was quit from the tray during this boot and the program has been open since")
        return True
    if pid:
        try:
            os.remove(_pid_path(base_dir))
        except OSError as caught_error:
            _log_swallowed("server_service: removing the pid file after stopping the service", caught_error)
    global _last_launch_time
    if time.monotonic() - _last_launch_time < SERVICE_RELAUNCH_COOLDOWN_S:
        log.info("[SERVER SERVICE] a start was made a moment ago (pid file %s); waiting before another", "present" if pid else "missing")
        return True
    command = _service_command(base_dir)
    if not getattr(sys, "frozen", False) and not os.path.isfile(command[1]):
        _server_breadcrumb(
            log, logging.ERROR, f"server-entry-point:{command[1]}",
            "[SERVER DIAG] Server service entry point is missing path=%s",
            command[1],
        )
        return False
    kwargs = {"cwd": base_dir, "close_fds": True}
    base_flags = 0
    if os.name == "nt":
        base_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
        # WARNING: the app ties every child it starts to a Windows Job Object that kills them all when the app exits. The service must break away from that job or it dies with the app (the tray icon vanished on close). The job allows breakaway; if some other job forbids it the launch is retried without the flag.
        kwargs["creationflags"] = base_flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    try:
        try:
            subprocess.Popen(command, **kwargs)
        except OSError:
            if os.name != "nt":
                raise
            kwargs["creationflags"] = base_flags
            subprocess.Popen(command, **kwargs)
        _last_launch_time = time.monotonic()
        set_autostart(base_dir, True)
        log.info("[SERVER SERVICE] started the server from the program (force=%s)", force)
        return True
    except Exception as exc:
        _server_breadcrumb(
            log, logging.ERROR, f"server-service-launch:{base_dir}",
            "[SERVER DIAG] Detached server service launch failed "
            "base_dir=%s: %s", base_dir, exc,
        )
        return False


# ============================================================================
# SECTION: Tray actions
# ============================================================================


def _tray_menu_colors(base_dir):
    """(Background Color hue, Border/Text Color hue) from the program's saved theme, so the tray menu wears the same colours; (None, None) when the settings file is missing or unreadable."""
    try:
        with open(_config_path(base_dir), "r", encoding="utf-8") as fh:
            config = json.load(fh).get("config", {})
        if not isinstance(config, dict):
            return None, None
        return config.get("theme_bg_hue"), config.get("theme_ui_hue")
    except (OSError, ValueError, AttributeError):
        return None, None


def _open_app(base_dir):
    """Start the Retro TV Emulator program from the tray menu (a built copy is this same exe without the service flag; a source copy is retro_tv_emulator.py beside the server folder)."""
    if getattr(sys, "frozen", False):
        command = [sys.executable]
    else:
        script = os.path.join(_project_root(), "retro_tv_emulator.py")
        if not os.path.isfile(script):
            return
        command = [sys.executable, script]
    try:
        subprocess.Popen(command, cwd=base_dir, close_fds=True)
    except OSError as exc:
        log.warning("[SERVER TRAY] could not open the program: %s", exc)


# ============================================================================
# SECTION: Send logs (tray)
# ============================================================================

_send_logs_lock = threading.Lock()
SEND_LOGS_WEBHOOK_URL = _gov.SEND_LOGS_WEBHOOK_URL if _gov is not None else "https://discord.com/api/webhooks/1575209056040259675/Bku0BysVpzKK7GtD-QeJBYM47wMhX8x6Y1KI1X_bw5vG48RWoHMtwOuTfKSKs9lkLuTh"
SEND_LOGS_MAX_ZIP_BYTES = _gov.SEND_LOGS_MAX_ZIP_BYTES if _gov is not None else 9000000
SEND_LOGS_FILE_TAIL_BYTES = _gov.SEND_LOGS_FILE_TAIL_BYTES if _gov is not None else 3000000
SEND_LOGS_FILE_TAIL_BYTES_LEAST = _gov.SEND_LOGS_FILE_TAIL_BYTES_LEAST if _gov is not None else 60000
SEND_LOGS_TIMEOUT_SECONDS = _gov.SEND_LOGS_TIMEOUT_SECONDS if _gov is not None else 60.0
SEND_LOGS_COPY_SECONDS = _gov.SEND_LOGS_TRAY_COPY_SECONDS if _gov is not None else 6.0
SEND_LOGS_TRAY_NOTE = "Sent from the server tray (it has no note box)."


def _send_logs_config_url(base_dir):
    """retro_config.json's send_logs_webhook_url, or empty when it is not set or the file cannot be read."""
    try:
        with open(_config_path(base_dir), "r", encoding="utf-8") as fh:
            config = json.load(fh).get("config", {})
        return str(config.get("send_logs_webhook_url", "") or "") if isinstance(config, dict) else ""
    except FileNotFoundError:
        # WARNING: the normal end: there is no retro_config.json yet.
        return ""
    except (OSError, ValueError, AttributeError) as caught_error:
        _log_swallowed("server_service: reading the send-logs address from retro_config.json", caught_error)
        return ""


def _send_logs_machine_text():
    """This computer's OS and core count for the zip's head, as the program's [BOOT ENV] line names them."""
    import platform
    try:
        os_text = platform.platform()
    except Exception:
        os_text = "unknown"
    return "OS=%s | CPU cores=%s | sent by the server service" % (os_text, os.cpu_count() or "unknown")


def send_logs_from_tray(base_dir):
    """Send logs on the tray menu: the program's own Send Logs steps (send_logs_finisher): the logs are copied, the personal parts taken out, the zip kept under Discord's size and posted to the Discord webhook; a message then says the logs were sent, or why not."""
    if not _send_logs_lock.acquire(blocking=False):
        return
    snapshot = ""
    out = ""
    try:
        import send_logs_finisher
        url = send_logs_finisher.pick_webhook_url(_send_logs_config_url(base_dir), SEND_LOGS_WEBHOOK_URL)
        if not url:
            log.warning("[SERVER TRAY] Send logs: no Discord webhook is set (SEND_LOGS_WEBHOOK_URL in performance_governor.py or send_logs_webhook_url in retro_config.json)")
            _tray_message("Send logs is not set up: no Discord webhook is set.", base_dir=base_dir)
            return
        auto_updater = _load_auto_updater(base_dir)
        try:
            version = str(auto_updater.current_version(base_dir) or "unknown")
        except Exception as exc:
            log.warning("[SERVER TRAY] Send logs: the version could not be read: %s", exc)
            version = "unknown"
        logs = _logs_dir(base_dir)
        temp = os.path.join(logs, "temp")
        os.makedirs(temp, exist_ok=True)
        # WARNING: the program may be writing its logs while the tray copies them (another process, so its writers cannot be paused from here); copy_logs cuts a line caught half written back to the last whole line.
        snapshot = tempfile.mkdtemp(prefix="retrotv_tray_logs_")
        count = send_logs_finisher.copy_logs(logs, snapshot, time.time() + SEND_LOGS_COPY_SECONDS)
        out = os.path.join(temp, "send_logs_tray.zip")
        machine = _send_logs_machine_text()
        size = send_logs_finisher.build_zip(out, SEND_LOGS_TRAY_NOTE, snapshot, SEND_LOGS_MAX_ZIP_BYTES,
                                            SEND_LOGS_FILE_TAIL_BYTES, SEND_LOGS_FILE_TAIL_BYTES_LEAST, version, machine)
        if size is None:
            log.warning("[SERVER TRAY] Send logs: the zip stayed bigger than %d bytes even with the smallest logs", SEND_LOGS_MAX_ZIP_BYTES)
            _tray_message("The logs could not be sent: they stay too big for Discord even cut down.", base_dir=base_dir)
            return
        message = "Retro TV logs V%s (sent from the server tray), %s" % (version, machine[:300])
        ok, why = send_logs_finisher.post_zip(url, out, message, SEND_LOGS_TIMEOUT_SECONDS, urlopen=auto_updater._urlopen)
        log.info("[SERVER TRAY] Send logs %s (%d file(s), %d bytes): %s", "sent" if ok else "failed", count, size, why)
        if ok:
            _tray_message("Logs sent.", base_dir=base_dir)
        else:
            _tray_message("The logs could not be sent (%s).\n\nCheck the internet connection and try again." % why, base_dir=base_dir)
    except Exception as exc:
        log.exception("[SERVER TRAY] Send logs failed")
        _tray_message("The logs could not be sent: %s" % exc, base_dir=base_dir)
    finally:
        if snapshot:
            import shutil
            shutil.rmtree(snapshot, ignore_errors=True)
        if out:
            try:
                os.remove(out)
            except OSError as caught_error:
                _log_swallowed("server_service: removing the logs zip after sending it", caught_error)
        _send_logs_lock.release()


# ============================================================================
# SECTION: Check for updates (tray)
# ============================================================================

_update_from_tray_lock = threading.Lock()
TRAY_UPDATE_PROGRESS_WRITE_S = _gov.TRAY_UPDATE_PROGRESS_WRITE_S if _gov is not None else 0.25
SERVER_SCAN_STATE_WRITE_S = _gov.SERVER_SCAN_STATE_WRITE_S if _gov is not None else 10.0


def _tray_message(text, question=False, base_dir=None):
    """The OK message of the tray's update check, shown by the update helper in the program's own colours (mouse, or Enter/Space/Escape, as in the program's other popups); a plain Windows message box is the fallback when the helper cannot show it, or for a Yes/No question, which is asked by _ask_yes_no instead. Returns True for Yes (or OK). Without Windows it only logs."""
    log.info("[SERVER TRAY] update message: %s", text.replace("\n", " "))
    if base_dir and not question:
        try:
            auto_updater = _load_auto_updater(base_dir)
            command = auto_updater._helper_command(base_dir) + ["--inform", text]
            kwargs = {"cwd": tempfile.gettempdir(), "close_fds": True}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            code = subprocess.run(command, timeout=600, **kwargs).returncode
            if code == 0:
                return True
            log.warning("[SERVER TRAY] the helper could not show the message (code %s)", code)
        except Exception as exc:
            log.warning("[SERVER TRAY] the helper could not show the message: %s", exc)
    if os.name != "nt":
        return False
    import ctypes
    # WARNING: Windows codes and flags: MB_YESNO / MB_OK; question / information icon; MB_TOPMOST | MB_SETFOREGROUND.
    flags = 0x4 if question else 0x0
    flags |= 0x20 if question else 0x40
    flags |= 0x00040000 | 0x00010000
    answer = ctypes.windll.user32.MessageBoxW(None, text, "Retro TV Server", flags)
    # WARNING: Windows codes and flags: IDOK / IDYES.
    return answer in (1, 6)


def _ask_yes_no(auto_updater, base_dir, text):
    """The Yes/No question of the tray's update check, shown by the update helper (mouse, or A and D then Enter, as in the program's own popups); a plain Windows box is the fallback when the helper cannot show it."""
    try:
        command = auto_updater._helper_command(base_dir) + ["--ask", text]
        kwargs = {"cwd": tempfile.gettempdir(), "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        code = subprocess.run(command, timeout=600, **kwargs).returncode
        if code in (0, 1):
            return code == 0
        log.warning("[SERVER TRAY] the helper could not show the question (code %s)", code)
    except Exception as exc:
        log.warning("[SERVER TRAY] the helper could not show the question: %s", exc)
    return _tray_message(text, question=True)


def _load_auto_updater(base_dir):
    """auto_updater lives in scripts/updater; the packaged program already has it on its path, a source run adds it."""
    try:
        import auto_updater
        return auto_updater
    except ImportError:
        for root in (base_dir, _project_root()):
            folder = os.path.join(root, "scripts", "updater")
            if os.path.isdir(folder) and folder not in sys.path:
                sys.path.insert(0, folder)
        import auto_updater
        return auto_updater


def check_for_updates_from_tray(base_dir, stop_event):
    """Check for updates from the tray menu, on its own thread: Yes/No box, then the update helper's one bar (downloading, preparing, installing, then install complete or update failed). The helper is started first and follows the progress file written here; on a good download this service ends itself so the helper can swap the files and start it again."""
    if not _update_from_tray_lock.acquire(blocking=False):
        return
    progress_path = ""
    try:
        try:
            auto_updater = _load_auto_updater(base_dir)
            try:
                config = _read_runtime_config(base_dir)[0]
            except (OSError, ValueError):
                config = {}
            result = auto_updater.check_for_update(base_dir, config=config)
        except Exception as exc:
            log.warning("[SERVER TRAY] update check failed: %s", exc)
            _tray_message("Could not check for updates.\n\n%s" % exc, base_dir=base_dir)
            return
        if not result.get("available") or not result.get("manifest"):
            _tray_message(result.get("message") or "You are up to date.", base_dir=base_dir)
            return
        manifest = result["manifest"]
        if not _ask_yes_no(auto_updater, base_dir, "Version %s is available (you have %s).\nInstall it now?" % (
                manifest.get("version", "?"), result.get("current_version", "?"))):
            return
        if _app_is_open(base_dir):
            _tray_message("Close the Retro TV program on this computer first, then check again.", base_dir=base_dir)
            return
        progress_path = os.path.join(auto_updater._update_dir(base_dir), "tray_progress.json")

        def _write(**state):
            try:
                auto_updater._atomic_json_write(progress_path, state)
            except OSError as exc:
                log.warning("[SERVER TRAY] could not write the update progress: %s", exc)

        _write(stage="downloading", fraction=0.0)
        try:
            helper_pid = auto_updater.launch_staged_update(
                base_dir, {"version": manifest.get("version", "")}, _service_command(base_dir),
                parent_pid=os.getpid(), progress_path=progress_path)
        except Exception as exc:
            log.warning("[SERVER TRAY] could not start the update helper: %s", exc)
            _tray_message("Update failed.\n\n%s" % exc, base_dir=base_dir)
            return
        log.info("[SERVER TRAY] update helper started: pid=%s version=%s", helper_pid, manifest.get("version", "?"))
        last = {"at": 0.0}

        def _progress(done, total):
            now = time.monotonic()
            if total and done >= total:
                _write(stage="preparing")
            elif now - last["at"] >= TRAY_UPDATE_PROGRESS_WRITE_S:
                last["at"] = now
                _write(stage="downloading", fraction=min(1.0, done / total) if total else 0.0)

        try:
            staged = auto_updater.stage_update(base_dir, manifest, progress_callback=_progress)
        except Exception as exc:
            log.warning("[SERVER TRAY] update download failed: %s", exc)
            _write(stage="failed", error=str(exc))
            return
        _write(stage="staged", staged_path=staged["staged_path"])
        log.info("[SERVER TRAY] update %s is staged; the service ends so the helper can install it", manifest.get("version", "?"))
        stop_event.set()
    except Exception:
        log.exception("[SERVER TRAY] the update from the tray failed")
        if progress_path:
            try:
                auto_updater._atomic_json_write(progress_path, {"stage": "failed", "error": "unexpected error"})
            except Exception as caught_error:
                _log_swallowed("server_service: writing the failed stage to the update progress file", caught_error)
    finally:
        _update_from_tray_lock.release()


# ============================================================================
# SECTION: Service loop
# ============================================================================


def run_service(base_dir):
    """Run until Server Mode is explicitly disabled or all slots disappear."""
    base_dir = os.path.abspath(base_dir)
    _configure_logging(base_dir)
    # WARNING: owner 2026-11-26: the service runs for weeks; the cache manager sweeps its old memo entries and left-behind temp files the same as the program does.
    try:
        import cache_manager as _service_cache_manager
        _service_cache_manager.start()
    except Exception as _cache_start_error:
        logging.getLogger(__name__).warning("[SERVER SERVICE] the cache manager did not start: %s", _cache_start_error)
    existing = _read_pid(base_dir)
    if existing and existing != os.getpid() and _pid_alive(existing):
        log.info("[SERVER SERVICE] already running as pid %s", existing)
        return 0
    if not _claim_single_instance(base_dir):
        log.info("[SERVER SERVICE] another server service is already running on this computer (this folder or another build); exiting")
        return 0

    _tidy_legacy_files(base_dir)
    clear_quit_marker(base_dir)
    try:
        _write_pid(base_dir)
    except OSError:
        log.exception("[SERVER SERVICE] could not claim pid file")
        return 1

    server_files.set_streaming(True, log)
    stop_event = threading.Event()
    def _stop_from_tray():
        # WARNING: owner 2026-11-15: Stop Server also turns streaming off (the sign-in the other computers use is disabled and their connections closed) and the next start of the service turns it back on; Server Mode stays on and the start-at-login entry stays, so the service comes back at the next boot (or when the program is opened while it was closed). Turning Server Mode off is done in the program.
        try:
            note_quit_from_tray(base_dir)
            server_files.stop_streaming_verified(log)
        finally:
            stop_event.set()

    tray = ServerTray(
        title="Retro TV Server",
        on_stop=_stop_from_tray,
        on_open_app=lambda: _open_app(base_dir),
        on_check_updates=lambda: threading.Thread(
            target=check_for_updates_from_tray, args=(base_dir, stop_event), daemon=True, name="TrayUpdateCheck").start(),
        on_send_logs=lambda: threading.Thread(
            target=send_logs_from_tray, args=(base_dir,), daemon=True, name="TraySendLogs").start(),
        launch_on_boot=lambda: launch_on_boot_enabled(base_dir),
        on_toggle_launch_on_boot=lambda: set_launch_on_boot(base_dir, not launch_on_boot_enabled(base_dir)),
        icon_paths=icon_candidates(base_dir),
        menu_colors=lambda: _tray_menu_colors(base_dir),
    )
    announcer = None
    broadcaster = None
    scanner = None
    caption_scanner = None
    _scan_state_written = {"value": None, "at": -1e9}
    keep_awake = None
    profile_planner = None
    device_id = server_discovery.get_or_create_device_id(base_dir)
    try:
        tray.start()
        log.info("[SERVER SERVICE] started (pid=%s)", os.getpid())
        bad_reads = 0
        _beat_at = -1e9
        while not stop_event.is_set():
            if time.monotonic() - _beat_at >= SERVER_HEARTBEAT_EVERY_S:
                _beat_at = time.monotonic()
                write_heartbeat(base_dir)
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
                and bool(slots)
            )
            if not enabled:
                log.info("[SERVER SERVICE] stopping: Server Mode is off, "
                         "or no named slot files remain")
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
                            (slot_id, name, server_discovery.slot_has_media(current_paths.get(str(slot_id), "")))
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
                    # WARNING: The Server's own screen mode is imposed on every streamed channel a User plays.
                    try:
                        current_config, _unused_slots = _read_runtime_config(base_dir)
                        return {"screen_mode": current_config.get("screen_mode", "normal")}
                    except Exception as caught_error:
                        _log_swallowed("server_service: reading the server's screen mode for a User", caught_error)
                        return {}

                # WARNING: owner 2026-11-17: the server finds the watermark of a channel in this folder, by the same rule as the app (server_sync.watermark_file_for).
                _wm_internal = os.path.join(base_dir, "_internal")
                server_sync.WATERMARK_ROOT = os.path.join(_wm_internal if os.path.isdir(_wm_internal) else base_dir, "main", "images", "splash screen images", "WATERMARK")
                _shared_library = {}
                _shared_library_lock = threading.Lock()
                def _probe_library():
                    # WARNING: owner 2026-11-18 (audit A6): one connection to the media library is kept for the life of the service and shared by the channel clocks and the scanner; the scanner used to open a new one, and set the database up again, on every pass.
                    with _shared_library_lock:
                        if "probe" not in _shared_library:
                            import media_library
                            _shared_library["probe"] = media_library.ProbeLibrary(media_library.MediaLibrary(
                                server_files.library_db_path(base_dir)))
                        return _shared_library["probe"]
                _clock_library = {}
                # WARNING: owner 2026-11-18 (audit A8): one reading of a slot's channel clocks, kept SERVER_CLOCK_KEEP_S, serves the clocks sent to Users, the scan order and the scanner's show on air; each read the hub on its own before.
                _channel_clocks = server_clock.ChannelClocks(keep_s=server_clock.KEEP_S,
                                                             missing_fn=lambda path: server_scanner.missing_items(_probe_library(), path))
                def _clock_provider(channels, slot_id=None):
                    # WARNING: the channel clocks are read from the same media library file the program writes its schedule hub into. Each show carries what the shared checklist still lacks for its file.
                    import media_library
                    probe = _probe_library()
                    if "hub" not in _clock_library:
                        _clock_library["hub"] = media_library.ScheduleHub(probe.library)
                    _clocks_now = _channel_clocks.snapshot(
                        _clock_library["hub"], [ch for ch, info in (channels or {}).items() if isinstance(info, dict) and info.get("active", True)],
                        slot_id=slot_id)
                    # WARNING: owner 2026-11-26: "that load bar between server streaming, profile watching on server, and the tray to all show the same load progress"; each channel's clock carries the file scan's [done, total] for the User's bar.
                    _slot_progress = (getattr(scanner, "progress", None) or {}).get(str(slot_id)) or {}
                    for _ch, _clock in (_clocks_now or {}).items():
                        if isinstance(_clock, dict) and str(_ch).zfill(2) in _slot_progress:
                            _clock["scan"] = list(_slot_progress[str(_ch).zfill(2)])
                    return _clocks_now
                # WARNING: owner 2026-11-24: captions are made once and kept in main/data/saved captions, the same folder the program on this PC uses; the file's library record notes them.
                import media
                import saved_captions
                saved_captions.set_root(media.saved_captions_root())
                def _note_saved_captions(key, name, covered, complete):
                    _probe_library().library.upsert_fields(key, saved_captions_file=name, saved_captions_covered=float(covered),
                                                           saved_captions_complete=1 if complete else 0)
                saved_captions.set_library_note(_note_saved_captions)
                # WARNING: owner log 2026-11-25 (send_logs_tray3): the service never installed the library's length reader, so every file read as not measured, no lineup could be built and the planner wrote 0 rows ("ch05 0 new, 0 ahead in the HUB" every 2 minutes); the program installs the same reader at its start.
                try:
                    import media_library
                    media.set_duration_hub(media_library.DurationHub(_probe_library()))
                    log.info("[SERVER PLAN] the library's length reader is installed; the planner reads every file's length from the library")
                except Exception as caught_error:
                    log.warning("[SERVER PLAN] the library's length reader could not be installed, so no channel can be planned: %s", caught_error)
                # WARNING: owner 2026-11-24: the service keeps every served profile's schedule in this server's HUB itself, with the program open or closed (server_planner).
                profile_planner = server_planner.ProfilePlanner(_current_slots, _probe_library)
                broadcaster = server_sync.ScheduleBroadcaster(
                    _current_slots, remote_id=device_id, on_slot_changed=profile_planner.wake, rows_provider=profile_planner.rows_for,
                    playback_provider=_current_playback,
                    files_provider=server_files.make_provider(
                        server_files.library_db_path(base_dir), base_dir),
                    clock_provider=_clock_provider,
                    warm_allowed=server_files.make_warm_allowed(base_dir),
                    captions=saved_captions)
                announcer.start()
                profile_planner.start()
                broadcaster.start()
                try:
                    import performance_governor as _scan_gov
                    _scan_cpu = _scan_gov.SystemCpuSampler()
                    _scan_cpu.percent()
                    _scan_cpu_percent = _scan_cpu.percent
                except Exception as exc:
                    log.warning("[SERVER SCAN] machine load is not readable (%s); scanning without load pacing", exc)
                    _scan_cpu_percent = None
                def _watched_channels(slot_id):
                    # WARNING: owner 2026-11-14: a profile viewed in the program on this computer is a watching User like one on another computer: its channel is scanned first.
                    found = set(broadcaster.watching(slot_id))
                    local = server_scanner.read_local_watching(base_dir)
                    if local is not None and local[0] == str(slot_id):
                        found.add(local[1])
                    return found
                scanner = server_scanner.FactsScanner(
                    _current_slots, lambda: _app_is_open(base_dir), _probe_library, server_scanner.measure_loudness,
                    watched_fn=_watched_channels,
                    anchors_fn=lambda: server_scanner.read_anchors(base_dir),
                    program_state_fn=lambda: server_scanner.read_program_scan_state(base_dir),
                    clocks=_channel_clocks,
                    next_files_fn=broadcaster.watching_next, regular_fn=lambda: _config_path(base_dir),
                    cpu_percent_fn=_scan_cpu_percent, streaming_fn=lambda: bool(broadcaster.watching()) or server_scanner.read_local_watching(base_dir) is not None)
                scanner.start()
                try:
                    import performance_governor as _caption_gov
                    _caption_cpu = _caption_gov.SystemCpuSampler()
                    # WARNING: owner logs 2026-11-25 (tray8): the caption scan's own model and this service are left out of the load it paces by; other programs and the open program still count.
                    import media as _caption_media
                    _caption_cpu = _caption_gov.SystemCpuSampler(own_seconds_fn=_caption_media.own_cpu_seconds)
                    _caption_cpu.percent()
                    _caption_cpu_percent = _caption_cpu.percent
                except Exception as exc:
                    log.warning("[CAPTION SCAN] machine load is not readable (%s); the caption scan paces by the program's level only", exc)
                    _caption_cpu_percent = None
                caption_scanner = server_captions.CaptionScanner(
                    _current_slots, _probe_library, scanner,
                    server_captions.CaptionPacer(
                        cpu_percent_fn=_caption_cpu_percent, program_open_fn=lambda: _app_is_open(base_dir),
                        program_state_fn=lambda: server_scanner.read_program_scan_state(base_dir),
                        streaming_fn=lambda: bool(broadcaster.watching()) or server_scanner.read_local_watching(base_dir) is not None),
                    live_keys_fn=broadcaster.live_caption_keys)
                # WARNING: owner 2026-11-25: profile scans, profile captions, then the regular channels' scans, then their captions; the file scan reaches the regular channels only once every profile file is captioned.
                scanner.regular_allowed_fn = lambda: caption_scanner.profiles_done
                # WARNING: owner 2026-11-25: the caption scan makes and improves captions at the performance mode this PC is set to in the program (Lite, Standard or Performance), read from the program's saved settings each time it is asked.
                caption_scanner.mode_fn = lambda: _saved_performance_mode(base_dir)
                caption_scanner.start()
                # WARNING: owner 2026-11-26: the tray's menu shows the same file scan progress the program's load bar shows.
                tray.scan_status = lambda: server_scanner.progress_line(scanner.progress, caption_scanner.profiles_done)
                keep_awake = server_scanner.DriveKeepAwake(_current_slots)
                keep_awake.start()
                log.info("[SERVER SERVICE] discovery and schedule "
                         "broadcasting started for %d slot(s)", len(slots))

            if scanner is not None and caption_scanner is not None:
                # WARNING: owner 2026-11-25: the open program keeps its future scans of the regular channels until every server profile file is scanned and captioned; this file tells it (written when it changes, else every SERVER_SCAN_STATE_WRITE_S).
                _profiles_done = bool(caption_scanner.profiles_done and scanner.profiles_idle)
                if _profiles_done != _scan_state_written["value"] or time.monotonic() - _scan_state_written["at"] >= SERVER_SCAN_STATE_WRITE_S:
                    server_scanner.write_service_scan_state(base_dir, _profiles_done, scanner.progress)
                    _scan_state_written.update(value=_profiles_done, at=time.monotonic())
            stop_event.wait(SERVICE_POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("[SERVER SERVICE] interrupted")
    except Exception:
        log.exception("[SERVER SERVICE] fatal service error")
        return 1
    finally:
        if scanner is not None:
            scanner.stop()
        if caption_scanner is not None:
            caption_scanner.stop()
        if keep_awake is not None:
            keep_awake.stop()
        if profile_planner is not None:
            profile_planner.stop()
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
        try:
            os.remove(_heartbeat_path(base_dir))
        except FileNotFoundError:
            # WARNING: the normal end: there was no heartbeat file to remove.
            pass
        except OSError as caught_error:
            _log_swallowed("server_service: removing the heartbeat file at exit", caught_error)
        # WARNING: explicit disable removes the Run value; an exit from a transient error leaves it so the next app launch can reinstall it.
        try:
            config, slots = _read_runtime_config(base_dir)
            if (not config.get("server_mode_enabled", False)
                    or not slots):
                set_autostart(base_dir, False)
        except Exception as caught_error:
            _log_swallowed("server_service: turning the autostart off at exit", caught_error)
        log.info("[SERVER SERVICE] stopped")
    return 0


# ============================================================================
# SECTION: Entry point
# ============================================================================


def main(argv=None):
    parser = argparse.ArgumentParser(description="Retro TV headless server")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--base-dir", default=None)
    args = parser.parse_args(argv)
    # WARNING: default is the project root (this file's parent folder), so a manual run without --base-dir finds retro_config.json and the pid file.
    base_dir = args.base_dir or _project_root()
    return run_service(base_dir)


__all__ = ["stop_service", "ensure_running", "run_service", "set_autostart", "launch_on_boot_enabled", "set_launch_on_boot", "main"]


if __name__ == "__main__":
    raise SystemExit(main())