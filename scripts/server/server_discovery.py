"""Small, dependency-free LAN discovery for Retro TV server slots.

Owns: UDP broadcast announce/listen of named server slots and the persisted
device id.
Does not own: the schedule transport (server_sync.py) or slot storage.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from typing import Callable, Iterable

from server_diagnostics import breadcrumb as _server_breadcrumb

log = logging.getLogger(__name__)

try:
    import performance_governor as _gov
except Exception as _governor_error:
    _gov = None
    log.warning("[FAILURE] server_discovery: performance_governor did not import, so the built-in numbers are used: %s", _governor_error)

# WARNING: owner 2026-11-26: every memo that grows with the library or with time is made by the cache manager, so none can grow without a limit.
import cache_manager


def _log_swallowed(where, error):
    """Writes the shared, counted [FAILURE] line through performance_governor (a plain warning when it did not import)."""
    if _gov is not None:
        return _gov.log_swallowed(where, error)
    log.warning("[FAILURE] %s: %s: %s", where, type(error).__name__, error)
    return None

# ============================================================================
# SECTION: Constants
# ============================================================================
# WARNING: the port is part of the network protocol: a server and its Users must use the same one, so it stays here and is not a setting.
DISCOVERY_PORT = 47531
# WARNING: the fallback numbers below must match SERVER_DISCOVERY_ANNOUNCE_INTERVAL and SERVER_DISCOVERY_ENTRY_TIMEOUT in performance_governor.py; they are used only when that module fails to import.
ANNOUNCE_INTERVAL = _gov.SERVER_DISCOVERY_ANNOUNCE_INTERVAL if _gov is not None else 2.0
ENTRY_TIMEOUT = _gov.SERVER_DISCOVERY_ENTRY_TIMEOUT if _gov is not None else 6.0
_MAGIC = "retro-tv-discovery-v1"


# ============================================================================
# SECTION: Device identity
# ============================================================================


def get_or_create_device_id(state_dir: str) -> str:
    path = os.path.join(state_dir, ".retro_tv_device_id")
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        if value:
            return value
    except FileNotFoundError:
        # WARNING: the normal end: no device id has been saved yet; a new one is made.
        pass
    except OSError as exc:
        _server_breadcrumb(
            log, logging.WARNING, f"device-id-read:{path}",
            "[SERVER DIAG] Discovery device-id read failed path=%s: %s",
            path, exc,
        )
    value = uuid.uuid4().hex
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(value)
        os.replace(tmp, path)
    except OSError as exc:
        _server_breadcrumb(
            log, logging.WARNING, f"device-id-write:{path}",
            "[SERVER DIAG] Discovery device-id persist failed path=%s: %s",
            path, exc,
        )
    return value


_NOT_PROGRAMS = ("Commercials", "Intros", "Outros")


def slot_has_media(path) -> bool:
    """True when the server's channel file at `path` has at least one station (05-44) that is on and has something scheduled; commercials, intros and outros alone are not a station's programs."""
    # WARNING: owner 2026-11-14: a server with no station that has media was listed to pick, and picking it opened the guide with every channel on Static.
    try:
        stat = os.stat(path)
        stamp = (stat.st_mtime_ns, stat.st_size)
        held = _slot_media_answers.get(path)
        if held is not None and held[0] == stamp:
            return held[1]
        # WARNING: owner 2026-11-18: the answer is worked out again only when the file changed; the announcer asks every 2 s and the whole file used to be parsed each time.
        answer = _slot_file_has_media(path)
        _slot_media_answers[path] = (stamp, answer)
        return answer
    except OSError:
        return True


_slot_media_answers = cache_manager.memo("server file answers")
def _slot_file_has_media(path) -> bool:
    """Reads the channel file and answers slot_has_media for it."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        channels = data.get("channels", {}) if isinstance(data, dict) else {}
        for key, channel in channels.items():
            if not (str(key).isdigit() and 5 <= int(key) <= 44) or not isinstance(channel, dict) or not channel.get("active", False):
                continue
            for table in ("schedules", "holiday_schedules"):
                items = channel.get(table, {})
                if isinstance(items, dict) and any(isinstance(v, (list, tuple)) and len(v) > 0 for k, v in items.items() if k not in _NOT_PROGRAMS):
                    return True
        return False
    except (OSError, ValueError, AttributeError, TypeError):
        # WARNING: A file that cannot be read now is not reported as empty: the server stays listed.
        return True


def slot_provider_from_db(db) -> Callable[[], list[tuple[str, str]]]:
    return lambda: list(db.list_server_slots()) if db is not None else []


# ============================================================================
# SECTION: Announcer (server side)
# ============================================================================


