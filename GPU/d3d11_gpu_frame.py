"""
D3D11 frame pipeline: uploads the composed pygame screen and runs the two shader passes into the swapchain back buffer.

Owns: the HLSL source of both passes, shader compilation, the uploaded-screen texture, the pass 1 output texture, the bezel and
LED overlay textures, the two constant buffers, and GPUFramePipeline.render().
Does not own: the device and swapchain (d3d11_gpu_core.py), the constant-buffer byte layouts (d3d11_gpu_reference.py), how the
bezel image and LED readout are drawn into pygame surfaces (the main script), Present, or the decision to use the GPU (G5).

Main thread only, except precompile_shaders(), which needs no device and may run on a worker thread.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from dataclasses import dataclass

import pygame

from d3d11_gpu_core import (
    CTX_OMSetRenderTargets, CTX_RSSetViewports, DEV_CreateRenderTargetView, DEV_CreateTexture2D,
    DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_SAMPLE_DESC, D3D11_TEXTURE2D_DESC, D3D11_VIEWPORT, GPUCoreError, HRESULT, UINT, VOIDP,
    call, checked_call, release,
)
from d3d11_gpu_reference import (
    COMPOSITE_CONSTANT_BUFFER_BYTES, CONSTANT_BUFFER_BYTES, MAX_SCALE_TAPS, SCAN_NONE, CompositePlan, FrameParams,
    Placement, ScalePlan, pack_composite_constants, pack_constants, scale_supported,
)

if sys.platform != "win32":
    raise ImportError("d3d11_gpu_frame is Windows-only (D3D11 via ctypes).")

log = logging.getLogger(__name__)

# ============================================================================
# SECTION: ctypes bindings & constants
# ============================================================================
DXGI_FORMAT_R8G8B8A8_UNORM = 28
D3D11_USAGE_DEFAULT = 0
D3D11_BIND_CONSTANT_BUFFER = 0x4
D3D11_BIND_SHADER_RESOURCE = 0x8
D3D11_BIND_RENDER_TARGET = 0x20
D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST = 4

# WARNING: a wrong slot number calls a different method and can crash the app; slots below follow the d3d11.h method order.
DEV_CreateBuffer = 3
DEV_CreateShaderResourceView = 7
DEV_CreateVertexShader = 12
DEV_CreatePixelShader = 15
CTX_PSSetShaderResources = 8
CTX_PSSetShader = 9
CTX_VSSetShader = 11
CTX_Draw = 13
CTX_PSSetConstantBuffers = 16
CTX_IASetPrimitiveTopology = 24
CTX_UpdateSubresource = 48
BLOB_GetBufferPointer = 3
BLOB_GetBufferSize = 4


class D3D11_BUFFER_DESC(ctypes.Structure):
    _fields_ = [("ByteWidth", UINT), ("Usage", UINT), ("BindFlags", UINT),
                ("CPUAccessFlags", UINT), ("MiscFlags", UINT), ("StructureByteStride", UINT)]


class D3D11_SUBRESOURCE_DATA(ctypes.Structure):
    _fields_ = [("pSysMem", VOIDP), ("SysMemPitch", UINT), ("SysMemSlicePitch", UINT)]


class GPUFrameError(GPUCoreError):
    """The frame pipeline could not draw this frame; the caller must use the CPU path for it."""


# ============================================================================
# SECTION: HLSL source
# ============================================================================
# WARNING: HLSL_PASS1_CORE and HLSL_PASS2_CORE are executed unchanged by the NumPy-twin test harness; only the *_BINDINGS text differs there, so never put texture or sampler code in a CORE string.
HLSL_VERTEX = """
struct VSOut { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };
VSOut VSMain(uint vid : SV_VertexID)
{
    VSOut o;
    float2 p = float2((vid << 1) & 2, vid & 2);
    o.uv = p;
    o.pos = float4(p * float2(2, -2) + float2(-1, 1), 0, 1);
    return o;
}
"""

HLSL_PASS1_BINDINGS = """
Texture2D srcTex : register(t0);
float4 LoadSrc(int2 p) { return srcTex.Load(int3(p, 0)); }
"""

HLSL_PASS1_CORE = """
cbuffer FrameCB
{
    float2 picSize;
    float curvature;
    float vignette;
    float scanFloor;
    float bloomStrength;
    float bloomGamma;
    float scanPeriod;
    float warpOn;
    float scanMode;
    float sampleSteps;
    float padA;
    float4 padB;
};

