"""The server's caption scan: once the media scan is done, the server captions the files its channels air soonest (today, then tomorrow), each file once, and saves the text in main/saved captions, where the program's live captions and every User find it.

Owns: the order of files to caption, captioning one file in short pieces, and the pacing that keeps the scan from ever hurting playback on this computer.
Does not own: the caption model and its helpers (media.py), the saved text (saved_captions.py) or sending it to Users (server_sync.py).

WARNING: owner 2026-11-24: "the server computer ... after scanning all media is done, starts a captions scan for each show in advance of when it plays", "with captions on or off", commercials included, each file once; a file with real subtitles or burned-in captions is never scanned.
WARNING: owner 2026-11-24: "do not let those captions mess up any playback of this program on any computer ... make the captions background scans as slow as they need to be on each computer as needed. if the computer is struggling with slowest scans, stop the scan"; CaptionPacer decides every rest and every stop, and counts other programs on the PC through the whole-PC load.
WARNING: owner 2026-11-24: "when the app is closed on the server computer, it uses that extra that was running the app, and put it into the scans unless throttled"; the caption model runs with more threads while the program is closed and is started again with fewer when it opens.
"""
from __future__ import annotations

import logging
# WARNING: owner 2026-11-26: every memo that grows with the library or with time is made by the cache manager, so none can grow without a limit.
import cache_manager
import os
import tempfile
import threading
import time

import server_clock
import server_scanner
import station_clock

log = logging.getLogger(__name__)

# ============================================================================
# SECTION: Settings
# ============================================================================
try:
    import performance_governor as _gov
except Exception as _governor_error:
    _gov = None
    logging.getLogger(__name__).warning("[FAILURE] server_captions: performance_governor did not import, so the built-in numbers are used: %s", _governor_error)


def _log_swallowed(where, error):
    """Writes the shared, counted [FAILURE] line through performance_governor (a plain warning when it did not import)."""
    if _gov is not None:
        return _gov.log_swallowed(where, error)
    logging.getLogger(__name__).warning("[FAILURE] %s: %s: %s", where, type(error).__name__, error)
    return None


# WARNING: the fallback values below must match the same names in performance_governor.py; they are used only when that module fails to import.
CAPTION_SCAN_CHUNK_SEC = _gov.CAPTION_SCAN_CHUNK_SEC if _gov is not None else 60.0
CAPTION_SCAN_LOOK_AHEAD_HOURS = _gov.CAPTION_SCAN_LOOK_AHEAD_HOURS if _gov is not None else 48.0
CAPTION_SCAN_REORDER_S = _gov.CAPTION_SCAN_REORDER_S if _gov is not None else 300.0
CAPTION_SCAN_IDLE_LOOK_S = _gov.CAPTION_SCAN_IDLE_LOOK_S if _gov is not None else 60.0
CAPTION_SCAN_REST_CLOSED = _gov.CAPTION_SCAN_REST_CLOSED if _gov is not None else 0.25
CAPTION_SCAN_REST_OPEN = _gov.CAPTION_SCAN_REST_OPEN if _gov is not None else {"lite": 8.0, "standard": 3.0, "performance": 1.5}
CAPTION_SCAN_BUSY_CPU_PERCENT = _gov.CAPTION_SCAN_BUSY_CPU_PERCENT if _gov is not None else 70.0
CAPTION_SCAN_STRAIN_LEVEL = _gov.CAPTION_SCAN_STRAIN_LEVEL if _gov is not None else 1
CAPTION_SCAN_BACKOFF_MAX = _gov.CAPTION_SCAN_BACKOFF_MAX if _gov is not None else 16.0
CAPTION_SCAN_STOP_AFTER_STRAINED = _gov.CAPTION_SCAN_STOP_AFTER_STRAINED if _gov is not None else 3
CAPTION_SCAN_CALM_TO_RESUME_S = _gov.CAPTION_SCAN_CALM_TO_RESUME_S if _gov is not None else 600.0
CAPTION_SCAN_CHECK_S = _gov.CAPTION_SCAN_CHECK_S if _gov is not None else 5.0
CAPTION_SCAN_MIN_LEAD_SECONDS = _gov.CAPTION_SCAN_MIN_LEAD_SECONDS if _gov is not None else 1800.0
CAPTION_SCAN_THREADS_APP_OPEN = _gov.CAPTION_SCAN_THREADS_APP_OPEN if _gov is not None else 1
CAPTION_SCAN_THREADS_CLOSED_RESERVED = _gov.CAPTION_SCAN_THREADS_CLOSED_RESERVED if _gov is not None else 1
CAPTION_SCAN_LANGUAGE_CHECK = _gov.CAPTION_SCAN_LANGUAGE_CHECK if _gov is not None else "every"
CAPTION_SCAN_LANGUAGE_WAIT_S = _gov.CAPTION_SCAN_LANGUAGE_WAIT_S if _gov is not None else 120.0
CAPTION_SCAN_SOUND_TAGS = _gov.CAPTION_SCAN_SOUND_TAGS if _gov is not None else True
CAPTION_SCAN_THIN_RECHECK = _gov.CAPTION_SCAN_THIN_RECHECK if _gov is not None else True
# WARNING: the fallback number below must match CAPTION_UNSURE_PAD_SEC in performance_governor.py; it is used only when that module fails to import.
CAPTION_UNSURE_PAD_SEC = _gov.CAPTION_UNSURE_PAD_SEC if _gov is not None else 1.0


