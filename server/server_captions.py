"""The server's caption scan: once the media scan is done, the server captions the files its channels air soonest (today, then tomorrow), each file once, and saves the text in main/saved captions, where the program's live captions and every User find it.

Owns: the order of files to caption, captioning one file in short pieces, and the pacing that keeps the scan from ever hurting playback on this computer.
Does not own: the caption model and its helpers (media.py), the saved text (saved_captions.py) or sending it to Users (server_sync.py).

WARNING: owner 2026-11-24: "the server computer ... after scanning all media is done, starts a captions scan for each show in advance of when it plays", "with captions on or off", commercials included, each file once; a file with real subtitles or burned-in captions is never scanned.
WARNING: owner 2026-11-24: "do not let those captions mess up any playback of this program on any computer ... make the captions background scans as slow as they need to be on each computer as needed. if the computer is struggling with slowest scans, stop the scan"; CaptionPacer decides every rest and every stop, and counts other programs on the PC through the whole-PC load.
WARNING: owner 2026-11-24: "when the app is closed on the server computer, it uses that extra that was running the app, and put it into the scans unless throttled"; the caption model runs with more threads while the program is closed and is started again with fewer when it opens.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time

import server_clock
import server_scanner
import station_clock

log = logging.getLogger(__name__)

# VALUES CHANGE HERE (mirrored in performance_governor.py): the caption scan's pieces of audio (seconds), how far ahead it looks at the schedule (hours), how often it works its order out again (seconds), and how often it looks again while it has nothing to do (seconds).
CAPTION_SCAN_CHUNK_SEC = 60.0
CAPTION_SCAN_LOOK_AHEAD_HOURS = 48.0
CAPTION_SCAN_REORDER_S = 300.0
CAPTION_SCAN_IDLE_LOOK_S = 60.0
# VALUES CHANGE HERE (mirrored in performance_governor.py): the rest after each piece, as a share of the time the piece took: with the program closed, and with it open by its performance mode; streaming to a User multiplies it by SERVER_SCAN_STREAMING_PAUSE_FACTOR.
CAPTION_SCAN_REST_CLOSED = 0.25
CAPTION_SCAN_REST_OPEN = {"lite": 8.0, "standard": 3.0, "performance": 1.5}
# VALUES CHANGE HERE (mirrored in performance_governor.py): the PC counts as struggling when the whole PC (every program on it) is busier than this percent, or the open program's throttle level is at least this; each struggling piece doubles the rest up to this many times, each calm one halves it again.
CAPTION_SCAN_BUSY_CPU_PERCENT = 70.0
CAPTION_SCAN_STRAIN_LEVEL = 1
CAPTION_SCAN_BACKOFF_MAX = 16.0
# VALUES CHANGE HERE (mirrored in performance_governor.py): the scan stops when the PC is still struggling after this many pieces at the slowest pace, and starts again (at the slowest pace) once the PC has been calm this many seconds in a row; it checks the PC this often (seconds) while it waits.
CAPTION_SCAN_STOP_AFTER_STRAINED = 3
CAPTION_SCAN_CALM_TO_RESUME_S = 600.0
CAPTION_SCAN_CHECK_S = 5.0
# VALUES CHANGE HERE (mirrored in performance_governor.py): the scan stays at least this many seconds ahead of the picture: a stretch the picture reaches sooner is left to live captions, and a scan the schedule has caught up with jumps to tomorrow's airings.
CAPTION_SCAN_MIN_LEAD_SECONDS = 1800.0
# VALUES CHANGE HERE (mirrored in performance_governor.py): caption model threads while the program is open, and the cores left free while it is closed (threads = logical cores - this, at least 1).
CAPTION_SCAN_THREADS_APP_OPEN = 1
CAPTION_SCAN_THREADS_CLOSED_RESERVED = 1
# VALUES CHANGE HERE (mirrored in performance_governor.py): how the language model helps the server's scan ("every" stretch of speech is checked, because the server's captions are shared with every PC whatever its mode), and how long (seconds) the scan waits for it to load before its first piece.
CAPTION_SCAN_LANGUAGE_CHECK = "every"
CAPTION_SCAN_LANGUAGE_WAIT_S = 120.0
# VALUES CHANGE HERE (mirrored in performance_governor.py): whether the server's scan tags sounds between the speech ([dog barking], [whistling]).
CAPTION_SCAN_SOUND_TAGS = True
try:
    import performance_governor as _gov
    CAPTION_SCAN_CHUNK_SEC = float(_gov.CAPTION_SCAN_CHUNK_SEC)
    CAPTION_SCAN_LOOK_AHEAD_HOURS = float(_gov.CAPTION_SCAN_LOOK_AHEAD_HOURS)
    CAPTION_SCAN_REORDER_S = float(_gov.CAPTION_SCAN_REORDER_S)
    CAPTION_SCAN_IDLE_LOOK_S = float(_gov.CAPTION_SCAN_IDLE_LOOK_S)
    CAPTION_SCAN_REST_CLOSED = float(_gov.CAPTION_SCAN_REST_CLOSED)
    CAPTION_SCAN_REST_OPEN = dict(_gov.CAPTION_SCAN_REST_OPEN)
    CAPTION_SCAN_BUSY_CPU_PERCENT = float(_gov.CAPTION_SCAN_BUSY_CPU_PERCENT)
    CAPTION_SCAN_STRAIN_LEVEL = int(_gov.CAPTION_SCAN_STRAIN_LEVEL)
    CAPTION_SCAN_BACKOFF_MAX = float(_gov.CAPTION_SCAN_BACKOFF_MAX)
    CAPTION_SCAN_STOP_AFTER_STRAINED = int(_gov.CAPTION_SCAN_STOP_AFTER_STRAINED)
    CAPTION_SCAN_CALM_TO_RESUME_S = float(_gov.CAPTION_SCAN_CALM_TO_RESUME_S)
    CAPTION_SCAN_CHECK_S = float(_gov.CAPTION_SCAN_CHECK_S)
    CAPTION_SCAN_MIN_LEAD_SECONDS = float(_gov.CAPTION_SCAN_MIN_LEAD_SECONDS)
    CAPTION_SCAN_THREADS_APP_OPEN = int(_gov.CAPTION_SCAN_THREADS_APP_OPEN)
    CAPTION_SCAN_THREADS_CLOSED_RESERVED = int(_gov.CAPTION_SCAN_THREADS_CLOSED_RESERVED)
    CAPTION_SCAN_LANGUAGE_CHECK = str(_gov.CAPTION_SCAN_LANGUAGE_CHECK)
    CAPTION_SCAN_LANGUAGE_WAIT_S = float(_gov.CAPTION_SCAN_LANGUAGE_WAIT_S)
    CAPTION_SCAN_SOUND_TAGS = bool(_gov.CAPTION_SCAN_SOUND_TAGS)
except Exception:
    pass


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
        except Exception:
            return default

    def program_open(self):
        return bool(self._call(self.program_open_fn, False))

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


class CaptionScanner:
    """Background thread of the server service: waits until the media scan is done, then captions the files in the order scan_plan gives (staying CAPTION_SCAN_MIN_LEAD_SECONDS ahead of the picture, jumping to tomorrow when the schedule catches up), one at a time, each in pieces of CAPTION_SCAN_CHUNK_SEC, saving each piece as it is done, so a User gets the start of a long show before its end is captioned. A file already saved whole, one with its own captions, one with none of its audio readable, and one a User is captioning live are passed over."""

    def __init__(self, slots_fn, probe_library_fn, facts_scanner, pacer, live_keys_fn=None):
        self.slots_fn = slots_fn
        self.probe_library_fn = probe_library_fn
        self.facts_scanner = facts_scanner
        self.pacer = pacer
        self.live_keys_fn = live_keys_fn
        self._stop = threading.Event()
        self._thread = None
        self._model = None
        self._model_threads = None
        self._passed_over = {}
        self._caught_up = False

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
        if self._model is not None and self._model_threads == want and not getattr(self._model, "dead", False):
            return self._model
        self._close_model()
        if not media._caption_memory_allows_load(notify=False):
            return None
        model = media._start_caption_model_process(want)
        if model is not None:
            self._model, self._model_threads = model, want
            # WARNING: the language model is started and waited for (up to CAPTION_SCAN_LANGUAGE_WAIT_S) before the first piece, so the scan's first pieces are made at its full grade instead of being made twice.
            media.start_language_model(CAPTION_SCAN_LANGUAGE_CHECK)
            media.wait_for_language_model(CAPTION_SCAN_LANGUAGE_WAIT_S)
            log.info("[CAPTION SCAN] caption model ready with %d thread(s) (the program is %s)", want, "open" if self.pacer.program_open() else "closed")
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
        if saved and saved_captions.is_complete(saved_captions.covered_at_least(saved, self.aim_grade()), saved.get("length")):
            return None
        return key, saved

    def aim_grade(self):
        """The grade this scan makes (saved_captions.GRADES of CAPTION_SCAN_LANGUAGE_CHECK), or the lowest once the language model could not load here, so a stretch the scan cannot make better is never scanned again and again."""
        import media
        import saved_captions
        if media.language_model_failed():
            return saved_captions.LOWEST_GRADE
        return saved_captions.grade_for(CAPTION_SCAN_LANGUAGE_CHECK)

    def _left_to_live(self, key, air_at, pos):
        """True when this piece is no longer the scan's to make: a User started captioning the file live, or the picture reaches it (air_at + pos) sooner than CAPTION_SCAN_MIN_LEAD_SECONDS from now."""
        if self.live_keys_fn is not None:
            try:
                if key in set(self.live_keys_fn() or ()):
                    return True
            except Exception:
                pass
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
                                                               language_check=CAPTION_SCAN_LANGUAGE_CHECK, sound_tags=CAPTION_SCAN_SOUND_TAGS)
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
                    except OSError:
                        pass
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
        if not getattr(self.facts_scanner, "idle", False):
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
            except Exception:
                return False
        order, caught_up = scan_plan(airing_order(hub, slots, now), now, _needed)
        if caught_up and not self._caught_up:
            log.info("[CAPTION SCAN] the schedule has caught up with the caption scan: what airs before midnight is left to live captions and the scan jumps to tomorrow")
        self._caught_up = caught_up
        for air_at, path in order:
            if self._stop.is_set() or self.pacer.stopped or time.monotonic() - started >= CAPTION_SCAN_REORDER_S:
                break
            try:
                wanted = self._wants(probe, path, live_keys)
            except Exception as exc:
                log.warning("[CAPTION SCAN] could not check %s: %s", os.path.basename(path), exc)
                continue
            if wanted is None:
                continue
            done += self.caption_file(path, wanted[0], wanted[1], air_at)
        return done

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
