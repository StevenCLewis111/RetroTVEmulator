"""Schedule-only server transport for Retro TV networking.

The protocol deliberately sends channel/schedule JSON only. Media files, ROMs,
cores, BIOS files, and general application settings never cross this boundary.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import socket
import struct
import threading
import time
from typing import Callable, Mapping

log = logging.getLogger(__name__)
from server_diagnostics import breadcrumb as _server_breadcrumb
PROTOCOL = "retro-tv-schedule-v1"
MAX_FRAME = 16 * 1024 * 1024


def _rewrite_remote(value, remote_id: str):
    """Rewrite every remote asset reference without damaging paired entries."""
    if isinstance(value, list):
        return [_rewrite_remote(item, remote_id) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _rewrite_remote(item, remote_id) for key, item in value.items()}
    for key in tuple(result):
        if key.endswith("_path_remote") or key == "remote_asset_id":
            result[key] = remote_id
    return result


def sanitize_channels(channels: Mapping, remote_id: str = "") -> dict:
    """Return a deep-copied schedule payload with no config or file contents."""
    safe = copy.deepcopy(dict(channels or {}))
    if remote_id:
        safe = _rewrite_remote(safe, remote_id)
    return safe


def build_file_listing_message(slot_id: str, relative_path: str, entries: list) -> dict:
    """Build a safe SERVER-drive directory response; paths stay relative."""
    return {"protocol": PROTOCOL, "type": "file_listing", "slot_id": str(slot_id),
            "path": str(relative_path or "."), "entries": entries}


# Server-owned playback settings that DO cross the wire, alongside the
# schedule. Everything else in the server's config still stays home.
VALID_SCREEN_MODES = ("normal", "stretch", "crop", "panscan")


def sanitize_playback(playback) -> dict:
    """Whitelist the playback settings a Server may impose on streamed channels."""
    out = {}
    if isinstance(playback, Mapping):
        mode = str(playback.get("screen_mode", "")).strip().lower()
        if mode in VALID_SCREEN_MODES:
            out["screen_mode"] = mode
    return out


def build_schedule_message(slot_id: str, channels: Mapping, revision: int,
                           remote_id: str = "", playback=None) -> dict:
    return {
        "playback": sanitize_playback(playback),
        "protocol": PROTOCOL,
        "type": "schedule",
        "slot_id": str(slot_id),
        "revision": int(revision),
        "channels": sanitize_channels(channels, remote_id),
    }


def _send_message(sock: socket.socket, message: dict) -> None:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_FRAME:
        raise ValueError("schedule payload is too large")
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    while size:
        chunk = sock.recv(size)
        if not chunk:
            raise ConnectionError("peer disconnected")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def recv_message(sock: socket.socket) -> dict:
    length = struct.unpack("!I", _recv_exact(sock, 4))[0]
    if length <= 0 or length > MAX_FRAME:
        raise ValueError("invalid schedule frame")
    message = json.loads(_recv_exact(sock, length).decode("utf-8"))
    if message.get("protocol") != PROTOCOL:
        raise ValueError("unsupported schedule protocol")
    return message


class ScheduleBroadcaster:
    """Polls slot files and pushes changed schedules to subscribed clients."""
    def __init__(self, slots: Callable[[], Mapping[str, str]], *, host="0.0.0.0",
                 port=47532, poll_interval=0.5, remote_id="", playback_provider=None):
        self.slots = slots
        self.playback_provider = playback_provider
        self.host, self.port = host, int(port)
        self.poll_interval = float(poll_interval)
        self.remote_id = remote_id
        self._stop = threading.Event()
        self._thread = None
        self._clients = set()
        # A client that connects after the first poll still needs the current
        # snapshot even when the slot has not changed since the previous poll.
        # Keep this separate from _last_payloads so a long-lived service can
        # serve late User connections without requiring a server-side edit.
        self._pending_snapshot_clients = set()
        self._lock = threading.Lock()
        self._revisions = {}
        self._last_payloads = {}

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="retro-tv-schedule-server", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
            self._pending_snapshot_clients.clear()
        for client in clients:
            try: client.shutdown(socket.SHUT_RDWR)
            except (OSError, ValueError): pass
            try: client.close()
            except (OSError, ValueError): pass
        if self._thread: self._thread.join(timeout=2.0)
        self._thread = None

    def _read_slot(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("channels", {})

    @staticmethod
    def list_directory(root, relative_path=".", valid_extensions=()):
        """Return metadata only, rejecting traversal outside the shared root."""
        try:
            base = os.path.realpath(root)
            requested = os.path.realpath(os.path.join(base, relative_path or "."))
            inside_root = os.path.commonpath((base, requested)) == base
        except (OSError, ValueError, TypeError) as exc:
            _server_breadcrumb(
                log, logging.WARNING, f"server-drive-path:{root}:{relative_path}",
                "[SERVER DIAG] SERVER-drive path validation failed root=%s "
                "relative=%s: %s", root, relative_path, exc,
            )
            return []
        if not inside_root:
            _server_breadcrumb(
                log, logging.WARNING, f"server-drive-traversal:{root}:{relative_path}",
                "[SERVER DIAG] SERVER-drive traversal rejected root=%s "
                "relative=%s", root, relative_path,
            )
            return []
        if not os.path.isdir(requested):
            _server_breadcrumb(
                log, logging.WARNING, f"server-drive-missing:{requested}",
                "[SERVER DIAG] SERVER-drive directory unavailable path=%s",
                requested,
            )
            return []
        allowed = {str(ext).lower() for ext in valid_extensions}
        result = []
        try:
            entries = sorted(os.scandir(requested), key=lambda item: item.name.lower())
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    result.append({"name": entry.name, "type": "dir"})
                elif not allowed or os.path.splitext(entry.name)[1].lower() in allowed:
                    result.append({"name": entry.name, "type": "file", "size": entry.stat().st_size})
        except OSError as exc:
            _server_breadcrumb(
                log, logging.WARNING, f"server-drive-scan:{requested}",
                "[SERVER DIAG] SERVER-drive directory read failed path=%s: %s",
                requested, exc,
            )
            return []
        return result

    def _broadcast(self, message, clients=None):
        dead = []
        if clients is None:
            with self._lock:
                targets = list(self._clients)
        else:
            targets = list(clients)
        for client in targets:
            try: _send_message(client, message)
            except (OSError, ConnectionError, ValueError):
                dead.append(client)
        if dead:
            with self._lock:
                for client in dead:
                    self._clients.discard(client)
                    self._pending_snapshot_clients.discard(client)
            for client in dead:
                client.close()
            _server_breadcrumb(
                log, logging.WARNING, f"schedule-broadcast-clients:{self.port}",
                "[SERVER DIAG] Schedule broadcast dropped client connection(s) "
                "port=%s count=%d", self.port, len(dead),
            )
        return set(targets) - set(dead)

    def _run(self):
        server = None
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port)); server.listen(8); server.settimeout(0.25)
            log.info("[SERVER DIAG] Schedule broadcaster listening host=%s port=%s",
                     self.host, self.port)
            while not self._stop.is_set():
                try:
                    client, address = server.accept(); client.settimeout(2.0)
                    with self._lock:
                        self._clients.add(client)
                        self._pending_snapshot_clients.add(client)
                    log.info("[SERVER DIAG] Schedule client connected peer=%s:%s",
                             address[0], address[1])
                except socket.timeout:
                    pass
                try:
                    slots = dict(self.slots() or {})
                except Exception as exc:
                    _server_breadcrumb(
                        log, logging.WARNING, f"schedule-slot-provider:{self.port}",
                        "[SERVER DIAG] Schedule slot provider failed port=%s: %s",
                        self.port, exc,
                    )
                    slots = {}
                try:
                    playback = sanitize_playback(
                        self.playback_provider() if self.playback_provider else None)
                except Exception as exc:
                    _server_breadcrumb(
                        log, logging.WARNING, f"schedule-playback-provider:{self.port}",
                        "[SERVER DIAG] Playback settings provider failed port=%s: %s",
                        self.port, exc,
                    )
                    playback = {}
                for slot_id, path in slots.items():
                    try:
                        channels = self._read_slot(path)
                        payload = json.dumps({"c": channels, "p": playback},
                                             sort_keys=True, separators=(",", ":"))
                        changed = payload != self._last_payloads.get(slot_id)
                        if changed:
                            self._last_payloads[slot_id] = payload
                            self._revisions[slot_id] = self._revisions.get(slot_id, 0) + 1
                        message = build_schedule_message(
                            slot_id, channels,
                            self._revisions.get(slot_id, 0),
                            self.remote_id,
                            playback,
                        )
                        if changed:
                            delivered = self._broadcast(message)
                            with self._lock:
                                for delivered_client in delivered:
                                    self._pending_snapshot_clients.discard(delivered_client)
                        else:
                            with self._lock:
                                pending = list(self._pending_snapshot_clients)
                            delivered = self._broadcast(message, pending)
                            with self._lock:
                                for pending_client in delivered:
                                    self._pending_snapshot_clients.discard(pending_client)
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        _server_breadcrumb(
                            log, logging.WARNING, f"schedule-slot-read:{path}",
                            "[SERVER DIAG] Schedule slot read failed slot=%s "
                            "path=%s: %s", slot_id, path, exc,
                        )
                self._stop.wait(self.poll_interval)
        except OSError as exc:
            _server_breadcrumb(
                log, logging.ERROR, f"schedule-listener:{self.host}:{self.port}",
                "[SERVER DIAG] Schedule broadcaster stopped host=%s port=%s: %s",
                self.host, self.port, exc, interval=0.0,
            )
        finally:
            if server is not None:
                server.close()


class ScheduleClient:
    def __init__(self, host, *, port=47532, slot_id=None, on_schedule=None,
                 on_status=None, timeout=4.0, retry_interval=3.0):
        self.host, self.port = host, int(port)
        # One physical server may advertise several named slots.  The picker
        # identifies the selected slot, so do not let another slot's schedule
        # overwrite the connection's live overlay.
        self.slot_id = str(slot_id) if slot_id is not None else None
        self.on_schedule, self.on_status = on_schedule, on_status
        self.timeout, self.retry_interval = float(timeout), float(retry_interval)
        self._stop = threading.Event(); self._thread = None; self.sock = None

    def _status(self, value):
        if self.on_status:
            try: self.on_status(value)
            except Exception as exc:
                _server_breadcrumb(
                    log, logging.WARNING, f"schedule-status-callback:{self.host}:{self.port}",
                    "[SERVER DIAG] Schedule status callback failed host=%s "
                    "port=%s: %s", self.host, self.port, exc,
                )

    def start(self):
        self._stop.clear(); self._thread = threading.Thread(target=self._run, daemon=True, name="retro-tv-schedule-client"); self._thread.start()

    def stop(self):
        self._stop.set()
        # Snapshot the socket before shutdown, since the worker's finally
        # block can clear self.sock between this check and the close call.
        sock = self.sock
        if sock is not None:
            try: sock.shutdown(socket.SHUT_RDWR)
            except (OSError, ValueError): pass
            try: sock.close()
            except (OSError, ValueError): pass
        if self._thread: self._thread.join(timeout=2.0)

    def _run(self):
        connection_state = None
        while not self._stop.is_set():
            try:
                self._status("connecting")
                self.sock = socket.create_connection((self.host, self.port), self.timeout)
                self._status("connected")
                if connection_state != "connected":
                    log.info("[SERVER DIAG] Schedule connection established "
                             "host=%s port=%s slot=%s",
                             self.host, self.port, self.slot_id)
                connection_state = "connected"
                while not self._stop.is_set():
                    message = recv_message(self.sock)
                    if (message.get("type") == "schedule"
                            and (self.slot_id is None
                                 or str(message.get("slot_id")) == self.slot_id)
                            and self.on_schedule):
                        self.on_schedule(message)
            except (OSError, ConnectionError, ValueError, json.JSONDecodeError) as exc:
                if not self._stop.is_set():
                    self._status("offline")
                    _server_breadcrumb(
                        log, logging.WARNING, f"schedule-client-offline:{self.host}:{self.port}",
                        "[SERVER DIAG] Schedule server offline host=%s port=%s "
                        "slot=%s: %s", self.host, self.port, self.slot_id, exc,
                    )
                    connection_state = "offline"
            finally:
                if self.sock:
                    try: self.sock.close()
                    except OSError: pass
                self.sock = None
            if not self._stop.is_set():
                self._stop.wait(self.retry_interval)
        self._status("stopped")
        log.info("[SERVER DIAG] Schedule connection stopped host=%s port=%s slot=%s",
                 self.host, self.port, self.slot_id)


__all__ = ["ScheduleBroadcaster", "ScheduleClient", "build_schedule_message", "build_file_listing_message", "sanitize_channels", "sanitize_playback", "recv_message"]