def saved_captions_lowest_mode_grade():
    """The grade Lite makes (saved_captions.GRADES "off"): a PC in Lite has nothing to improve with."""
    import saved_captions
    return saved_captions.grade_for("off")


# ============================================================================
# SECTION: Pacing: how long the scan rests and when it stops
# ============================================================================


class CaptionPacer:
    """Decides how long the caption scan rests after each piece and when it stops. The PC struggles when the whole PC is busier than CAPTION_SCAN_BUSY_CPU_PERCENT (other programs count), when the open program's throttle level is CAPTION_SCAN_STRAIN_LEVEL or more, or when the open program is captioning live. Each struggling piece doubles the rest, up to CAPTION_SCAN_BACKOFF_MAX times; after CAPTION_SCAN_STOP_AFTER_STRAINED struggling pieces at the slowest pace the scan stops, until the PC has been calm CAPTION_SCAN_CALM_TO_RESUME_S seconds in a row."""

    def __init__(self, cpu_percent_fn=None, program_open_fn=None, program_state_fn=None, streaming_fn=None, clock=None):
        self.cpu_percent_fn = cpu_percent_fn
        self.program_open_fn = program_open_fn
        self.program_state_fn = program_state_fn
        self.streaming_fn = streaming_fn
        self._clock = clock or time.monotonic
        self.backoff = 1.0
        self._strained_at_max = 0
        self.stopped = False

    def _call(self, fn, default):
        try:
            return fn() if fn is not None else default
        except Exception as call_error:
            _log_swallowed("server_captions: asking %s how the PC is doing" % getattr(fn, "__name__", "a pacer reading"), call_error)
            return default

    def program_open(self):
        return bool(self._call(self.program_open_fn, False))

    def program_state(self):
        """The open program's last scan report (server_scanner.read_program_scan_state), or None."""
        return self._call(self.program_state_fn, None)

    def strained(self):
        """True while the PC is struggling (see the class)."""
        busy = self._call(self.cpu_percent_fn, None)
        if busy is not None and busy >= CAPTION_SCAN_BUSY_CPU_PERCENT:
            return True
        if self.program_open():
            state = self._call(self.program_state_fn, None) or {}
            if int(state.get("level") or 0) >= CAPTION_SCAN_STRAIN_LEVEL or state.get("live_captions"):
                return True
        return False

    def rest_share(self):
        """The rest after a piece as a share of the time the piece took, before the back-off."""
        if self.program_open():
            state = self._call(self.program_state_fn, None) or {}
            share = float(CAPTION_SCAN_REST_OPEN.get(str(state.get("perf_mode") or "lite"), max(CAPTION_SCAN_REST_OPEN.values())))
        else:
            share = float(CAPTION_SCAN_REST_CLOSED)
        if self._call(self.streaming_fn, False):
            share *= float(server_scanner.SERVER_SCAN_STREAMING_PAUSE_FACTOR)
        return share

    def after_piece(self, worked_seconds, strained=None):
        """Seconds to rest after a piece that took worked_seconds (strained: whether the PC struggles, looked at here when not given); sets stopped when the PC still struggles at the slowest pace."""
        if self.strained() if strained is None else strained:
            if self.backoff >= CAPTION_SCAN_BACKOFF_MAX:
                self._strained_at_max += 1
                if self._strained_at_max >= CAPTION_SCAN_STOP_AFTER_STRAINED:
                    self.stopped = True
                    log.info("[CAPTION SCAN] this PC is still struggling at the slowest caption scan pace; the scan stops until the PC has been calm for %.0f s", CAPTION_SCAN_CALM_TO_RESUME_S)
            self.backoff = min(CAPTION_SCAN_BACKOFF_MAX, self.backoff * 2.0)
        else:
            self._strained_at_max = 0
            self.backoff = max(1.0, self.backoff / 2.0)
        return max(0.0, float(worked_seconds)) * self.rest_share() * self.backoff

    def before_piece(self, stop_event):
        """Holds the next piece while the PC struggles: every CAPTION_SCAN_CHECK_S of struggling counts as a struggling piece (the rest doubles, and at the slowest pace it counts toward the stop). Returns False when the scan is to stop."""
        while not stop_event.is_set() and not self.stopped and self.strained():
            self.after_piece(CAPTION_SCAN_CHECK_S, strained=True)
            if not self.stopped:
                stop_event.wait(CAPTION_SCAN_CHECK_S)
        return not stop_event.is_set() and not self.stopped

    def wait_until_calm(self, stop_event):
        """While stopped: returns once the PC has been calm CAPTION_SCAN_CALM_TO_RESUME_S seconds in a row (the scan goes on at the slowest pace), or when stop_event is set."""
        calm_since = None
        while not stop_event.is_set():
            now = self._clock()
            if self.strained():
                calm_since = None
            elif calm_since is None:
                calm_since = now
            elif now - calm_since >= CAPTION_SCAN_CALM_TO_RESUME_S:
                self.stopped = False
                self._strained_at_max = 0
                self.backoff = CAPTION_SCAN_BACKOFF_MAX
                log.info("[CAPTION SCAN] this PC has been calm for %.0f s; the caption scan goes on at the slowest pace", CAPTION_SCAN_CALM_TO_RESUME_S)
                return
            stop_event.wait(CAPTION_SCAN_CHECK_S)

    def threads(self):
        """Caption model threads for now: CAPTION_SCAN_THREADS_APP_OPEN while the program is open; all logical cores but CAPTION_SCAN_THREADS_CLOSED_RESERVED while it is closed."""
        if self.program_open():
            return max(1, int(CAPTION_SCAN_THREADS_APP_OPEN))
        return max(1, (os.cpu_count() or 2) - int(CAPTION_SCAN_THREADS_CLOSED_RESERVED))


