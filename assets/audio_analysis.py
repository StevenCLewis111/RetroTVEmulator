"""Real-music analysis for the Standard and Performance visualizers.

Owns: decoding a song once with ffmpeg in a background thread, turning it into a
small per-frame track (overall level, 3 bands, 24-band spectrum, beats), caching
that track on disk, and looking up the frame for the song's current position.
Does not own: drawing (visualizers.py) or choosing the performance mode and the
song position (retro_tv_emulator.py).

WARNING: nothing here may run on the render thread except TrackAnalyzer.get() and
reactive_frame(); both only read memory. Decoding and disk reads stay on the worker.
"""
import collections
import hashlib
import logging
import os
import subprocess
import threading
import time

import numpy as np


# ============================================================================
# SECTION: Tunables
# ============================================================================

try:
    import performance_governor as _governor
except Exception as _governor_import_error:
    _governor = None
    logging.getLogger(__name__).warning("[VISUALIZER ANALYSIS] could not import performance_governor, using the fallback values written in audio_analysis.py: %s", _governor_import_error)

try:
    from performance_governor import log_swallowed as _log_swallowed
except Exception:
    def _log_swallowed(where, error):
        """Fallback used only when performance_governor cannot be imported: the failure line is lost, nothing else changes."""
        return None

# WARNING: raise ANALYSIS_VERSION whenever an AUDIO_ANALYSIS_ number of the analysis table in performance_governor.py changes, or cached tracks built with the old numbers are kept.
ANALYSIS_VERSION = 1
# WARNING: the fallback numbers below must match AUDIO_ANALYSIS_SAMPLE_RATE, AUDIO_ANALYSIS_FPS, AUDIO_ANALYSIS_FFT_SIZE, AUDIO_ANALYSIS_SPECTRUM_BINS, AUDIO_ANALYSIS_SPECTRUM_LOW_HZ, AUDIO_ANALYSIS_SPECTRUM_HIGH_HZ, AUDIO_ANALYSIS_BAND_LOW_TOP_HZ, AUDIO_ANALYSIS_BAND_MID_TOP_HZ, AUDIO_ANALYSIS_MAX_ANALYZED_SECONDS, AUDIO_ANALYSIS_CHUNK_FRAMES, AUDIO_ANALYSIS_BEAT_FLUX_BANDS, AUDIO_ANALYSIS_BEAT_REFRACTORY_FRAMES, AUDIO_ANALYSIS_BEAT_PEAK_RADIUS_FRAMES in performance_governor.py; they are used only when that module fails to import.
SAMPLE_RATE = _governor.AUDIO_ANALYSIS_SAMPLE_RATE if _governor is not None else 22050
FPS = _governor.AUDIO_ANALYSIS_FPS if _governor is not None else 25
FFT_SIZE = _governor.AUDIO_ANALYSIS_FFT_SIZE if _governor is not None else 2048
SPECTRUM_BINS = _governor.AUDIO_ANALYSIS_SPECTRUM_BINS if _governor is not None else 24
SPECTRUM_LOW_HZ = _governor.AUDIO_ANALYSIS_SPECTRUM_LOW_HZ if _governor is not None else 40.0
SPECTRUM_HIGH_HZ = _governor.AUDIO_ANALYSIS_SPECTRUM_HIGH_HZ if _governor is not None else 10000.0
BAND_LOW_TOP_HZ = _governor.AUDIO_ANALYSIS_BAND_LOW_TOP_HZ if _governor is not None else 250.0
BAND_MID_TOP_HZ = _governor.AUDIO_ANALYSIS_BAND_MID_TOP_HZ if _governor is not None else 2500.0
MAX_ANALYZED_SECONDS = _governor.AUDIO_ANALYSIS_MAX_ANALYZED_SECONDS if _governor is not None else 1500
CHUNK_FRAMES = _governor.AUDIO_ANALYSIS_CHUNK_FRAMES if _governor is not None else 1000
BEAT_FLUX_BANDS = _governor.AUDIO_ANALYSIS_BEAT_FLUX_BANDS if _governor is not None else 16
BEAT_REFRACTORY_FRAMES = _governor.AUDIO_ANALYSIS_BEAT_REFRACTORY_FRAMES if _governor is not None else 5
BEAT_PEAK_RADIUS_FRAMES = _governor.AUDIO_ANALYSIS_BEAT_PEAK_RADIUS_FRAMES if _governor is not None else 2
HOP = SAMPLE_RATE // FPS
BEAT_WINDOW_FRAMES = FPS
# WARNING: the fallback numbers below must match AUDIO_ANALYSIS_DECODE_TIMEOUT_SECONDS, AUDIO_ANALYSIS_FAILED_RETRY_SECONDS, AUDIO_ANALYSIS_MEMORY_TRACKS, AUDIO_ANALYSIS_MAX_QUEUED_TRACKS, AUDIO_ANALYSIS_MAX_CACHE_FILES, AUDIO_ANALYSIS_WORKER_IDLE_SECONDS in performance_governor.py; they are used only when that module fails to import.
DECODE_TIMEOUT_SECONDS = _governor.AUDIO_ANALYSIS_DECODE_TIMEOUT_SECONDS if _governor is not None else 240
FAILED_RETRY_SECONDS = _governor.AUDIO_ANALYSIS_FAILED_RETRY_SECONDS if _governor is not None else 600
MEMORY_TRACKS = _governor.AUDIO_ANALYSIS_MEMORY_TRACKS if _governor is not None else 4
MAX_QUEUED_TRACKS = _governor.AUDIO_ANALYSIS_MAX_QUEUED_TRACKS if _governor is not None else 3
MAX_CACHE_FILES = _governor.AUDIO_ANALYSIS_MAX_CACHE_FILES if _governor is not None else 400
WORKER_IDLE_SECONDS = _governor.AUDIO_ANALYSIS_WORKER_IDLE_SECONDS if _governor is not None else 30.0
# WARNING: the fallback numbers below must match AUDIO_ANALYSIS_NORMALISE_FLOOR_PERCENTILE, AUDIO_ANALYSIS_NORMALISE_CEILING_PERCENTILE, AUDIO_ANALYSIS_NORMALISE_MIN_SPAN_DB, AUDIO_ANALYSIS_SPECTRUM_GAMMA, AUDIO_ANALYSIS_LEVEL_GAMMA, AUDIO_ANALYSIS_BEAT_THRESHOLD_SPREAD, AUDIO_ANALYSIS_BEAT_THRESHOLD_FLOOR, AUDIO_ANALYSIS_BEAT_SCALE_PERCENTILE, AUDIO_ANALYSIS_BEAT_SCALE_SHARE, AUDIO_ANALYSIS_BEAT_MIN_STRENGTH in performance_governor.py; they are used only when that module fails to import.
NORMALISE_FLOOR_PERCENTILE = _governor.AUDIO_ANALYSIS_NORMALISE_FLOOR_PERCENTILE if _governor is not None else 5
NORMALISE_CEILING_PERCENTILE = _governor.AUDIO_ANALYSIS_NORMALISE_CEILING_PERCENTILE if _governor is not None else 98
NORMALISE_MIN_SPAN_DB = _governor.AUDIO_ANALYSIS_NORMALISE_MIN_SPAN_DB if _governor is not None else 6.0
SPECTRUM_GAMMA = _governor.AUDIO_ANALYSIS_SPECTRUM_GAMMA if _governor is not None else 1.3
LEVEL_GAMMA = _governor.AUDIO_ANALYSIS_LEVEL_GAMMA if _governor is not None else 1.2
BEAT_THRESHOLD_SPREAD = _governor.AUDIO_ANALYSIS_BEAT_THRESHOLD_SPREAD if _governor is not None else 1.0
BEAT_THRESHOLD_FLOOR = _governor.AUDIO_ANALYSIS_BEAT_THRESHOLD_FLOOR if _governor is not None else 0.004
BEAT_SCALE_PERCENTILE = _governor.AUDIO_ANALYSIS_BEAT_SCALE_PERCENTILE if _governor is not None else 95
BEAT_SCALE_SHARE = _governor.AUDIO_ANALYSIS_BEAT_SCALE_SHARE if _governor is not None else 0.5
BEAT_MIN_STRENGTH = _governor.AUDIO_ANALYSIS_BEAT_MIN_STRENGTH if _governor is not None else 0.3

