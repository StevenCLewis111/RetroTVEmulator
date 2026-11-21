"""Background file scan for the server service: while the program is closed, the scheduled files of every server still get their length, picture size and picture box read into the media library, so a User never meets a server with no scan data.

Owns: finding scheduled files with no facts yet and reading them one at a time at low priority.
Does not own: the library (media_library.py), what is scheduled (the slot files) or sending the facts (server_sync.py).

WARNING: owner 2026-11-10: a server set up with media and then closed before the scans finished must keep scanning in the background. The scan goes quickly while the program is closed (the running marker in logs/temp) and slowly while it is open; it picks files with scan_pipeline.py, the same order the program scans in.
"""
from __future__ import annotations

import json
import logging
import ntpath
import os
import threading
import time

import drive_profile
import file_profile
import server_clock
import server_files

log = logging.getLogger(__name__)

AUDIO_SUFFIXES = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".wma", ".aac")

# VALUES CHANGE HERE (mirrored in performance_governor.py): the scan looks for unscanned files this often (seconds), waits this long between two files so the PC stays usable, and gives up on a file after this many failed reads.
SERVER_SCAN_LOOK_S = 20.0
SERVER_SCAN_PAUSE_S = 0.5
SERVER_SCAN_OPEN_PAUSE_S = 1.0
SERVER_SCAN_REORDER_S = 60.0
SERVER_SCAN_BUSY_CPU_PERCENT = 70.0
SERVER_SCAN_BUSY_WAIT_S = 10.0
SERVER_SCAN_BUSY_MAX_WAIT_S = 120.0
SERVER_SCAN_STREAMING_PAUSE_FACTOR = 3.0
SERVER_SCAN_MAX_TRIES = 3
WATCHING_FRESH_S = 30.0
PROGRAM_STATE_FRESH_S = 45.0
# VALUES CHANGE HERE (mirrored in performance_governor.py): the service reads a small piece of one scheduled file on every media drive this often (seconds) so a hard drive never spins down between two streams, and gives up on a read after the second number of seconds.
SERVER_KEEP_AWAKE_INTERVAL_SECONDS = 120.0
SERVER_KEEP_AWAKE_READ_TIMEOUT_SECONDS = 10.0
try:
    import performance_governor as _gov
    SERVER_SCAN_LOOK_S = float(_gov.SERVER_SCAN_LOOK_S)
    SERVER_SCAN_PAUSE_S = float(_gov.SERVER_SCAN_PAUSE_S)
    SERVER_SCAN_OPEN_PAUSE_S = float(_gov.SERVER_SCAN_OPEN_PAUSE_S)
    SERVER_SCAN_REORDER_S = float(_gov.SERVER_SCAN_REORDER_S)
    SERVER_SCAN_BUSY_CPU_PERCENT = float(_gov.SERVER_SCAN_BUSY_CPU_PERCENT)
    SERVER_SCAN_BUSY_WAIT_S = float(_gov.SERVER_SCAN_BUSY_WAIT_S)
    SERVER_SCAN_BUSY_MAX_WAIT_S = float(_gov.SERVER_SCAN_BUSY_MAX_WAIT_S)
    SERVER_SCAN_STREAMING_PAUSE_FACTOR = float(_gov.SERVER_SCAN_STREAMING_PAUSE_FACTOR)
    SERVER_SCAN_MAX_TRIES = int(_gov.SERVER_SCAN_MAX_TRIES)
    WATCHING_FRESH_S = float(_gov.WATCHING_FRESH_S)
    PROGRAM_STATE_FRESH_S = float(_gov.PROGRAM_STATE_FRESH_S)
    SERVER_KEEP_AWAKE_INTERVAL_SECONDS = float(_gov.SERVER_KEEP_AWAKE_INTERVAL_SECONDS)
    SERVER_KEEP_AWAKE_READ_TIMEOUT_SECONDS = float(_gov.SERVER_KEEP_AWAKE_READ_TIMEOUT_SECONDS)
except Exception:
    pass


def _named_key(path):
    """One spelling of a server file path, so a path a User sent matches the slot file's own."""
    return str(path or "").replace("/", "\\").lower()


