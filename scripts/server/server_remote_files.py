"""User-side handling of a server's file facts.

Owns: turning the server's file entries into \\\\<server>\\<share>\\<path> locations, rewriting a received
schedule to use them, and saving the server's library facts for those files in this PC's own library so
this PC never scans them over the network.
Does not own: the wire protocol (server_sync.py) or reading the server's library (server_files.py).

WARNING: a media file is never copied or downloaded here. The player opens the file straight from the
server's shared folder and streams it.
"""
from __future__ import annotations

import logging
import ntpath
import math
import os
import threading
import time

from server_files import SHARE_PASSWORD, SHARE_USER

log = logging.getLogger(__name__)

# ============================================================================
# SECTION: Settings
# ============================================================================
try:
    import performance_governor as _gov
except Exception as _governor_error:
    _gov = None
    log.warning("[FAILURE] server_remote_files: performance_governor did not import, so the built-in numbers are used: %s", _governor_error)


def _log_swallowed(where, error):
    """Writes the shared, counted [FAILURE] line through performance_governor (a plain warning when it did not import)."""
    if _gov is not None:
        return _gov.log_swallowed(where, error)
    log.warning("[FAILURE] %s: %s: %s", where, type(error).__name__, error)
    return None


# WARNING: the fallback numbers below must match their SERVER_ names in performance_governor.py; they are used only when that module fails to import.
SIGN_IN_RETRY_EVERY_SECONDS = _gov.SERVER_SIGN_IN_RETRY_EVERY_SECONDS if _gov is not None else 5.0
SIGN_IN_RETRY_FOR_SECONDS = _gov.SERVER_SIGN_IN_RETRY_FOR_SECONDS if _gov is not None else 180.0
SIGN_IN_ADDRESS_FALLBACK_AFTER_ROUNDS = _gov.SERVER_SIGN_IN_ADDRESS_FALLBACK_AFTER_ROUNDS if _gov is not None else 2


# ============================================================================
# SECTION: Paths on the server's shares
# ============================================================================
_BAD_NAME_CHARS = set('\\/:*?"<>|')


def _norm(path) -> str:
    return os.path.normpath(os.path.abspath(str(path))).lower()


def _win(path) -> str:
    return ntpath.normcase(ntpath.normpath(str(path)))


def unc_path(host, share, rel):
    """\\\\host\\share\\rel for one entry, or None when any part could point somewhere else (parent folders, drive letters, other hosts)."""
    host = str(host or "")
    share = str(share or "")
    if not host or any(c in host for c in '\\/"<>|*? ') or not share or any(c in share for c in _BAD_NAME_CHARS):
        return None
    parts = [p for p in str(rel or "").replace("/", "\\").split("\\") if p]
    if not parts or any(p in (".", "..") or ":" in p or any(c in p for c in '*?"<>|') for p in parts):
        return None
    return "\\\\" + host + "\\" + share + "\\" + "\\".join(parts)


_sign_in_events = {}
_sign_in_lock = threading.Lock()


# ============================================================================
# SECTION: Signing in to the server's shares
# ============================================================================


def sign_in_failed(host):
    """True when the last sign-in to this server's shares was not accepted."""
    return str(host or "").lower() in _last_sign_in_said


def begin_sign_in(host):
    """Marks this server's sign-in as not done yet; wait_for_sign_in then holds callers until it is."""
    with _sign_in_lock:
        event = threading.Event()
        _sign_in_events[str(host or "").lower()] = event
        return event


def wait_for_sign_in(host, timeout=12.0):
    """Waits (at most `timeout` seconds) for this server's sign-in to finish; True when it did or none was started."""
    with _sign_in_lock:
        event = _sign_in_events.get(str(host or "").lower())
    return True if event is None else event.wait(max(0.0, float(timeout)))


_share_aliases = {}
_last_sign_in_said = {}


def share_host(host):
    """The name this PC opens the server's shares under: the server's name, or its network address when Windows holds a connection to the name under another sign-in (error 1219 that dropping did not clear)."""
    return _share_aliases.get(str(host or "").lower()) or str(host or "")