_LOGGER = logging.getLogger(__name__)
_LOGGER.addHandler(logging.NullHandler())


# ============================================================================
# SECTION: Analysis maths
# ============================================================================


def _band_matrix():
    """Returns the (FFT bins x 24) averaging matrix for the log-spaced spectrum bands."""
    freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / SAMPLE_RATE)
    edges = np.geomspace(SPECTRUM_LOW_HZ, SPECTRUM_HIGH_HZ, SPECTRUM_BINS + 1)
    matrix = np.zeros((len(freqs), SPECTRUM_BINS), dtype=np.float32)
    for k in range(SPECTRUM_BINS):
        members = np.where((freqs >= edges[k]) & (freqs < edges[k + 1]))[0]
        if len(members) == 0:
            centre = (edges[k] + edges[k + 1]) / 2.0
            members = np.array([int(np.argmin(np.abs(freqs - centre)))])
        matrix[members, k] = 1.0 / len(members)
    return matrix


def _three_band_matrix():
    """Returns the (FFT bins x 3) averaging matrix for the low, mid and high bands."""
    freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / SAMPLE_RATE)
    limits = [(SPECTRUM_LOW_HZ, BAND_LOW_TOP_HZ), (BAND_LOW_TOP_HZ, BAND_MID_TOP_HZ),
              (BAND_MID_TOP_HZ, SPECTRUM_HIGH_HZ)]
    matrix = np.zeros((len(freqs), 3), dtype=np.float32)
    for k, (low, high) in enumerate(limits):
        members = np.where((freqs >= low) & (freqs < high))[0]
        matrix[members, k] = 1.0 / max(1, len(members))
    return matrix