# ============================================================================
# SECTION: What to caption first
# ============================================================================


def airing_order(hub, slots, now_epoch, look_ahead_hours=None):
    """[(clock time the file starts airing, path)] of every airing on the slots' channels from now to look_ahead_hours ahead, in time order; a show already on air counts from its own start."""
    end = now_epoch + 3600.0 * float(CAPTION_SCAN_LOOK_AHEAD_HOURS if look_ahead_hours is None else look_ahead_hours)
    found = []
    for slot_id, channels, _config in slots:
        active = [ch for ch, info in (channels or {}).items() if isinstance(info, dict) and info.get("active", True)]
        with server_clock._slot_scope(slot_id):
            for ch in active:
                key = str(ch).zfill(2)
                rows = []
                for reader in (lambda: hub.rows_between(key, now_epoch, now_epoch + 1.0, now_epoch, full=True),
                               lambda: hub.planned_rows_between(key, now_epoch, end),
                               lambda: hub.future_rows_between(key, now_epoch, end)):
                    try:
                        got = reader()
                    except Exception as exc:
                        log.warning("[CAPTION SCAN] the schedule of channel %s could not be read: %s", key, exc)
                        continue
                    if isinstance(got, list):
                        rows.extend(got)
                for row in rows:
                    if row.get("path") and row.get("planned_start") is not None:
                        found.append((float(row["planned_start"]), str(row["path"])))
    return sorted(set(found))


def next_midnight(now_epoch):
    """The clock time of the next midnight on this PC."""
    import datetime
    day = datetime.datetime.fromtimestamp(now_epoch).date() + datetime.timedelta(days=1)
    return datetime.datetime(day.year, day.month, day.day).timestamp()


