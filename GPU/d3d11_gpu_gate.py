"""
GPU gate for the GPU frame path: opens the D3D11 core and frame pipeline, proves them with a start-up self-check, and decides when the app falls back to the CPU path.

Owns: GateSettings, the gate states, opening and closing the core and pipeline, the self-check pictures and their comparison with
the NumPy reference, failure and device-lost counting, the retry cool-down, and one log line per state change.
Does not own: any D3D11 call (d3d11_gpu_core.py, d3d11_gpu_frame.py), any tunable number (performance_governor.py supplies them
through GateSettings), whether the person wants the GPU path (the caller checks the button and mode before acquire), or drawing
the real frame (the main script).

Main thread only, except the self-check preparation thread, which runs NumPy and the shader compiler and never touches D3D11 objects.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from dataclasses import dataclass

import numpy as np
import pygame

from d3d11_gpu_reference import (
    SCAN_BLOOM, SCAN_PLAIN, CompositePlan, FrameParams, Placement, PowerOn, ScalePlan, compare_frames,
    reference_composite, reference_frame, scale_supported,
)

log = logging.getLogger(__name__)

# ============================================================================
# SECTION: States and settings
# ============================================================================

# WARNING: must equal ADAPTER_POLICY_NAMED_PREFIX in d3d11_gpu_core.py; the core reads the policy text built here.
ADAPTER_POLICY_NAMED_PREFIX = "adapter:"

STATE_CLOSED = "closed"
STATE_READY = "ready"
STATE_COOLDOWN = "cooldown"
STATE_DISABLED = "disabled"


@dataclass(frozen=True)
class GateSettings:
    """The gate tunables, filled from performance_governor.gpu_gate_values()."""
    adapter_policy: str
    check_width: int
    check_height: int
    tolerance: int
    max_over_fraction: float
    failure_limit: int
    lost_limit: int
    retry_cooldown_s: float


@dataclass(frozen=True)
class Backend:
    """The D3D11 classes and error types the gate drives; tests pass a fake one."""
    core_class: object
    pipeline_class: object
    core_error: type
    lost_error: type
    frame_error: type
    precompile: object = None


def load_backend() -> Backend:
    """Imports the real Windows-only core and frame modules."""
    from d3d11_gpu_core import DeviceLostError, GPUCore, GPUCoreError
    from d3d11_gpu_frame import GPUFrameError, GPUFramePipeline, precompile_shaders
    return Backend(GPUCore, GPUFramePipeline, GPUCoreError, DeviceLostError, GPUFrameError, precompile_shaders)


class SelfCheckError(RuntimeError):
    """The GPU picture differs from the NumPy reference by more than the allowed share, or the check could not run."""


# ============================================================================
# SECTION: Self-check pictures
# ============================================================================


def _check_picture(width: int, height: int) -> np.ndarray:
    xs = np.linspace(0.0, 255.0, width, dtype=np.float32)[None, :]
    ys = np.linspace(0.0, 255.0, height, dtype=np.float32)[:, None]
    pic = np.empty((height, width, 3), dtype=np.uint8)
    pic[..., 0] = xs
    pic[..., 1] = ys
    pic[..., 2] = (xs + ys) * 0.5
    rng = np.random.RandomState(1234)
    bx, by, bw, bh = width // 8, height // 8, width // 4, height // 4
    pic[by:by + bh, bx:bx + bw] = 255
    pic[by + bh:by + 2 * bh, bx:bx + bw] = 0
    pic[height // 2:height // 2 + bh, width // 2:width // 2 + bw] = rng.randint(0, 256, (bh, bw, 3)).astype(np.uint8)
    pic[:, ::16] = (255, 255, 0)
    pic[height - bh:, width - bw:] = rng.randint(0, 256, (bh, bw, 3)).astype(np.uint8)
    return pic


def _overlay_rgba(width: int, height: int, red_first: bool) -> np.ndarray:
    rgba = np.empty((height, width, 4), dtype=np.uint8)
    ramp_x = np.linspace(0.0, 255.0, width, dtype=np.float32)[None, :]
    ramp_y = np.linspace(0.0, 255.0, height, dtype=np.float32)[:, None]
    rgba[..., 0] = ramp_x if red_first else ramp_y
    rgba[..., 1] = ramp_y if red_first else ramp_x
    rgba[..., 2] = 128
    rgba[..., 3] = ramp_x
    return rgba


def _surface_from_rgb(rgb: np.ndarray, template) -> pygame.Surface:
    height, width = rgb.shape[:2]
    surface = pygame.Surface((width, height), 0, template if template is not None else 32)
    pygame.surfarray.blit_array(surface, np.ascontiguousarray(rgb.transpose(1, 0, 2)))
    return surface


def _surface_from_rgba(rgba: np.ndarray) -> pygame.Surface:
    height, width = rgba.shape[:2]
    return pygame.image.frombuffer(rgba.tobytes(), (width, height), "RGBA").copy()


# ============================================================================
# SECTION: Self-check
# ============================================================================


@dataclass(frozen=True, eq=False)
class _CheckCase:
    name: str
    params: FrameParams
    plan: ScalePlan
    bar_color: tuple
    power_on: PowerOn | None
    overlays: bool
    bezel_place: Placement | None
    led_place: Placement | None
    bezel_rgba: np.ndarray | None
    led_rgba: np.ndarray | None
    expected: np.ndarray


@dataclass(frozen=True, eq=False)
class PreparedCheck:
    """The GPU-free half of the self-check: pictures, plans and the NumPy reference results for one window size and effect set."""
    key: tuple
    width: int
    height: int
    src_rgb: np.ndarray
    cases: tuple
    build_ms: float = 0.0
    shader_ms: float = 0.0


def self_check_key(width: int, height: int, effects: FrameParams, settings: GateSettings) -> tuple:
    """Returns the key under which a PreparedCheck is reusable: the window size, the effect values and the check picture size."""
    return (width, height, effects, settings.check_width, settings.check_height)


def prepare_self_check(width: int, height: int, effects: FrameParams, settings: GateSettings) -> PreparedCheck:
    """Builds the self-check pictures, plans and NumPy reference results without touching the GPU; raises SelfCheckError when a picture cannot be scaled."""
    started = time.perf_counter()
    bw, bh = width, height
    cw, ch = settings.check_width, settings.check_height
    src_rgb = _check_picture(cw, ch)
    inset_x, inset_y = bw // 8, bh // 8
    plan_inset = ScalePlan(0, 0, cw, ch, bw - 2 * inset_x, bh - 2 * inset_y, inset_x, inset_y)
    plan_full = ScalePlan(0, 0, cw, ch, bw, bh, 0, 0)
    for plan in (plan_inset, plan_full):
        if not scale_supported(plan):
            raise SelfCheckError("self-check picture %dx%d cannot be scaled to %dx%d" % (cw, ch, plan.draw_w, plan.draw_h))
    warp_params = dataclasses.replace(effects, width=cw, height=ch, warp=True, scan_mode=SCAN_BLOOM)
    plain_params = dataclasses.replace(effects, width=cw, height=ch, warp=False, scan_mode=SCAN_PLAIN)
    power_on = PowerOn(0, 0, bw, bh, bh // 4, (3 * bh) // 4, 60)
    bar_color = (20, 10, 30)
    cases = []
    for name, params, plan, case_power_on, overlays in (
            ("warp+bloom", warp_params, plan_inset, None, False),
            ("plain+overlays+power-on", plain_params, plan_full, power_on, True)):
        bezel_place = led_place = None
        bezel_rgba = led_rgba = None
        if overlays:
            bezel_place = Placement(-(bw // 20), bh // 10, max(8, bw // 3), max(8, bh // 3))
            led_place = Placement(bw // 2, bh // 2, max(8, bw // 8), max(4, bh // 12))
            bezel_rgba = _overlay_rgba(bezel_place.w, bezel_place.h, True)
            led_rgba = _overlay_rgba(led_place.w, led_place.h, False)
        visible = (plan.dest_x, plan.dest_y, plan.dest_x + plan.draw_w, plan.dest_y + plan.draw_h)
        composite = CompositePlan(plan, visible, bar_color, case_power_on, bezel_place, led_place)
        expected = reference_composite(reference_frame(src_rgb, params), composite, bw, bh, bezel_rgba, led_rgba)
        cases.append(_CheckCase(name, params, plan, bar_color, case_power_on, overlays, bezel_place, led_place,
                                bezel_rgba, led_rgba, expected))
    return PreparedCheck(self_check_key(width, height, effects, settings), width, height, src_rgb, tuple(cases),
                         (time.perf_counter() - started) * 1000.0)


def _run_case(core, pipeline, settings, case, surface):
    if case.overlays:
        pipeline.set_bezel(("self-check", "bezel"), _surface_from_rgba(case.bezel_rgba), case.bezel_place.x, case.bezel_place.y)
        pipeline.set_led(("self-check", "led"), _surface_from_rgba(case.led_rgba), case.led_place.x, case.led_place.y)
    try:
        pipeline.render(surface, case.params, case.plan, None, case.bar_color, case.power_on, draw_overlays=case.overlays)
        gpu = core.read_back_buffer()[:, :, 2::-1]
    finally:
        if case.overlays:
            pipeline.clear_bezel()
            pipeline.clear_led()
    result = compare_frames(gpu, case.expected, settings.tolerance, settings.max_over_fraction)
    result["name"] = case.name
    return result


def run_self_check(core, pipeline, effects: FrameParams, settings: GateSettings, template=None, prepared=None) -> list:
    """Draws two known pictures through the pipeline, reads each back and compares it with the NumPy reference; raises SelfCheckError on any mismatch."""
    bw, bh = core.size
    if prepared is None:
        prepared = prepare_self_check(bw, bh, effects, settings)
    elif (prepared.width, prepared.height) != (bw, bh):
        raise SelfCheckError("prepared self-check is for %dx%d but the window is %dx%d" % (prepared.width, prepared.height, bw, bh))
    surface = _surface_from_rgb(prepared.src_rgb, template)
    results = [_run_case(core, pipeline, settings, case, surface) for case in prepared.cases]
    failed = [r for r in results if not r["ok"]]
    if failed:
        raise SelfCheckError("; ".join("%s: max %d, mean %.3f, over tolerance %.5f" % (r["name"], r["max"], r["mean"], r["over_tolerance"])
                                       for r in failed))
    return results


# ============================================================================
# SECTION: Gate
# ============================================================================


class GPUGate:
    """Owns the GPU core and pipeline for the run and says whether the caller may draw with them this frame."""

    def __init__(self, settings: GateSettings, backend: Backend | None = None, clock=time.monotonic):
        self._settings = settings
        self._base_policy = settings.adapter_policy
        self._backend = backend
        self._clock = clock
        self._state = STATE_CLOSED
        self._core = None
        self._pipeline = None
        self._open_failures = 0
        self._frame_failures = 0
        self._lost_count = 0
        self._retry_at = 0.0
        self._disabled_reason = None
        self._prepared = None
        self._prep_thread = None
        self._prep_error = None

    # -- state ----------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def core(self):
        return self._core

    @property
    def pipeline(self):
        return self._pipeline

    @property
    def disabled_reason(self):
        return self._disabled_reason

    # -- opening ---------------------------------------------------------------
    def acquire(self, hwnd: int, width: int, height: int, effects: FrameParams, template=None):
        """Returns the frame pipeline when the GPU path may draw this frame, opening it (with the self-check) when needed; None means draw on the CPU."""
        if self._state == STATE_READY:
            return self._pipeline
        if self._state == STATE_DISABLED or self._clock() < self._retry_at:
            return None
        if not self._ensure_backend():
            return None
        prepared = self._prepared_check(width, height, effects)
        if prepared is None:
            return None
        self._open(hwnd, width, height, effects, template, prepared)
        return self._pipeline if self._state == STATE_READY else None

    def _ensure_backend(self) -> bool:
        if self._backend is not None:
            return True
        try:
            self._backend = load_backend()
        except ImportError as exc:
            self._disable("GPU modules unavailable: %s" % exc)
            return False
        return True

    def _prepared_check(self, width, height, effects):
        key = self_check_key(width, height, effects, self._settings)
        thread = self._prep_thread
        if thread is not None:
            if thread.is_alive():
                return None
            self._prep_thread = None
            error, self._prep_error = self._prep_error, None
            if error is not None:
                if isinstance(error, SelfCheckError):
                    self._disable("self-check failed: %s" % error)
                else:
                    self._disable("self-check reference could not be built: %s" % error)
                return None
        prepared = self._prepared
        if prepared is not None and prepared.key == key:
            return prepared
        self._prep_thread = threading.Thread(target=self._prepare_worker, args=(width, height, effects),
                                             name="GPUSelfCheckPrep", daemon=True)
        self._prep_thread.start()
        return None

    def _prepare_worker(self, width, height, effects):
        try:
            prepared = prepare_self_check(width, height, effects, self._settings)
            shader_ms = 0.0
            precompile = self._backend.precompile
            if precompile is not None:
                started = time.perf_counter()
                try:
                    precompile()
                    shader_ms = (time.perf_counter() - started) * 1000.0
                except Exception as exc:
                    log.warning("[GPU GATE] compiling the shaders in the background failed, the open will compile them: %s", exc)
            self._prepared = dataclasses.replace(prepared, shader_ms=shader_ms)
        except BaseException as exc:
            self._prep_error = exc

    def _open(self, hwnd, width, height, effects, template, prepared):
        backend = self._backend
        started = self._clock()
        core = pipeline = None
        try:
            core = backend.core_class(hwnd, width, height, self._settings.adapter_policy)
            core.probe()
            device_done = self._clock()
            pipeline = backend.pipeline_class(core)
            shaders_done = self._clock()
            results = run_self_check(core, pipeline, effects, self._settings, template, prepared)
        except SelfCheckError as exc:
            self._teardown(core, pipeline)
            self._disable("self-check failed: %s" % exc)
            return
        except backend.lost_error as exc:
            self._teardown(core, pipeline)
            self._note_lost("opening", exc)
            return
        except Exception as exc:
            self._teardown(core, pipeline)
            self._note_open_failure(exc)
            return
        self._core = core
        self._pipeline = pipeline
        self._state = STATE_READY
        self._open_failures = 0
        self._frame_failures = 0
        finished = self._clock()
        log.info("[GPU GATE] ready on %s; self-check %s; opened in %.0f ms on the main thread (device %.0f, shaders %.0f, check %.0f); "
                 "reference built in the background in %.0f ms, shaders compiled in %.0f ms",
                 core.describe(),
                 ", ".join("%s max %d mean %.3f" % (r["name"], r["max"], r["mean"]) for r in results),
                 (finished - started) * 1000.0, (device_done - started) * 1000.0, (shaders_done - device_done) * 1000.0,
                 (finished - shaders_done) * 1000.0, prepared.build_ms, prepared.shader_ms)

    # -- running -----------------------------------------------------------------
    def report_success(self) -> None:
        """Tells the gate the last GPU frame was drawn and presented, which clears the failed-frames-in-a-row count."""
        self._frame_failures = 0

    def report_failure(self, exc: BaseException, stage: str = "frame") -> None:
        """Counts a failed GPU frame; a lost device closes the GPU path for a cool-down, repeated failures switch it off for the run."""
        if self._state != STATE_READY:
            return
        if isinstance(exc, self._backend.lost_error):
            self._close_resources()
            self._note_lost(stage, exc)
            return
        self._frame_failures += 1
        log.warning("[GPU GATE] %s failed, drawing this frame on the CPU (%d of %d): %s",
                    stage, self._frame_failures, self._settings.failure_limit, exc)
        if self._frame_failures >= self._settings.failure_limit:
            self._disable("%d failed frames in a row, last: %s" % (self._frame_failures, exc))

    def resize(self, width: int, height: int) -> bool:
        """Resizes the swapchain; returns False (and counts a failure) when it could not."""
        if self._state != STATE_READY:
            return False
        try:
            self._core.resize(width, height)
        except Exception as exc:
            self.report_failure(exc, "resize")
            return False
        return True

    def present(self) -> bool:
        """Presents the back buffer; returns False (and counts a failure) when it could not."""
        if self._state != STATE_READY:
            return False
        try:
            self._core.present()
        except Exception as exc:
            self.report_failure(exc, "present")
            return False
        return True

    # -- failure policy ---------------------------------------------------------------
    def _note_open_failure(self, exc):
        self._open_failures += 1
        if self._open_failures >= self._settings.failure_limit:
            self._disable("could not open %d times in a row, last: %s" % (self._open_failures, exc))
            return
        self._cool_down("could not open (%d of %d): %s" % (self._open_failures, self._settings.failure_limit, exc))

    def _note_lost(self, stage, exc):
        self._lost_count += 1
        if self._lost_count >= self._settings.lost_limit:
            self._disable("device lost %d times, last during %s: %s" % (self._lost_count, stage, exc))
            return
        self._cool_down("device lost during %s (%d of %d): %s" % (stage, self._lost_count, self._settings.lost_limit, exc))

    def _cool_down(self, reason):
        self._state = STATE_COOLDOWN
        self._retry_at = self._clock() + self._settings.retry_cooldown_s
        log.warning("[GPU GATE] %s; drawing on the CPU, retrying in %.0f s", reason, self._settings.retry_cooldown_s)

    def _disable(self, reason):
        self._close_resources()
        self._state = STATE_DISABLED
        self._disabled_reason = reason
        log.warning("[GPU GATE] switched off for this run, drawing on the CPU: %s", reason)

    # -- teardown ----------------------------------------------------------------------
    @staticmethod
    def _teardown(core, pipeline) -> None:
        for name, obj in (("pipeline", pipeline), ("core", core)):
            if obj is None:
                continue
            try:
                obj.close()
            except Exception as exc:
                log.warning("[GPU GATE] closing the %s failed: %s", name, exc)

    def _close_resources(self) -> None:
        core, pipeline = self._core, self._pipeline
        self._core = None
        self._pipeline = None
        self._teardown(core, pipeline)

    def close(self) -> None:
        """Releases the GPU path; the next acquire opens it again unless the gate is switched off."""
        self._close_resources()
        if self._state in (STATE_READY, STATE_COOLDOWN):
            self._state = STATE_CLOSED

    def set_adapter(self, name) -> None:
        """Selects the GPU by its adapter name for the next open (an empty name goes back to the governor's policy); a changed choice closes the GPU path and gives it a fresh try."""
        policy = (ADAPTER_POLICY_NAMED_PREFIX + str(name)) if name else self._base_policy
        if policy == self._settings.adapter_policy:
            return
        self._settings = dataclasses.replace(self._settings, adapter_policy=policy)
        log.info("[GPU GATE] adapter choice changed to %s", name if name else "the governor policy (%s)" % policy)
        self.reset()

    def reset(self) -> None:
        """Closes the GPU path and clears every failure count and the switched-off state, for when the person turns the GPU button on again."""
        self._close_resources()
        self._state = STATE_CLOSED
        self._open_failures = 0
        self._frame_failures = 0
        self._lost_count = 0
        self._retry_at = 0.0
        self._disabled_reason = None
