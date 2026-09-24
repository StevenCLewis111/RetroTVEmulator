"""
Persistent D3D11 CRT-warp renderer (build step 4).

Lives in root/GPU alongside d3d11_warp_poc.py.

This is the "real" successor to d3d11_warp_poc.py, which you confirmed
working ("warp showed up, game ran smooth, nothing looked off"). The POC's
`run_gpu_warp()` created a device, compiled shaders, made textures, drew
one frame, read it back, and released EVERYTHING -- fine for a one-shot
validation script, far too expensive to do 60x/second. This module keeps
all of that alive across frames instead: create device/shaders/sampler
once, resize textures only when the picture's (w, h) actually changes,
and do the minimum per frame (upload -> draw -> readback).

WHAT CHANGED FROM THE POC, AND WHY
    - Vignette/corner-darkening was REMOVED from the pixel shader. The
      real pipeline (_apply_crt_shader in retro_tv_emulator.py) already
      computes its own vignette+corner mask on the CPU (`_build_crt_shader_
      maps`' `mult3`, cached per (w,h,curvature)) and multiplies it in
      AFTER the warp, regardless of which backend did the warp. Leaving
      vignette in the shader too would double-darken the corners. This
      module's only job is the expensive per-pixel gather -- the same
      thing cv2.remap does on the CPU path -- not the shading on top of it.
    - Manual UV clamping (`saturate(srcUV)`) was removed from the shader
      because the sampler's own D3D11_TEXTURE_ADDRESS_CLAMP mode already
      does that at the hardware level, which is the same semantics as the
      CPU path's cv2.BORDER_REPLICATE. One less thing to keep in sync.
    - Device/shaders/sampler/constant-buffer are created ONCE in
      GPUWarpRenderer.__init__ and kept for the process lifetime. Only the
      source/render-target/staging textures are recreated, and only when
      the requested (w, h) changes size (Bezel style switch, window
      resize) -- exactly the same cadence _build_crt_shader_maps and the
      CPU path's own scratch buffers already cache on.
    - Upload/readback scratch buffers (the (h,w,4) RGBA staging array and
      the read-back numpy view) are also cached per (w, h) so normal
      frames allocate nothing.

STEP 5 ADDITION -- bezel-compositing scale: GPUWarpRenderer.scale() reuses
this SAME device/context/sampler (no second D3D11 device spun up for a
second job) to offload _composite_bezel_frame's pygame.transform.
smoothscale call -- the resize from the finished picture into the Bezel's
cutout size. This is a genuinely different operation from warp() (resize,
not a per-pixel remap), with its own passthrough pixel shader and its own
differently-sized textures (input size != output size, unlike warp's
same-size-in-out), so it gets its own texture cache and its own method,
sharing only the device-level plumbing.

IMPORTANT QUALITY CAVEAT: pygame's smoothscale does an area-averaged
resize, which handles large DOWNSCALES (the common case here -- shrinking
a full window down into a small Bezel cutout) without aliasing. A single-
level bilinear GPU sample would NOT match that on a big downscale (e.g.
4x reduction) -- it would look noticeably worse, not just "different."
To actually match, the source texture here is created with a full mip
chain (MipLevels=0, MISC_GENERATE_MIPS) and GenerateMips() is called
after every upload, with the existing trilinear sampler (already
MIN_MAG_MIP_LINEAR) sampling across it. This should visually track
smoothscale closely, but "should" is doing real work in that sentence --
please specifically A/B this against the CPU path (toggle the System-menu
GPU/CPU override) on a genuinely large downscale (small window or a
Bezel style with a small screen cutout) and confirm it doesn't look
softer/blockier than before, since this is the one part of this whole
GPU effort that trades one *quality algorithm* for another rather than
just moving the same math to different hardware.

STILL UNVERIFIED THE SAME WAY THE POC WAS: every vtable offset here is
carried over unchanged from d3d11_warp_poc.py, which you already ran
successfully on your machine -- so the offsets themselves are proven. What
ISN'T yet proven is the *persistent* reuse pattern (UpdateSubresource on a
kept-alive DEFAULT texture instead of CreateTexture2D fresh every time,
resource resize/Release on a size change, calling Draw many times against
the same device). Please playtest specifically: extended play (a few
minutes, not one frame) with Bezel + warp on, AND resizing the window or
switching Bezel style mid-session, to shake out anything that only shows
up on the 2nd+ frame or on a resize.

FAILS SOFT: any exception anywhere in here (missing DLL, no D3D11-capable
adapter, a bad vtable call) is caught by get_shared_renderer() below,
logged ONCE, and every caller falls back to the existing CPU cv2.remap
path for the rest of the session -- never a crash, matching the same
soft-fail convention _detect_gpu_tier() already uses for GPU detection.
"""
from __future__ import annotations