static const float TWO_PI = 6.28318530718;

float3 LoadRGB255(int2 p, int2 size)
{
    int2 q = clamp(p, int2(0, 0), size - int2(1, 1));
    return LoadSrc(q).rgb * 255.0;
}

float3 SampleBilinear(float2 uv, int2 size)
{
    float2 base = floor(uv);
    float2 f = uv - base;
    int2 p0 = int2(base);
    float3 a = LoadRGB255(p0, size);
    float3 b = LoadRGB255(p0 + int2(1, 0), size);
    float3 c = LoadRGB255(p0 + int2(0, 1), size);
    float3 d = LoadRGB255(p0 + int2(1, 1), size);
    float3 top = a * (1.0 - f.x) + b * f.x;
    float3 bot = c * (1.0 - f.x) + d * f.x;
    return top * (1.0 - f.y) + bot * f.y;
}

float RoundEven(float v)
{
    float lo = floor(v);
    float fr = v - lo;
    if (fr > 0.5)
    {
        return lo + 1.0;
    }
    if (fr < 0.5)
    {
        return lo;
    }
    return lo + fmod(lo, 2.0);
}

float ScanShape(float row)
{
    float t = row / scanPeriod;
    return 0.5 + 0.5 * cos((t - floor(t)) * TWO_PI);
}

float3 ShadeFrame(int2 pix)
{
    int2 size = int2(picSize);
    float2 pos = float2(pix);
    float3 warped;
    float mult = 1.0;
    float mapY = pos.y;

    if (warpOn > 0.5)
    {
        float2 n = pos / max(picSize - 1.0, 1.0) * 2.0 - 1.0;
        float r2 = dot(n, n);
        float factor = (1.0 + curvature * r2) / (1.0 + curvature);
        float2 sRaw = ((n * factor + 1.0) * 0.5) * (picSize - 1.0);
        float inside = (sRaw.x >= 0.0 && sRaw.x <= picSize.x - 1.0 && sRaw.y >= 0.0 && sRaw.y <= picSize.y - 1.0) ? 1.0 : 0.0;
        float2 s = clamp(sRaw, 0.0, picSize - 1.0);
        float vr = saturate(sqrt(dot(n, n) * 0.85));
        mult = (1.0 - vignette * vr * vr) * inside;
        mapY = s.y;
        float2 q = s;
        if (sampleSteps > 0.5)
        {
            q = float2(RoundEven(s.x * sampleSteps), RoundEven(s.y * sampleSteps)) / sampleSteps;
        }
        warped = floor(SampleBilinear(q, size) + 0.5);
    }
    else
    {
        warped = floor(LoadRGB255(pix, size) + 0.5);
    }

    if (scanMode > 1.5)
    {
        float baseMult = scanFloor + (1.0 - scanFloor) * ScanShape(mapY);
        float bloomRange = (1.0 - baseMult) * bloomStrength;
        float lum = saturate((warped.r + warped.g + warped.b) * (1.0 / 765.0));
        float bloom = pow(max(lum, 1e-7), bloomGamma);
        mult *= baseMult + bloom * bloomRange;
    }
    else if (scanMode > 0.5)
    {
        float rowMult = scanFloor + (1.0 - scanFloor) * ScanShape(pos.y);
        float alpha = floor(clamp((1.0 - rowMult) * 255.0, 0.0, 255.0));
        mult *= 1.0 - alpha / 255.0;
    }

    return clamp(floor(warped * mult + 0.5), 0.0, 255.0);
}
"""

HLSL_PASS1_ENTRY = """
struct VSOut { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };
float4 PSFrame(VSOut i) : SV_TARGET
{
    return float4(ShadeFrame(int2(i.pos.xy)) * (1.0 / 255.0), 1.0);
}
"""

HLSL_PASS2_BINDINGS = """
Texture2D srcTex : register(t0);
Texture2D bezelTex : register(t1);
Texture2D ledTex : register(t2);
float4 LoadSrc(int2 p) { return srcTex.Load(int3(p, 0)); }
float4 LoadBezel(int2 p) { return bezelTex.Load(int3(p, 0)); }
float4 LoadLed(int2 p) { return ledTex.Load(int3(p, 0)); }
"""

HLSL_PASS2_CORE = """
cbuffer CompositeCB
{
    float2 srcOrigin;
    float2 srcSize;
    float2 drawSize;
    float2 destOrigin;
    float4 visRect;
    float3 barColor;
    float padBar;
    float2 bezelOrigin;
    float2 bezelSize;
    float2 ledOrigin;
    float2 ledSize;
    float powerOn;
    float bandY0;
    float bandY1;
    float powerWhite;
    float4 powerRect;
};