def _normalise_db(values_db, floor_percentile, ceiling_percentile, min_span_db):
    """Maps decibel columns to 0..1 using each column's own quiet and loud percentiles, so a quiet band still moves."""
    low = np.percentile(values_db, floor_percentile, axis=0)
    high = np.percentile(values_db, ceiling_percentile, axis=0)
    high = np.maximum(high, low + min_span_db)
    return np.clip((values_db - low) / (high - low), 0.0, 1.0)


def _detect_beats(spectrum_norm):
    """Returns a 0..1 strength per frame, non-zero only on frames where a beat lands."""
    frames = spectrum_norm.shape[0]
    beats = np.zeros(frames, dtype=np.float32)
    if frames < 4:
        return beats
    rises = np.maximum(0.0, np.diff(spectrum_norm[:, :BEAT_FLUX_BANDS], axis=0))
    flux = np.concatenate([[0.0], rises.sum(axis=1) / BEAT_FLUX_BANDS])
    window = np.ones(BEAT_WINDOW_FRAMES) / BEAT_WINDOW_FRAMES
    local_mean = np.convolve(flux, window, mode="same")
    local_var = np.convolve((flux - local_mean) ** 2, window, mode="same")
    threshold = local_mean + np.sqrt(local_var) * BEAT_THRESHOLD_SPREAD + BEAT_THRESHOLD_FLOOR
    scale = max(1e-6, float(np.percentile(flux, BEAT_SCALE_PERCENTILE)) * BEAT_SCALE_SHARE)
    last_beat = -BEAT_REFRACTORY_FRAMES
    for i in range(frames):
        if flux[i] <= threshold[i]:
            continue
        lo, hi = max(0, i - BEAT_PEAK_RADIUS_FRAMES), min(frames, i + BEAT_PEAK_RADIUS_FRAMES + 1)
        if flux[i] < flux[lo:hi].max():
            continue
        if i - last_beat < BEAT_REFRACTORY_FRAMES:
            continue
        beats[i] = min(1.0, max(BEAT_MIN_STRENGTH, BEAT_MIN_STRENGTH + (flux[i] - threshold[i]) / scale))
        last_beat = i
    return beats