import ctypes
import struct
import sys

import numpy as np

if sys.platform != "win32":
    raise ImportError("d3d11_warp_renderer is Windows-only (D3D11 via ctypes).")

# ---------------------------------------------------------------------------
# ctypes bindings -- copied from the validated d3d11_warp_poc.py unchanged.
# Vtable slot indices are the documented d3d11.h layout (IUnknown = 0-2).
# ---------------------------------------------------------------------------
HRESULT = ctypes.c_long
UINT = ctypes.c_uint32
FLOAT = ctypes.c_float

D3D_DRIVER_TYPE_HARDWARE = 1
D3D_FEATURE_LEVEL_11_0 = 0xB000
D3D_FEATURE_LEVEL_10_0 = 0xA000
D3D11_SDK_VERSION = 7

DXGI_FORMAT_R8G8B8A8_UNORM = 28

D3D11_USAGE_DEFAULT = 0
D3D11_USAGE_STAGING = 3

D3D11_BIND_SHADER_RESOURCE = 0x8
D3D11_BIND_RENDER_TARGET = 0x20
D3D11_BIND_CONSTANT_BUFFER = 1

D3D11_CPU_ACCESS_READ = 0x20000

D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST = 4

D3D11_MAP_READ = 1

D3D11_FILTER_MIN_MAG_MIP_LINEAR = 0x15
D3D11_TEXTURE_ADDRESS_CLAMP = 3
D3D11_COMPARISON_NEVER = 1

D3D11_RESOURCE_MISC_GENERATE_MIPS = 0x1

RELEASE = 2


class DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", UINT), ("Quality", UINT)]


class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [
        ("Width", UINT), ("Height", UINT), ("MipLevels", UINT), ("ArraySize", UINT),
        ("Format", UINT), ("SampleDesc", DXGI_SAMPLE_DESC),
        ("Usage", UINT), ("BindFlags", UINT), ("CPUAccessFlags", UINT), ("MiscFlags", UINT),
    ]


class D3D11_SUBRESOURCE_DATA(ctypes.Structure):
    _fields_ = [("pSysMem", ctypes.c_void_p), ("SysMemPitch", UINT), ("SysMemSlicePitch", UINT)]


class D3D11_VIEWPORT(ctypes.Structure):
    _fields_ = [("TopLeftX", FLOAT), ("TopLeftY", FLOAT), ("Width", FLOAT),
                ("Height", FLOAT), ("MinDepth", FLOAT), ("MaxDepth", FLOAT)]


class D3D11_BUFFER_DESC(ctypes.Structure):
    _fields_ = [("ByteWidth", UINT), ("Usage", UINT), ("BindFlags", UINT),
                ("CPUAccessFlags", UINT), ("MiscFlags", UINT), ("StructureByteStride", UINT)]


class D3D11_SAMPLER_DESC(ctypes.Structure):
    _fields_ = [
        ("Filter", UINT), ("AddressU", UINT), ("AddressV", UINT), ("AddressW", UINT),
        ("MipLODBias", FLOAT), ("MaxAnisotropy", UINT), ("ComparisonFunc", UINT),
        ("BorderColor", FLOAT * 4), ("MinLOD", FLOAT), ("MaxLOD", FLOAT),
    ]


class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [("pData", ctypes.c_void_p), ("RowPitch", UINT), ("DepthPitch", UINT)]


def _com(ptr, index, restype, argtypes):
    """Call the `index`-th vtable slot on a COM interface pointer.

    Two dereferences required: ptr -> lpVtbl (object's first field) ->
    slot. The previous version only did one hop and read the object's own
    data as a vtable, which is what caused the access-violation crash
    logged as 'GPU warp renderer unavailable' -- see d3d11_warp_poc.py's
    _com for the full explanation, since this is the same bug.
    """
    lpVtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    vtbl = ctypes.cast(lpVtbl, ctypes.POINTER(ctypes.c_void_p * (index + 1))).contents
    func_ty = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return ctypes.cast(vtbl[index], func_ty)


def _call(ptr, index, restype, argtypes, *args):
    return _com(ptr, index, restype, argtypes)(ptr, *args)