static const int MAX_TAPS = 16;

int AxisTaps(int d, int sn, int dn)
{
    if (dn <= sn)
    {
        float r = (float)sn / (float)dn;
        float a = (float)d * r;
        return min(MAX_TAPS, (int)ceil(a + r) - (int)floor(a));
    }
    return 2;
}

float AxisWeight(int tap, int d, int sn, int dn, out int s)
{
    if (dn <= sn)
    {
        float r = (float)sn / (float)dn;
        float a = (float)d * r;
        float b = a + r;
        s = (int)floor(a) + tap;
        if (s >= sn)
        {
            s = sn - 1;
            return 0.0;
        }
        return max(0.0, min(b, (float)(s + 1)) - max(a, (float)s)) / r;
    }
    float p = (float)d * (float)(sn - 1) / (float)dn;
    int p0 = (int)floor(p);
    float f = p - (float)p0;
    s = min(p0 + tap, sn - 1);
    return tap == 0 ? 1.0 - f : (tap == 1 ? f : 0.0);
}

float3 ShadeScale(int2 pix)
{
    int2 d = pix - int2(destOrigin);
    int2 sn = int2(srcSize);
    int2 dn = int2(drawSize);
    int2 origin = int2(srcOrigin);
    int tx = AxisTaps(d.x, sn.x, dn.x);
    int ty = AxisTaps(d.y, sn.y, dn.y);
    float3 acc = float3(0.0, 0.0, 0.0);
    float total = 0.0;
    [loop]
    for (int j = 0; j < ty; j++)
    {
        int sy;
        float wy = AxisWeight(j, d.y, sn.y, dn.y, sy);
        [loop]
        for (int i = 0; i < tx; i++)
        {
            int sx;
            float wx = AxisWeight(i, d.x, sn.x, dn.x, sx);
            float wgt = wx * wy;
            acc += wgt * LoadSrc(origin + int2(sx, sy)).rgb * 255.0;
            total += wgt;
        }
    }
    return clamp(floor(acc / max(total, 1e-6) + 0.5), 0.0, 255.0);
}

float3 OverPixel(float3 dst, float4 src)
{
    int3 s = int3(floor(src.rgb * 255.0 + 0.5));
    int a = (int)floor(src.a * 255.0 + 0.5);
    int3 d = int3(dst);
    int3 r = (((s - d) * a + s) >> 8) + d;
    return float3(r);
}

bool InsideRect(float2 p, float4 r)
{
    return p.x >= r.x && p.x < r.z && p.y >= r.y && p.y < r.w;
}