def share_hosts(host):
    """Every name the server's shares may be opened under on this PC (the server's name first)."""
    names = [str(host or "")]
    alias = _share_aliases.get(str(host or "").lower())
    if alias and alias not in names:
        names.append(alias)
    return tuple(names)


def _address_of(host):
    """The server's IPv4 address as text, or None when its name does not resolve."""
    import socket
    try:
        text = str(socket.gethostbyname(str(host)))
    except OSError as caught_error:
        _log_swallowed("server_remote_files: looking up the server's network address", caught_error)
        return None
    return text if text and not text.startswith("127.") else None


def _default_run(cmd):
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _drop_connections_to(host, run):
    """Disconnects every Windows connection this PC holds to the host's shares (Windows refuses a second sign-in under another name while one exists, error 1219)."""
    listed = run(["net", "use"])
    text = (getattr(listed, "stdout", "") or "")
    prefix = "\\\\" + str(host).lower() + "\\"
    dropped = 0
    for line in text.splitlines():
        for word in line.split():
            if word.lower().startswith(prefix):
                run(["net", "use", word, "/delete", "/y"])
                dropped += 1
                break
    # WARNING: owner 2026-11-13: Windows opens a hidden connection (one `net use` does not list) the moment any \\host\share path is touched before the sign-in; the CRT log showed "dropped 0" while 1219 kept coming. Those are cancelled by name, forced: the IPC$ share, the host itself and every share this app has touched.
    names = ["\\\\" + str(host) + "\\IPC$", "\\\\" + str(host)]
    names += _touched_share_roots(host)
    for name in names:
        if _force_cancel_connection(name):
            dropped += 1
        else:
            try:
                run(["net", "use", name, "/delete", "/y"])
            except Exception as caught_error:
                _log_swallowed("server_remote_files: dropping an old share connection", caught_error)
    return dropped


def _touched_share_roots(host):
    """The \\\\host\\share roots media.py has probed, so each hidden connection can be cancelled by name."""
    found = []
    try:
        import media
        wanted = "\\\\" + str(host).lower() + "\\"
        for root in list(getattr(media, "_watched_drive_roots", ())):
            text = str(root).rstrip("\\/")
            if text.lower().startswith(wanted):
                parts = text.split("\\")
                if len(parts) >= 4 and text not in found:
                    found.append("\\\\" + parts[2] + "\\" + parts[3])
    except Exception as caught_error:
        _log_swallowed("server_remote_files: listing this PC's share connections", caught_error)
        return []
    return found