class ServerAnnouncer:
    def __init__(self, get_slots: Callable[[], Iterable], *, device_id=None,
                 host_name=None, port=DISCOVERY_PORT, interval=ANNOUNCE_INTERVAL):
        self.get_slots = get_slots
        self.device_id = device_id or uuid.uuid4().hex
        self.host_name = host_name or socket.gethostname()
        self.port = int(port)
        self.interval = float(interval)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="retro-tv-announcer", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.interval + 1.0))
        self._thread = None

    def _packet(self):
        slots = []
        try:
            for item in self.get_slots() or []:
                ready = True
                if isinstance(item, dict):
                    slot_id, name = item.get("slot_id"), item.get("name")
                    ready = item.get("ready", True) is not False
                else:
                    slot_id, name = item[0], item[1]
                    if len(item) > 2:
                        ready = bool(item[2])
                if slot_id and str(name).strip():
                    slots.append({"slot_id": str(slot_id), "name": str(name).strip(), "ready": bool(ready)})
        except Exception as exc:
            _server_breadcrumb(
                log, logging.WARNING, "discovery-slot-provider",
                "[SERVER DIAG] Discovery slot provider failed: %s", exc,
            )
            return None
        if not slots:
            return None
        return json.dumps({"magic": _MAGIC, "device_id": self.device_id,
                           "host": self.host_name, "slots": slots}, separators=(",", ":")).encode()

    def _run(self):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            while not self._stop.is_set():
                packet = self._packet()
                if packet:
                    try:
                        sock.sendto(packet, ("<broadcast>", self.port))
                    except OSError as exc:
                        _server_breadcrumb(
                            log, logging.WARNING, f"discovery-send:{self.port}",
                            "[SERVER DIAG] LAN discovery announce failed "
                            "port=%s: %s", self.port, exc,
                        )
                self._stop.wait(self.interval)
        except OSError as exc:
            _server_breadcrumb(
                log, logging.ERROR, f"discovery-announcer:{self.port}",
                "[SERVER DIAG] LAN discovery announcer stopped port=%s: %s",
                self.port, exc, interval=0.0,
            )
        finally:
            if sock is not None:
                sock.close()


# ============================================================================
# SECTION: Listener (client side)
# ============================================================================


class ServerListener:
    def __init__(self, *, device_id=None, port=DISCOVERY_PORT, entry_timeout=ENTRY_TIMEOUT):
        self.device_id = device_id
        self.port = int(port)
        self.entry_timeout = float(entry_timeout)
        self._entries = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="retro-tv-listener", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try: sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError: pass
            sock.bind(("", self.port))
            sock.settimeout(0.5)
            while not self._stop.is_set():
                now = time.monotonic()
                try:
                    raw, address = sock.recvfrom(65535)
                    data = json.loads(raw.decode("utf-8"))
                    if data.get("magic") != _MAGIC or data.get("device_id") == self.device_id:
                        continue
                    host = str(data.get("host") or address[0])
                    slots = [
                        {
                            "slot_id": str(s.get("slot_id", "")).strip(),
                            "name": str(s.get("name", "")).strip(),
                            "host": host,
                            "ready": s.get("ready", True) is not False,
                        }
                        for s in data.get("slots", [])
                        if str(s.get("name", "")).strip()
                        and str(s.get("slot_id", "")).strip()
                    ]
                    with self._lock:
                        self._entries[data["device_id"]] = {"host": host, "slots": slots,
                                                              "last_seen": now}
                except socket.timeout:
                    # WARNING: the normal end: no announcement arrived during this wait.
                    pass
                except (OSError, ValueError, UnicodeError, KeyError, TypeError,
                        AttributeError) as exc:
                    _server_breadcrumb(
                        log, logging.WARNING, f"discovery-packet:{self.port}",
                        "[SERVER DIAG] Invalid LAN discovery packet port=%s: %s",
                        self.port, exc,
                    )
                    continue
                with self._lock:
                    stale = [k for k, v in self._entries.items() if now - v["last_seen"] > self.entry_timeout]
                    for key in stale:
                        self._entries.pop(key, None)
        except OSError as exc:
            _server_breadcrumb(
                log, logging.ERROR, f"discovery-listener:{self.port}",
                "[SERVER DIAG] LAN discovery listener stopped port=%s: %s",
                self.port, exc, interval=0.0,
            )
        finally:
            if sock is not None:
                sock.close()

    def get_discovered_entries(self):
        now = time.monotonic()
        with self._lock:
            result = [
                dict(slot) for value in self._entries.values()
                if now - value["last_seen"] <= self.entry_timeout
                for slot in value["slots"]
            ]
        return result


# ============================================================================
# SECTION: Exports
# ============================================================================
__all__ = ["slot_has_media", "ServerAnnouncer", "ServerListener", "get_or_create_device_id", "slot_provider_from_db"]