# Plain-English hints for the HRESULTs a community tester is actually
# likely to hit -- driver/hardware problems, not app bugs. Keyed by the
# unsigned 32-bit code. Deliberately small: this is the list of things a
# non-dev CAN do something about (update driver, free memory, switch to
# CPU-only), not a full HRESULT reference.
_HRESULT_HINTS = {
    0x80070005: "Access denied -- try running as your normal user (not elevated/admin), or check antivirus/Defender isn't blocking the process.",
    0x8007000E: "Out of memory on the GPU -- close other GPU-heavy apps (games, browsers with hardware acceleration) and try again.",
    0x887A0005: "GPU driver crashed or reset (DXGI_ERROR_DEVICE_REMOVED) -- common right after a driver update or waking from sleep. Update your GPU driver, or restart the app.",
    0x887A0006: "GPU stopped responding to a command (DXGI_ERROR_DEVICE_HUNG) -- usually a driver issue. Update your GPU driver.",
    0x887A0020: "GPU driver reported an internal fault (DXGI_ERROR_DRIVER_INTERNAL_ERROR). Update your GPU driver.",
    0x887A0004: "This GPU/driver doesn't support a feature this app needs (DXGI_ERROR_UNSUPPORTED). Update your GPU driver, or switch to CPU-only in System menu.",
    0x80004005: "Unspecified GPU driver failure (E_FAIL). Update your GPU driver, or switch to CPU-only in System menu if it keeps happening.",
    0x80070057: "Internal bug in how this app is calling Direct3D (E_INVALIDARG), not a hardware problem. Please report this with your GPU model and driver version.",
}


def _describe_hr(hr) -> str:
    code = hr & 0xFFFFFFFF
    hint = _HRESULT_HINTS.get(code)
    return f"0x{code:08X} ({hint})" if hint else f"0x{code:08X} (unrecognized code -- please report this with your GPU model and driver version)"


def _check(hr, what):
    if hr != 0:
        raise RuntimeError(f"{what} failed: hr={_describe_hr(hr)}")


CTX_VSSetConstantBuffers = 7
CTX_PSSetShaderResources = 8
CTX_PSSetShader = 9
CTX_PSSetSamplers = 10
CTX_VSSetShader = 11
CTX_Draw = 13
CTX_Map = 14
CTX_Unmap = 15
CTX_PSSetConstantBuffers = 16
CTX_IASetPrimitiveTopology = 24
CTX_OMSetRenderTargets = 33
CTX_RSSetViewports = 44
CTX_CopyResource = 47
CTX_UpdateSubresource = 48
CTX_ClearRenderTargetView = 50
CTX_GenerateMips = 54

DEV_CreateBuffer = 3
DEV_CreateTexture2D = 5
DEV_CreateShaderResourceView = 7
DEV_CreateRenderTargetView = 9
DEV_CreateVertexShader = 12
DEV_CreatePixelShader = 15
DEV_CreateSamplerState = 23

# ---------------------------------------------------------------------------
# Shaders. Same fullscreen-triangle vertex shader as the POC. Pixel shader
# is the POC's warp math with vignette/manual-clamp removed -- see the
# module docstring for why.
# ---------------------------------------------------------------------------
VERTEX_SHADER_SRC = b"""
struct VSOut { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };
VSOut VSMain(uint vid : SV_VertexID) {
    VSOut o;
    float2 p = float2((vid << 1) & 2, vid & 2);
    o.uv = p;
    o.pos = float4(p * float2(2,-2) + float2(-1,1), 0, 1);
    return o;
}
"""

PIXEL_SHADER_SRC = b"""
cbuffer WarpParams : register(b0) {
    float curvature;
    float3 _pad;
};
Texture2D srcTex : register(t0);
SamplerState samp : register(s0);

struct VSOut { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };

float4 PSMain(VSOut i) : SV_TARGET {
    float nx = i.uv.x * 2.0 - 1.0;
    float ny = i.uv.y * 2.0 - 1.0;
    float r2 = nx * nx + ny * ny;
    float factor = (1.0 + curvature * r2) / (1.0 + curvature);
    float wx = nx * factor;
    float wy = ny * factor;
    float2 srcUV = float2((wx + 1.0) * 0.5, (wy + 1.0) * 0.5);
    // No manual clamp/vignette here on purpose -- see module docstring.
    // The sampler's ADDRESS_CLAMP mode matches cv2.BORDER_REPLICATE, and
    // the CPU side applies its own vignette+corner mask after this.
    float3 color = srcTex.Sample(samp, srcUV).rgb;
    return float4(color, 1.0);
}
"""

# Plain resize passthrough for the bezel-compositing scale (step 5) --
# no warp math at all, just samples the (mip-mapped) source at this
# pixel's own UV. All the actual resize quality comes from the mip chain
# + trilinear sampler (see GPUWarpRenderer.scale's texture setup), not
# from anything in this shader.
PIXEL_SHADER_PASSTHROUGH_SRC = b"""
Texture2D srcTex : register(t0);
SamplerState samp : register(s0);

struct VSOut { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };

float4 PSMain(VSOut i) : SV_TARGET {
    float3 color = srcTex.Sample(samp, i.uv).rgb;
    return float4(color, 1.0);
}
"""


