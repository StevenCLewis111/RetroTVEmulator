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

# VALUES CHANGE HERE (bump ANALYSIS_VERSION after changing any number below so cached tracks are rebuilt)
ANALYSIS_VERSION = 1
SAMPLE_RATE = 22050
FPS = 25
HOP = SAMPLE_RATE // FPS
FFT_SIZE = 2048
SPECTRUM_BINS = 24
SPECTRUM_LOW_HZ = 40.0
SPECTRUM_HIGH_HZ = 10000.0
BAND_LOW_TOP_HZ = 250.0
BAND_MID_TOP_HZ = 2500.0
MAX_ANALYZED_SECONDS = 1500
DECODE_TIMEOUT_SECONDS = 240
FAILED_RETRY_SECONDS = 600
MEMORY_TRACKS = 4
MAX_QUEUED_TRACKS = 3
MAX_CACHE_FILES = 400
CHUNK_FRAMES = 1000
BEAT_FLUX_BANDS = 16
BEAT_WINDOW_FRAMES = FPS
BEAT_REFRACTORY_FRAMES = 5
BEAT_PEAK_RADIUS_FRAMES = 2

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
    threshold = local_mean + np.sqrt(local_var) * 1.0 + 0.004
    scale = max(1e-6, float(np.percentile(flux, 95)) * 0.5)
    last_beat = -BEAT_REFRACTORY_FRAMES
    for i in range(frames):
        if flux[i] <= threshold[i]:
            continue
        lo, hi = max(0, i - BEAT_PEAK_RADIUS_FRAMES), min(frames, i + BEAT_PEAK_RADIUS_FRAMES + 1)
        if flux[i] < flux[lo:hi].max():
            continue
        if i - last_beat < BEAT_REFRACTORY_FRAMES:
            continue
        beats[i] = min(1.0, max(0.3, 0.3 + (flux[i] - threshold[i]) / scale))
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

    spectrum_norm = _normalise_db(spectrum_db, 5, 98, 6.0)
    band_norm = _normalise_db(band_db, 5, 98, 6.0)
    level_norm = _normalise_db(level_db[:, None], 5, 98, 6.0)[:, 0]
    beats = _detect_beats(spectrum_norm)

    def quantise(values, gamma):
        return np.round(np.clip(values, 0.0, 1.0) ** gamma * 255.0).astype(np.uint8)

    return {
        "spectrum": quantise(spectrum_norm, 1.3),
        "bands": quantise(band_norm, 1.3),
        "level": quantise(level_norm, 1.2),
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
    try:
        with np.load(file_path) as data:
            if int(data["version"]) != ANALYSIS_VERSION:
                return None
            arrays = {name: data[name] for name in ("spectrum", "bands", "level", "beat")}
    except (OSError, KeyError, ValueError):
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
    """Analyzes songs one at a time on a background thread; get() never blocks and returns None until a song is ready."""

    def __init__(self, ffmpeg_path_fn, cache_dir_fn, log=None):
        self._ffmpeg_path_fn = ffmpeg_path_fn
        self._cache_dir_fn = cache_dir_fn
        self._log = log or _LOGGER
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._memory = collections.OrderedDict()
        self._queue = collections.deque()
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

    def _worker(self):
        while True:
            with self._lock:
                key = self._queue.popleft() if self._queue else None
            if key is None:
                self._wake.clear()
                if not self._wake.wait(timeout=30.0):
                    with self._lock:
                        if not self._queue:
                            self._thread = None
                            return
                continue
            self._process(key)

    def _process(self, key):
        started = time.monotonic()
        stage = "starting"
        try:
            stage = "opening cache directory"
            cache_dir = os.path.join(self._cache_dir_fn(), "visualizer_analysis")
            os.makedirs(cache_dir, exist_ok=True)
            stage = "building cache key"
            cache_path = _cache_file(cache_dir, key)
            stage = "reading cache"
            arrays = _load_cached(cache_path)
            source = "cache"
            if arrays is None:
                source = "ffmpeg"
                stage = "decoding with ffmpeg"
                arrays = analyze_samples(decode_samples(self._ffmpeg_path_fn(), key))
                if arrays is None:
                    raise RuntimeError("audio too short to analyze")
                stage = "writing cache"
                _save_cached(cache_path, arrays)
                stage = "pruning cache"
                _prune_cache(cache_dir)
            stage = "building track"
            analysis = TrackAnalysis(arrays)
        except subprocess.TimeoutExpired:
            self._log.warning("[VISUALIZER ANALYSIS] failed at %s for %s: ffmpeg timed out after %ss",
                              stage, os.path.basename(key), DECODE_TIMEOUT_SECONDS)
            with self._lock:
                self._failed[key] = time.monotonic()
            return
        except Exception as error:
            self._log.warning("[VISUALIZER ANALYSIS] failed at %s for %s after %.1fs: %s: %s",
                              stage, os.path.basename(key), time.monotonic() - started,
                              type(error).__name__, error)
            with self._lock:
                self._failed[key] = time.monotonic()
            return
        with self._lock:
            self._memory[key] = analysis
            while len(self._memory) > MEMORY_TRACKS:
                self._memory.popitem(last=False)
        self._log.info("[VISUALIZER ANALYSIS] %s ready from %s in %.1fs (%d frames)",
                       os.path.basename(key), source, time.monotonic() - started, analysis.frames)


__all__ = ["TrackAnalyzer", "TrackAnalysis", "reactive_frame", "analyze_samples", "decode_samples"]