float3 ShadeComposite(int2 pix)
{
    float2 p = float2(pix);
    float3 colour = barColor;
    if (InsideRect(p, visRect))
    {
        colour = ShadeScale(pix);
    }
    if (powerOn > 0.5 && InsideRect(p, powerRect))
    {
        if (p.y < bandY0 || p.y >= bandY1)
        {
            colour = float3(0.0, 0.0, 0.0);
        }
        else
        {
            colour = min(colour + powerWhite, 255.0);
        }
    }
    if (bezelSize.x > 0.5)
    {
        int2 b = pix - int2(bezelOrigin);
        if (b.x >= 0 && b.y >= 0 && b.x < (int)bezelSize.x && b.y < (int)bezelSize.y)
        {
            colour = OverPixel(colour, LoadBezel(b));
        }
    }
    if (ledSize.x > 0.5)
    {
        int2 l = pix - int2(ledOrigin);
        if (l.x >= 0 && l.y >= 0 && l.x < (int)ledSize.x && l.y < (int)ledSize.y)
        {
            colour = OverPixel(colour, LoadLed(l));
        }
    }
    return clamp(colour, 0.0, 255.0);
}
"""

HLSL_PASS2_ENTRY = """
struct VSOut { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };
float4 PSComposite(VSOut i) : SV_TARGET
{
    return float4(ShadeComposite(int2(i.pos.xy)) * (1.0 / 255.0), 1.0);
}
"""


# ============================================================================
# SECTION: Shader compilation
# ============================================================================


def compile_shader(source: str, entry: str, target: str) -> bytes:
    """Compiles HLSL source with d3dcompiler_47 and returns the bytecode; raises GPUFrameError with the compiler's message on failure."""
    d3dcompiler = ctypes.WinDLL("d3dcompiler_47.dll")
    d3d_compile = d3dcompiler.D3DCompile
    d3d_compile.restype = HRESULT
    d3d_compile.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, VOIDP, VOIDP, ctypes.c_char_p,
                            ctypes.c_char_p, UINT, UINT, ctypes.POINTER(VOIDP), ctypes.POINTER(VOIDP)]
    data = source.encode("ascii")
    code_blob = VOIDP()
    err_blob = VOIDP()
    hr = d3d_compile(data, len(data), None, None, None, entry.encode("ascii"), target.encode("ascii"), 0, 0,
                     ctypes.byref(code_blob), ctypes.byref(err_blob))
    message = ""
    if err_blob:
        message = ctypes.string_at(call(err_blob, BLOB_GetBufferPointer, VOIDP, []),
                                   call(err_blob, BLOB_GetBufferSize, ctypes.c_size_t, [])).decode(errors="replace")
    try:
        if hr < 0 or not code_blob:
            raise GPUFrameError("D3DCompile(%s) failed: hr=0x%08X\n%s" % (entry, hr & 0xFFFFFFFF, message))
        return ctypes.string_at(call(code_blob, BLOB_GetBufferPointer, VOIDP, []),
                                call(code_blob, BLOB_GetBufferSize, ctypes.c_size_t, []))
    finally:
        release(code_blob)
        release(err_blob)


_shader_code = None
_shader_code_lock = threading.Lock()


def precompile_shaders() -> dict:
    """Compiles the three shaders once per run and returns their bytecode as {"vs", "ps1", "ps2"}; safe on a worker thread."""
    global _shader_code
    with _shader_code_lock:
        if _shader_code is None:
            _shader_code = {
                "vs": compile_shader(HLSL_VERTEX, "VSMain", "vs_4_0"),
                "ps1": compile_shader(HLSL_PASS1_BINDINGS + HLSL_PASS1_CORE + HLSL_PASS1_ENTRY, "PSFrame", "ps_4_0"),
                "ps2": compile_shader(HLSL_PASS2_BINDINGS + HLSL_PASS2_CORE + HLSL_PASS2_ENTRY, "PSComposite", "ps_4_0"),
            }
        return _shader_code


# ============================================================================
# SECTION: Frame pipeline
# ============================================================================


@dataclass(frozen=True)
class ClipRect:
    """A rectangle in window pixels."""
    x: int
    y: int
    w: int
    h: int