def _compile_shader(source: bytes, entry: bytes, target: bytes) -> bytes:
    d3dcompiler = ctypes.WinDLL("d3dcompiler_47.dll")
    D3DCompile = d3dcompiler.D3DCompile
    code_blob = ctypes.c_void_p()
    err_blob = ctypes.c_void_p()
    hr = D3DCompile(
        source, len(source), None, None, None,
        entry, target, 0, 0,
        ctypes.byref(code_blob), ctypes.byref(err_blob),
    )
    if hr != 0:
        msg = ""
        if err_blob:
            get_ptr = _com(err_blob, 3, ctypes.c_void_p, [])
            get_size = _com(err_blob, 4, ctypes.c_size_t, [])
            ptr = get_ptr(err_blob)
            size = get_size(err_blob)
            msg = ctypes.string_at(ptr, size).decode(errors="replace")
        raise RuntimeError(f"D3DCompile failed (hr=0x{hr & 0xFFFFFFFF:08X}):\n{msg}")
    get_ptr = _com(code_blob, 3, ctypes.c_void_p, [])
    get_size = _com(code_blob, 4, ctypes.c_size_t, [])
    bytecode = ctypes.string_at(get_ptr(code_blob), get_size(code_blob))
    _call(code_blob, RELEASE, ctypes.c_ulong, [])
    if err_blob:
        _call(err_blob, RELEASE, ctypes.c_ulong, [])
    return bytecode


