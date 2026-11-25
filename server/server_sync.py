"""Schedule-only server transport for Retro TV networking.

Owns: the length-prefixed JSON protocol, ScheduleBroadcaster (server) and
ScheduleClient (user).
Does not own: discovery (server_discovery.py) or slot file writing.

WARNING: the protocol sends channel/schedule JSON and, in "files" messages,
the library facts of the scheduled files (length, loudness, letterbox box and
where each file sits on one of the server's shared folders), with a channel
that has its watermark on, that one small image file, and in "captions"
messages the saved caption text of files (owner 2026-11-24: captions are made
once and shared, a User sends back what it captioned live). Media files,
ROMs, cores, BIOS files, and general application settings must never cross
this boundary; a User PC plays a file straight from the server's shared
folder over Windows file sharing.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import select
import socket
import struct
import threading
import uuid
import time
from typing import Callable, Mapping

import station_clock
from server_diagnostics import breadcrumb as _server_breadcrumb

log = logging.getLogger(__name__)

# ============================================================================
# SECTION: Protocol constants & payload shaping
# ============================================================================
# WARNING: owner 2026-11-19: v2 has no login and no pairing (any computer on the home network that runs the program may watch); a computer of another version gets "outdated" and the other side never reads its messages.
PROTOCOL = "retro-tv-schedule-v2"
MAX_FRAME = 16 * 1024 * 1024
# VALUES CHANGE HERE: a computer has this many seconds after the server's hello to say which profile it watches.
JOIN_ANSWER_S = 5.0
# VALUES CHANGE HERE (mirrored in performance_governor.py): a server with nothing new to say still sends a small "ping" this often, and a User waits this long for any message before it calls the server lost.
SERVER_KEEPALIVE_S = 5.0
CLIENT_READ_TIMEOUT_S = 15.0
# VALUES CHANGE HERE (mirrored in performance_governor.py): the server looks at its media library for new file facts this often (seconds), and sends them to a User this many files per message.
SERVER_FILES_REFRESH_S = 30.0
SERVER_FILES_BATCH = 300
# VALUES CHANGE HERE (mirrored in performance_governor.py): the server sends every User the clock of each channel this often (seconds); a User that finds its file list differs from the server's asks for it again at most this many times in a minute, and rescans what is still missing by itself after that.
SERVER_CLOCKS_REFRESH_S = 5.0
CLIENT_RESYNC_MAX_PER_MIN = 3
# VALUES CHANGE HERE (mirrored in performance_governor.py): a User tells the server which channel it is watching this often (seconds), and the server counts a report as current for this long.
WATCHING_REPORT_S = 5.0
WATCHING_FRESH_S = 30.0
# VALUES CHANGE HERE (mirrored in performance_governor.py): the most files of the watched channel a User names with each watching report.
WATCHING_NEXT_FILES = 12
# VALUES CHANGE HERE (mirrored in performance_governor.py): a User asks the server to read the airing part of its channels' files into the server's own memory this often (seconds); the server reads at most this many bytes of one file per ask, and at most this many files per ask, and ignores asks from one User closer together than WARM_MIN_GAP_S.
WARM_REPORT_S = 30.0
WARM_RANGE_MAX_BYTES = 48 * 1024 * 1024
WARM_MAX_FILES = 60
WARM_MIN_GAP_S = 10.0
WARM_REMEMBER_S = 120.0
# VALUES CHANGE HERE (mirrored in performance_governor.py): the biggest watermark image file, in bytes, the server sends with a channel.
WATERMARK_SERVER_MAX_BYTES = 131072
# VALUES CHANGE HERE (mirrored in performance_governor.py): the server looks for newly saved captions this often (seconds) and sends them to every User as one batch; one message carries at most this many files' captions, and the server sends at most one caption message per pass of its loop so pings and clocks are never held up.
SERVER_CAPTIONS_PUSH_S = 300.0
SERVER_CAPTIONS_PER_MESSAGE = 20
try:
    import performance_governor as _gov
    SERVER_KEEPALIVE_S = float(_gov.SERVER_KEEPALIVE_S)
    CLIENT_READ_TIMEOUT_S = float(_gov.CLIENT_READ_TIMEOUT_S)
    SERVER_FILES_REFRESH_S = float(_gov.SERVER_FILES_REFRESH_S)
    SERVER_FILES_BATCH = int(_gov.SERVER_FILES_BATCH)
    SERVER_CLOCKS_REFRESH_S = float(_gov.SERVER_CLOCKS_REFRESH_S)
    CLIENT_RESYNC_MAX_PER_MIN = int(_gov.CLIENT_RESYNC_MAX_PER_MIN)
    WATCHING_REPORT_S = float(_gov.WATCHING_REPORT_S)
    WATCHING_FRESH_S = float(_gov.WATCHING_FRESH_S)
    WARM_REPORT_S = float(_gov.SERVER_WARM_REPORT_S)
    WARM_RANGE_MAX_BYTES = int(_gov.SERVER_WARM_RANGE_MAX_MB) * 1024 * 1024
    WARM_MAX_FILES = int(_gov.SERVER_WARM_MAX_FILES)
    WARM_MIN_GAP_S = float(_gov.SERVER_WARM_MIN_GAP_S)
    WATERMARK_SERVER_MAX_BYTES = int(_gov.WATERMARK_SERVER_MAX_BYTES)
    SERVER_CAPTIONS_PUSH_S = float(_gov.SERVER_CAPTIONS_PUSH_S)
    SERVER_CAPTIONS_PER_MESSAGE = int(_gov.SERVER_CAPTIONS_PER_MESSAGE)
except Exception:
    pass


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


# WARNING: besides the schedules, a channel's on/off switch and the few settings that decide how its schedule is read go to a User PC. Without "active" a User PC keeps its own switch, which is off for every channel but 05, so only channel 05 ever played; without "is_visualizer" a music channel is not played as one. Nothing else about a channel (names, effects, commercials, video settings) and nothing about games is sent.
SCHEDULE_FIELDS = ("schedules", "holiday_schedules")
# WARNING: owner 2026-11-12: the logs of two PCs on one server showed the same file on air at different points (the server PC's channel 05 cycle held 91794 s with commercials in it, the User PC rebuilt 77083 s with none), because whether commercials, intros and outros are inserted, and where, decides the length of every block and was not sent; they are sent now, with the marathon start, so every PC builds the same block.
STREAM_SETTING_FIELDS = ("active", "is_visualizer", "visualizer_style", "block_mode", "scheduling_mode",
                         "episode_order_mode", "pair_threshold_minutes", "commercials_enabled", "commercial_placement",
                         "intros_enabled", "outros_enabled", "marathon_anchor_epoch", "watermark_enabled")


# WARNING: owner 2026-11-10: every PC on a server must air the same thing at the same moment, so the server also sends where each channel's rotation stands (its start number, show positions and order, block starts, saved block lineup); without them each PC drew its own start number and built its own lineup. Keys that begin with "_" are this PC's own working flags and never leave it.
STREAM_STATE_FIELDS = ("playback_log", "holiday_playback_log")


def _shareable_state(value):
    """A copy of a channel's rotation state without the working flags (keys that begin with an underscore)."""
    if isinstance(value, Mapping):
        return {key: _shareable_state(item) for key, item in value.items()
                if not (isinstance(key, str) and key.startswith("_"))}
    if isinstance(value, list):
        return [_shareable_state(item) for item in value]
    return copy.deepcopy(value)