def _intersect(a: ClipRect, b: ClipRect):
    x0, y0 = max(a.x, b.x), max(a.y, b.y)
    x1, y1 = min(a.x + a.w, b.x + b.w), min(a.y + a.h, b.y + b.h)
    return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None


def render_overlay_surface(draw, rect, margin=None):
    """Runs draw(scratch, local_rect) on a transparent per-pixel-alpha surface around the window rectangle `rect`; returns (surface, x, y, draw's result)."""
    base = margin if margin is not None else max(rect.width, rect.height) // 2 + 8
    for m in (base, base * 2):
        scratch = pygame.Surface((rect.width + 2 * m, rect.height + 2 * m), pygame.SRCALPHA)
        result = draw(scratch, pygame.Rect(m, m, rect.width, rect.height))
        alpha = pygame.surfarray.pixels_alpha(scratch)
        try:
            clear = not (alpha[0, :].any() or alpha[-1, :].any() or alpha[:, 0].any() or alpha[:, -1].any())
        finally:
            del alpha
        if clear:
            return scratch, rect.x - m, rect.y - m, result
    raise GPUFrameError("overlay drawing reaches beyond %d pixels around its rectangle" % (base * 2))


class _Overlay:
    """One straight-alpha overlay texture (bezel image or LED readout), re-uploaded only when its key changes."""

    def __init__(self):
        self.tex = VOIDP()
        self.srv = VOIDP()
        self.key = None
        self.fmt = None
        self.placement = None


