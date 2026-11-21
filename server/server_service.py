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

import server_clock
import server_discovery
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
SERVICE_POLL_INTERVAL = 1.0
# VALUES CHANGE HERE (the app waits this many seconds after starting the service before it may start another; mirrors SERVER_SERVICE_RELAUNCH_COOLDOWN_S in performance_governor.py)
SERVICE_RELAUNCH_COOLDOWN_S = 15.0
# VALUES CHANGE HERE (mirrored in performance_governor.py): the service writes a heartbeat this often (seconds); the program ends and starts again a service whose heartbeat is older than the stale limit.
SERVER_HEARTBEAT_EVERY_S = 5.0
SERVER_HEARTBEAT_STALE_S = 90.0
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


def _configure_logging(base_dir):
    """Give the detached process its own small rotating log."""
    if log.handlers:
        return
    try:
        path = os.path.join(_logs_dir(base_dir), "server_service.log")
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1 * 1024 * 1024, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
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
        except OSError:
            pass


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
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
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, 0, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # access denied: the process exists
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _hold_mutex(name):
    """True when this process is the first to hold the named Windows mutex; it is held until the process ends."""
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, 1, name)
    _MUTEX_HANDLES.append(handle)
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


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
    except (OSError, ValueError):
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
    except OSError:
        pass


def heartbeat_age(base_dir, now=None):
    """Seconds since the service last wrote its heartbeat, or None when there is no readable heartbeat (an older service, or one that has not written yet)."""
    try:
        with open(_heartbeat_path(base_dir), "r", encoding="ascii") as fh:
            written = float(fh.read().strip())
    except (OSError, ValueError):
        return None
    return max(0.0, (time.time() if now is None else float(now)) - written)


def _remove_pid(base_dir):
    try:
        if _read_pid(base_dir) == os.getpid():
            os.remove(_pid_path(base_dir))
    except OSError:
        pass


# ============================================================================
# SECTION: Runtime config
# ============================================================================


def _config_path(base_dir):
    return os.path.join(base_dir, "retro_config.json")