# WARNING: set by the program that runs the broadcaster (the app or the server service) to its WATERMARK folder; empty means only a channel's own remembered pick can be sent.
WATERMARK_ROOT = ""
WATERMARK_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif")


def watermark_file_for(channel_key: str, picked: str, root: str):
    """Returns (path, kind) for the watermark a channel shows, kind being "picked", "folder", "default" or "" (nothing): the image picked with the Add button (while its file exists; the older default path stored by default is not a pick); else the first image, by name, in the channel's own folder (WATERMARK/Channel NN); else the one default, root/default/watermark.png (the older root/watermark.png is used when it is the only one)."""
    # WARNING: owner 2026-11-17: the one rule for every channel on this PC and on the server; a pick made in the app always wins over files put into the folder later.
    root = str(root or "")
    default = os.path.join(root, "default", "watermark.png") if root else ""
    legacy = os.path.join(root, "watermark.png") if root else ""
    picked = str(picked or "")
    if picked and os.path.isfile(picked):
        # WARNING: the older default path was stored in every channel's record by default, so it is not a pick; the new default path is stored only when the Add button picked it.
        if not legacy or os.path.normcase(os.path.abspath(picked)) != os.path.normcase(os.path.abspath(legacy)):
            return picked, ("default" if default and os.path.normcase(os.path.abspath(picked)) == os.path.normcase(os.path.abspath(default)) else "picked")
    if root:
        try:
            wanted = ("channel " + str(channel_key).zfill(2)).lower()
            for entry in sorted(os.listdir(root), key=str.lower):
                folder = os.path.join(root, entry)
                if entry.lower() == wanted and os.path.isdir(folder):
                    images = sorted((n for n in os.listdir(folder) if os.path.splitext(n)[1].lower() in WATERMARK_IMAGE_EXTS
                                     and os.path.isfile(os.path.join(folder, n))), key=str.lower)
                    if images:
                        return os.path.join(folder, images[0]), "folder"
        except OSError as exc:
            log.warning("[WATERMARK] could not list %s: %s", root, exc)
        if os.path.isfile(default):
            return default, "default"
        if os.path.isfile(legacy):
            return legacy, "default"
    return "", ""


_watermark_images = {}


def _watermark_image(channel_key: str, channel: Mapping):
    """The small watermark image of a channel whose watermark is on, as {"name", "sha", "b64"}, or None; read from the server's own disk once per file change, and left out (with a log line) when it is missing or bigger than WATERMARK_SERVER_MAX_BYTES."""
    if not channel.get("watermark_enabled", False):
        return None
    path, kind = watermark_file_for(channel_key, channel.get("watermark_path", ""), WATERMARK_ROOT)
    if not path:
        _wm_missing = ("none", channel_key)
        if _watermark_images.get(_wm_missing) is None:
            _watermark_images[_wm_missing] = True
            log.warning("[WATERMARK] channel %s: the watermark is on but this computer has no picked image, no image in the channel's folder and no default; the channel goes out with none", channel_key)
        return None
    try:
        stat = os.stat(path)
        stamp = (stat.st_size, stat.st_mtime_ns)
        held = _watermark_images.get(path)
        if held is not None and held[0] == stamp:
            return held[1]
        if stat.st_size > WATERMARK_SERVER_MAX_BYTES:
            log.warning("[WATERMARK] channel %s: %s is %d bytes, more than the %d a server sends; the channel goes out with no watermark image",
                        channel_key, os.path.basename(path), stat.st_size, WATERMARK_SERVER_MAX_BYTES)
            image = None
        else:
            with open(path, "rb") as fh:
                data = fh.read()
            # WARNING: owner 2026-11-17: the project's default watermark is WATERMARK/default/watermark.png; an image picked or put in the channel's own folder is the channel creator's, and "is_default" tells a User PC which of the two it is getting.
            _is_default = kind == "default"
            image = {"name": os.path.basename(path), "sha": hashlib.sha1(data).hexdigest()[:16],
                     "b64": base64.b64encode(data).decode("ascii"), "is_default": _is_default}
            log.info("[WATERMARK] channel %s: sending %s (%d bytes, %s, %s) with the channel", channel_key, image["name"], len(data), image["sha"],
                     "the default watermark" if _is_default else "picked for this channel")
        _watermark_images[path] = (stamp, image)
        return image
    except OSError as exc:
        if _watermark_images.get(path, (None, 1))[0] != "missing":
            log.warning("[WATERMARK] channel %s: the watermark image %s cannot be read on the server: %s", channel_key, path, exc)
        _watermark_images[path] = ("missing", None)
        return None


def sanitize_channels(channels: Mapping, remote_id: str = "") -> dict:
    """Return the channel schedule data, the few settings that decide how it plays and where its rotation stands; never anything else about a channel, and never games."""
    safe = {}
    for key, channel in dict(channels or {}).items():
        if not isinstance(channel, Mapping):
            continue
        safe[str(key)] = {
            field: copy.deepcopy(channel[field])
            for field in SCHEDULE_FIELDS + STREAM_SETTING_FIELDS
            if field in channel
        }
        for field in STREAM_STATE_FIELDS:
            if isinstance(channel.get(field), Mapping):
                safe[str(key)][field] = _shareable_state(channel[field])
        _image = _watermark_image(str(key), channel)
        if _image is not None:
            safe[str(key)]["watermark_image"] = _image
    if remote_id:
        safe = _rewrite_remote(safe, remote_id)
    return safe


def sanitize_playback(playback) -> dict:
    """Do not transmit video/playback settings through the schedule server."""
    return {}


def local_utc_offset_s() -> int:
    """This computer's zone, in seconds east of UTC, right now (daylight time included)."""
    return int(time.localtime().tm_gmtoff)


SERVER_ID_FILE = "server_id.txt"