class GPUFramePipeline:
    """Draws one composed screen per call into the core's back buffer: pass 1 (warp, vignette, scan lines), pass 2 (scale, power-on, bezel, LED)."""

    def __init__(self, core):
        self._core = core
        self._device = core.device
        self._context = core.context
        self._vs = VOIDP()
        self._ps1 = VOIDP()
        self._ps2 = VOIDP()
        self._cb1 = VOIDP()
        self._cb2 = VOIDP()
        self._src_tex = VOIDP()
        self._src_srv = VOIDP()
        self._mid_tex = VOIDP()
        self._mid_srv = VOIDP()
        self._mid_rtv = VOIDP()
        self._size = None
        self._src_format = None
        self._bezel = _Overlay()
        self._led = _Overlay()
        self._last_cb1 = None
        self._last_cb2 = None
        self._closed = False
        try:
            self._create_pipeline()
        except Exception:
            self.close()
            raise

    # -- setup ---------------------------------------------------------------
    def _create_pipeline(self):
        codes = precompile_shaders()
        vs_code, ps1_code, ps2_code = codes["vs"], codes["ps1"], codes["ps2"]
        shader_args = [VOIDP, ctypes.c_size_t, VOIDP, ctypes.POINTER(VOIDP)]
        checked_call("CreateVertexShader", self._device, DEV_CreateVertexShader, shader_args,
                     vs_code, len(vs_code), None, ctypes.byref(self._vs))
        checked_call("CreatePixelShader(pass 1)", self._device, DEV_CreatePixelShader, shader_args,
                     ps1_code, len(ps1_code), None, ctypes.byref(self._ps1))
        checked_call("CreatePixelShader(pass 2)", self._device, DEV_CreatePixelShader, shader_args,
                     ps2_code, len(ps2_code), None, ctypes.byref(self._ps2))
        self._cb1 = self._create_constant_buffer(CONSTANT_BUFFER_BYTES, "pass 1")
        self._cb2 = self._create_constant_buffer(COMPOSITE_CONSTANT_BUFFER_BYTES, "pass 2")

    def _create_constant_buffer(self, size, label):
        desc = D3D11_BUFFER_DESC(size, D3D11_USAGE_DEFAULT, D3D11_BIND_CONSTANT_BUFFER, 0, 0, 0)
        zeros = ctypes.create_string_buffer(size)
        init = D3D11_SUBRESOURCE_DATA(ctypes.cast(zeros, VOIDP), 0, 0)
        out = VOIDP()
        checked_call("CreateBuffer(%s constants)" % label, self._device, DEV_CreateBuffer,
                     [ctypes.POINTER(D3D11_BUFFER_DESC), ctypes.POINTER(D3D11_SUBRESOURCE_DATA), ctypes.POINTER(VOIDP)],
                     ctypes.byref(desc), ctypes.byref(init), ctypes.byref(out))
        return out

    # -- sized resources -------------------------------------------------------
    def _release_sized(self):
        for obj in (self._mid_rtv, self._mid_srv, self._mid_tex, self._src_srv, self._src_tex):
            release(obj)
        self._src_tex = VOIDP()
        self._src_srv = VOIDP()
        self._mid_tex = VOIDP()
        self._mid_srv = VOIDP()
        self._mid_rtv = VOIDP()
        self._size = None
        self._src_format = None

    def _create_texture(self, width, height, fmt, bind_flags, label):
        desc = D3D11_TEXTURE2D_DESC(width, height, 1, 1, fmt, DXGI_SAMPLE_DESC(1, 0), D3D11_USAGE_DEFAULT,
                                    bind_flags, 0, 0)
        tex = VOIDP()
        checked_call("CreateTexture2D(%s)" % label, self._device, DEV_CreateTexture2D,
                     [ctypes.POINTER(D3D11_TEXTURE2D_DESC), VOIDP, ctypes.POINTER(VOIDP)],
                     ctypes.byref(desc), None, ctypes.byref(tex))
        return tex

    def _create_srv(self, tex, label):
        srv = VOIDP()
        checked_call("CreateShaderResourceView(%s)" % label, self._device, DEV_CreateShaderResourceView,
                     [VOIDP, VOIDP, ctypes.POINTER(VOIDP)], tex, None, ctypes.byref(srv))
        return srv

    def _ensure_textures(self, width, height, src_format):
        if self._size == (width, height) and self._src_format == src_format:
            return
        self._release_sized()
        try:
            self._src_tex = self._create_texture(width, height, src_format, D3D11_BIND_SHADER_RESOURCE, "screen")
            self._src_srv = self._create_srv(self._src_tex, "screen")
            self._mid_tex = self._create_texture(width, height, DXGI_FORMAT_B8G8R8A8_UNORM,
                                                 D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET, "pass 1 output")
            self._mid_srv = self._create_srv(self._mid_tex, "pass 1 output")
            checked_call("CreateRenderTargetView(pass 1 output)", self._device, DEV_CreateRenderTargetView,
                         [VOIDP, VOIDP, ctypes.POINTER(VOIDP)], self._mid_tex, None, ctypes.byref(self._mid_rtv))
        except Exception:
            self._release_sized()
            raise
        self._size = (width, height)
        self._src_format = src_format

    # -- overlays (bezel image, LED readout) ----------------------------------------
    @staticmethod
    def _alpha_format_for(surface) -> int:
        if surface.get_bytesize() != 4:
            raise GPUFrameError("overlay surface is not 32-bit (%d bytes per pixel)" % surface.get_bytesize())
        masks = tuple(surface.get_masks())
        if masks == (0xFF0000, 0xFF00, 0xFF, 0xFF000000):
            return DXGI_FORMAT_B8G8R8A8_UNORM
        if masks == (0xFF, 0xFF00, 0xFF0000, 0xFF000000):
            return DXGI_FORMAT_R8G8B8A8_UNORM
        raise GPUFrameError("overlay surface channel masks %s are not BGRA or RGBA" % (masks,))

    def _set_overlay(self, overlay, key, surface, x, y):
        if self._closed:
            raise GPUFrameError("GPUFramePipeline is closed")
        w, h = surface.get_size()
        placement = Placement(int(x), int(y), w, h)
        if overlay.key == key and overlay.placement == placement:
            return
        fmt = self._alpha_format_for(surface)
        if overlay.placement is None or (overlay.placement.w, overlay.placement.h) != (w, h) or overlay.fmt != fmt:
            self._release_overlay(overlay)
            try:
                overlay.tex = self._create_texture(w, h, fmt, D3D11_BIND_SHADER_RESOURCE, "overlay")
                overlay.srv = self._create_srv(overlay.tex, "overlay")
            except Exception:
                self._release_overlay(overlay)
                raise
            overlay.fmt = fmt
        self._upload_surface(overlay.tex, surface)
        overlay.key = key
        overlay.placement = placement

    @staticmethod
    def _release_overlay(overlay):
        release(overlay.srv)
        release(overlay.tex)
        overlay.srv = VOIDP()
        overlay.tex = VOIDP()
        overlay.key = None
        overlay.fmt = None
        overlay.placement = None

    def set_bezel(self, key, surface, x, y) -> None:
        """Sets the bezel image (already scaled, per-pixel alpha) drawn at window position (x, y); re-uploads only when `key` or the position changes."""
        self._set_overlay(self._bezel, key, surface, x, y)

    def clear_bezel(self) -> None:
        """Stops drawing the bezel image and frees its texture."""
        self._release_overlay(self._bezel)

    def set_led(self, key, surface, x, y) -> None:
        """Sets the LED readout overlay (per-pixel alpha) drawn at window position (x, y); re-uploads only when `key` or the position changes."""
        self._set_overlay(self._led, key, surface, x, y)

    def clear_led(self) -> None:
        """Stops drawing the LED readout and frees its texture."""
        self._release_overlay(self._led)

    # -- per frame -----------------------------------------------------------------
    @staticmethod
    def _format_for(surface) -> int:
        if surface.get_bytesize() != 4:
            raise GPUFrameError("screen surface is not 32-bit (%d bytes per pixel)" % surface.get_bytesize())
        masks = tuple(surface.get_masks()[:3])
        if masks == (0xFF0000, 0xFF00, 0xFF):
            return DXGI_FORMAT_B8G8R8A8_UNORM
        if masks == (0xFF, 0xFF00, 0xFF0000):
            return DXGI_FORMAT_R8G8B8A8_UNORM
        raise GPUFrameError("screen surface channel masks %s are not BGRX or RGBX" % (masks,))

    def _upload_surface(self, texture, surface):
        px = pygame.surfarray.pixels2d(surface)
        try:
            address = px.ctypes.data
            pitch = px.strides[1]
            if px.strides[0] != 4:
                raise GPUFrameError("surface pixels are not tightly packed")
            call(self._context, CTX_UpdateSubresource, None, [VOIDP, UINT, VOIDP, VOIDP, UINT, UINT],
                 texture, 0, None, address, pitch, 0)
        finally:
            del px

    def _update_constants(self, buffer, data, last_attr):
        if getattr(self, last_attr) == data:
            return
        raw = ctypes.create_string_buffer(data, len(data))
        call(self._context, CTX_UpdateSubresource, None, [VOIDP, UINT, VOIDP, VOIDP, UINT, UINT],
             buffer, 0, None, ctypes.cast(raw, VOIDP), 0, 0)
        setattr(self, last_attr, data)

    def _set_viewport(self, x, y, w, h):
        vp = D3D11_VIEWPORT(float(x), float(y), float(w), float(h), 0.0, 1.0)
        call(self._context, CTX_RSSetViewports, None, [UINT, ctypes.POINTER(D3D11_VIEWPORT)], 1, ctypes.byref(vp))

    def _draw(self, pixel_shader, srvs, constant_buffer):
        ctx = self._context
        views = (VOIDP * len(srvs))(*[s.value for s in srvs])
        call(ctx, CTX_IASetPrimitiveTopology, None, [UINT], D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST)
        call(ctx, CTX_VSSetShader, None, [VOIDP, VOIDP, UINT], self._vs, None, 0)
        call(ctx, CTX_PSSetShader, None, [VOIDP, VOIDP, UINT], pixel_shader, None, 0)
        call(ctx, CTX_PSSetShaderResources, None, [UINT, UINT, ctypes.POINTER(VOIDP)], 0, len(srvs), views)
        call(ctx, CTX_PSSetConstantBuffers, None, [UINT, UINT, ctypes.POINTER(VOIDP)], 0, 1, ctypes.byref(constant_buffer))
        call(ctx, CTX_Draw, None, [UINT, UINT], 3, 0)

    def _unbind_inputs(self):
        none = (VOIDP * 3)()
        call(self._context, CTX_PSSetShaderResources, None, [UINT, UINT, ctypes.POINTER(VOIDP)], 0, 3, none)

    def _run_pass1(self, params: FrameParams):
        self._update_constants(self._cb1, pack_constants(params), "_last_cb1")
        w, h = self._size
        call(self._context, CTX_OMSetRenderTargets, None, [UINT, ctypes.POINTER(VOIDP), VOIDP],
             1, ctypes.byref(self._mid_rtv), None)
        self._set_viewport(0, 0, w, h)
        self._draw(self._ps1, [self._src_srv], self._cb1)

    def _run_pass2(self, composite: CompositePlan, source_srv):
        self._core.begin_frame()
        self._update_constants(self._cb2, pack_composite_constants(composite), "_last_cb2")
        self._draw(self._ps2, [source_srv, self._bezel.srv, self._led.srv], self._cb2)

    def render(self, surface, params: FrameParams, plan: ScalePlan, clip=None, bar_color=(0, 0, 0), power_on=None,
               draw_overlays: bool = True) -> None:
        """Draws `surface` through pass 1 (skipped when no effect is on) and pass 2 into the back buffer; draw_overlays False leaves the bezel and LED out; does not present."""
        if self._closed:
            raise GPUFrameError("GPUFramePipeline is closed")
        width, height = surface.get_size()
        if (params.width, params.height) != (width, height):
            raise GPUFrameError("shader picture %dx%d does not cover the %dx%d screen surface" %
                                (params.width, params.height, width, height))
        if not scale_supported(plan):
            raise GPUFrameError("scale %dx%d -> %dx%d is outside what pass 2 can average (limit %d texels per axis)" %
                                (plan.src_w, plan.src_h, plan.draw_w, plan.draw_h, MAX_SCALE_TAPS))
        if (plan.src_x < 0 or plan.src_y < 0 or plan.src_x + plan.src_w > width or plan.src_y + plan.src_h > height):
            raise GPUFrameError("scale source rectangle is outside the screen surface")
        self._ensure_textures(width, height, self._format_for(surface))
        self._upload_surface(self._src_tex, surface)
        source_srv = self._src_srv
        if params.warp or params.scan_mode != SCAN_NONE:
            self._run_pass1(params)
            source_srv = self._mid_srv
        bw, bh = self._core.size
        visible = _intersect(ClipRect(plan.dest_x, plan.dest_y, plan.draw_w, plan.draw_h),
                             clip if clip is not None else ClipRect(0, 0, bw, bh))
        if visible is not None:
            visible = _intersect(ClipRect(*visible), ClipRect(0, 0, bw, bh))
        if visible is not None:
            visible = (visible[0], visible[1], visible[0] + visible[2], visible[1] + visible[3])
        composite = CompositePlan(plan, visible, tuple(bar_color), power_on,
                                  self._bezel.placement if (draw_overlays and self._bezel.tex) else None,
                                  self._led.placement if (draw_overlays and self._led.tex) else None)
        self._run_pass2(composite, source_srv)
        self._unbind_inputs()

    # -- teardown ------------------------------------------------------------------------
    def close(self) -> None:
        """Releases everything; safe to call twice and after a failed constructor."""
        if self._closed:
            return
        self._closed = True
        self._release_sized()
        self._release_overlay(self._bezel)
        self._release_overlay(self._led)
        for obj in (self._cb2, self._cb1, self._ps2, self._ps1, self._vs):
            release(obj)
        self._cb1 = self._cb2 = self._ps1 = self._ps2 = self._vs = VOIDP()