def scheduled_paths(slot_paths):
    """Every media file path written in the channels of the given slot files, in a fixed order."""
    found = {}
    for path in dict(slot_paths or {}).values():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                channels = json.load(fh).get("channels", {})
        except (OSError, ValueError):
            continue
        for item in server_files.collect_paths(channels):
            found[item] = True
    return sorted(found, key=str.lower)


def drive_candidates(paths, per_drive=3):
    """{drive root: up to per_drive scheduled file paths on it}, in the given order, so a read can fall back to another file when one is gone."""
    by_root = {}
    for path in paths:
        drive, _rest = ntpath.splitdrive(str(path))
        root = (drive + "\\") if drive else "/"
        files = by_root.setdefault(root, [])
        if len(files) < per_drive and path not in files:
            files.append(path)
    return by_root


def read_one_piece(path, deep_round=0):
    """Reads 64 KB of the file, at a different place each round for a big file, so the drive spins up and a read cannot be answered from memory; True when it read."""
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > 131072:
                handle.seek((deep_round * 7919 * 65536) % (size - 65536))
            return bool(handle.read(65536))
    except OSError:
        return False


class DriveKeepAwake:
    """Keeps every drive that holds scheduled media awake while the service runs, program open or closed."""

    # WARNING: owner 2026-11-11: with the program closed nothing else touched the server PC's drives, so a hard drive could spin down and the next stream waited for it; one drive is read at a time, each on its own short-lived thread so a sleeping drive never holds up the rest.
    def __init__(self, slots_fn):
        self.slots_fn = slots_fn
        self._stop = threading.Event()
        self._thread = None
        self._round = 0
        self._readers = drive_profile.DriveReaders("KeepAwake")
        self._busy_logged = []

    def start(self):
        self._thread = threading.Thread(target=self._run, name="DriveKeepAwake", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def round_once(self):
        """One round: reads one real file on each drive; returns {drive root: True, False, or "busy" when its last read has not come back}."""
        try:
            by_root = drive_candidates(scheduled_paths(self.slots_fn()))
        except Exception as exc:
            log.warning("[SERVER KEEP AWAKE] could not list the scheduled files: %s", exc)
            return {}
        self._round += 1
        round_no = self._round

        def _job(files):
            def _read():
                for path in files[round_no % len(files):] + files[:round_no % len(files)]:
                    if read_one_piece(path, round_no):
                        return True
                return False
            return _read

        # WARNING: owner 2026-11-18: one reader thread per drive for the life of the service; a drive whose last read never came back is skipped, so a hung drive cannot pile threads up.
        return self._readers.run_round({root: _job(files) for root, files in by_root.items()}, SERVER_KEEP_AWAKE_READ_TIMEOUT_SECONDS)

    def _run(self):
        while not self._stop.is_set():
            results = self.round_once()
            slow = [root for root, ok in results.items() if ok is False]
            busy = [root for root, ok in results.items() if ok == "busy"]
            if slow:
                log.warning("[SERVER KEEP AWAKE] no read finished on %s; the drive may be asleep, missing or slow", ", ".join(sorted(slow)))
            if busy and busy != self._busy_logged:
                log.warning("[SERVER KEEP AWAKE] %s still busy from the last read; skipped this round", ", ".join(busy))
            self._busy_logged = busy
            self._stop.wait(SERVER_KEEP_AWAKE_INTERVAL_SECONDS)


def measure_loudness(probe, path):
    """Measures a file's loudness gain the way the program does and stores it in the library with the settings it was measured under; True when a gain was stored."""
    # WARNING: owner 2026-11-14: the measured gain is finished by loudness_pipeline (the show's smoothing, then the series' learned offset), the same code the program uses, and a gain stored by the owner is never replaced.
    import loudness_pipeline
    import media
    info = {}
    gain = media.get_media_loudness_gain(path, treat_fallback_as_unknown=True, shortfall_out=info,
                                         raise_on_no_audio=False, raise_on_unplayable=False, raise_on_timeout=False)
    if not gain:
        return False
    norm = os.path.normpath(path)
    gain = loudness_pipeline.finish_measured_gain(norm, float(gain), info)
    music = path.lower().endswith(AUDIO_SUFFIXES)
    held = (probe.get_cached(path) or {}).get("gain_scan_version")
    stored = bool(probe.store_gain(path, float(gain),
                                   target_lufs=media.MUSIC_TARGET_LUFS if music else media.TARGET_LUFS,
                                   scan_version=media.LOUDNESS_GAIN_SCAN_VERSION,
                                   convergence_db=media.LOUDNESS_CONVERGENCE_DB,
                                   checked=bool(info.get("double_checked")),
                                   windows=info.get("windows")))
    offset = loudness_pipeline.take_learned_offset_applied(norm)
    if stored and held != media.OWNER_GAIN_SCAN_VERSION:
        probe.store_file_fields(path, learn_applied_db=float(offset) if offset else None)
    return stored


# ============================================================================
# SECTION: The program's scan order, for the service
# ============================================================================


def _intro_seed(ch_info):
    seed = (ch_info.get("playback_log") or {}).get("intro_seed")
    return seed if isinstance(seed, int) and not isinstance(seed, bool) else 0


def read_slots(slot_paths):
    """[(slot id, channels, settings)] for every readable slot file, in slot order."""
    out = []
    for slot_id, path in dict(slot_paths or {}).items():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            channels = data.get("channels", {})
            config = data.get("config", {})
        except (OSError, ValueError):
            continue
        if isinstance(channels, dict):
            out.append((str(slot_id), channels, config if isinstance(config, dict) else {}))
    return out


def server_files_paths(entry):
    """The file path(s) one schedule entry names, the way the program reads an entry."""
    import scan_pipeline
    return scan_pipeline.paths_from_entry(entry)


def read_anchors(base_dir):
    """{channel: anchor} the program saved for what each channel was airing, or {} when it has not run."""
    try:
        with open(os.path.join(base_dir, "logs", "playback_runtime.json"), "r", encoding="utf-8") as fh:
            anchors = json.load(fh).get("anchors", {})
        return anchors if isinstance(anchors, dict) else {}
    except (OSError, ValueError):
        return {}


LOCAL_WATCHING_FILE = "watching_local.json"
PROGRAM_STATE_FILE = "program_scan_state.json"


def read_program_scan_state(base_dir, now=None):
    """{"mode": "regular" or "profile", "caught_up": bool} the program last reported, or None when there is no report or it is older than PROGRAM_STATE_FRESH_S (a closed or stuck program never holds the tray back or lets it run ahead on an old word)."""
    try:
        with open(os.path.join(base_dir, "logs", "temp", PROGRAM_STATE_FILE), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        mode, caught_up, at = str(data.get("mode") or ""), bool(data.get("caught_up")), float(data.get("at") or 0.0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if mode not in ("regular", "profile") or (time.time() if now is None else now) - at > PROGRAM_STATE_FRESH_S:
        return None
    return {"mode": mode, "caught_up": caught_up}


def read_local_watching(base_dir, fresh_s=None, now=None):
    """(slot id, channel) of the server profile channel the program is showing on this computer right now, or None. The program writes it every few seconds while a profile is viewed, and a report older than WATCHING_FRESH_S counts as nobody watching, so a closed or crashed program never leaves a channel first in line."""
    fresh = float(WATCHING_FRESH_S if fresh_s is None else fresh_s)
    try:
        with open(os.path.join(base_dir, "logs", "temp", LOCAL_WATCHING_FILE), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        slot_id, channel, at = str(data.get("slot_id") or ""), str(data.get("channel") or ""), float(data.get("at") or 0.0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not slot_id or not channel or (time.time() if now is None else now) - at > fresh:
        return None
    return slot_id, channel.zfill(2)


def _holiday_lead_days():
    try:
        import performance_governor as gov
        return int(gov.HOLIDAY_SCAN_LEAD_DAYS)
    except Exception:
        return 7


def make_slot_source(channels, pending_fn, watched=(), anchors=None, hub_now=None, config=None):
    """A scan_pipeline.ScanSource for one server profile's saved channels: the program's own lineup code (media.read_block_lineup) builds each channel's airing lineup and scan_pipeline picks the order, so the service scans in the order the program does.

    pending_fn(path) -> True while the file still lacks a scan. `watched` = channels a User is watching (scanned first); `hub_now(channel)` = the path the shared schedule hub says is airing now (the program's own record), `anchors` = what the program last saved each channel as airing; `config` = the profile's settings (holiday switches).
    """
    import broadcast_clock
    import media
    import scan_pipeline

    anchors = anchors or {}
    watched = {str(ch).zfill(2) for ch in watched}
    lineups = {}
    snapshot = {}

    def _exists(path):
        try:
            return bool(media._cached_path_exists(path))
        except Exception:
            return os.path.exists(path)

    def _seed(ch_info, block_offset):
        return "%d:%d" % (_intro_seed(ch_info), int(block_offset))

    def _now():
        return broadcast_clock.now().replace(tzinfo=None)

    def _ordered(ch, info):
        if ch in lineups:
            return lineups[ch]
        result = ([], None)
        try:
            if not info.get("is_visualizer", False):
                now = _now()
                name = scan_pipeline.airing_block_name(info, now)
                if name == "Marathon":
                    items = info.get("schedules", {}).get("Marathon", [])
                    ordered = media.build_ordered_tracks(items, "marathon", "%d:marathon" % _intro_seed(info) if items else 0)
                    result = (ordered, "Marathon") if ordered else ([], None)
                else:
                    result = media.read_block_lineup(ch, info, name, now, _exists, _seed)
        except Exception as exc:
            log.warning("[SERVER SCAN] could not build the lineup of channel %s: %s", ch, exc)
        lineups[ch] = result
        return result

    def _peek(ch, info, name, now):
        try:
            return media.read_block_lineup(ch, info, name, now, _exists, _seed, peek_only=True)
        except Exception as exc:
            log.warning("[SERVER SCAN] could not build the %s lineup of channel %s: %s", name, ch, exc)
            return [], None

    def _now_file(ch):
        ordered = _ordered(ch, (channels or {}).get(ch, {}))[0]
        airing = [p for entry in ordered for p in scan_pipeline.paths_from_entry(entry)]
        known = {os.path.normpath(p) for p in airing}
        if (channels or {}).get(ch, {}).get("is_visualizer", False):
            # WARNING: owner 2026-11-14: a music channel has no lineup of known lengths, so the song the shared hub or the saved position names is the one playing.
            for candidate in ((hub_now(ch) if hub_now else ""), (anchors.get(ch) or {}).get("file", "")):
                if candidate:
                    return candidate
            return ""
        # WARNING: owner 2026-11-14: what airs now is read from the shared schedule hub first (the program's own record in the media library), then from the program's last saved positions, and only then from the lineup's first show.
        for candidate in ((hub_now(ch) if hub_now else ""), (anchors.get(ch) or {}).get("file", "")):
            if candidate and os.path.normpath(candidate) in known:
                return candidate
        return airing[0] if airing else ""

    def _music_pending(ch, info):
        # WARNING: owner 2026-11-14: a music channel's songs still waiting for a scan, in the order they are listed (the same songs the program walks after its lineup).
        found = []
        for entry in (info.get("schedules", {}).get("Music Tracks", []) if info.get("active", True) else []):
            for path in server_files_paths(entry):
                if os.path.isfile(path) and pending_fn(path):
                    found.append(path)
        return found

    def _front_load(items):
        if not watched:
            return items
        mine = [it for it in items if len(it) > 1 and str(it[1]) in watched]
        return mine + [it for it in items if not (len(it) > 1 and str(it[1]) in watched)] if mine else items

    def _record(ch):
        if not snapshot:
            snapshot.update(scan_pipeline.build_scan_snapshot(source)["channels"])
        return snapshot.get(ch)

    source = scan_pipeline.ScanSource(
        channels=lambda: channels,
        ordered_content=_ordered,
        now_file=_now_file,
        block_peek=_peek,
        visualizer_pending=_music_pending,
        music_done=lambda path, entry: not pending_fn(path),
        now=_now,
        exists=_exists,
        stages_pending=lambda path, has_gain, has_dur, lookups: bool(pending_fn(path)),
        timeline_next=lambda ch, current, n=4: None,
        front_load=_front_load,
        holiday_key=lambda ch: media.holiday_key_for_date(broadcast_clock.date(), media.holiday_forced_key_for_channel(config or {}, ch), broadcast_clock.date()),
        today=lambda: broadcast_clock.date(),
        holiday_lead_days=_holiday_lead_days(),
        log_missing=lambda path, ch, block: None,
        channel_record=_record,
        lookups=10 ** 9,
    )
    return source


def missing_items(probe, path, want_gain=True):
    """What the shared checklist (file_profile) still lacks for a file on this server's disk: a list of 'header', 'box', 'gain', 'burned'; [] for a complete file and for a file that is not there."""
    if not os.path.isfile(path):
        return []
    entry = probe.get_cached(path) or {}
    audio = path.lower().endswith(AUDIO_SUFFIXES)
    # WARNING: owner 2026-11-14: the burned-in caption verdict is a file fact like the picture box; the server reads it once so a User PC never decodes a server file's picture to decide it. A file with caption text of its own is never searched.
    try:
        import media_library
        box_version = media_library.PICTURE_BOX_DETECTOR_VERSION
    except Exception:
        box_version = None
    try:
        import media
        gain_version = media.LOUDNESS_GAIN_SCAN_VERSION if want_gain else None
    except Exception:
        gain_version = None
    verdict = probe.get_burned_in_verdict(path) if not audio else None
    # WARNING: owner 2026-11-18: the caption file beside a video is looked for only while the burned-in item is still open (a video with its header read, no verdict and no text track of its own), and only once per file; it used to list the file's folder every time the file was asked about, thousands of times a minute on a big server.
    sidecar_found = False
    if (not audio and verdict is None and entry.get("header_probed_at") and entry.get("duration")
            and not file_profile.has_text_subtitles(entry)):
        sidecar_found = _sidecar_known(path)
    return file_profile.missing(
        entry, is_audio=audio, box_version=box_version, gain_version=gain_version,
        burned_verdict=verdict, want_gain=want_gain, want_burned=True, sidecar_found=sidecar_found)


_sidecar_answers = {}


def _sidecar_known(path):
    """True when a caption file sits beside the video; looked up once per file for the life of the process."""
    key = os.path.normcase(os.path.normpath(path))
    found = _sidecar_answers.get(key)
    if found is None:
        try:
            import media
            found = bool(media._find_sidecar_caption_file(path))
        except Exception as exc:
            log.warning("[SERVER SCAN] could not look for a caption file beside %s: %s", os.path.basename(path), exc)
            return False
        _sidecar_answers[key] = found
    return found


class FactsScanner:
    """Reads the missing facts of the scheduled files in the order the program scans in (airing now on every channel, then commercials, the next shows, the deeper lineup; a channel a User is watching first): the header, the picture box (also when it came from an older detector) and, when a loudness function is given, the loudness gain. It runs while the program is open too, slowly, because the program scans only the profile it shows."""

    def __init__(self, slots_fn, program_open_fn, probe_library_fn, loudness_fn=None, watched_fn=None, anchors_fn=None,
                 cpu_percent_fn=None, streaming_fn=None, program_state_fn=None, should_stop_fn=None, clocks=None, next_files_fn=None):
        self.slots_fn = slots_fn
        self.program_open_fn = program_open_fn
        self.probe_library_fn = probe_library_fn
        self.loudness_fn = loudness_fn
        self.watched_fn = watched_fn
        # next_files_fn(slot_id) -> [(channel, [file paths])] the watching Users named (the show on air first, the next shows, the files their schedule waits to have measured).
        self.next_files_fn = next_files_fn
        self.anchors_fn = anchors_fn
        # cpu_percent_fn() -> how busy the whole PC is (0-100) or None; streaming_fn() -> True while a User is watching one of this server's channels.
        self.cpu_percent_fn = cpu_percent_fn
        self.streaming_fn = streaming_fn
        self._music_behind = {}
        # program_state_fn() -> read_program_scan_state(): what the open program is showing and whether its own quick scans are done.
        self.program_state_fn = program_state_fn
        # should_stop_fn() -> True when the owner of this scanner wants it to stop after the file it is on (the program's own idle scan stops when regular mode has work again).
        self.should_stop_fn = should_stop_fn
        # clocks: server_clock.ChannelClocks; the show on air and the next shows of every channel are read first.
        self.clocks = clocks
        self._stop = threading.Event()
        self._thread = None
        self._tries = {}

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="ServerFactsScan")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _needs(self, probe, path):
        """The kinds of facts ('header', 'box', 'gain', 'burned') this file still lacks and has tries left for."""
        need = missing_items(probe, path, want_gain=self.loudness_fn is not None)
        return [kind for kind in need if self._tries.get((path, kind), 0) < SERVER_SCAN_MAX_TRIES]

    def _hub(self, probe):
        """The schedule hub over the scanner's library, made once per library."""
        held = getattr(self, "_held_hub", None)
        if held is None or held[0] is not probe:
            import media_library
            held = (probe, media_library.ScheduleHub(probe.library))
            self._held_hub = held
        return held[1]

    def _hub_now(self, probe, slot_id=None, channels=None):
        """A function giving the path the shared schedule hub says airs now on a channel, or '' when it holds none."""
        try:
            hub = self._hub(probe)
        except Exception as exc:
            log.warning("[SERVER SCAN] the schedule hub could not be opened, the hub's order is skipped: %s", exc)
            return None
        if self.clocks is not None and channels is not None:
            # WARNING: owner 2026-11-18 (audit A8): the show on air comes from the same reading of the clocks as the scan order and the clocks sent to Users.
            try:
                now_files = self.clocks.now_files(hub, [ch for ch, info in channels.items() if isinstance(info, dict) and info.get("active", True)], slot_id=slot_id)
            except Exception as exc:
                log.warning("[SERVER SCAN] the channel clocks could not be read for slot %s: %s", slot_id, exc)
                now_files = {}
            return lambda channel: now_files.get(str(channel).zfill(2), "") or ""

        def _at(channel):
            try:
                with server_clock._slot_scope(slot_id):
                    row = hub.at(str(channel).zfill(2), time.time())
            except Exception as exc:
                log.warning("[SERVER SCAN] could not read the hub's show on channel %s of slot %s: %s", channel, slot_id, exc)
                return ""
            return (row or {}).get("path", "") or ""
        return _at

    def ordered_items(self, probe):
        """[(path, scan class, watched)] for every scheduled file in scan order: the shared pipeline's order first, the music channels' songs lined up with it, then any scheduled file the lineups did not name (found in the slot files in a fixed order)."""
        slot_paths = self.slots_fn()
        out, seen = [], set()
        try:
            import performance_governor as gov
            import scan_pipeline
            import music_pipeline
            min_scanned = int(gov.SEASON_GUESS_MIN_SCANNED)
        except Exception as exc:
            log.warning("[SERVER SCAN] the shared scan order is not available (%s); scanning in file order", exc)
            scan_pipeline = None
            min_scanned = 2
        if scan_pipeline is not None:
            for slot_id, channels, config in read_slots(slot_paths):
                try:
                    watched = self.watched_fn(slot_id) if self.watched_fn else ()
                    anchors = self.anchors_fn() if self.anchors_fn else {}
                    source = make_slot_source(channels, lambda path: self._needs(probe, path), watched, anchors,
                                              self._hub_now(probe, slot_id, channels), config)
                    tiers = scan_pipeline.gather_probe_priority_queue(source)
                    deep = scan_pipeline.gather_deep_backlog_queue(source)
                    items = scan_pipeline.order_scan_pipeline(
                        tiers, deep, lambda ch: scan_pipeline.channel_scan_rank(ch, {}),
                        lambda found: scan_pipeline.season_seeds_first(found, min_scanned))
                    # WARNING: owner 2026-11-14: the songs of a music channel are lined up with the TV files by the one music order the program uses (the playing song and the quick songs with what airs now, the first waves with the next shows, the rest with the deep backlog; a song never ahead of an unscanned file of its class).
                    music = music_pipeline.as_scan_items(music_pipeline.gather_queue(source, self._music_behind))
                    items = sorted(list(items) + music, key=lambda it: it[0])
                except Exception as exc:
                    log.warning("[SERVER SCAN] could not order the files of slot %s: %s", slot_id, exc)
                    continue
                # WARNING: owner 2026-11-14: a channel a User is watching is scanned like the one the owner is watching: all its files come before the other channels' files, in their own order.
                watching = {str(ch).zfill(2) for ch in (watched or ())}
                if watching:
                    items = sorted(items, key=lambda it: 0 if str(it[3]) in watching else 1)
                # WARNING: the shared schedule hub's clock decides what airs now and next on a channel, so those files come before the lineup's own order; a file only scanned here is never scanned again below.
                for _clock_ch, _clock_rank, _clock_path in self._clock_files(probe, channels, watched, slot_id):
                    if _clock_path not in seen:
                        seen.add(_clock_path)
                        out.append((_clock_path, 0 if _clock_rank == 0 else 2, _clock_ch in watching))
                # WARNING: owner 2026-11-18: while this PC's program is closed nothing keeps the hub's clocks current, so the files a watching User names (worked out by its own schedule) stand in for them; only a file this slot schedules is taken.
                for _named in self._named_files(slot_paths, slot_id):
                    if _named[0] not in seen:
                        seen.add(_named[0])
                        out.append(_named)
                for item in items:
                    if item[5] not in seen:
                        seen.add(item[5])
                        out.append((item[5], item[0], str(item[3]) in watching))
        for path in scheduled_paths(slot_paths):
            if path not in seen:
                seen.add(path)
                out.append((path, 3, False))
        return out

    def _named_files(self, slot_paths, slot_id):
        """[(path, scan class, True)] of the files the watching Users named for this slot, the show on air of each channel first; a path this slot does not schedule is left out."""
        try:
            named = self.next_files_fn(slot_id) if self.next_files_fn else []
        except Exception as exc:
            log.warning("[SERVER SCAN] the files named by watching Users could not be read: %s", exc)
            return []
        if not named:
            return []
        allowed = self._scheduled_lookup(slot_paths)
        out, taken = [], set()
        for _channel, files in named:
            for place, path in enumerate(files):
                found = allowed.get(_named_key(path))
                if found is not None and found not in taken:
                    taken.add(found)
                    out.append((found, 0 if place == 0 else 2, True))
        return out

    def _scheduled_lookup(self, slot_paths):
        """{spelling: path} of every file the slot files schedule, read again only when a slot file's size or time changed."""
        stamp = []
        for path in sorted(dict(slot_paths or {}).values()):
            try:
                st = os.stat(path)
                stamp.append((path, st.st_mtime_ns, st.st_size))
            except OSError:
                stamp.append((path, None, None))
        cached = getattr(self, "_scheduled_cache", None)
        if cached is None or cached[0] != stamp:
            cached = (stamp, {_named_key(p): p for p in scheduled_paths(slot_paths)})
            self._scheduled_cache = cached
        return cached[1]

    def _clock_files(self, probe, channels, watched, slot_id=None):
        """[(channel, place, path)] of the shows on air and the next shows of the slot's channels, from the channel clocks; [] without clocks."""
        if self.clocks is None:
            return []
        try:
            return self.clocks.files_in_order(self._hub(probe), [ch for ch, info in (channels or {}).items() if isinstance(info, dict) and info.get("active", True)], watched, slot_id=slot_id)
        except Exception as exc:
            log.warning("[SERVER SCAN] the channel clocks could not be read: %s", exc)
            return []

    def _scan_file(self, probe, path):
        """Reads whatever the file still lacks; True when something was stored."""
        need = self._needs(probe, path)
        if not need:
            return False
        read = False
        try:
            if "header" in need:
                self._tries[(path, "header")] = self._tries.get((path, "header"), 0) + 1
                facts = probe.header_facts(path)
                if facts and facts.get("ok") and facts.get("duration"):
                    if not path.lower().endswith(AUDIO_SUFFIXES):
                        probe.detect_picture_box_if_needed(path)
                    read = True
                    log.info("[SERVER SCAN] read %s", os.path.basename(path))
            else:
                if "box" in need:
                    self._tries[(path, "box")] = self._tries.get((path, "box"), 0) + 1
                    probe.detect_picture_box_if_needed(path)
                    read = True
                    log.info("[SERVER SCAN] picture box of %s read again (an older detector made the saved one)", os.path.basename(path))
                if "gain" in need and not self._stop.is_set():
                    self._tries[(path, "gain")] = self._tries.get((path, "gain"), 0) + 1
                    if self.loudness_fn(probe, path):
                        read = True
                        log.info("[SERVER SCAN] loudness of %s measured", os.path.basename(path))
                    else:
                        log.info("[SERVER SCAN] no loudness gain for %s (try %d of %d)", os.path.basename(path),
                                 self._tries[(path, "gain")], SERVER_SCAN_MAX_TRIES)
                if "burned" in need and not self._stop.is_set():
                    self._tries[(path, "burned")] = self._tries.get((path, "burned"), 0) + 1
                    if probe.detect_burned_in_if_needed(path) is not None:
                        read = True
        except Exception as exc:
            log.warning("[SERVER SCAN] could not read %s: %s", path, exc)
        return read

    def scan_once(self):
        """One pass in scan order; returns how many files were read. The order is made again every SERVER_SCAN_REORDER_S seconds, so a channel a User turns to, or a block that comes on air, is scanned next."""
        probe = self.probe_library_fn()
        done = 0
        started = time.monotonic()
        for path, scan_class, watched in self.ordered_items(probe):
            if self._stop.is_set() or self._asked_to_stop():
                break
            if time.monotonic() - started >= SERVER_SCAN_REORDER_S:
                break
            # WARNING: owner 2026-11-14: a file airing now (or on a channel a User watches) is never held back by load; the rest wait while the whole PC is busy.
            if not (watched or scan_class == 0):
                self._wait_while_busy()
            if self._scan_file(probe, path):
                done += 1
            # WARNING: owner 2026-11-14: with the program open the scan goes on, one file every few seconds, because the program only measures what it plays and a User PC was left without the gain and picture box of every other scheduled file.
            pause = self._pause_seconds()
            if not watched and self._streaming():
                pause *= SERVER_SCAN_STREAMING_PAUSE_FACTOR
            self._stop.wait(pause)
        return done

    def _asked_to_stop(self):
        try:
            return bool(self.should_stop_fn and self.should_stop_fn())
        except Exception:
            return False

    def _pause_seconds(self):
        """The rest between two files. Full pace (SERVER_SCAN_PAUSE_S) while the program is closed, while it shows a server profile (the owner is watching what this scan feeds), and while it shows regular mode with its own quick scans done; the slow pace (SERVER_SCAN_OPEN_PAUSE_S) while regular mode is still working through them or the program has not said."""
        # WARNING: owner 2026-11-14: regular mode is the first priority while it is still catching up; once it is past its quick scans the tray catches the server profiles up.
        if not self.program_open_fn():
            return SERVER_SCAN_PAUSE_S
        try:
            state = self.program_state_fn() if self.program_state_fn else None
        except Exception:
            state = None
        if state and (state.get("mode") == "profile" or state.get("caught_up")):
            return SERVER_SCAN_PAUSE_S
        return SERVER_SCAN_OPEN_PAUSE_S

    def _streaming(self):
        """True while a User is watching one of this server's channels."""
        try:
            return bool(self.streaming_fn and self.streaming_fn())
        except Exception:
            return False

    def _wait_while_busy(self):
        """Waits (never longer than SERVER_SCAN_BUSY_MAX_WAIT_S) while the whole PC is busier than SERVER_SCAN_BUSY_CPU_PERCENT."""
        if self.cpu_percent_fn is None:
            return
        waited = 0.0
        while waited < SERVER_SCAN_BUSY_MAX_WAIT_S and not self._stop.is_set():
            try:
                busy = self.cpu_percent_fn()
            except Exception:
                return
            if busy is None or busy < SERVER_SCAN_BUSY_CPU_PERCENT:
                return
            self._stop.wait(SERVER_SCAN_BUSY_WAIT_S)
            waited += SERVER_SCAN_BUSY_WAIT_S

    def _run(self):
        worked = False
        while not self._stop.wait(0 if worked else SERVER_SCAN_LOOK_S):
            worked = False
            try:
                worked = self.scan_once() > 0
            except Exception:
                log.exception("[SERVER SCAN] pass failed")
