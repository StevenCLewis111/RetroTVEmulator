"""
Reference math for the GPU frame shaders: one definition of the constant-buffer layouts and a NumPy twin of both shader passes.

Owns: FrameParams and pack_constants() (pass 1: warp, vignette, scan lines), ScalePlan, CompositePlan and
pack_composite_constants() (pass 2: scale into the bezel cutout, power-on effect, bezel image, LED readout), power_on_plan(),
reference_frame(), reference_scale() and reference_composite() (the shader formulas in NumPy), scale_supported() and
compare_frames() (the checks used by the GPU frame module and the GPU self-check).
Does not own: any D3D11 call, any pygame surface, any tunable number (the caller passes them in).
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass

import numpy as np

# ============================================================================
# SECTION: Pass 1 parameters (warp, vignette, scan lines)
# ============================================================================

SCAN_NONE = 0
SCAN_PLAIN = 1
SCAN_BLOOM = 2

# WARNING: must equal OpenCV's remap sub-pixel step (1/32, INTER_BITS=5); any other value makes the GPU picture drift from the CPU picture.
DEFAULT_SAMPLE_STEPS = 32


@dataclass(frozen=True)
class FrameParams:
    """Values for one frame of pass 1; width and height are the picture the shader covers; sample_steps 0 samples exactly."""
    width: int
    height: int
    warp: bool = False
    curvature: float = 0.0
    vignette: float = 0.0
    scan_mode: int = SCAN_NONE
    scan_lines: int = 240
    scan_floor: float = 0.70
    bloom_strength: float = 0.95
    bloom_gamma: float = 1.6
    sample_steps: int = DEFAULT_SAMPLE_STEPS


# WARNING: the field order and the 16-byte rows below must match cbuffer FrameCB in d3d11_gpu_frame.py, or every shader value is read from the wrong slot.
CONSTANT_BUFFER_BYTES = 64


def pack_constants(p: FrameParams) -> bytes:
    """Packs FrameParams into the 64-byte pass 1 cbuffer: four float4 rows."""
    period = max(1.0, p.height / float(max(1, p.scan_lines)))
    return struct.pack(
        "<16f",
        float(p.width), float(p.height), float(p.curvature), float(p.vignette),
        float(p.scan_floor), float(p.bloom_strength), float(p.bloom_gamma), float(period),
        float(1 if p.warp else 0), float(p.scan_mode), float(p.sample_steps), 0.0,
        0.0, 0.0, 0.0, 0.0,
    )


# ============================================================================
# SECTION: Pass 2 parameters (scale into the bezel cutout)
# ============================================================================

# WARNING: must equal MAX_TAPS in the pass 2 shader source in d3d11_gpu_frame.py; a larger shrink ratio than this is not averaged correctly.
MAX_SCALE_TAPS = 16
COMPOSITE_CONSTANT_BUFFER_BYTES = 128


@dataclass(frozen=True)
class ScalePlan:
    """The source rectangle of the pass 1 picture and where its scaled copy sits in window coordinates."""
    src_x: int
    src_y: int
    src_w: int
    src_h: int
    draw_w: int
    draw_h: int
    dest_x: int
    dest_y: int


@dataclass(frozen=True)
class PowerOn:
    """The power-on effect for one frame: its rectangle, the rows of the lit band, and the additive white level."""
    x0: int
    y0: int
    x1: int
    y1: int
    band_y0: int
    band_y1: int
    white: int


@dataclass(frozen=True)
class Placement:
    """Where an overlay texture sits in window pixels."""
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class CompositePlan:
    """Everything pass 2 draws into the window: the scaled picture, bar colour, power-on effect, bezel image and LED readout."""
    scale: ScalePlan
    visible: tuple | None = None
    bar_color: tuple = (0, 0, 0)
    power_on: PowerOn | None = None
    bezel: Placement | None = None
    led: Placement | None = None


# WARNING: the field order and the 16-byte rows below must match cbuffer CompositeCB in d3d11_gpu_frame.py, or every pass 2 value is read from the wrong slot.
def pack_composite_constants(c: CompositePlan) -> bytes:
    """Packs CompositePlan into the 128-byte pass 2 cbuffer: eight float4 rows."""
    p = c.scale
    vis = c.visible if c.visible is not None else (0, 0, 0, 0)
    po = c.power_on
    bz = c.bezel
    led = c.led
    return struct.pack(
        "<32f",
        float(p.src_x), float(p.src_y), float(p.src_w), float(p.src_h),
        float(p.draw_w), float(p.draw_h), float(p.dest_x), float(p.dest_y),
        float(vis[0]), float(vis[1]), float(vis[2]), float(vis[3]),
        float(c.bar_color[0]), float(c.bar_color[1]), float(c.bar_color[2]), 0.0,
        float(bz.x if bz else 0), float(bz.y if bz else 0), float(bz.w if bz else 0), float(bz.h if bz else 0),
        float(led.x if led else 0), float(led.y if led else 0), float(led.w if led else 0), float(led.h if led else 0),
        float(1 if po else 0), float(po.band_y0 if po else 0), float(po.band_y1 if po else 0), float(po.white if po else 0),
        float(po.x0 if po else 0), float(po.y0 if po else 0), float(po.x1 if po else 0), float(po.y1 if po else 0),
    )


def power_on_plan(elapsed_ms: float, rect: tuple, expand_ms: float, total_ms: float, peak_alpha: float):
    """Returns the PowerOn for `rect` (x, y, w, h) at `elapsed_ms`, or None when the effect is over or the rectangle is empty."""
    x, y, w, h = rect
    if elapsed_ms >= total_ms or w <= 0 or h <= 0:
        return None
    expand = min(1.0, elapsed_ms / expand_ms) ** 2
    band_h = max(2, int(h * expand))
    band_y = y + (h - band_h) // 2
    bright = (1.0 - min(1.0, elapsed_ms / total_ms)) ** 2
    white = int(peak_alpha * bright) if bright > 0 and band_h > 0 else 0
    return PowerOn(x, y, x + w, y + h, band_y, band_y + band_h, max(0, white))


def scale_supported(plan: ScalePlan) -> bool:
    """Returns whether pass 2 can average this shrink ratio exactly (at most MAX_SCALE_TAPS source texels per axis)."""
    if plan.src_w <= 0 or plan.src_h <= 0 or plan.draw_w <= 0 or plan.draw_h <= 0:
        return False
    for src, dst in ((plan.src_w, plan.draw_w), (plan.src_h, plan.draw_h)):
        if dst < src and math.ceil(src / dst) + 1 > MAX_SCALE_TAPS:
            return False
    return True


# ============================================================================
# SECTION: NumPy twin of pass 1
# ============================================================================

_TWO_PI = np.float32(2.0 * np.pi)


def _quantize(v: np.ndarray, steps: int) -> np.ndarray:
    if steps <= 0:
        return v
    s = np.float32(steps)
    return (np.rint(v * s) / s).astype(np.float32)


def _bilinear_clamped(src: np.ndarray, sx: np.ndarray, sy: np.ndarray) -> np.ndarray:
    h, w = src.shape[0], src.shape[1]
    x0 = np.floor(sx).astype(np.int32)
    y0 = np.floor(sy).astype(np.int32)
    fx = (sx - x0.astype(np.float32))[:, :, None]
    fy = (sy - y0.astype(np.float32))[:, :, None]
    x0c = np.clip(x0, 0, w - 1)
    y0c = np.clip(y0, 0, h - 1)
    x1c = np.clip(x0 + 1, 0, w - 1)
    y1c = np.clip(y0 + 1, 0, h - 1)
    s = src.astype(np.float32)
    top = s[y0c, x0c] * (1.0 - fx) + s[y0c, x1c] * fx
    bot = s[y1c, x0c] * (1.0 - fx) + s[y1c, x1c] * fx
    return top * (1.0 - fy) + bot * fy


def _scan_shape(rows: np.ndarray, period: float) -> np.ndarray:
    frac = rows / np.float32(period)
    frac = frac - np.floor(frac)
    return np.float32(0.5) + np.float32(0.5) * np.cos(frac * _TWO_PI)


def reference_frame(src: np.ndarray, p: FrameParams) -> np.ndarray:
    """Runs the pass 1 formulas on a (h, w, 3) uint8 picture and returns the (h, w, 3) uint8 result."""
    h, w = p.height, p.width
    if src.shape[0] != h or src.shape[1] != w:
        raise ValueError("source size does not match FrameParams")
    xs = np.arange(w, dtype=np.float32)[None, :]
    ys = np.arange(h, dtype=np.float32)[:, None]
    nx = (xs / np.float32(max(w - 1, 1))) * 2.0 - 1.0
    ny = (ys / np.float32(max(h - 1, 1))) * 2.0 - 1.0
    nx = np.broadcast_to(nx, (h, w))
    ny = np.broadcast_to(ny, (h, w))

    if p.warp:
        r2 = nx * nx + ny * ny
        factor = (1.0 + np.float32(p.curvature) * r2) / np.float32(1.0 + p.curvature)
        sx_raw = ((nx * factor + 1.0) * 0.5) * np.float32(w - 1)
        sy_raw = ((ny * factor + 1.0) * 0.5) * np.float32(h - 1)
        valid = ((sx_raw >= 0) & (sx_raw <= w - 1) & (sy_raw >= 0) & (sy_raw <= h - 1)).astype(np.float32)
        sx = np.clip(sx_raw, 0, w - 1).astype(np.float32)
        sy = np.clip(sy_raw, 0, h - 1).astype(np.float32)
        vradius = np.sqrt(nx * nx * 0.85 + ny * ny * 0.85)
        vig = (1.0 - np.float32(p.vignette) * np.clip(vradius, 0.0, 1.0) ** 2) * valid
        sampled = _bilinear_clamped(src, _quantize(sx, p.sample_steps), _quantize(sy, p.sample_steps))
        warped = np.floor(sampled + 0.5)
        mult = vig.astype(np.float32)
        map_y = sy
    else:
        warped = src.astype(np.float32)
        mult = np.ones((h, w), dtype=np.float32)
        map_y = np.broadcast_to(ys, (h, w))

    period = max(1.0, h / float(max(1, p.scan_lines)))
    if p.scan_mode == SCAN_BLOOM:
        shape = _scan_shape(map_y, period)
        base = (p.scan_floor + (1.0 - p.scan_floor) * shape).astype(np.float32)
        bloom_range = ((1.0 - base) * np.float32(p.bloom_strength)).astype(np.float32)
        sums = warped[:, :, 0] + warped[:, :, 1] + warped[:, :, 2]
        lum = np.clip(sums * np.float32(1.0 / (3.0 * 255.0)), 0.0, 1.0)
        bloom = np.power(lum, np.float32(p.bloom_gamma)).astype(np.float32)
        mult = mult * (base + bloom * bloom_range)
    elif p.scan_mode == SCAN_PLAIN:
        shape = _scan_shape(np.broadcast_to(ys, (h, w)), period)
        row_mult = p.scan_floor + (1.0 - p.scan_floor) * shape
        alpha = np.floor(np.clip((1.0 - row_mult) * 255.0, 0, 255))
        mult = mult * (1.0 - alpha / 255.0).astype(np.float32)

    out = warped * mult[:, :, None]
    return np.clip(np.floor(out + 0.5), 0, 255).astype(np.uint8)


# ============================================================================
# SECTION: NumPy twin of pass 2
# ============================================================================


def _axis_weights(src_n: int, dst_n: int) -> np.ndarray:
    weights = np.zeros((dst_n, src_n), dtype=np.float64)
    if dst_n <= src_n:
        ratio = src_n / dst_n
        for d in range(dst_n):
            a = d * ratio
            b = a + ratio
            for s in range(int(math.floor(a)), min(src_n, int(math.ceil(b)))):
                weights[d, s] = max(0.0, min(b, s + 1) - max(a, s)) / ratio
    else:
        for d in range(dst_n):
            pos = d * (src_n - 1) / dst_n
            s0 = int(math.floor(pos))
            f = pos - s0
            weights[d, s0] += 1.0 - f
            weights[d, min(s0 + 1, src_n - 1)] += f
    return weights / weights.sum(axis=1, keepdims=True)


def reference_scale(src: np.ndarray, plan: ScalePlan) -> np.ndarray:
    """Runs the pass 2 formulas on a (h, w, 3) uint8 picture and returns the (draw_h, draw_w, 3) uint8 scaled copy of the source rectangle."""
    region = src[plan.src_y:plan.src_y + plan.src_h, plan.src_x:plan.src_x + plan.src_w].astype(np.float64)
    if region.shape[0] != plan.src_h or region.shape[1] != plan.src_w:
        raise ValueError("source rectangle is outside the picture")
    out = region
    if plan.draw_h != plan.src_h:
        wy = _axis_weights(plan.src_h, plan.draw_h)
        out = (wy @ out.reshape(plan.src_h, plan.src_w * 3)).reshape(plan.draw_h, plan.src_w, 3)
    if plan.draw_w != plan.src_w:
        wx = _axis_weights(plan.src_w, plan.draw_w)
        out = np.matmul(out.transpose(0, 2, 1), wx.T).transpose(0, 2, 1)
    return np.clip(np.floor(out + 0.5), 0, 255).astype(np.uint8)


def _over(out: np.ndarray, rgba: np.ndarray, place: Placement) -> None:
    x0, y0 = max(place.x, 0), max(place.y, 0)
    x1, y1 = min(place.x + place.w, out.shape[1]), min(place.y + place.h, out.shape[0])
    if x1 <= x0 or y1 <= y0:
        return
    s = rgba[y0 - place.y:y1 - place.y, x0 - place.x:x1 - place.x].astype(np.int32)
    d = out[y0:y1, x0:x1].astype(np.int32)
    a = s[..., 3:4]
    res = (((s[..., :3] - d) * a + s[..., :3]) >> 8) + d
    out[y0:y1, x0:x1] = np.clip(res, 0, 255).astype(np.uint8)


def reference_composite(src: np.ndarray, plan: CompositePlan, win_w: int, win_h: int,
                        bezel_rgba: np.ndarray | None = None, led_rgba: np.ndarray | None = None) -> np.ndarray:
    """Runs the pass 2 formulas and returns the (win_h, win_w, 3) uint8 window: bars, scaled picture, power-on effect, bezel, LED."""
    out = np.empty((win_h, win_w, 3), dtype=np.uint8)
    out[:, :] = np.array(plan.bar_color, dtype=np.uint8)
    if plan.visible is not None:
        x0, y0, x1, y1 = plan.visible
        scaled = reference_scale(src, plan.scale)
        out[y0:y1, x0:x1] = scaled[y0 - plan.scale.dest_y:y1 - plan.scale.dest_y, x0 - plan.scale.dest_x:x1 - plan.scale.dest_x]
    po = plan.power_on
    if po is not None:
        px0, py0, px1, py1 = max(po.x0, 0), max(po.y0, 0), min(po.x1, win_w), min(po.y1, win_h)
        for y in range(py0, py1):
            if y < po.band_y0 or y >= po.band_y1:
                out[y, px0:px1] = 0
            else:
                out[y, px0:px1] = np.minimum(out[y, px0:px1].astype(np.int32) + po.white, 255).astype(np.uint8)
    if plan.bezel is not None and bezel_rgba is not None:
        _over(out, bezel_rgba, plan.bezel)
    if plan.led is not None and led_rgba is not None:
        _over(out, led_rgba, plan.led)
    return out


# ============================================================================
# SECTION: Tolerance check
# ============================================================================


def compare_frames(a: np.ndarray, b: np.ndarray, tolerance: int, max_over_fraction: float = 0.0) -> dict:
    """Returns max and mean absolute difference and the share of channel values off by more than `tolerance`; ok when that share is at most `max_over_fraction`."""
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16))
    over = float((diff > tolerance).mean())
    return {
        "max": int(diff.max()),
        "mean": float(diff.mean()),
        "over_tolerance": over,
        "ok": bool(over <= max_over_fraction),
    }
