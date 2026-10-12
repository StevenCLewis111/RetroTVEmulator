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

# ============================================================================
# SECTION: Constants
# ============================================================================
DISCOVERY_PORT = 47531
ANNOUNCE_INTERVAL = 2.0
ENTRY_TIMEOUT = 6.0
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
                if isinstance(item, dict):
                    slot_id, name = item.get("slot_id"), item.get("name")
                else:
                    slot_id, name = item[0], item[1]
                if slot_id and str(name).strip():
                    slots.append({"slot_id": str(slot_id), "name": str(name).strip()})
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
                        }
                        for s in data.get("slots", [])
                        if str(s.get("name", "")).strip()
                        and str(s.get("slot_id", "")).strip()
                    ]
                    with self._lock:
                        self._entries[data["device_id"]] = {"host": host, "slots": slots,
                                                              "last_seen": now}
                except socket.timeout:
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
                    if stale:
                        log.info(
                            "[SERVER DIAG] Discovery entries expired count=%d",
                            len(stale),
                        )
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
__all__ = ["ServerAnnouncer", "ServerListener", "get_or_create_device_id", "slot_provider_from_db"]