class GPUWarpRenderer:
    """Created once (see get_shared_renderer below) and reused for the
    life of the process. `warp(arr, w, h, curvature)` takes the same
    pygame-surfarray-style (w, h, 3) uint8 view _apply_crt_shader already
    has as `arr`, and returns a (w, h, 3) float32 array -- same shape/
    dtype cv2.remap(...).astype(np.float32) produces today, so the caller
    doesn't need to know which backend did the work.
    """

    def __init__(self):
        d3d11 = ctypes.WinDLL("d3d11.dll")
        device = ctypes.c_void_p()
        context = ctypes.c_void_p()
        feature_level = ctypes.c_uint32()
        feature_levels = (ctypes.c_uint32 * 2)(D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_0)
        hr = d3d11.D3D11CreateDevice(
            None, D3D_DRIVER_TYPE_HARDWARE, None, 0,
            feature_levels, 2, D3D11_SDK_VERSION,
            ctypes.byref(device), ctypes.byref(feature_level), ctypes.byref(context),
        )
        if hr != 0:
            # This is the single most likely failure point for a community
            # tester -- no D3D11-capable adapter at all (remote
            # desktop/RDP sessions, a VM without GPU passthrough, or truly
            # ancient hardware) rather than a driver hiccup on real
            # hardware. Worth its own hint on top of the generic HRESULT
            # table, since boot-time DXGI detection (_detect_gpu_tier)
            # already found SOME adapter before this runs -- so this
            # specific failure usually means detection saw a device that
            # can't actually create a D3D11 HARDWARE device (e.g. RDP's
            # "Microsoft Basic Render Driver" reporting as present but
            # capped below feature level 10.0).
            raise RuntimeError(
                f"D3D11CreateDevice failed: hr={_describe_hr(hr)}. "
                f"If you're on Remote Desktop or a VM without GPU passthrough, "
                f"this is expected -- use CPU-only render mode in System menu. "
                f"On real hardware, this usually means an outdated or missing "
                f"GPU driver.")
        self._device = device
        self._context = context

        vs_bytecode = _compile_shader(VERTEX_SHADER_SRC, b"VSMain", b"vs_4_0")
        ps_bytecode = _compile_shader(PIXEL_SHADER_SRC, b"PSMain", b"ps_4_0")
        ps_passthrough_bytecode = _compile_shader(PIXEL_SHADER_PASSTHROUGH_SRC, b"PSMain", b"ps_4_0")

        vs = ctypes.c_void_p()
        _check(_call(device, DEV_CreateVertexShader, HRESULT,
                     [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     vs_bytecode, len(vs_bytecode), None, ctypes.byref(vs)), "CreateVertexShader")
        ps = ctypes.c_void_p()
        _check(_call(device, DEV_CreatePixelShader, HRESULT,
                     [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     ps_bytecode, len(ps_bytecode), None, ctypes.byref(ps)), "CreatePixelShader")
        ps_passthrough = ctypes.c_void_p()
        _check(_call(device, DEV_CreatePixelShader, HRESULT,
                     [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     ps_passthrough_bytecode, len(ps_passthrough_bytecode), None, ctypes.byref(ps_passthrough)), "CreatePixelShader(passthrough)")
        self._vs, self._ps, self._ps_passthrough = vs, ps, ps_passthrough

        samp_desc = D3D11_SAMPLER_DESC(
            D3D11_FILTER_MIN_MAG_MIP_LINEAR, D3D11_TEXTURE_ADDRESS_CLAMP,
            D3D11_TEXTURE_ADDRESS_CLAMP, D3D11_TEXTURE_ADDRESS_CLAMP,
            0.0, 1, D3D11_COMPARISON_NEVER, (FLOAT * 4)(0, 0, 0, 0), 0.0, 3.4e38,
        )
        sampler = ctypes.c_void_p()
        _check(_call(device, DEV_CreateSamplerState, HRESULT,
                     [ctypes.POINTER(D3D11_SAMPLER_DESC), ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(samp_desc), ctypes.byref(sampler)), "CreateSamplerState")
        self._sampler = sampler

        cb_desc = D3D11_BUFFER_DESC(16, D3D11_USAGE_DEFAULT, D3D11_BIND_CONSTANT_BUFFER, 0, 0, 0)
        cb_init_data = struct.pack("<ffff", 0.0, 0.0, 0.0, 0.0)
        cb_init = D3D11_SUBRESOURCE_DATA(
            ctypes.cast(ctypes.create_string_buffer(cb_init_data), ctypes.c_void_p), 0, 0)
        cbuf = ctypes.c_void_p()
        _check(_call(device, DEV_CreateBuffer, HRESULT,
                     [ctypes.POINTER(D3D11_BUFFER_DESC), ctypes.POINTER(D3D11_SUBRESOURCE_DATA), ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(cb_desc), ctypes.byref(cb_init), ctypes.byref(cbuf)), "CreateBuffer(const)")
        self._cbuf = cbuf

        # Sized resources -- created lazily by _ensure_size() on first
        # warp() call and whenever (w, h) changes.
        self._size = None
        self._src_tex = None
        self._srv = None
        self._rt_tex = None
        self._rtv = None
        self._stage_tex = None
        # Upload/readback scratch buffers, cached per (w, h) too so a
        # steady-state frame at a fixed picture size allocates nothing.
        self._upload_buf = None   # (h, w, 4) uint8, contiguous
        self._readback_out = None  # (w, h, 3) float32, contiguous

        # Scale (step 5, bezel compositing) -- separate sized-resource
        # cache from warp's above, keyed on (src_w, src_h, dst_w, dst_h)
        # since input and output sizes genuinely differ here (unlike
        # warp's same-size-in-out). Lazily created by _ensure_scale_size.
        self._scale_key = None
        self._scale_src_tex = None
        self._scale_srv = None
        self._scale_rt_tex = None
        self._scale_rtv = None
        self._scale_stage_tex = None
        self._scale_upload_buf = None   # (src_h, src_w, 4) uint8
        self._scale_readback_out = None  # (dst_w, dst_h, 3) uint8

    def _release_sized(self):
        for obj in (self._stage_tex, self._rtv, self._rt_tex, self._srv, self._src_tex):
            if obj is not None:
                _call(obj, RELEASE, ctypes.c_ulong, [])
        self._src_tex = self._srv = self._rt_tex = self._rtv = self._stage_tex = None

    def _ensure_size(self, w: int, h: int):
        if self._size == (w, h):
            return
        self._release_sized()
        device = self._device

        src_desc = D3D11_TEXTURE2D_DESC(
            w, h, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_SAMPLE_DESC(1, 0),
            D3D11_USAGE_DEFAULT, D3D11_BIND_SHADER_RESOURCE, 0, 0,
        )
        src_tex = ctypes.c_void_p()
        _check(_call(device, DEV_CreateTexture2D, HRESULT,
                     [ctypes.POINTER(D3D11_TEXTURE2D_DESC), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(src_desc), None, ctypes.byref(src_tex)), "CreateTexture2D(src)")
        srv = ctypes.c_void_p()
        _check(_call(device, DEV_CreateShaderResourceView, HRESULT,
                     [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     src_tex, None, ctypes.byref(srv)), "CreateShaderResourceView")

        rt_desc = D3D11_TEXTURE2D_DESC(
            w, h, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_SAMPLE_DESC(1, 0),
            D3D11_USAGE_DEFAULT, D3D11_BIND_RENDER_TARGET, 0, 0,
        )
        rt_tex = ctypes.c_void_p()
        _check(_call(device, DEV_CreateTexture2D, HRESULT,
                     [ctypes.POINTER(D3D11_TEXTURE2D_DESC), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(rt_desc), None, ctypes.byref(rt_tex)), "CreateTexture2D(rt)")
        rtv = ctypes.c_void_p()
        _check(_call(device, DEV_CreateRenderTargetView, HRESULT,
                     [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     rt_tex, None, ctypes.byref(rtv)), "CreateRenderTargetView")

        stage_desc = D3D11_TEXTURE2D_DESC(
            w, h, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_SAMPLE_DESC(1, 0),
            D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_READ, 0,
        )
        stage_tex = ctypes.c_void_p()
        _check(_call(device, DEV_CreateTexture2D, HRESULT,
                     [ctypes.POINTER(D3D11_TEXTURE2D_DESC), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(stage_desc), None, ctypes.byref(stage_tex)), "CreateTexture2D(stage)")

        self._src_tex, self._srv = src_tex, srv
        self._rt_tex, self._rtv = rt_tex, rtv
        self._stage_tex = stage_tex
        self._size = (w, h)
        self._upload_buf = np.empty((h, w, 4), dtype=np.uint8)
        self._upload_buf[:, :, 3] = 255
        self._readback_out = np.empty((w, h, 3), dtype=np.float32)

    def warp(self, arr: np.ndarray, w: int, h: int, curvature: float) -> np.ndarray:
        """`arr`: pygame-surfarray view, shape (w, h, 3), uint8. Returns a
        fresh-view-safe (w, h, 3) float32 array (values 0-255, same as
        cv2.remap's output cast to float32)."""
        self._ensure_size(w, h)
        device, context = self._device, self._context

        # Pack into (h, w, 4) contiguous RGBA -- D3D wants row-major
        # (height-major) with a real alpha channel; pygame's arr is
        # (width, height, 3). This transpose+copy is the GPU path's own
        # equivalent of the CPU path's non-contiguous src-copy -- unlike
        # that one, this always runs (upload has to happen either way),
        # but it replaces the ~7ms src-copy + ~10ms remap combined, not
        # just one of them.
        np.copyto(self._upload_buf[:, :, :3], arr.transpose(1, 0, 2))
        row_bytes = w * 4
        _call(context, CTX_UpdateSubresource, None,
              [ctypes.c_void_p, UINT, ctypes.c_void_p, ctypes.c_void_p, UINT, UINT],
              self._src_tex, 0, None,
              self._upload_buf.ctypes.data_as(ctypes.c_void_p), row_bytes, 0)

        cb_data = struct.pack("<ffff", float(curvature), 0.0, 0.0, 0.0)
        _call(context, CTX_UpdateSubresource, None,
              [ctypes.c_void_p, UINT, ctypes.c_void_p, ctypes.c_void_p, UINT, UINT],
              self._cbuf, 0, None,
              ctypes.cast(ctypes.create_string_buffer(cb_data), ctypes.c_void_p), 0, 0)

        viewport = D3D11_VIEWPORT(0, 0, float(w), float(h), 0.0, 1.0)
        _call(context, CTX_RSSetViewports, None, [UINT, ctypes.POINTER(D3D11_VIEWPORT)], 1, ctypes.byref(viewport))
        _call(context, CTX_OMSetRenderTargets, None,
              [UINT, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p], 1, ctypes.byref(self._rtv), None)
        _call(context, CTX_IASetPrimitiveTopology, None, [UINT], D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST)
        _call(context, CTX_VSSetShader, None, [ctypes.c_void_p, ctypes.c_void_p, UINT], self._vs, None, 0)
        _call(context, CTX_PSSetShader, None, [ctypes.c_void_p, ctypes.c_void_p, UINT], self._ps, None, 0)
        _call(context, CTX_PSSetShaderResources, None,
              [UINT, UINT, ctypes.POINTER(ctypes.c_void_p)], 0, 1, ctypes.byref(self._srv))
        _call(context, CTX_PSSetSamplers, None,
              [UINT, UINT, ctypes.POINTER(ctypes.c_void_p)], 0, 1, ctypes.byref(self._sampler))
        _call(context, CTX_PSSetConstantBuffers, None,
              [UINT, UINT, ctypes.POINTER(ctypes.c_void_p)], 0, 1, ctypes.byref(self._cbuf))
        _call(context, CTX_Draw, None, [UINT, UINT], 3, 0)

        _call(context, CTX_CopyResource, None, [ctypes.c_void_p, ctypes.c_void_p], self._stage_tex, self._rt_tex)
        mapped = D3D11_MAPPED_SUBRESOURCE()
        hr = _call(context, CTX_Map, HRESULT,
                   [ctypes.c_void_p, UINT, UINT, UINT, ctypes.POINTER(D3D11_MAPPED_SUBRESOURCE)],
                   self._stage_tex, 0, D3D11_MAP_READ, 0, ctypes.byref(mapped))
        _check(hr, "Map(stage)")
        try:
            buf = ctypes.string_at(mapped.pData, mapped.RowPitch * h)
        finally:
            _call(context, CTX_Unmap, None, [ctypes.c_void_p, UINT], self._stage_tex, 0)

        pitch_px = mapped.RowPitch // 4
        hw4 = np.frombuffer(buf, dtype=np.uint8).reshape(h, pitch_px, 4)[:, :w, :3]
        np.copyto(self._readback_out, hw4.transpose(1, 0, 2).astype(np.float32))
        return self._readback_out

    # ------------------------------------------------------------------
    # Bezel-compositing scale (step 5) -- resize source (sw, sh) into a
    # (dw, dh) picture, replacing pygame.transform.smoothscale. Source
    # texture uses a full mip chain so the trilinear sampler handles
    # large downscales the way smoothscale's area-averaging does -- see
    # the module docstring's quality caveat.
    # ------------------------------------------------------------------
    def _release_scale_sized(self):
        for obj in (self._scale_stage_tex, self._scale_rtv, self._scale_rt_tex,
                    self._scale_srv, self._scale_src_tex):
            if obj is not None:
                _call(obj, RELEASE, ctypes.c_ulong, [])
        self._scale_src_tex = self._scale_srv = None
        self._scale_rt_tex = self._scale_rtv = self._scale_stage_tex = None

    def _ensure_scale_size(self, sw: int, sh: int, dw: int, dh: int):
        key = (sw, sh, dw, dh)
        if self._scale_key == key:
            return
        self._release_scale_sized()
        device = self._device

        # MipLevels=0 asks D3D to compute a full chain from (sw, sh) down
        # to 1x1; BindFlags needs RENDER_TARGET too (not just SHADER_
        # RESOURCE) because GenerateMips renders each smaller level from
        # the one above it internally -- that's also why MiscFlags has to
        # say GENERATE_MIPS up front, it can't be added after creation.
        src_desc = D3D11_TEXTURE2D_DESC(
            sw, sh, 0, 1, DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_SAMPLE_DESC(1, 0),
            D3D11_USAGE_DEFAULT, D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET,
            0, D3D11_RESOURCE_MISC_GENERATE_MIPS,
        )
        src_tex = ctypes.c_void_p()
        _check(_call(device, DEV_CreateTexture2D, HRESULT,
                     [ctypes.POINTER(D3D11_TEXTURE2D_DESC), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(src_desc), None, ctypes.byref(src_tex)), "CreateTexture2D(scale-src)")
        srv = ctypes.c_void_p()
        _check(_call(device, DEV_CreateShaderResourceView, HRESULT,
                     [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     src_tex, None, ctypes.byref(srv)), "CreateShaderResourceView(scale-src)")

        rt_desc = D3D11_TEXTURE2D_DESC(
            dw, dh, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_SAMPLE_DESC(1, 0),
            D3D11_USAGE_DEFAULT, D3D11_BIND_RENDER_TARGET, 0, 0,
        )
        rt_tex = ctypes.c_void_p()
        _check(_call(device, DEV_CreateTexture2D, HRESULT,
                     [ctypes.POINTER(D3D11_TEXTURE2D_DESC), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(rt_desc), None, ctypes.byref(rt_tex)), "CreateTexture2D(scale-rt)")
        rtv = ctypes.c_void_p()
        _check(_call(device, DEV_CreateRenderTargetView, HRESULT,
                     [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     rt_tex, None, ctypes.byref(rtv)), "CreateRenderTargetView(scale-rt)")

        stage_desc = D3D11_TEXTURE2D_DESC(
            dw, dh, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_SAMPLE_DESC(1, 0),
            D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_READ, 0,
        )
        stage_tex = ctypes.c_void_p()
        _check(_call(device, DEV_CreateTexture2D, HRESULT,
                     [ctypes.POINTER(D3D11_TEXTURE2D_DESC), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(stage_desc), None, ctypes.byref(stage_tex)), "CreateTexture2D(scale-stage)")

        self._scale_src_tex, self._scale_srv = src_tex, srv
        self._scale_rt_tex, self._scale_rtv = rt_tex, rtv
        self._scale_stage_tex = stage_tex
        self._scale_key = key
        self._scale_upload_buf = np.empty((sh, sw, 4), dtype=np.uint8)
        self._scale_upload_buf[:, :, 3] = 255
        self._scale_readback_out = np.empty((dw, dh, 3), dtype=np.uint8)

    def scale(self, arr: np.ndarray, sw: int, sh: int, dw: int, dh: int) -> np.ndarray:
        """`arr`: pygame-surfarray view, shape (sw, sh, 3), uint8. Returns
        a fresh-view-safe (dw, dh, 3) uint8 array -- feed straight into
        pygame.surfarray.make_surface(), same as smoothscale's return
        value would be blitted."""
        self._ensure_scale_size(sw, sh, dw, dh)
        device, context = self._device, self._context

        np.copyto(self._scale_upload_buf[:, :, :3], arr.transpose(1, 0, 2))
        row_bytes = sw * 4
        _call(context, CTX_UpdateSubresource, None,
              [ctypes.c_void_p, UINT, ctypes.c_void_p, ctypes.c_void_p, UINT, UINT],
              self._scale_src_tex, 0, None,
              self._scale_upload_buf.ctypes.data_as(ctypes.c_void_p), row_bytes, 0)
        # Build the rest of the mip chain from the level-0 upload above --
        # has to happen every frame, the source content changes every
        # frame even though the texture object itself is kept alive.
        _call(context, CTX_GenerateMips, None, [ctypes.c_void_p], self._scale_srv)

        viewport = D3D11_VIEWPORT(0, 0, float(dw), float(dh), 0.0, 1.0)
        _call(context, CTX_RSSetViewports, None, [UINT, ctypes.POINTER(D3D11_VIEWPORT)], 1, ctypes.byref(viewport))
        _call(context, CTX_OMSetRenderTargets, None,
              [UINT, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p], 1, ctypes.byref(self._scale_rtv), None)
        _call(context, CTX_IASetPrimitiveTopology, None, [UINT], D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST)
        _call(context, CTX_VSSetShader, None, [ctypes.c_void_p, ctypes.c_void_p, UINT], self._vs, None, 0)
        _call(context, CTX_PSSetShader, None, [ctypes.c_void_p, ctypes.c_void_p, UINT], self._ps_passthrough, None, 0)
        _call(context, CTX_PSSetShaderResources, None,
              [UINT, UINT, ctypes.POINTER(ctypes.c_void_p)], 0, 1, ctypes.byref(self._scale_srv))
        _call(context, CTX_PSSetSamplers, None,
              [UINT, UINT, ctypes.POINTER(ctypes.c_void_p)], 0, 1, ctypes.byref(self._sampler))
        _call(context, CTX_Draw, None, [UINT, UINT], 3, 0)

        _call(context, CTX_CopyResource, None, [ctypes.c_void_p, ctypes.c_void_p], self._scale_stage_tex, self._scale_rt_tex)
        mapped = D3D11_MAPPED_SUBRESOURCE()
        hr = _call(context, CTX_Map, HRESULT,
                   [ctypes.c_void_p, UINT, UINT, UINT, ctypes.POINTER(D3D11_MAPPED_SUBRESOURCE)],
                   self._scale_stage_tex, 0, D3D11_MAP_READ, 0, ctypes.byref(mapped))
        _check(hr, "Map(scale-stage)")
        try:
            buf = ctypes.string_at(mapped.pData, mapped.RowPitch * dh)
        finally:
            _call(context, CTX_Unmap, None, [ctypes.c_void_p, UINT], self._scale_stage_tex, 0)

        pitch_px = mapped.RowPitch // 4
        hw4 = np.frombuffer(buf, dtype=np.uint8).reshape(dh, pitch_px, 4)[:, :dw, :3]
        np.copyto(self._scale_readback_out, hw4.transpose(1, 0, 2))
        return self._scale_readback_out

    def release(self):
        self._release_sized()
        self._release_scale_sized()
        for obj in (self._cbuf, self._sampler, self._ps_passthrough, self._ps, self._vs, self._context, self._device):
            if obj is not None:
                _call(obj, RELEASE, ctypes.c_ulong, [])


_shared = {"renderer": None, "tried": False, "warned": False}


def get_shared_renderer():
    """Lazily create and cache a single GPUWarpRenderer for the process.
    Returns None (every time, cheaply, after the first attempt) if D3D11
    isn't available on this machine for any reason -- never raises. Callers
    (see _gpu_warp_backend_requested/_apply_crt_shader in
    retro_tv_emulator.py) already only call this when boot-time DXGI
    detection found a real adapter, but this stays defensive regardless
    since a detected adapter is not a 100% guarantee device creation or
    shader compile will also succeed on it.
    """
    if _shared["tried"]:
        return _shared["renderer"]
    _shared["tried"] = True
    try:
        _shared["renderer"] = GPUWarpRenderer()
    except Exception as e:  # noqa: BLE001 - deliberately broad, see docstring
        _shared["renderer"] = None
        if not _shared["warned"]:
            import logging
            logging.getLogger(__name__).warning(
                "GPU warp renderer unavailable -- staying on CPU warp for "
                "the rest of this session (picture will still work, just "
                "without GPU offload; no restart needed to keep playing). "
                "Reason: %s | If this keeps happening, force CPU-only "
                "mode yourself via System menu > render mode, and please "
                "report this line along with your GPU model.", e)
            _shared["warned"] = True
    return _shared["renderer"]