def scan_plan(airings, now_epoch, still_needed):
    """(the order to caption in as [(clock time it airs, path)], whether the schedule caught up with the scan): each file once, at its first airing the scan can still stay CAPTION_SCAN_MIN_LEAD_SECONDS ahead of. When a file airing sooner than that still needs captions (still_needed(path)), the scan has been caught up: what airs before the next midnight is left to live captions and the scan jumps to tomorrow's airings, taking today's rest only once tomorrow's are done."""
    # WARNING: owner 2026-11-24: "if im live captions on a user computer ... and server is in the same time spot or close to it, it needs to jump to tomorrow. and start future scanning again after closing the gap. i dont want them scanning the same thing."
    lead_until = now_epoch + CAPTION_SCAN_MIN_LEAD_SECONDS
    near = {path for at, path in airings if at < lead_until}
    caught_up = any(still_needed(path) for path in sorted(near))
    first = {}
    for at, path in airings:
        norm = os.path.normcase(os.path.normpath(path))
        if at >= lead_until and norm not in first:
            first[norm] = (at, path)
    order = sorted(first.values())
    if caught_up:
        tomorrow = next_midnight(now_epoch)
        order = [item for item in order if item[0] >= tomorrow] + [item for item in order if item[0] < tomorrow]
    return order, caught_up


# ============================================================================
# SECTION: The caption scan thread
# ============================================================================