def _folder_safe_name(name, fallback):
    """A server name as a folder name: Windows-forbidden characters removed, trailing dots and spaces trimmed; the fallback when nothing is left."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name or "")).strip().rstrip(". ")
    return cleaned or str(fallback)


def _server_data_dir(base_dir):
    """Where the server and user channel files are: <install>/_internal/server/.json in an installed build, <base>/server/.json otherwise; the older <install>/server/.json is used when it is the only one that exists."""
    # WARNING: owner 2026-11-11: must agree with server_data_dir in media.py (the program moves the old folder into _internal; the service only reads).
    internal = os.path.join(base_dir, "_internal")
    new_dir = os.path.join(internal if os.path.isdir(internal) else base_dir, "server", ".json")
    old_dir = os.path.join(base_dir, "server", ".json")
    if not os.path.isdir(new_dir) and os.path.isdir(old_dir):
        return old_dir
    return new_dir


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
# VALUES CHANGE HERE (mirrored in performance_governor.py): the computer's start time is worked out from its uptime, so two readings of the same boot can differ by a little; readings this close are the same boot.
SAME_BOOT_TOLERANCE_S = 120
try:
    import performance_governor as _gov_quit
    SAME_BOOT_TOLERANCE_S = int(_gov_quit.SAME_BOOT_TOLERANCE_S)
except Exception:
    pass


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
        except OSError:
            pass
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
    except Exception:
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
    except OSError:
        pass


def start_is_held(base_dir):
    """True when the owner quit the service from the tray while the program was open and this is still the same boot: the service then waits for the next boot. A quit made while the program was closed, or one from an earlier boot, does not hold anything (the marker is cleared), so opening the program starts the service again."""
    try:
        with open(_quit_marker_path(base_dir), "r", encoding="utf-8") as fh:
            marker = json.load(fh)
    except (OSError, ValueError):
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
        except OSError:
            pass
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
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "retro_tv_emulator.py")
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
# VALUES CHANGE HERE: folders inside logs that are never packed (downloaded update packages are huge), and the largest single file packed, in bytes.
SEND_LOGS_SKIP_FOLDERS = (".retro_tv_updates", "__pycache__")
SEND_LOGS_MAX_FILE_BYTES = 64 * 1024 * 1024


def pack_logs_zip(base_dir, destination_dir=None, now=None):
    """Zips the install's logs folder (minus update downloads) into one file and returns its path."""
    import zipfile
    logs = _logs_dir(base_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    target_dir = destination_dir or os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.isdir(target_dir):
        target_dir = base_dir
    out = os.path.join(target_dir, "RetroTV_logs_%s.zip" % stamp)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
        for folder, dirs, files in os.walk(logs):
            dirs[:] = [d for d in dirs if d not in SEND_LOGS_SKIP_FOLDERS]
            for name in files:
                full = os.path.join(folder, name)
                try:
                    if os.path.getsize(full) > SEND_LOGS_MAX_FILE_BYTES:
                        continue
                    bundle.write(full, os.path.relpath(full, logs))
                except OSError as exc:
                    log.warning("[SERVER TRAY] log file skipped %s: %s", full, exc)
    return out


def send_logs_from_tray(base_dir):
    """Send logs on the tray menu: packs the logs into one zip on the Desktop, shows it in Explorer and says where it is."""
    if not _send_logs_lock.acquire(blocking=False):
        return
    try:
        out = pack_logs_zip(base_dir)
        log.info("[SERVER TRAY] logs packed: %s (%.1f MB)", out, os.path.getsize(out) / 1048576.0)
        if os.name == "nt":
            subprocess.Popen(["explorer.exe", "/select,", out])
        _tray_message("Your logs are packed in one file:\n\n%s\n\nSend that file." % out, base_dir=base_dir)
    except Exception as exc:
        log.exception("[SERVER TRAY] could not pack the logs")
        _tray_message("The logs could not be packed: %s" % exc, base_dir=base_dir)
    finally:
        _send_logs_lock.release()


# ============================================================================
# SECTION: Check for updates (tray)
# ============================================================================

_update_from_tray_lock = threading.Lock()
# VALUES CHANGE HERE: the fewest seconds between two writes of the update progress file the helper's bar reads.
TRAY_UPDATE_PROGRESS_WRITE_S = 0.25


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
    flags = 0x4 if question else 0x0          # MB_YESNO / MB_OK
    flags |= 0x20 if question else 0x40       # question / information icon
    flags |= 0x00040000 | 0x00010000          # MB_TOPMOST | MB_SETFOREGROUND
    answer = ctypes.windll.user32.MessageBoxW(None, text, "Retro TV Server", flags)
    return answer in (1, 6)                   # IDOK / IDYES


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
    """auto_updater lives in the updater folder; the packaged program already has it on its path, a source run adds it."""
    try:
        import auto_updater
        return auto_updater
    except ImportError:
        for root in (base_dir, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
            folder = os.path.join(root, "updater")
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
            except Exception:
                pass
    finally:
        _update_from_tray_lock.release()


# ============================================================================
# SECTION: Service loop
# ============================================================================


def run_service(base_dir):
    """Run until Server Mode is explicitly disabled or all slots disappear."""
    base_dir = os.path.abspath(base_dir)
    _configure_logging(base_dir)
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
    keep_awake = None
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
                    # The Server's own screen mode is imposed on every
                    # streamed channel a User plays.
                    try:
                        current_config, _unused_slots = _read_runtime_config(base_dir)
                        return {"screen_mode": current_config.get("screen_mode", "normal")}
                    except Exception:
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
                    return _channel_clocks.snapshot(
                        _clock_library["hub"], [ch for ch, info in (channels or {}).items() if isinstance(info, dict) and info.get("active", True)],
                        slot_id=slot_id)
                broadcaster = server_sync.ScheduleBroadcaster(
                    _current_slots, remote_id=device_id,
                    playback_provider=_current_playback,
                    files_provider=server_files.make_provider(
                        server_files.library_db_path(base_dir), base_dir),
                    clock_provider=_clock_provider,
                    warm_allowed=server_files.make_warm_allowed(base_dir))
                announcer.start()
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
                    next_files_fn=broadcaster.watching_next,
                    cpu_percent_fn=_scan_cpu_percent, streaming_fn=lambda: bool(broadcaster.watching()) or server_scanner.read_local_watching(base_dir) is not None)
                scanner.start()
                keep_awake = server_scanner.DriveKeepAwake(_current_slots)
                keep_awake.start()
                log.info("[SERVER SERVICE] discovery and schedule "
                         "broadcasting started for %d slot(s)", len(slots))

            stop_event.wait(SERVICE_POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("[SERVER SERVICE] interrupted")
    except Exception:
        log.exception("[SERVER SERVICE] fatal service error")
        return 1
    finally:
        if scanner is not None:
            scanner.stop()
        if keep_awake is not None:
            keep_awake.stop()
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
        except OSError:
            pass
        # NOTE: explicit disable removes the Run value; an exit from a
        # transient error leaves it so the next app launch can reinstall it.
        try:
            config, slots = _read_runtime_config(base_dir)
            if (not config.get("server_mode_enabled", False)
                    or not slots):
                set_autostart(base_dir, False)
        except Exception:
            pass
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
    # NOTE: default is the project root (this file's parent folder), so a
    # manual run without --base-dir finds retro_config.json and the pid file.
    base_dir = args.base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return run_service(base_dir)


__all__ = ["stop_service", "ensure_running", "run_service", "set_autostart", "launch_on_boot_enabled", "set_launch_on_boot", "main"]


if __name__ == "__main__":
    raise SystemExit(main())