def analyze_samples(samples):
    """Turns mono float samples at SAMPLE_RATE into the per-frame arrays (uint8, 0..255), or None when the audio is too short."""
    if len(samples) < FFT_SIZE + HOP * 4:
        return None
    frame_count = (len(samples) - FFT_SIZE) // HOP + 1
    window = np.hanning(FFT_SIZE).astype(np.float32)
    spectrum_matrix = _band_matrix()
    band_matrix = _three_band_matrix()
    view = np.lib.stride_tricks.sliding_window_view(samples, FFT_SIZE)[::HOP]

    spectrum_db_parts, band_db_parts, rms_parts = [], [], []
    for start in range(0, frame_count, CHUNK_FRAMES):
        chunk = np.ascontiguousarray(view[start:start + CHUNK_FRAMES])
        rms_parts.append(np.sqrt(np.mean(chunk * chunk, axis=1)))
        magnitude = np.abs(np.fft.rfft(chunk * window, axis=1)).astype(np.float32) / FFT_SIZE
        spectrum_db_parts.append(20.0 * np.log10(magnitude @ spectrum_matrix + 1e-7))
        band_db_parts.append(20.0 * np.log10(magnitude @ band_matrix + 1e-7))

    spectrum_db = np.concatenate(spectrum_db_parts)
    band_db = np.concatenate(band_db_parts)
    level_db = 20.0 * np.log10(np.concatenate(rms_parts) + 1e-7)

    spectrum_norm = _normalise_db(spectrum_db, NORMALISE_FLOOR_PERCENTILE, NORMALISE_CEILING_PERCENTILE, NORMALISE_MIN_SPAN_DB)
    band_norm = _normalise_db(band_db, NORMALISE_FLOOR_PERCENTILE, NORMALISE_CEILING_PERCENTILE, NORMALISE_MIN_SPAN_DB)
    level_norm = _normalise_db(level_db[:, None], NORMALISE_FLOOR_PERCENTILE, NORMALISE_CEILING_PERCENTILE, NORMALISE_MIN_SPAN_DB)[:, 0]
    beats = _detect_beats(spectrum_norm)

    def quantise(values, gamma):
        return np.round(np.clip(values, 0.0, 1.0) ** gamma * 255.0).astype(np.uint8)

    return {
        "spectrum": quantise(spectrum_norm, SPECTRUM_GAMMA),
        "bands": quantise(band_norm, SPECTRUM_GAMMA),
        "level": quantise(level_norm, LEVEL_GAMMA),
        "beat": quantise(beats, 1.0),
    }


# ============================================================================
# SECTION: Track data and frame lookup
# ============================================================================


class TrackAnalysis:
    """One analyzed song: per-frame level, 3 bands, 24-band spectrum and beat strength, all 0..1 floats."""

    def __init__(self, arrays):
        self.spectrum = arrays["spectrum"].astype(np.float32) / 255.0
        self.bands = arrays["bands"].astype(np.float32) / 255.0
        self.level = arrays["level"].astype(np.float32) / 255.0
        self.beat = arrays["beat"].astype(np.float32) / 255.0
        self.frames = int(self.level.shape[0])
        self.duration = self.frames / float(FPS)


def reactive_frame(analysis, position_seconds, state, full):
    """Returns the music numbers for one render frame, or None when the position is outside the analyzed audio.

    state is a dict the caller keeps for one visualizer owner; it remembers the last frame so a beat that lands
    between two render frames is reported once and never twice. full=True adds the 24-band spectrum (Performance);
    False leaves it None (Standard uses the 3 bands).
    """
    if analysis is None or position_seconds is None or analysis.frames < 2:
        return None
    x = float(position_seconds) * FPS
    if x < 0.0 or x > analysis.frames - 1:
        return None
    i0 = int(x)
    i1 = min(analysis.frames - 1, i0 + 1)
    f = x - i0
    last = state.get("idx", -1)
    if last < 0 or i0 < last or i0 - last > FPS or i0 == last:
        beat = 0.0
    else:
        beat = float(analysis.beat[last + 1:i0 + 1].max())
    state["idx"] = i0

    def lerp(array):
        return array[i0] + (array[i1] - array[i0]) * f

    return {
        "level": float(lerp(analysis.level)),
        "bands": tuple(float(v) for v in lerp(analysis.bands)),
        "spectrum": [float(v) for v in lerp(analysis.spectrum)] if full else None,
        "beat": beat,
        "full": bool(full),
    }


# ============================================================================
# SECTION: Disk cache and ffmpeg decode
# ============================================================================


def _cache_file(cache_dir, path):
    stat = os.stat(path)
    key = "%s|%d|%d|%d" % (os.path.normpath(path), stat.st_size, stat.st_mtime_ns, ANALYSIS_VERSION)
    return os.path.join(cache_dir, "viz_%s.npz" % hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:24])