def _force_cancel_connection(name):
    """Windows WNetCancelConnection2 with force; True when Windows says it was cancelled."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return ctypes.windll.mpr.WNetCancelConnection2W(str(name), 0, True) == 0
    except Exception as caught_error:
        _log_swallowed("server_remote_files: dropping a share connection through Windows", caught_error)
        return False


def sign_in_to_server(host, run=None, attempts=3, pause=2.0):
    """Signs this PC in to the server's shared folders with the read-only account the server's app made (no Windows setup, no administrator rights needed on this PC). Returns True when Windows accepted it; a failure is logged and the share may still open with whatever sign-in Windows already holds."""
    # WARNING: owner 2026-11-13: after a restart in shell mode the first file opens ran before this sign-in finished, Windows kept a connection under another name (error 1219) and every open then failed with 'user name or password is incorrect'; a 1219 now drops the old connections and signs in again, and callers wait on wait_for_sign_in.
    event = None
    with _sign_in_lock:
        event = _sign_in_events.get(str(host or "").lower())
    try:
        if os.name != "nt" and run is None:
            return False
        runner = run or _default_run
        command = ["net", "use", "\\\\" + str(host) + "\\IPC$", SHARE_PASSWORD,
                   "/user:" + SHARE_USER, "/persistent:no"]
        for attempt in range(1, max(1, int(attempts)) + 1):
            try:
                result = runner(command)
            except Exception as exc:
                log.warning("[SERVER SYNC] share sign-in to %s failed: %s: %s", host, type(exc).__name__, exc)
                result = None
            if result is not None and getattr(result, "returncode", 1) == 0:
                _last_sign_in_said.pop(str(host or "").lower(), None)
                return True
            said = ""
            if result is not None:
                said = (getattr(result, "stdout", "") or getattr(result, "stderr", "") or "").strip()
                _last_sign_in_said[str(host or "").lower()] = said
                log.warning("[SERVER SYNC] share sign-in to %s was not accepted (try %d): %s", host, attempt, said[:200])
            if "1219" in said or "multiple connections" in said.lower():
                try:
                    log.info("[SERVER SYNC] dropped %d old connection(s) to %s before signing in again", _drop_connections_to(host, runner), host)
                except Exception as exc:
                    log.warning("[SERVER SYNC] could not drop old connections to %s: %s", host, exc)
            if attempt < attempts and pause:
                time.sleep(pause)
        return False
    finally:
        if event is not None:
            event.set()


def _held_under_another_name(host):
    said = _last_sign_in_said.get(str(host or "").lower(), "")
    return "1219" in said or "multiple connections" in said.lower()


def _sign_in_under_the_address(host, run=None):
    """Windows keeps one sign-in per server name, so a connection it holds to the name under another account (made by an early file touch before this sign-in) blocks the sign-in for good; the same server under its network address has no such connection. True when that sign-in worked and the shares will be opened under the address from now on."""
    # WARNING: owner 2026-11-14: after a restart the CRT sat on 1219 for eight hours, dropping 0 connections every 20 s, and no channel could open a file.
    address = _address_of(host)
    if not address:
        return False
    if not sign_in_to_server(address, run=run, attempts=1, pause=0):
        return False
    _share_aliases[str(host or "").lower()] = address
    log.warning("[SERVER SYNC] Windows holds a connection to %s under another sign-in; the shares are now opened under its address %s", host, address)
    return True


def sign_in_until_it_works(host, on_late_success=None, run=None, every=None, total=None, sleep=None):
    """Signs in to the server's shares and, when the first round fails (no network yet, a wrong name held), keeps trying every few seconds for a few minutes; the callers waiting on wait_for_sign_in are released after the first round, and on_late_success runs once if a later round is the one that works. Returns True when signed in."""
    every = SIGN_IN_RETRY_EVERY_SECONDS if every is None else every
    total = SIGN_IN_RETRY_FOR_SECONDS if total is None else total
    pause = sleep or time.sleep
    if sign_in_to_server(host, run=run):
        return True
    held_rounds = 1 if _held_under_another_name(host) else 0
    waited = 0.0
    while waited < total:
        if held_rounds >= SIGN_IN_ADDRESS_FALLBACK_AFTER_ROUNDS and _sign_in_under_the_address(host, run=run):
            if on_late_success is not None:
                try:
                    on_late_success()
                except Exception as exc:
                    log.warning("[SERVER SYNC] the late sign-in follow-up failed: %s", exc)
            return True
        pause(every)
        waited += every
        if sign_in_to_server(host, run=run, attempts=1, pause=0):
            log.info("[SERVER SYNC] share sign-in to %s worked after %.0f s of retrying", host, waited)
            if on_late_success is not None:
                try:
                    on_late_success()
                except Exception as exc:
                    log.warning("[SERVER SYNC] the late sign-in follow-up failed: %s", exc)
            return True
        held_rounds = held_rounds + 1 if _held_under_another_name(host) else 0
    log.warning("[SERVER SYNC] share sign-in to %s did not work in %.0f s; the shares may be unreachable", host, total)
    return False


# ============================================================================
# SECTION: Files the server measures
# ============================================================================

# WARNING: the library columns that describe one loudness scan; they move together, never one at a time.
_GAIN_GROUP = ("gain", "gain_target_lufs", "gain_scan_version", "gain_convergence_db", "gain_windows", "track_gains", "gain_checked")


_SERVER_OWNED = set()


def _burned_in_version(reason):
    """The detector version a saved burned-in reason names ('burned_in_v4' -> 4); 0 for anything else."""
    try:
        text = str(reason or "")
        return int(text[len("burned_in_v"):]) if text.startswith("burned_in_v") else 0
    except ValueError as caught_error:
        _log_swallowed("server_remote_files: reading the version from a burned-in reason", caught_error)
        return 0