class CaptionScanner:
    """Background thread of the server service: waits until the media scan is done, then captions the files in the order scan_plan gives (staying CAPTION_SCAN_MIN_LEAD_SECONDS ahead of the picture, jumping to tomorrow when the schedule catches up), one at a time, each in pieces of CAPTION_SCAN_CHUNK_SEC, saving each piece as it is done, so a User gets the start of a long show before its end is captioned. A file already saved whole, one with its own captions, one with none of its audio readable, and one a User is captioning live are passed over."""

    def __init__(self, slots_fn, probe_library_fn, facts_scanner, pacer, live_keys_fn=None):
        self.slots_fn = slots_fn
        self.probe_library_fn = probe_library_fn
        self.facts_scanner = facts_scanner
        self.pacer = pacer
        # WARNING: owner 2026-11-25: profile scans, then profile captions, then the regular channels' scans, then their captions; profiles_done opens the regular channels' file scans, and the regular captions wait for the file scan's regular_done.
        self.profiles_done = False
        self._regular_logged = False
        self.live_keys_fn = live_keys_fn
        self._stop = threading.Event()
        self._thread = None
        self._model = None
        self._model_threads = None
        self._passed_over = cache_manager.memo("caption files passed over")
        self._caught_up = False
        # WARNING: owner 2026-11-25: the scan works at the performance mode this PC is set to (mode_fn, given by the service); improving is True only during the pass that improves captions saved at a lower mode, which runs after every other scan is done.
        self.mode_fn = None
        self.improving = False
        self._model_size = None
        self._improve_logged = False

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="ServerCaptionScan")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _close_model(self):
        if self._model is not None:
            try:
                self._model.close()
            except Exception as exc:
                log.warning("[CAPTION SCAN] the caption model could not be closed: %s", exc)
        self._model, self._model_threads = None, None
        import media
        media.release_language_model("the caption scan closed its caption model")

    def _model_for_now(self):
        """The caption model with the thread count for now (pacer.threads()), started again when the program opened or closed since it was started; None when it cannot load (too little memory, or no caption library)."""
        import media
        want = self.pacer.threads()
        size = media.caption_word_model(self.scan_mode())
        if self._model is not None and self._model_threads == want and getattr(self, "_model_size", size) == size and not getattr(self._model, "dead", False):
            return self._model
        self._close_model()
        if not media._caption_memory_allows_load(notify=False):
            return None
        model = media._start_caption_model_process(want, size=size)
        if model is not None:
            self._model, self._model_threads, self._model_size = model, want, size
            # WARNING: the language model is started and waited for (up to CAPTION_SCAN_LANGUAGE_WAIT_S) before the first piece, so the scan's first pieces are made at its full grade instead of being made twice.
            media.start_language_model(self.scan_check() if self.scan_check() != "off" else CAPTION_SCAN_LANGUAGE_CHECK)
            media.wait_for_language_model(CAPTION_SCAN_LANGUAGE_WAIT_S)
            log.info("[CAPTION SCAN] caption model '%s' ready with %d thread(s) (the program is %s, this PC is in %s mode)", size, want, "open" if self.pacer.program_open() else "closed", self.scan_mode())
        return model

    def _wants(self, probe, path, live_keys):
        """(content key, saved copy or None) when the file still needs captions from this scan, else None."""
        import media
        import saved_captions
        if not os.path.isfile(path):
            return None
        if self._passed_over.get(path) == os.path.getmtime(path):
            return None
        info = media.resolve_caption_source(path)
        if info.get("kind") is not None:
            self._passed_over[path] = os.path.getmtime(path)
            return None
        if path.lower().endswith(server_scanner.AUDIO_SUFFIXES):
            return None
        try:
            verdict = probe.get_burned_in_verdict(path)
        except Exception:
            verdict = None
        if verdict:
            self._passed_over[path] = os.path.getmtime(path)
            return None
        key = probe.keys.resolve(path)
        if not key or key in live_keys:
            return None
        saved = saved_captions.read(key)
        # WARNING: owner 2026-11-25: a file with captions saved all through, at any mode, waits for the improving pass, which comes after every other scan is done.
        if saved and not getattr(self, "improving", False) and saved_captions.is_complete(saved_captions.covered_at_least(saved, saved_captions.LOWEST_GRADE), saved.get("length")):
            return None
        if saved and saved_captions.is_complete(saved_captions.covered_at_least(saved, self.aim_grade()), saved.get("length")):
            return None
        return key, saved

    def scan_mode(self):
        """The performance mode this PC is set to (lite, standard or performance): the program's saved mode (mode_fn), else the governor's own."""
        mode_fn = getattr(self, "mode_fn", None)
        if mode_fn is not None:
            try:
                mode = mode_fn()
                if mode:
                    return str(mode)
            except Exception as mode_error:
                _log_swallowed("server_captions: reading this PC's performance mode", mode_error)
        try:
            import performance_governor
            return str(performance_governor.get_mode())
        except Exception as mode_error:
            _log_swallowed("server_captions: asking the governor for the performance mode", mode_error)
            return "lite"

    def scan_check(self):
        """The language check of this PC's mode (media.caption_language_check): "off" in Lite, "unsure" in Standard, "every" in Performance."""
        import media
        return media.caption_language_check(self.scan_mode())

    def aim_grade(self):
        """The grade this scan makes (saved_captions.GRADES of CAPTION_SCAN_LANGUAGE_CHECK), or the lowest once the language model could not load here, so a stretch the scan cannot make better is never scanned again and again."""
        import media
        import saved_captions
        if media.language_model_failed():
            return saved_captions.grade_for("off")
        return saved_captions.grade_for(self.scan_check())

    def _left_to_live(self, key, air_at, pos):
        """True when this piece is no longer the scan's to make: a User started captioning the file live, or the picture reaches it (air_at + pos) sooner than CAPTION_SCAN_MIN_LEAD_SECONDS from now."""
        if self.live_keys_fn is not None:
            try:
                if key in set(self.live_keys_fn() or ()):
                    return True
            except Exception as live_error:
                _log_swallowed("server_captions: reading which files the open program captions live", live_error)
        return air_at is not None and air_at + pos - station_clock.now_epoch() < CAPTION_SCAN_MIN_LEAD_SECONDS

    def caption_file(self, path, key, saved, air_at=None):
        """Captions the stretches of the file not saved yet or saved at a lower grade than this scan makes (aim_grade; a stretch saved as well or better is never scanned again), piece by piece, resting after each piece as the pacer says; a piece that has become live captions' to make (_left_to_live) ends the file here. Returns how many pieces were saved."""
        import media
        import saved_captions
        probe = media._asr_probe_media(path)
        if probe is None or not probe.get("duration"):
            self._passed_over[path] = os.path.getmtime(path)
            return 0
        if not probe.get("has_audio"):
            self._passed_over[path] = os.path.getmtime(path)
            return 0
        duration = float(probe["duration"])
        # WARNING: owner 2026-11-24: "if user scans it but server has better results its rescanned"; a stretch a User saved in Lite or Standard counts as not saved here, and the better copy replaces it on every computer.
        held = saved_captions.covered_at_least(saved, self.aim_grade())
        # WARNING: owner 2026-11-25: before the improving pass only what has no captions at all is made; in the improving pass Standard reads again only the unsure stretches of a lower copy, Performance makes the lower stretches again in full.
        if not getattr(self, "improving", False):
            held = saved_captions.covered_at_least(saved, saved_captions.LOWEST_GRADE)
        elif self.scan_check() == "unsure":
            return self._improve_unsure(path, key, saved, duration, air_at)
        todo = saved_captions.missing_stretches([[0.0, duration]], held)
        prompt = media._asr_prompt_for(path)
        name = os.path.basename(path)
        done = 0
        for a, b in todo:
            pos = a
            while pos < b - 0.05:
                if not self.pacer.before_piece(self._stop):
                    return done
                if self._left_to_live(key, air_at, pos):
                    log.info("[CAPTION SCAN] %s from %.0fs is left to live captions (a User captions it live, or it airs within %.0f s); the scan moves on",
                             name, pos, CAPTION_SCAN_MIN_LEAD_SECONDS)
                    return done
                model = self._model_for_now()
                if model is None:
                    return done
                threads_now = self._model_threads
                piece_end = min(b, pos + CAPTION_SCAN_CHUNK_SEC)
                at_end = piece_end >= duration - 0.5
                audio_end = duration if at_end else min(duration, piece_end + media._ASR_CHUNK_OVERLAP_SEC)
                wav_fd, wav_path = tempfile.mkstemp(prefix="caption_scan_", suffix=".wav")
                os.close(wav_fd)
                started = time.monotonic()
                cues = []
                try:
                    status = media._asr_extract_chunk(path, pos, audio_end - pos, wav_path)
                    if status == "failed":
                        log.info("[CAPTION SCAN] the audio of %s at %.0fs could not be read; the file is passed over", name, pos)
                        self._passed_over[path] = os.path.getmtime(path)
                        return done
                    if status == "ok":
                        # WARNING: a piece in progress is given up the moment the program opens (its thread count changes), so the scan never holds the cores the program's start and playback need.
                        def _give_up(_threads=threads_now):
                            return self._stop.is_set() or self.pacer.threads() != _threads
                        # WARNING: owner 2026-11-24: the title words (prompt) only drop captions that echo them, they are not a hint to the caption model; the server checks every stretch's language (CAPTION_SCAN_LANGUAGE_CHECK) because its captions are shared with every PC whatever its mode.
                        segments = media._asr_transcribe_chunk(model, wav_path, media._ASR_LANGUAGE, media._ASR_BEAM_SIZE,
                                                               initial_prompt=None, should_stop=_give_up,
                                                               language_check=self.scan_check(), sound_tags=CAPTION_SCAN_SOUND_TAGS,
                                                               thin_recheck=CAPTION_SCAN_THIN_RECHECK)
                        if _give_up():
                            log.info("[CAPTION SCAN] %s: the piece at %.0fs was given up (the program opened or the service is stopping)", name, pos)
                            return done
                        cues, _drops = media._asr_filter_cues(segments, pos, piece_end, at_end, prompt=prompt)
                except Exception as exc:
                    log.warning("[CAPTION SCAN] %s at %.0fs could not be captioned: %s", name, pos, exc)
                    self._close_model()
                    return done
                finally:
                    try:
                        os.remove(wav_path)
                    except FileNotFoundError:
                        # WARNING: the normal end: the piece of audio was never written (its cut failed), so there is nothing to remove.
                        pass
                    except OSError as wav_error:
                        _log_swallowed("server_captions: removing a piece of audio the scan read", wav_error)
                worked = time.monotonic() - started
                saved_captions.add(key, [list(c) for c in cues], [[pos, piece_end]], duration, "server",
                                   grade=saved_captions.grade_for(getattr(segments, "language_check", "off")))
                done += 1
                log.info("[CAPTION SCAN] %s %.0f-%.0fs of %.0fs: %d cue(s) in %.1fs", name, pos, piece_end, duration, len(cues), worked)
                pos = piece_end
                rest = self.pacer.after_piece(worked)
                if rest > 0:
                    self._stop.wait(rest)
        return done

    def scan_once(self):
        """Works out the airing order and captions what still needs it, until the order is CAPTION_SCAN_REORDER_S old; returns how many pieces were saved."""
        # WARNING: owner 2026-11-25: the profiles' captions come before the regular channels' file scans, so the caption scan waits only for the profiles' files to be scanned.
        if not (getattr(self.facts_scanner, "profiles_idle", False) or getattr(self.facts_scanner, "idle", False)):
            return 0
        probe = self.probe_library_fn()
        import media_library
        hub = media_library.ScheduleHub(probe.library)
        slots = list(server_scanner.read_slots(self.slots_fn()))
        # WARNING: owner 2026-11-24: everything on the schedule reads the one station clock (station_clock.now_epoch), never this computer's clock directly.
        now = station_clock.now_epoch()
        started = time.monotonic()
        done = 0
        try:
            live_keys = set(self.live_keys_fn() or ()) if self.live_keys_fn else set()
        except Exception:
            live_keys = set()

        def _needed(path):
            try:
                return self._wants(probe, path, set()) is not None
            except Exception as need_error:
                _log_swallowed("server_captions: checking whether %s still needs captions" % os.path.basename(str(path)), need_error)
                return False
        order, caught_up = scan_plan(airing_order(hub, slots, now), now, _needed)
        if caught_up and not self._caught_up:
            log.info("[CAPTION SCAN] the schedule has caught up with the caption scan: what airs before midnight is left to live captions and the scan jumps to tomorrow")
        self._caught_up = caught_up
        done, finished, wanted_any = self._caption_in_order(order, probe, live_keys, started)
        self.profiles_done = finished and not wanted_any and bool(getattr(self.facts_scanner, "profiles_idle", True))
        if not self.profiles_done or not self._regular_scanned():
            self._regular_logged = False
            return done
        regular = self._regular_order(hub, now, _needed)
        if regular and not self._regular_logged:
            self._regular_logged = True
            log.info("[CAPTION SCAN] every server profile file is captioned and the regular channels' files are scanned: the regular channels' files are captioned now")
        more, _finished, _wanted = self._caption_in_order(regular, probe, live_keys, started)
        # WARNING: owner 2026-11-25: "this happens after all other scans are finished. starting with tomorrows schedule and moving through the media in order across all channels that need it".
        if not (_finished and not _wanted and getattr(self.facts_scanner, "idle", False)) or self.aim_grade() <= saved_captions_lowest_mode_grade():
            return done + more
        better = self._improve_order(hub, slots, now, regular)
        self.improving = True
        try:
            gained, _f, wanted = self._caption_in_order(better, probe, live_keys, started)
        finally:
            self.improving = False
        if wanted and not self._improve_logged:
            self._improve_logged = True
            log.info("[CAPTION SCAN] every file is scanned and captioned: captions saved at a lower mode are improved now in %s mode, tomorrow's schedule first", self.scan_mode())
        elif not wanted:
            self._improve_logged = False
        return done + more + gained

    def _improve_order(self, hub, slots, now, regular):
        """[(clock time it airs or None, path)] for the improving pass: every airing from the next midnight on, in time order across all channels, then the airings before it, then the regular channels' other files; each file once."""
        tomorrow = next_midnight(now)
        airings = airing_order(hub, slots, now)
        seen, out = set(), []
        for at, path in [a for a in airings if a[0] >= tomorrow] + [a for a in airings if a[0] < tomorrow] + list(regular or []):
            norm = os.path.normcase(os.path.normpath(path))
            if norm not in seen:
                seen.add(norm)
                out.append((at, path))
        return out

    def _improve_unsure(self, path, key, saved, duration, air_at):
        """Standard's improving: the unsure captions ([inaudible], other languages, sounds, music) in the stretches saved at a lower mode are read again with the language check and sound tags on every stretch, and each lower stretch is saved again at Standard with the rest of its captions kept as they were. Returns how many stretches were saved."""
        import media
        import saved_captions
        name = os.path.basename(path)
        aim = self.aim_grade()
        done = 0
        for low_a, low_b in saved_captions.below_grade(saved, aim):
            if not self.pacer.before_piece(self._stop):
                return done
            if self._left_to_live(key, air_at, low_a):
                return done
            model = self._model_for_now()
            if model is None:
                return done
            unsure = saved_captions.overlap(saved_captions.unsure_spans(saved, CAPTION_UNSURE_PAD_SEC), [[low_a, low_b]])
            kept = [list(c) for c in saved.get("cues") or [] if low_a - 0.05 <= float(c[0]) < low_b
                    and not any(a <= float(c[0]) < b for a, b in unsure)]
            fresh = []
            started = time.monotonic()
            for a, b in unsure:
                wav_fd, wav_path = tempfile.mkstemp(prefix="caption_improve_", suffix=".wav")
                os.close(wav_fd)
                try:
                    if media._asr_extract_chunk(path, a, b - a, wav_path) != "ok":
                        continue
                    segments = media._asr_transcribe_chunk(model, wav_path, media._ASR_LANGUAGE, media._ASR_BEAM_SIZE,
                                                           initial_prompt=None, should_stop=self._stop.is_set,
                                                           language_check="every", sound_tags=True, thin_recheck=True)
                    cues, _drops = media._asr_filter_cues(segments, a, b, b >= duration - 0.5, prompt=media._asr_prompt_for(path))
                    fresh.extend(list(c) for c in cues)
                except Exception as exc:
                    log.warning("[CAPTION SCAN] %s %.0f-%.0fs could not be improved: %s", name, a, b, exc)
                    self._close_model()
                    return done
                finally:
                    try:
                        os.remove(wav_path)
                    except FileNotFoundError:
                        # WARNING: the normal end: the piece of audio was never written (its cut failed), so there is nothing to remove.
                        pass
                    except OSError as wav_error:
                        _log_swallowed("server_captions: removing a piece of audio the improving pass read", wav_error)
            saved_captions.add(key, sorted(kept + fresh, key=lambda c: c[0]), [[low_a, low_b]], duration, "server", grade=aim)
            done += 1
            worked = time.monotonic() - started
            log.info("[CAPTION SCAN] %s %.0f-%.0fs improved to %s: %d unsure stretch(es) read again, %d caption(s) kept, %d new, in %.1fs",
                     name, low_a, low_b, self.scan_mode(), len(unsure), len(kept), len(fresh), worked)
            rest = self.pacer.after_piece(max(worked, 0.1))
            if rest > 0:
                self._stop.wait(rest)
        return done

    def _regular_scanned(self):
        """True when the regular channels' files are scanned: by the tray's file scan while the program is closed, by the program itself (its own report) while it is open."""
        if not self.pacer.program_open():
            return bool(getattr(self.facts_scanner, "regular_done", False))
        state = self.pacer.program_state()
        return bool(state and state.get("regular_done"))

    def _caption_in_order(self, order, probe, live_keys, started):
        """Captions the files of order that still need it; (pieces saved, whether the whole order was gone through, whether any file still needed captions)."""
        done, wanted_any = 0, False
        for air_at, path in order:
            if self._stop.is_set() or self.pacer.stopped or time.monotonic() - started >= CAPTION_SCAN_REORDER_S:
                return done, False, wanted_any
            try:
                wanted = self._wants(probe, path, live_keys)
            except Exception as exc:
                log.warning("[CAPTION SCAN] could not check %s: %s", os.path.basename(path), exc)
                continue
            if wanted is None:
                continue
            wanted_any = True
            done += self.caption_file(path, wanted[0], wanted[1], air_at)
        return done, True, wanted_any

    def _regular_order(self, hub, now, still_needed):
        """[(clock time it airs or None, path)] of the regular channels' files: their airings in time order (read under the setup in use), then every other file they schedule; [] while the program is open or there are no regular channels."""
        paths = self.facts_scanner._regular_paths() if hasattr(self.facts_scanner, "_regular_paths") else {}
        if not paths:
            return []
        slots = [(None, channels, config) for _regular_id, channels, config in server_scanner.read_slots(paths)]
        order, _caught_up = scan_plan(airing_order(hub, slots, now), now, still_needed)
        planned = {os.path.normcase(os.path.normpath(path)) for _at, path in order}
        rest = [(None, path) for path in server_scanner.scheduled_paths(paths) if os.path.normcase(os.path.normpath(path)) not in planned]
        return list(order) + rest

    def _run(self):
        try:
            while not self._stop.is_set():
                if self.pacer.stopped:
                    self._close_model()
                    self.pacer.wait_until_calm(self._stop)
                    continue
                try:
                    worked = self.scan_once() > 0
                except Exception:
                    log.exception("[CAPTION SCAN] pass failed")
                    worked = False
                if not worked:
                    # WARNING: with nothing to caption the model is let go, so it holds no memory while the scan waits.
                    self._close_model()
                    self._stop.wait(CAPTION_SCAN_IDLE_LOOK_S)
        finally:
            self._close_model()


__all__ = ["CaptionPacer", "CaptionScanner", "airing_order", "scan_plan", "next_midnight"]