def _load_cached(file_path):
    """Returns the cached arrays of one song, or None when it is not cached yet or the cache file cannot be used (the song is then decoded again and the file replaced)."""
    if not os.path.exists(file_path):
        return None
    try:
        with np.load(file_path) as data:
            if int(data["version"]) != ANALYSIS_VERSION:
                return None
            arrays = {name: data[name] for name in ("spectrum", "bands", "level", "beat")}
    except Exception as error:
        _LOGGER.warning("[VISUALIZER ANALYSIS] cache file %s could not be read, the song is analyzed again: %s: %s",
                        os.path.basename(file_path), type(error).__name__, error)
        return None
    frames = arrays["level"].shape[0]
    if (arrays["spectrum"].shape != (frames, SPECTRUM_BINS) or arrays["bands"].shape != (frames, 3)
            or arrays["beat"].shape[0] != frames):
        return None
    return arrays


def _save_cached(file_path, arrays):
    temp_path = file_path[:-4] + ".tmp.npz"
    np.savez_compressed(temp_path, version=np.array(ANALYSIS_VERSION), **arrays)
    os.replace(temp_path, file_path)


def _prune_cache(cache_dir):
    try:
        entries = [os.path.join(cache_dir, n) for n in os.listdir(cache_dir) if n.startswith("viz_") and n.endswith(".npz")]
        if len(entries) <= MAX_CACHE_FILES:
            return
        entries.sort(key=os.path.getmtime)
        for stale in entries[:len(entries) - MAX_CACHE_FILES]:
            os.remove(stale)
    except OSError as error:
        _LOGGER.warning("visualizer analysis cache prune failed: %s", error)


def decode_samples(ffmpeg_path, path):
    """Decodes a song to mono float32 samples at SAMPLE_RATE with one low-priority ffmpeg thread; raises on failure."""
    command = [ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path, "-vn",
               "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE), "-t", str(MAX_ANALYZED_SECONDS),
               "-threads", "1", "-f", "s16le", "-"]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000)
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=DECODE_TIMEOUT_SECONDS, creationflags=flags)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError("ffmpeg decode failed (exit %s): %s" % (
            result.returncode, result.stderr.decode("utf-8", "replace").strip()[-200:]))
    return np.frombuffer(result.stdout, dtype="<i2").astype(np.float32) / 32768.0


# ============================================================================
# SECTION: Background analyzer
# ============================================================================