def server_owns(path):
    """True when this User PC got the file from the connected server, so the server (not this PC) measures its loudness and reads its length."""
    try:
        return bool(path) and _norm(path) in _SERVER_OWNED
    except Exception as caught_error:
        _log_swallowed("server_remote_files: checking whether the server owns a file", caught_error)
        return False


class RemoteFileMap:
    """What this User PC knows about the connected server's files."""

    def __init__(self, host, library_provider):
        self.host = str(host or "")
        self._library_provider = library_provider
        self._unc_by_server_path = {}
        self._server_path_by_unc = {}
        self.unshared = []
        self.imported = 0
        self.mapping_changed = True
        self._had_update = False

    def update(self, entries, changed=None):
        """Takes a server's file entries: remembers each file's location on the server's share and saves the server's facts for it; with changed (a set of server paths) only those files' facts are saved again, the rest are only located. Returns (files located, files not on any share)."""
        mapping, unshared, imported = {}, [], 0
        _originals = {}
        library = None
        _tally = {"files": 0, "no_gain": 0, "no_box": 0, "no_length": 0}
        _mismatches = []
        for entry in entries or []:
            if not isinstance(entry, dict) or not entry.get("p"):
                continue
            unc = unc_path(share_host(self.host), entry.get("s"), entry.get("r")) if entry.get("s") else None
            if unc is None:
                unshared.append(str(entry["p"]))
                continue
            mapping[_win(entry["p"])] = unc
            _originals[_win(entry["p"])] = str(entry["p"])
            _tally["files"] += 1
            _sent_facts = entry.get("f") if isinstance(entry.get("f"), dict) else {}
            _tally["no_gain"] += 0 if _sent_facts.get("gain") else 1
            _tally["no_box"] += 0 if _sent_facts.get("picture_box") else 1
            _tally["no_length"] += 0 if _sent_facts.get("duration") else 1
            if entry.get("key") and isinstance(entry.get("f"), dict) and (changed is None or str(entry["p"]) in changed):
                if library is None:
                    try:
                        library = self._library_provider()
                    except Exception as exc:
                        log.warning("[SERVER SYNC] could not open this PC's library for server file facts: %s", exc)
                        library = False
                if library:
                    try:
                        if self._save_facts(library, unc, entry, _mismatches):
                            imported += 1
                    except Exception as exc:
                        log.warning("[SERVER SYNC] could not save server facts for %s: %s", unc, exc)
        _SERVER_OWNED.clear()
        _SERVER_OWNED.update(_norm(_u) for _u in mapping.values())
        # WARNING: owner 2026-11-13: file facts change every few seconds while the server scans; only a change in where files live may rebuild the schedule, or the lineup flips between shows.
        if _tally["files"] and _tally != getattr(self, "_last_tally", None):
            self._last_tally = dict(_tally)
            log.info("[SERVER SYNC] what the server's library sent for %d file(s): %d without a loudness gain, %d without a picture box, %d without a length (this PC must read those itself)",
                     _tally["files"], _tally["no_gain"], _tally["no_box"], _tally["no_length"])
        if _mismatches:
            _worst = sorted(_mismatches, key=lambda m: -abs(m[1]))[:5]
            _signature = (len(_mismatches), round(_worst[0][1], 1))
            if _signature != getattr(self, "_last_mismatch", None):
                self._last_mismatch = _signature
                log.warning("[GAIN MISMATCH] %d of %d file(s) have a different loudness gain on this PC than in the server's library (more than 1 dB apart; this PC keeps its own). Furthest apart: %s",
                            len(_mismatches), _tally["files"],
                            "; ".join("%s: this PC %+.1f dB over the server's (scan versions here %s, server %s)" % (n, d, h, s_) for n, d, h, s_ in _worst))
        self.mapping_changed = (mapping != self._unc_by_server_path) or not self._had_update
        self._had_update = True
        self._unc_by_server_path = mapping
        self._server_path_by_unc = {_norm(_u): _originals.get(_p, _p) for _p, _u in mapping.items()}
        self.unshared = unshared
        self.imported += imported
        return len(mapping), len(unshared)

    @staticmethod
    def _save_facts(library, unc, entry, mismatches=None):
        """Stores the server's path->key row and fills only the library fields this PC does not already have; this PC's own measurements are never overwritten."""
        key = str(entry["key"])
        size, mtime = entry.get("size"), entry.get("mtime")
        if size is None or mtime is None:
            return False
        library.store_path_key(_norm(unc), int(size), float(mtime), key)
        have = library.get_entry(key) or {}
        fields = {c: v for c, v in entry["f"].items() if have.get(c) is None}
        # WARNING: owner 2026-11-14: a result this PC holds from an older detector or scan is replaced by the server's newer one (as a whole group), or this PC rescans files the server already measured; an owner's own gain (scan version 0) and a result at the same or a newer version here are never replaced.
        sent = entry["f"]
        if (sent.get("picture_box") and have.get("picture_box") is not None
                and (sent.get("picture_box_detector_version") or 0) > (have.get("picture_box_detector_version") or 0)):
            fields["picture_box"] = sent["picture_box"]
            fields["picture_box_detector_version"] = sent["picture_box_detector_version"]
        # WARNING: owner 2026-11-14: the burned-in caption verdict (source and reason) moves as one pair; a newer detector's answer from the server replaces an older one here, so a User PC never decodes a server file's picture to decide it.
        fields.pop("captions_source", None)
        fields.pop("captions_reason", None)
        _sent_bi, _held_bi = _burned_in_version(sent.get("captions_reason")), _burned_in_version(have.get("captions_reason"))
        if sent.get("captions_source") and _sent_bi and _sent_bi > _held_bi:
            fields["captions_source"] = sent["captions_source"]
            fields["captions_reason"] = sent["captions_reason"]
        _held_scan, _sent_scan = have.get("gain_scan_version"), sent.get("gain_scan_version")
        # WARNING: owner 2026-11-15: a gain this PC already holds is kept, so the same file can play at one level here and another on the server; this only counts the files where the two disagree by more than 1 dB so the log shows how many and by how much.
        try:
            if mismatches is not None and sent.get("gain") and have.get("gain") and _held_scan != 0 and _sent_scan != 0:
                _diff_db = 20.0 * math.log10(float(have["gain"]) / float(sent["gain"]))
                if abs(_diff_db) > 1.0:
                    mismatches.append((os.path.basename(str(unc)), _diff_db, _held_scan, _sent_scan))
        except Exception as _swallowed_error:
            log.debug("[SERVER SYNC] gain comparison skipped: %s", _swallowed_error)
        if (sent.get("gain") and have.get("gain") is not None and _held_scan and _sent_scan and _sent_scan > _held_scan):
            for _column in _GAIN_GROUP:
                if _column in sent:
                    fields[_column] = sent[_column]
        if fields or not have:
            library.upsert_fields(key, size=int(size), **fields)
        return True

    def rewrite(self, channels):
        """(a copy of the channels with every known server file path replaced by its share location, how many server file paths stayed unchanged)."""
        left = [0]
        table = self._unc_by_server_path

        def walk(value):
            if isinstance(value, str):
                if len(value) > 3 and value[1:3] in (":\\", ":/"):
                    found = table.get(_win(value))
                    if found is not None:
                        return found
                    left[0] += 1
                return value
            if isinstance(value, dict):
                return {_key(k): walk(v) for k, v in value.items()}
            if isinstance(value, list):
                return [walk(v) for v in value]
            return value

        def _key(name):
            # WARNING: the server's saved block lineup holds lengths keyed by file path, so a key that is a known server file path moves to its share location too.
            if isinstance(name, str) and len(name) > 3 and name[1:3] in (":\\", ":/"):
                found = table.get(_win(name))
                if found is not None:
                    return found
            return name
        return walk(channels), left[0]

    def server_path_for(self, unc):
        """The file's path on the server (the server's own drive path) for its share location on this PC, or None when the server did not send it."""
        return self._server_path_by_unc.get(_norm(unc))

    def known(self):
        return len(self._unc_by_server_path)


__all__ = ["RemoteFileMap", "server_owns", "share_host", "share_hosts", "unc_path", "sign_in_to_server", "begin_sign_in", "wait_for_sign_in", "sign_in_until_it_works", "sign_in_failed"]