def server_id_for_slot(slot_path: str) -> str:
    """The unique ID of the server whose channel file is slot_path, kept beside it; made once, and gone with the folder when the server is removed, so a server made again under the same name has a new one."""
    folder = os.path.dirname(os.path.abspath(slot_path))
    id_path = os.path.join(folder, SERVER_ID_FILE)
    try:
        with open(id_path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        if value:
            return value
    except OSError:
        pass
    value = uuid.uuid4().hex
    try:
        with open(id_path, "w", encoding="utf-8") as fh:
            fh.write(value)
    except OSError as exc:
        log.warning("[SERVER DIAG] could not write the server ID %s: %s", id_path, exc)
    return value


def build_schedule_message(slot_id: str, channels: Mapping, revision: int,
                           remote_id: str = "", playback=None, server_id: str = "") -> dict:
    return {
        "server_id": str(server_id or ""),
        "clock": {"utc_offset_s": local_utc_offset_s()},
        "playback": sanitize_playback(playback),
        "protocol": PROTOCOL,
        "type": "schedule",
        "slot_id": str(slot_id),
        "revision": int(revision),
        "channels": sanitize_channels(channels, remote_id),
    }


def files_digest(entries) -> str:
    """The fingerprint of a list of file entries; the server and every User compute it the same way, so a User can tell its list is the server's."""
    return hashlib.sha1(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _entry_order(entry) -> str:
    return str(entry.get("p", "")).lower()


# ============================================================================
# SECTION: Wire framing
# ============================================================================


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


# ============================================================================
# SECTION: Broadcaster (server side)
# ============================================================================


def _clock_shape(clocks):
    """The channel clocks without the seconds before each show starts or ends, which change every moment: what a User needs sent again only when it changes."""
    out = {}
    for channel, clock in clocks.items():
        shows = [clock.get("now") or {}] + list(clock.get("next") or [])
        out[channel] = [{k: v for k, v in show.items() if k not in ("starts_in", "remaining")} for show in shows]
    return out


class ScheduleBroadcaster:
    """Polls slot files and pushes changed schedules to subscribed clients."""
    def __init__(self, slots: Callable[[], Mapping[str, str]], *, host="0.0.0.0",
                 port=47532, poll_interval=0.5, remote_id="", playback_provider=None,
                 files_provider=None, warm_allowed=None, clock_provider=None, captions=None, on_slot_changed=None, rows_provider=None):
        self.slots = slots
        # rows_provider(slot_id, channels) -> (digest, {channel: [row]}): the profile's schedule as this server's HUB holds it, sent to the Users watching the slot (owner 2026-11-24: "everything goes through the HUB").
        self.rows_provider = rows_provider
        self._rows_digest_sent = {}
        self._pending_rows_clients = set()
        # on_slot_changed(slot_id): told when a profile's file changed on disk (the service plans that profile's schedule again).
        self.on_slot_changed = on_slot_changed
        # captions: the saved_captions module of this server (held, read, add); None shares no captions.
        self.captions = captions
        self._captions_sent = None
        self._captions_pushed_at = -1e9
        self._captions_queue = []
        self._live_caption_keys = {}
        # The profile each connected computer said it watches (None: every profile); a message about one profile goes only to the computers watching it.
        self._slot_of = {}
        # warm_allowed(path) -> True when the server may read this file for a User's warm ask (it lies in a shared folder); None refuses every ask.
        self.warm_allowed = warm_allowed
        self._warm_pending = {}
        self._warm_event = threading.Event()
        self._warm_last_ask = {}
        self._warmed = {}
        self._warm_thread = None
        # files_provider(channels) -> list of file entries (server_files.py); None sends no file facts.
        self.files_provider = files_provider
        self._files_digest = {}
        self._files_checked = {}
        self._files_entries = {}
        self._pending_files_clients = set()
        # clock_provider(channels, slot_id) -> {channel: {"now": ..., "next": [...]}} (server_clock.ChannelClocks); None sends no clocks.
        self.clock_provider = clock_provider
        self._clocks_sent_at = {}
        self._clocks_shape = {}
        self._pending_clock_clients = set()
        self.playback_provider = playback_provider
        self.host, self.port = host, int(port)
        self.poll_interval = float(poll_interval)
        self.remote_id = remote_id
        self._stop = threading.Event()
        self._thread = None
        self._clients = set()
        # NOTE: a client that connects after the first poll still needs the
        # current snapshot even when the slot has not changed. Kept separate
        # from _last_payloads so late User connections get served without a
        # server-side edit.
        self._pending_snapshot_clients = set()
        self._lock = threading.Lock()
        self._revisions = {}
        self._last_payloads = {}
        self._last_playback_keys = {}
        self._slot_cache = {}
        self._last_ping = 0.0
        # {client socket: (slot id or None, channel, monotonic time reported, [files of that channel the User named, on air first])}
        self._watching = {}

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="retro-tv-schedule-server", daemon=True)
        self._thread.start()
        if self.warm_allowed is not None and not (self._warm_thread and self._warm_thread.is_alive()):
            self._warm_thread = threading.Thread(target=self._warm_loop, name="retro-tv-warm", daemon=True)
            self._warm_thread.start()

    def stop(self):
        self._stop.set()
        self._warm_event.set()
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
            self._slot_of.clear()
            self._pending_snapshot_clients.clear()
            self._pending_files_clients.clear()
            self._pending_clock_clients.clear()
        for client in clients:
            try: client.shutdown(socket.SHUT_RDWR)
            except (OSError, ValueError): pass
            try: client.close()
            except (OSError, ValueError): pass
        if self._thread: self._thread.join(timeout=2.0)
        self._thread = None

    def watching(self, slot_id=None):
        """The channels Users reported watching within the last WATCHING_FRESH_S seconds, as a set of channel strings; limited to one slot when slot_id is given (a report with no slot counts for every slot)."""
        now = time.monotonic()
        found = set()
        with self._lock:
            for reported_slot, channel, at, _next in self._watching.values():
                if now - at > WATCHING_FRESH_S:
                    continue
                if slot_id is None or reported_slot in (None, "", str(slot_id)):
                    found.add(channel)
        return found

    def watching_next(self, slot_id=None):
        """[(channel, [server file paths])] the watching Users named within the last WATCHING_FRESH_S seconds: the show on air, the next shows and the files their schedule waits to have measured; limited to one slot when slot_id is given."""
        now = time.monotonic()
        found = []
        with self._lock:
            for reported_slot, channel, at, files in self._watching.values():
                if files and now - at <= WATCHING_FRESH_S and (slot_id is None or reported_slot in (None, "", str(slot_id))):
                    found.append((channel, list(files)))
        return found

    def _read_watching(self):
        """Takes in the 'watching' messages Users have sent; never blocks and never drops a client for anything but a closed connection."""
        with self._lock:
            clients = list(self._clients)
        if not clients:
            return
        try:
            ready, _w, _x = select.select(clients, [], [], 0)
        except (OSError, ValueError):
            return
        for client in ready:
            try:
                message = recv_message(client)
            except socket.timeout:
                continue
            except (OSError, ConnectionError, ValueError, json.JSONDecodeError):
                with self._lock:
                    self._clients.discard(client)
                    self._slot_of.pop(client, None)
                    self._pending_snapshot_clients.discard(client)
                    self._pending_files_clients.discard(client)
                    self._pending_clock_clients.discard(client)
                    self._pending_rows_clients.discard(client)
                    self._watching.pop(client, None)
                    self._live_caption_keys.pop(client, None)
                try: client.close()
                except OSError: pass
                continue
            if message.get("type") == "captions_have":
                self._take_captions_have(client, message)
                continue
            if message.get("type") == "captions_put":
                self._take_captions_put(message)
                continue
            if message.get("type") == "warm":
                self._take_warm_ask(client, message)
                continue
            if message.get("type") == "resync":
                log.info("[SERVER SYNC] a User asked for the whole file list again (slot %s)", message.get("slot_id"))
                with self._lock:
                    self._pending_files_clients.add(client)
                continue
            if message.get("type") == "watching" and str(message.get("channel", "")).isdigit():
                slot = message.get("slot_id")
                files = message.get("next")
                files = [f for f in files if isinstance(f, str) and f][:WATCHING_NEXT_FILES] if isinstance(files, list) else []
                live = message.get("live_captions")
                with self._lock:
                    self._watching[client] = (None if slot in (None, "") else str(slot),
                                              str(message["channel"]).zfill(2), time.monotonic(), files)
                    self._live_caption_keys[client] = (time.monotonic(), [str(k) for k in live if isinstance(k, str) and k][:WATCHING_NEXT_FILES]
                                                       if isinstance(live, list) else [])

    def live_caption_keys(self):
        """The content keys of the files the watching Users are captioning live right now (reported within WATCHING_FRESH_S seconds); the server's caption scan leaves them to those Users."""
        now = time.monotonic()
        with self._lock:
            return {key for at, keys in self._live_caption_keys.values() if now - at <= WATCHING_FRESH_S for key in keys}

    def _take_captions_have(self, client, message):
        """A User said which saved captions it holds ({content key: seconds covered}) when it connected: every file this server holds more of is queued for that User."""
        if self.captions is None:
            return
        have = message.get("keys")
        have = have if isinstance(have, dict) else {}
        try:
            mine = self.captions.held()
        except Exception as exc:
            log.warning("[SERVER CAPTIONS] the saved captions could not be listed: %s", exc)
            return
        # WARNING: owner 2026-11-24: copies are compared by graded seconds (saved_captions.graded_seconds), so a User whose copy was made in Lite gets the server's better copy of the same stretch.
        wanted = [key for key, info in mine.items() if info[3] > float(have.get(key) or 0) + 1.0]
        with self._lock:
            self._captions_queue.extend((client, key) for key in wanted)
        log.info("[SERVER CAPTIONS] a User holds %d saved caption file(s); %d of this server's %d are sent to it", len(have), len(wanted), len(mine))

    def _take_captions_put(self, message):
        """A User sent what it captioned live: each file is saved where this server holds nothing yet for that stretch or holds it at a lower grade (between copies of the same grade the first wins, a later one changes nothing); the next push passes it on to the other Users."""
        if self.captions is None:
            return
        taken = 0
        for item in (message.get("items") or [])[:SERVER_CAPTIONS_PER_MESSAGE]:
            try:
                if self.captions.add_item(item, "user"):
                    taken += 1
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("[SERVER CAPTIONS] a User's captions could not be read: %s", exc)
        if taken:
            log.info("[SERVER CAPTIONS] kept %d file(s) of captions a User made live", taken)

    def _captions_item(self, key):
        return self.captions.item_for(key)

    def _send_captions(self):
        """Once every SERVER_CAPTIONS_PUSH_S seconds the files whose saved captions grew since the last push are queued for every User; one message of at most SERVER_CAPTIONS_PER_MESSAGE files goes out per call, to one User, so a big batch is spread over the loop's passes."""
        if self.captions is None:
            return
        now = time.monotonic()
        if now - self._captions_pushed_at >= SERVER_CAPTIONS_PUSH_S:
            self._captions_pushed_at = now
            try:
                mine = self.captions.held(refresh=True)
            except Exception as exc:
                log.warning("[SERVER CAPTIONS] the saved captions could not be listed: %s", exc)
                mine = {}
            # WARNING: the first look only notes what is held: a User gets what it lacks when it connects (captions_have), so only what changed after that look is pushed to everyone.
            if self._captions_sent is not None:
                changed = [key for key, info in mine.items() if self._captions_sent.get(key) != info]
                if changed:
                    with self._lock:
                        clients = list(self._clients)
                        self._captions_queue.extend((client, key) for client in clients for key in changed)
                    log.info("[SERVER CAPTIONS] %d file(s) of new saved captions are sent to %d User(s)", len(changed), len(clients))
            self._captions_sent = dict(mine)
        with self._lock:
            if not self._captions_queue:
                return
            client = self._captions_queue[0][0]
            keys = [key for c, key in self._captions_queue if c is client][:SERVER_CAPTIONS_PER_MESSAGE]
            taken = set(keys)
            self._captions_queue = [(c, key) for c, key in self._captions_queue if not (c is client and key in taken)]
            alive = client in self._clients
        if not alive:
            return
        items = [item for item in (self._captions_item(key) for key in keys) if item]
        if items:
            self._broadcast({"protocol": PROTOCOL, "type": "captions", "items": items}, [client])

    def _take_warm_ask(self, client, message):
        """Takes in a User's ask to read the airing part of some channel files into this server's memory: [[path, start byte, length], ...]. Asks closer together than WARM_MIN_GAP_S from one User are ignored; the newest range for a file wins; nothing is read here (the warm thread does it)."""
        now = time.monotonic()
        key = id(client)
        if now - self._warm_last_ask.get(key, -1e9) < WARM_MIN_GAP_S:
            return
        ranges = message.get("ranges")
        if not isinstance(ranges, list):
            return
        self._warm_last_ask[key] = now
        if len(self._warm_last_ask) > 200:
            self._warm_last_ask.clear()
        with self._lock:
            for item in ranges[:WARM_MAX_FILES]:
                try:
                    path, start, length = str(item[0]), int(item[1]), int(item[2])
                except (TypeError, ValueError, IndexError):
                    continue
                if start < 0 or length <= 0 or not path:
                    continue
                self._warm_pending[path] = (start, min(length, WARM_RANGE_MAX_BYTES))
        self._warm_event.set()

    def _warm_loop(self):
        """Reads the asked ranges of shared files so this server's own memory holds them when a User tunes there; reads only inside shared folders and only what is not already held."""
        while not self._stop.is_set():
            self._warm_event.wait(2.0)
            self._warm_event.clear()
            with self._lock:
                pending, self._warm_pending = self._warm_pending, {}
            for path, (start, length) in pending.items():
                if self._stop.is_set():
                    break
                try:
                    self._warm_one(path, start, length)
                except Exception as exc:
                    _server_breadcrumb(log, logging.WARNING, "schedule-warm-read",
                                       "[SERVER DIAG] warm read of %s failed: %s", os.path.basename(path), exc)

    def _warm_one(self, path, start, length):
        if self.warm_allowed is None or not self.warm_allowed(path):
            return
        now = time.monotonic()
        held = self._warmed.get(path)
        if held is not None and now - held[2] < WARM_REMEMBER_S and held[0] <= start < held[1]:
            length -= held[1] - start
            start = held[1]
            if length <= 0:
                self._warmed[path] = (held[0], held[1], now)
                return
        done = 0
        with open(path, "rb", buffering=0) as fh:
            fh.seek(start)
            while done < length and not self._stop.is_set():
                chunk = fh.read(min(1024 * 1024, length - done))
                if not chunk:
                    break
                done += len(chunk)
        first = held[0] if (held is not None and now - held[2] < WARM_REMEMBER_S and held[0] <= start <= held[1]) else start
        self._warmed[path] = (first, start + done, now)
        if len(self._warmed) > 300:
            for old in sorted(self._warmed, key=lambda k: self._warmed[k][2])[:100]:
                self._warmed.pop(old, None)

    def _read_slot(self, path):
        """The slot file's channels and whether the file changed since the last read; the file is read again only when its size or time changed."""
        stat = os.stat(path)
        stamp = (stat.st_mtime_ns, stat.st_size)
        held = self._slot_cache.get(path)
        if held is not None and held[0] == stamp:
            return held[1], False
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        channels = data.get("channels", {})
        self._slot_cache[path] = (stamp, channels)
        return channels, True

    def _broadcast(self, message, clients=None):
        dead = []
        if clients is None:
            with self._lock:
                targets = list(self._clients)
        else:
            targets = list(clients)
        slot_of_message = message.get("slot_id")
        sent = []
        for client in targets:
            if slot_of_message not in (None, "") and not self._may_see(client, slot_of_message):
                continue
            try:
                _send_message(client, message)
                sent.append(client)
            except (OSError, ConnectionError, ValueError):
                dead.append(client)
        if dead:
            with self._lock:
                for client in dead:
                    self._clients.discard(client)
                    self._slot_of.pop(client, None)
                    self._pending_snapshot_clients.discard(client)
                    self._pending_files_clients.discard(client)
                    self._pending_clock_clients.discard(client)
                    self._pending_rows_clients.discard(client)
            for client in dead:
                client.close()
            _server_breadcrumb(
                log, logging.WARNING, f"schedule-broadcast-clients:{self.port}",
                "[SERVER DIAG] Schedule broadcast dropped client connection(s) "
                "port=%s count=%d", self.port, len(dead),
            )
        # WARNING: owner 2026-11-19: only the computers the message was really sent to count as served; a computer watching another profile was skipped, and counting it as served dropped it from the waiting lists, so it never got its own profile's first schedule.
        return set(sent)

    def _may_see(self, client, slot_id):
        """True when the computer behind this connection watches this profile (or every profile)."""
        watched = self._slot_of.get(client)
        return watched in (None, "") or str(watched) == str(slot_id)

    def _admit(self, client, address):
        """Runs in its own thread: a new connection gets the server's hello and is let in once it says which profile it watches."""
        host = address[0]
        try:
            _send_message(client, {"protocol": PROTOCOL, "type": "hello", "server": self.remote_id})
            client.settimeout(JOIN_ANSWER_S)
            reply = recv_message(client)
            if reply.get("type") != "join":
                client.close()
                return
            slot = reply.get("slot_id")
            client.settimeout(2.0)
            with self._lock:
                self._slot_of[client] = None if slot in (None, "") else str(slot)
                self._clients.add(client)
                self._pending_snapshot_clients.add(client)
                self._pending_files_clients.add(client)
                self._pending_clock_clients.add(client)
                self._pending_rows_clients.add(client)
            log.info("[SERVER SYNC] a computer connected peer=%s profile=%s", host, slot)
        except (OSError, ConnectionError, ValueError, json.JSONDecodeError):
            try: client.close()
            except OSError: pass

    def _send_files(self, slot_id, channels):
        """Sends a slot's file facts to every User when they changed and to a User that has not had them yet; the facts always go out before the schedule they describe."""
        if self.files_provider is None:
            return
        now = time.monotonic()
        with self._lock:
            waiting = [c for c in self._pending_files_clients if self._may_see(c, slot_id)]
        due = now - self._files_checked.get(slot_id, -1e9) >= SERVER_FILES_REFRESH_S
        if not due and not waiting and slot_id in self._files_entries:
            return
        changed = False
        if due or slot_id not in self._files_entries:
            self._files_checked[slot_id] = now
            try:
                entries = list(self.files_provider(channels) or [])
            except Exception as exc:
                _server_breadcrumb(
                    log, logging.WARNING, f"schedule-files-provider:{self.port}",
                    "[SERVER DIAG] File facts provider failed port=%s: %s", self.port, exc)
                return
            digest = files_digest(entries)
            changed = digest != self._files_digest.get(slot_id)
            previous = self._files_entries.get(slot_id)
            self._files_entries[slot_id] = entries
            self._files_digest[slot_id] = digest
            previous_digest = None
            if changed and previous is not None:
                previous_digest = files_digest(previous)
        entries = self._files_entries.get(slot_id, [])
        digest = self._files_digest.get(slot_id, "")
        if changed and previous_digest is not None:
            # WARNING: a User that already holds the list gets only the entries that changed and the paths that went; a User still waiting for its first list gets all of it.
            held = {e.get("p"): e for e in previous}
            now_paths = {e.get("p") for e in entries}
            changed_entries = [e for e in entries if held.get(e.get("p")) != e]
            removed = [p for p in held if p not in now_paths]
            log.info("[SERVER SYNC] slot %s file facts changed: sending %d changed and %d removed of %d (digest %s)", slot_id, len(changed_entries), len(removed), len(entries), digest[:8])
            self._send_parts(slot_id, "files_delta", changed_entries, digest, extra={"base": previous_digest, "removed": removed}, targets=None, skip=set(waiting))
            if waiting:
                self._send_parts(slot_id, "files", entries, digest, targets=waiting)
            return
        if changed:
            targets = None
        elif waiting:
            targets = waiting
        else:
            return
        log.info("[SERVER SYNC] slot %s: sending the whole file list, %d files (digest %s), to %s", slot_id, len(entries), digest[:8], "every User" if targets is None else "%d waiting User(s)" % len(targets))
        self._send_parts(slot_id, "files", entries, digest, targets=targets)

    def _send_parts(self, slot_id, kind, entries, digest, extra=None, targets=None, skip=()):
        """Sends a list of file entries as numbered parts of SERVER_FILES_BATCH files; a User that took the whole list is no longer waiting for one."""
        if targets is None:
            with self._lock:
                targets = [c for c in self._clients if c not in skip and self._may_see(c, slot_id)]
        if not targets:
            return
        batch = max(1, int(SERVER_FILES_BATCH))
        parts = [entries[i:i + batch] for i in range(0, len(entries), batch)] or [[]]
        delivered = None
        for index, part in enumerate(parts):
            message = {"protocol": PROTOCOL, "type": kind, "slot_id": str(slot_id),
                       "digest": digest, "seq": index, "total": len(parts), "files": part}
            if extra and index == 0:
                message.update(extra)
            delivered = self._broadcast(message, targets)
            if not delivered:
                log.warning("[SERVER SYNC] slot %s: no User took part %d of %d of the %s message", slot_id, index + 1, len(parts), kind)
                break
            targets = list(delivered)
        if delivered and kind == "files":
            with self._lock:
                for client in delivered:
                    self._pending_files_clients.discard(client)

    def _send_clocks(self, slot_id, channels):
        """Looks at the slot's channel clocks (the show on air with the seconds left, and the next shows) every SERVER_CLOCKS_REFRESH_S seconds and sends them to every User watching the slot when a show, a start or what a show still lacks changed; a User that just connected gets them at once. A User counts the seconds down itself from the moment they arrived."""
        if self.clock_provider is None:
            return
        now = time.monotonic()
        if now - self._clocks_sent_at.get(slot_id, -1e9) < SERVER_CLOCKS_REFRESH_S:
            return
        self._clocks_sent_at[slot_id] = now
        try:
            clocks = self.clock_provider(channels, slot_id)
        except Exception as exc:
            _server_breadcrumb(log, logging.WARNING, f"schedule-clock-provider:{self.port}",
                               "[SERVER DIAG] Channel clock provider failed port=%s: %s", self.port, exc)
            return
        # WARNING: owner 2026-11-18 (audit A7): the clocks went to every User every SERVER_CLOCKS_REFRESH_S whether or not anything in them changed; the seconds left are left out of the comparison because they always change.
        shape = json.dumps(_clock_shape(clocks or {}), sort_keys=True)
        with self._lock:
            newcomers = {client for client in self._pending_clock_clients if self._may_see(client, slot_id)}
        if shape == self._clocks_shape.get(slot_id):
            if not newcomers:
                return
            targets = newcomers
        else:
            targets = None
        self._clocks_shape[slot_id] = shape
        _server_breadcrumb(log, logging.INFO, f"schedule-clocks-sent:{self.port}:{slot_id}",
                           "[SERVER SYNC] slot %s clocks sent for %d channel(s)%s", slot_id, len(clocks or {}),
                           "" if clocks else " (the hub holds no show on air for this slot; nothing for a User to count down from)", interval=60.0)
        self._broadcast({"protocol": PROTOCOL, "type": "clocks", "slot_id": str(slot_id),
                         "sent_at": station_clock.now_epoch(), "channels": clocks or {}}, targets)
        with self._lock:
            self._pending_clock_clients -= {client for client in self._pending_clock_clients if self._may_see(client, slot_id)}

    def _send_rows(self, slot_id, channels):
        """Sends the profile's schedule rows (the HUB's, rows_provider) to every User watching the slot when they changed, and to a User that just connected; packed with zlib, since 8 days of two channels are thousands of rows."""
        if self.rows_provider is None:
            return
        try:
            digest, rows = self.rows_provider(slot_id, channels)
        except Exception as exc:
            _server_breadcrumb(log, logging.WARNING, f"schedule-rows-provider:{self.port}",
                               "[SERVER DIAG] Schedule rows provider failed port=%s: %s", self.port, exc)
            return
        with self._lock:
            newcomers = {client for client in self._pending_rows_clients if self._may_see(client, slot_id)}
        if digest == self._rows_digest_sent.get(slot_id):
            if not newcomers:
                return
            targets = newcomers
        else:
            targets = None
        self._rows_digest_sent[slot_id] = digest
        import zlib
        packed = base64.b64encode(zlib.compress(json.dumps(rows, separators=(",", ":")).encode("utf-8"), 6)).decode("ascii")
        self._broadcast({"protocol": PROTOCOL, "type": "hub_rows", "slot_id": str(slot_id), "digest": digest,
                         "sent_at": station_clock.now_epoch(), "z": packed}, targets)
        _server_breadcrumb(log, logging.INFO, f"schedule-rows-sent:{self.port}:{slot_id}",
                           "[SERVER SYNC] slot %s schedule rows sent: %d channel(s), %d row(s), %d KB packed (digest %s)", slot_id, len(rows),
                           sum(len(v) for v in rows.values()), len(packed) // 1024, digest[:8], interval=0.0)
        with self._lock:
            self._pending_rows_clients -= {client for client in self._pending_rows_clients if self._may_see(client, slot_id)}

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
                    threading.Thread(target=self._admit, args=(client, address), daemon=True, name="retro-tv-admit").start()
                except socket.timeout:
                    pass
                self._read_watching()
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
                with self._lock:
                    anyone = bool(self._clients)
                for slot_id, path in slots.items():
                    try:
                        channels, reread = self._read_slot(path)
                        # WARNING: owner 2026-11-18: the slot file is read and compared only when it changed on disk or the playback settings did; it used to be read, parsed and written out again twice a second for every slot, with or without a User.
                        playback_key = json.dumps(playback, sort_keys=True)
                        changed = False
                        if reread or playback_key != self._last_playback_keys.get(slot_id):
                            self._last_playback_keys[slot_id] = playback_key
                            payload = json.dumps({"c": channels, "p": playback},
                                                 sort_keys=True, separators=(",", ":"))
                            changed = payload != self._last_payloads.get(slot_id)
                            if changed:
                                self._last_payloads[slot_id] = payload
                                self._revisions[slot_id] = self._revisions.get(slot_id, 0) + 1
                                if self.on_slot_changed is not None:
                                    try:
                                        self.on_slot_changed(slot_id)
                                    except Exception as exc:
                                        log.warning("[SERVER SYNC] telling the planner that slot %s changed failed: %s", slot_id, exc)
                        if not anyone:
                            continue
                        self._send_files(slot_id, channels)
                        self._send_clocks(slot_id, channels)
                        self._send_rows(slot_id, channels)
                        with self._lock:
                            pending_now = bool(self._pending_snapshot_clients)
                        if not changed and not pending_now:
                            continue
                        message = build_schedule_message(
                            slot_id, channels,
                            self._revisions.get(slot_id, 0),
                            self.remote_id,
                            playback,
                            server_id_for_slot(path),
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
                # WARNING: a User reads with a time limit; without a message every few seconds an unchanged schedule looked like a lost server and the link dropped and reconnected over and over.
                if anyone:
                    self._send_captions()
                _now = time.monotonic()
                if _now - self._last_ping >= SERVER_KEEPALIVE_S:
                    self._last_ping = _now
                    self._broadcast({"protocol": PROTOCOL, "type": "ping", "utc_offset_s": local_utc_offset_s(), "server_time": station_clock.now_epoch()})
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


# ============================================================================
# SECTION: Client (user side)
# ============================================================================


class ScheduleClient:
    def __init__(self, host, *, port=47532, slot_id=None, on_schedule=None,
                 on_status=None, timeout=4.0, retry_interval=2.0, on_files=None, on_clock=None, on_clocks=None, watching_provider=None, warm_provider=None, on_server_time=None, next_files_provider=None,
                 captions_have_provider=None, on_captions=None, live_captions_provider=None, on_rows=None):
        self.host, self.port = host, int(port)
        # on_rows({"slot_id", "digest", "channels": {channel: [row]}}): the profile's schedule as the server's HUB holds it (owner 2026-11-24).
        self.on_rows = on_rows
        # captions_have_provider() -> {content key: seconds covered} of the saved captions this User holds, told to the server once per connection; on_captions([items]) takes a batch the server sent; live_captions_provider() -> [content keys] this User is captioning live, sent with each watching report.
        self.captions_have_provider = captions_have_provider
        self.on_captions = on_captions
        self.live_captions_provider = live_captions_provider
        # watching_provider() -> the channel this User is watching on the picked server ("07") or None; the server scans that channel's files first.
        self.watching_provider = watching_provider
        # warm_provider() -> [[path on the server, start byte, length], ...] of the airing part of the channels this User may tune to, asked of the server every WARM_REPORT_S seconds; None or an empty list asks nothing.
        self.warm_provider = warm_provider
        # next_files_provider() -> [server file paths] of the watched channel (the show on air first, then the next shows and the files the schedule waits to have measured); sent with each watching report so the server scans them first while its own program is closed.
        self.next_files_provider = next_files_provider
        self.on_files = on_files
        self.on_clock = on_clock
        # on_server_time(server epoch, this computer's epoch when it arrived): the server's own clock, sent with every ping.
        self.on_server_time = on_server_time
        # on_clocks({"slot_id", "sent_at", "received_at", "channels"}) gets the server's channel clocks; received_at is this computer's own monotonic time, so the seconds left can be counted down here.
        self.on_clocks = on_clocks
        self._file_parts = {}
        # The list of file entries this computer holds for the slot (by server path), the digest it had when it was complete, and when it last asked for a fresh copy.
        self._held_files = {}
        self._held_digest = ""
        self._resync_times = []
        self._send_lock = threading.Lock()
        # NOTE: one physical server may advertise several named slots. Only the
        # picked slot's schedule may update this connection's live overlay.
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

    def _take_clock(self, message):
        """Hands the server's zone offset, sent with every schedule and ping, to on_clock, and the server's own time to on_server_time."""
        # WARNING: this one stays on this computer's own clock: it is how far that clock is from the server's station clock (the skew the station clock then adds), so it must never read the station clock itself.
        arrived = time.time()
        if self.on_server_time:
            # WARNING: only the small ping is timed; a schedule message can take seconds to arrive, which would read as the server's clock being behind.
            stamp = message.get("server_time") if message.get("type") == "ping" else None
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                try:
                    self.on_server_time(float(stamp), arrived)
                except Exception as exc:
                    _server_breadcrumb(
                        log, logging.WARNING, f"schedule-server-time-callback:{self.host}:{self.port}",
                        "[SERVER DIAG] Server time callback failed host=%s port=%s: %s", self.host, self.port, exc)
        if not self.on_clock:
            return
        offset = None
        if message.get("type") == "schedule" and isinstance(message.get("clock"), Mapping):
            if self.slot_id is not None and str(message.get("slot_id")) != self.slot_id:
                return
            offset = message["clock"].get("utc_offset_s")
        elif message.get("type") == "ping":
            offset = message.get("utc_offset_s")
        if offset is None:
            return
        try:
            self.on_clock(int(offset))
        except Exception as exc:
            _server_breadcrumb(
                log, logging.WARNING, f"schedule-clock-callback:{self.host}:{self.port}",
                "[SERVER DIAG] Clock callback failed host=%s port=%s: %s", self.host, self.port, exc)

    def _send(self, sock, message):
        """Sends one message; the watching thread and the reading thread share the socket, so one frame is never written in the middle of another."""
        with self._send_lock:
            _send_message(sock, message)

    def _wanted_slot(self, message):
        return self.slot_id is None or str(message.get("slot_id")) == self.slot_id

    def _take_files(self, message):
        """Collects the numbered parts of a full file list ("files") or of the changes to it ("files_delta"), keeps the whole list, checks it against the server's digest and hands it to on_files once it is complete."""
        if not self._wanted_slot(message):
            return
        digest = str(message.get("digest", ""))
        total = int(message.get("total", 1) or 1)
        kind = message.get("type")
        held = self._file_parts.setdefault((kind, digest), {})
        held[int(message.get("seq", 0))] = message
        if len(held) < total:
            return
        parts = [held[seq] for seq in sorted(held)]
        self._file_parts.clear()
        files = [entry for part in parts for entry in (part.get("files") or [])]
        if kind == "files_delta":
            first = parts[0]
            if str(first.get("base", "")) != self._held_digest:
                self._ask_for_fresh_list("the changes were made against a list this computer does not hold")
                return
            changed = {str(e.get("p")): e for e in files if isinstance(e, dict) and e.get("p")}
            for path in first.get("removed") or []:
                self._held_files.pop(str(path), None)
            self._held_files.update(changed)
            changed_paths = set(changed)
        else:
            self._held_files = {str(e.get("p")): e for e in files if isinstance(e, dict) and e.get("p")}
            changed_paths = None
        everything = sorted(self._held_files.values(), key=_entry_order)
        if files_digest(everything) != digest:
            self._held_digest = ""
            self._ask_for_fresh_list("the list this computer holds does not match the server's")
            return
        self._held_digest = digest
        log.info("[SERVER SYNC] slot %s file list in step with the server: %d files (digest %s, %s)", message.get("slot_id"), len(everything), digest[:8], "changes" if kind == "files_delta" else "whole list")
        if self.on_files:
            try:
                self.on_files({"slot_id": message.get("slot_id"), "digest": digest, "files": everything, "changed": changed_paths})
            except Exception as exc:
                _server_breadcrumb(
                    log, logging.WARNING, f"schedule-files-callback:{self.host}:{self.port}",
                    "[SERVER DIAG] File facts callback failed host=%s port=%s: %s",
                    self.host, self.port, exc)

    def _ask_for_fresh_list(self, why):
        """Asks the server to send the whole list again, at most CLIENT_RESYNC_MAX_PER_MIN times a minute; after that this computer reads what is still missing itself, as it always could."""
        now = time.monotonic()
        self._resync_times = [t for t in self._resync_times if now - t < 60.0]
        if len(self._resync_times) >= CLIENT_RESYNC_MAX_PER_MIN:
            log.warning("[SERVER SYNC] %s; the server was already asked %d times this minute, so what is missing is read here", why, len(self._resync_times))
            return
        self._resync_times.append(now)
        sock = self.sock
        if sock is None:
            log.warning("[SERVER SYNC] %s, but there is no connection to ask the server over", why)
            return
        log.info("[SERVER SYNC] asking the server for its whole file list again: %s", why)
        try:
            self._send(sock, {"protocol": PROTOCOL, "type": "resync", "slot_id": self.slot_id})
        except (OSError, ConnectionError, ValueError) as exc:
            log.warning("[SERVER SYNC] the request for the whole file list could not be sent: %s", exc)

    def _tell_captions_held(self, sock):
        """Tells the server, once per connection, which saved captions this User holds, so the server sends only what it lacks; after that the server sends new captions when it has them and this User never asks."""
        if self.captions_have_provider is None:
            return
        try:
            have = dict(self.captions_have_provider() or {})
        except Exception as exc:
            log.warning("[SERVER CAPTIONS] the saved captions of this PC could not be listed: %s", exc)
            have = {}
        try:
            self._send(sock, {"protocol": PROTOCOL, "type": "captions_have", "keys": {str(k): float(v) for k, v in have.items()}})
        except (OSError, ConnectionError, ValueError) as exc:
            log.warning("[SERVER CAPTIONS] could not tell the server which captions this PC holds: %s", exc)

    def _take_captions(self, message):
        """Hands a batch of saved captions the server sent to on_captions."""
        if not self.on_captions:
            return
        try:
            self.on_captions([item for item in (message.get("items") or []) if isinstance(item, dict) and item.get("key")])
        except Exception as exc:
            _server_breadcrumb(
                log, logging.WARNING, f"schedule-captions-callback:{self.host}:{self.port}",
                "[SERVER DIAG] Captions callback failed host=%s port=%s: %s", self.host, self.port, exc)

    def send_captions(self, items):
        """Sends the server what this User captioned live ([{"key", "length", "covered", "grades", "cues"}]); False when there is no connection to send it over."""
        sock = self.sock
        if sock is None or not items:
            return False
        try:
            self._send(sock, {"protocol": PROTOCOL, "type": "captions_put", "items": list(items)[:SERVER_CAPTIONS_PER_MESSAGE]})
        except (OSError, ConnectionError, ValueError) as exc:
            log.warning("[SERVER CAPTIONS] could not send this PC's captions to the server: %s", exc)
            return False
        return True

    def _take_rows(self, message):
        """Unpacks the server's schedule rows and hands them to on_rows."""
        if not self.on_rows or not self._wanted_slot(message):
            return
        try:
            import zlib
            rows = json.loads(zlib.decompress(base64.b64decode(message.get("z") or "")).decode("utf-8"))
            self.on_rows({"slot_id": message.get("slot_id"), "digest": message.get("digest"), "channels": rows if isinstance(rows, dict) else {}})
        except Exception as exc:
            _server_breadcrumb(
                log, logging.WARNING, f"schedule-rows-callback:{self.host}:{self.port}",
                "[SERVER DIAG] Schedule rows from the server could not be taken host=%s port=%s: %s", self.host, self.port, exc)

    def _take_clocks(self, message):
        """Hands the server's channel clocks, with the moment this computer received them, to on_clocks."""
        if not self.on_clocks or not self._wanted_slot(message):
            return
        try:
            self.on_clocks({"slot_id": message.get("slot_id"), "sent_at": message.get("sent_at"),
                            "received_at": time.monotonic(), "channels": message.get("channels") or {}})
        except Exception as exc:
            _server_breadcrumb(
                log, logging.WARNING, f"schedule-clocks-callback:{self.host}:{self.port}",
                "[SERVER DIAG] Channel clocks callback failed host=%s port=%s: %s", self.host, self.port, exc)

    def start(self):
        self._stop.clear(); self._thread = threading.Thread(target=self._run, daemon=True, name="retro-tv-schedule-client"); self._thread.start()

    def stop(self):
        self._stop.set()
        # NOTE: snapshot the socket first -- the worker's finally block can
        # clear self.sock between this check and the close call.
        sock = self.sock
        if sock is not None:
            try: sock.shutdown(socket.SHUT_RDWR)
            except (OSError, ValueError): pass
            try: sock.close()
            except (OSError, ValueError): pass
        if self._thread: self._thread.join(timeout=2.0)

    def _handshake(self, sock):
        """Reads the server's hello and says which profile this computer watches; True once the server let this connection in, False (status "outdated") for a server of another protocol."""
        try:
            hello = recv_message(sock)
        except ValueError as exc:
            if "protocol" not in str(exc):
                raise
            hello = {}
        if hello.get("type") != "hello":
            self._status("outdated")
            return False
        _send_message(sock, {"protocol": PROTOCOL, "type": "join", "slot_id": self.slot_id})
        return True

    def _report_watching(self, sock):
        """Tells the server which of its channels this User is watching, every few seconds while this connection lasts, so the server scans that channel's files first; also asks it, every WARM_REPORT_S seconds, to read the airing part of the channel files into its memory."""
        warm_sent = -1e9
        while not self._stop.is_set() and self.sock is sock:
            channel = None
            if self.watching_provider is not None:
                try:
                    channel = self.watching_provider()
                except Exception as exc:
                    _server_breadcrumb(
                        log, logging.WARNING, f"schedule-watching-provider:{self.host}:{self.port}",
                        "[SERVER DIAG] Watching provider failed host=%s port=%s: %s", self.host, self.port, exc)
            if channel:
                files = []
                if self.next_files_provider is not None:
                    try:
                        files = [str(f) for f in (self.next_files_provider() or []) if f][:WATCHING_NEXT_FILES]
                    except Exception as exc:
                        _server_breadcrumb(
                            log, logging.WARNING, f"schedule-next-files-provider:{self.host}:{self.port}",
                            "[SERVER DIAG] Next files provider failed host=%s port=%s: %s", self.host, self.port, exc)
                live = []
                if self.live_captions_provider is not None:
                    try:
                        live = [str(k) for k in (self.live_captions_provider() or []) if k][:WATCHING_NEXT_FILES]
                    except Exception as exc:
                        _server_breadcrumb(
                            log, logging.WARNING, f"schedule-live-captions-provider:{self.host}:{self.port}",
                            "[SERVER DIAG] Live captions provider failed host=%s port=%s: %s", self.host, self.port, exc)
                try:
                    self._send(sock, {"protocol": PROTOCOL, "type": "watching",
                                      "slot_id": self.slot_id, "channel": str(channel).zfill(2), "next": files, "live_captions": live})
                except (OSError, ConnectionError, ValueError):
                    return
            if self.warm_provider is not None and time.monotonic() - warm_sent >= WARM_REPORT_S:
                warm_sent = time.monotonic()
                try:
                    ranges = self.warm_provider()
                except Exception as exc:
                    ranges = None
                    _server_breadcrumb(
                        log, logging.WARNING, f"schedule-warm-provider:{self.host}:{self.port}",
                        "[SERVER DIAG] Warm provider failed host=%s port=%s: %s", self.host, self.port, exc)
                if ranges:
                    try:
                        self._send(sock, {"protocol": PROTOCOL, "type": "warm", "slot_id": self.slot_id,
                                          "ranges": [list(r) for r in ranges[:WARM_MAX_FILES]]})
                    except (OSError, ConnectionError, ValueError):
                        return
            self._stop.wait(WATCHING_REPORT_S)

    def _run(self):
        connection_state = None
        while not self._stop.is_set():
            try:
                self._status("connecting")
                self.sock = socket.create_connection((self.host, self.port), self.timeout)
                self.sock.settimeout(CLIENT_READ_TIMEOUT_S)
                if not self._handshake(self.sock):
                    self._stop.wait(max(self.retry_interval, 5.0))
                    continue
                self._status("connected")
                self._held_files, self._held_digest, self._file_parts = {}, "", {}
                if connection_state != "connected":
                    log.info("[SERVER DIAG] Schedule connection established "
                             "host=%s port=%s slot=%s",
                             self.host, self.port, self.slot_id)
                connection_state = "connected"
                threading.Thread(target=self._report_watching, args=(self.sock,), daemon=True, name="retro-tv-watching-report").start()
                self._tell_captions_held(self.sock)
                while not self._stop.is_set():
                    message = recv_message(self.sock)
                    self._take_clock(message)
                    if message.get("type") in ("files", "files_delta"):
                        self._take_files(message)
                    elif message.get("type") == "captions":
                        self._take_captions(message)
                    elif message.get("type") == "clocks":
                        self._take_clocks(message)
                    elif message.get("type") == "hub_rows":
                        self._take_rows(message)
                    elif (message.get("type") == "schedule"
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


# ============================================================================
# SECTION: Exports
# ============================================================================
__all__ = ["ScheduleBroadcaster", "ScheduleClient", "build_schedule_message", "sanitize_channels", "sanitize_playback", "recv_message"]