class TrackAnalyzer:
    """Analyzes songs one at a time on a background thread, the song playing now before any read-ahead song; get() never blocks and returns None until a song is ready."""

    def __init__(self, ffmpeg_path_fn, cache_dir_fn, log=None):
        self._ffmpeg_path_fn = ffmpeg_path_fn
        self._cache_dir_fn = cache_dir_fn
        self._log = log or _LOGGER
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._memory = collections.OrderedDict()
        self._queue = collections.deque()
        self._ahead = collections.deque()
        self._failed = {}
        self._thread = None

    def get(self, path):
        """Returns the TrackAnalysis for path when it is ready, else queues it (newest first) and returns None."""
        if not path:
            return None
        key = os.path.normpath(path)
        with self._lock:
            ready = self._memory.get(key)
            if ready is not None:
                self._memory.move_to_end(key)
                return ready
            failed_at = self._failed.get(key)
            if failed_at is not None and time.monotonic() - failed_at < FAILED_RETRY_SECONDS:
                return None
            if key in self._queue:
                return None
            self._queue.appendleft(key)
            while len(self._queue) > MAX_QUEUED_TRACKS:
                self._queue.pop()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="visualizer-analysis", daemon=True)
                self._thread.start()
        self._wake.set()
        return None

    def is_analyzed(self, path):
        """True when the song is in memory, already in the disk cache, or failed within FAILED_RETRY_SECONDS (nothing to do for it now); reads only the file's size and time, never the cache file."""
        if not path:
            return False
        key = os.path.normpath(path)
        with self._lock:
            if key in self._memory:
                return True
            failed_at = self._failed.get(key)
            if failed_at is not None and time.monotonic() - failed_at < FAILED_RETRY_SECONDS:
                return True
        try:
            return os.path.exists(_cache_file(os.path.join(self._cache_dir_fn(), "visualizer_analysis"), key))
        except OSError as error:
            self._log.warning("[VISUALIZER ANALYSIS] could not check the analysis cache for %s: %s", os.path.basename(key), error)
            return False

    def analyze_ahead(self, items):
        """Replaces the read-ahead list with items, (path, seconds to wait after it), in the order they will play; read-ahead songs go to the disk cache only and always wait behind the song playing now."""
        with self._lock:
            was_idle = not self._ahead
            self._ahead = collections.deque((os.path.normpath(p), max(0.0, float(w))) for p, w in items if p)
            if not self._ahead:
                return
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="visualizer-analysis", daemon=True)
                self._thread.start()
        # WARNING: waking a busy worker would cut its pause between read-ahead songs short; only an idle worker is woken.
        if was_idle:
            self._wake.set()

    def _worker(self):
        while True:
            wait_after = 0.0
            with self._lock:
                key = self._queue.popleft() if self._queue else None
                ahead = False
                if key is None and self._ahead:
                    key, wait_after = self._ahead.popleft()
                    ahead = True
            if key is None:
                self._wake.clear()
                if not self._wake.wait(timeout=WORKER_IDLE_SECONDS):
                    with self._lock:
                        if not self._queue and not self._ahead:
                            self._thread = None
                            return
                continue
            worked = self._process(key, keep=not ahead)
            if ahead and worked and wait_after > 0.0:
                with self._lock:
                    live_waiting = bool(self._queue)
                if not live_waiting:
                    self._wake.clear()
                    self._wake.wait(timeout=wait_after)

    def _process(self, key, keep=True):
        """Analyzes one song from the disk cache or with ffmpeg; keep=False stores it in the disk cache only. False when there was nothing to do."""
        started = time.monotonic()
        stage = "starting"
        try:
            stage = "opening cache directory"
            cache_dir = os.path.join(self._cache_dir_fn(), "visualizer_analysis")
            os.makedirs(cache_dir, exist_ok=True)
            stage = "building cache key"
            cache_path = _cache_file(cache_dir, key)
            if not keep:
                with self._lock:
                    if key in self._memory:
                        return False
                if os.path.exists(cache_path):
                    return False
            stage = "reading cache"
            arrays = _load_cached(cache_path)
            source = "cache"
            if arrays is None:
                source = "ffmpeg"
                stage = "decoding with ffmpeg"
                arrays = analyze_samples(decode_samples(self._ffmpeg_path_fn(), key))
                if arrays is None:
                    raise RuntimeError("audio too short to analyze")
                try:
                    _save_cached(cache_path, arrays)
                except Exception as error:
                    self._log.warning("[VISUALIZER ANALYSIS] could not write the analysis cache for %s (kept in memory only): %s: %s",
                                      os.path.basename(key), type(error).__name__, error)
                _prune_cache(cache_dir)
            stage = "building track"
            analysis = TrackAnalysis(arrays)
        except subprocess.TimeoutExpired:
            self._log.warning("[VISUALIZER ANALYSIS] failed at %s for %s: ffmpeg timed out after %ss",
                              stage, os.path.basename(key), DECODE_TIMEOUT_SECONDS)
            with self._lock:
                self._failed[key] = time.monotonic()
            return True
        except Exception as error:
            self._log.warning("[VISUALIZER ANALYSIS] failed at %s for %s after %.1fs: %s: %s",
                              stage, os.path.basename(key), time.monotonic() - started,
                              type(error).__name__, error)
            with self._lock:
                self._failed[key] = time.monotonic()
            return True
        if not keep:
            self._log.info("[VISUALIZER ANALYSIS] %s analyzed ahead from %s in %.1fs (%d frames)",
                           os.path.basename(key), source, time.monotonic() - started, analysis.frames)
            return True
        with self._lock:
            self._memory[key] = analysis
            while len(self._memory) > MEMORY_TRACKS:
                self._memory.popitem(last=False)
        self._log.info("[VISUALIZER ANALYSIS] %s ready from %s in %.1fs (%d frames)",
                       os.path.basename(key), source, time.monotonic() - started, analysis.frames)
        return True


__all__ = ["TrackAnalyzer", "TrackAnalysis", "reactive_frame", "analyze_samples", "decode_samples"]
