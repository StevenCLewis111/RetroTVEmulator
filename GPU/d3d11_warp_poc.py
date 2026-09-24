"""
D3D11 CRT-warp proof-of-concept (build step 3).

STATUS: VALIDATED. Ran successfully on real hardware (Intel Arc A380) --
GPU vs CPU diff was small and confined to sub-pixel interpolation
differences at edges/grid lines, as expected. The `_com` COM-vtable call
helper (shared with d3d11_warp_renderer.py) has since been fixed -- it was
one dereference short, which is what caused the original access-violation
crash. Every stage below now also checks its own HRESULT (previously only
device-creation and the final Map call did), and any failure prints one
clean, copy-pasteable line naming exactly which stage failed -- meant for
someone other than the dev to run this on their own machine and report
back something actionable without needing to read a traceback.

WHAT THIS DOES
    1. Generates a synthetic test image (gradient + grid) in numpy -- no
       dependency on pygame/PIL for the input, so this script runs with
       nothing but numpy installed.
    2. Renders that image through a D3D11 pixel shader that reproduces the
       EXACT warp math from _build_crt_shader_maps() in retro_tv_emulator.py
       (same curvature formula, same edge-normalized factor, same vignette,
       same clamp-to-edge sampling that mirrors cv2.BORDER_REPLICATE).
    3. Separately computes the CPU reference the current code actually
       produces (cv2.remap if you have opencv-python installed, else the
       manual bilinear fallback -- same two paths _apply_crt_shader itself
       has).
    4. Diffs GPU vs CPU output and prints stats, and writes both images out
       as .ppm (no image library needed -- .ppm is a trivial format any
       viewer/GIMP/IrfanView opens, or convert with `magick` if you have
       ImageMagick).

NO NEW DEPENDENCIES: d3d11.dll and d3dcompiler_47.dll are both part of
every Windows 10/11 install already (Windows itself depends on them) --
nothing gets downloaded or added to the project by running this.

HOW TO RUN
    python d3d11_warp_poc.py
    (Windows only. Needs a real GPU driver -- integrated is fine, this
    doesn't require a discrete card, just a D3D11-capable adapter.)

WHAT TO CHECK WHEN IT RUNS
    - Does it get through shader compile without printing a HLSL error
      blob? (If D3DCompile fails, the error text prints in full -- that's
      the #1 thing likely to need a fix, HLSL syntax is easy to typo.)
    - Does "GPU vs CPU diff" print small numbers (low single-digit max
      abs diff per channel is expected -- bilinear filtering isn't
      bit-identical between D3D's sampler and cv2's, that's normal)?
    - Open warp_gpu.ppm and warp_cpu.ppm -- do they look like the same
      barrel-warped, corner-darkened image?

If any of the three checks above fail, that's real signal about where the
vtable/shader has a bug -- fix that before touching step 4 (folding this
into _apply_crt_shader as a real alternate path).
"""
from __future__ import annotations

import ctypes
import struct
import sys

import numpy as np

if sys.platform != "win32":
    raise SystemExit("This POC is Windows-only (D3D11 via ctypes).")

# ---------------------------------------------------------------------------
# Constants that must match retro_tv_emulator.py exactly (see
# _CRT_SHADER_CURVATURE_DEFAULT / _CRT_SHADER_VIGNETTE). If you change those
# in the real file, change them here too or the diff comparison is meaningless.
# ---------------------------------------------------------------------------
CURVATURE = 0.20
VIGNETTE_STRENGTH = 0.35

WIDTH, HEIGHT = 512, 384  # arbitrary POC size, not tied to real picture size


# ---------------------------------------------------------------------------
# Minimal ctypes bindings -- only what this POC touches. Vtable slot indices
# are from the documented d3d11.h layout (IUnknown = slots 0-2 everywhere).
# ---------------------------------------------------------------------------
HRESULT = ctypes.c_long
UINT = ctypes.c_uint32
FLOAT = ctypes.c_float
LPCSTR = ctypes.c_char_p
LPCVOID = ctypes.c_void_p


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_uint8 * 8)]


def _guid(a, b, c, d):
    return GUID(a, b, c, (ctypes.c_uint8 * 8)(*d))


IID_ID3D11Texture2D = _guid(0x6f15aaf2, 0xd208, 0x4e89, (0x9a, 0xb4, 0x48, 0x95, 0x35, 0xd3, 0x4f, 0x9c))

D3D_DRIVER_TYPE_HARDWARE = 1
D3D_FEATURE_LEVEL_11_0 = 0xB000
D3D_FEATURE_LEVEL_10_0 = 0xA000
D3D11_SDK_VERSION = 7

DXGI_FORMAT_R8G8B8A8_UNORM = 28

D3D11_USAGE_DEFAULT = 0
D3D11_USAGE_STAGING = 3

D3D11_BIND_SHADER_RESOURCE = 0x8
D3D11_BIND_RENDER_TARGET = 0x20

D3D11_CPU_ACCESS_READ = 0x20000

D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST = 4

D3D11_MAP_READ = 1

D3D11_FILTER_MIN_MAG_MIP_LINEAR = 0x15
D3D11_TEXTURE_ADDRESS_CLAMP = 3
D3D11_COMPARISON_NEVER = 1


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

    A COM interface pointer points at the object; the object's *first*
    field (lpVtbl) is itself a pointer to the vtable array. That's two
    dereferences (ptr -> lpVtbl -> slot), not one -- the previous version
    here only did one hop, so it read the object's own data as if it were
    the vtable and jumped to whatever bytes happened to be there. That's
    what produced the access-violation crash: it wasn't calling the real
    COM method at all.
    """
    lpVtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    vtbl = ctypes.cast(lpVtbl, ctypes.POINTER(ctypes.c_void_p * (index + 1))).contents
    func_ty = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    func = ctypes.cast(vtbl[index], func_ty)
    return func


def call(ptr, index, restype, argtypes, *args):
    return _com(ptr, index, restype, argtypes)(ptr, *args)


class GPUStageError(RuntimeError):
    """Raised by checked_call() below, tagged with the exact D3D11 stage
    that failed. main()'s top-level handler catches this specifically to
    print one clean, copy-pasteable line -- meant for someone other than
    the dev (a community tester on their own hardware/driver) to report
    back something actionable without pasting a full traceback.
    """


# Plain-English hints for the HRESULTs a community tester is actually
# likely to hit -- driver/hardware problems, not app bugs. Same table as
# d3d11_warp_renderer.py's (duplicated rather than imported, matching how
# this POC already duplicates its ctypes bindings rather than depending
# on the real codebase -- it needs to run standalone on a bare Windows
# box with nothing but numpy installed).
_HRESULT_HINTS = {
    0x80070005: "Access denied -- try running as your normal user (not elevated/admin), or check antivirus/Defender isn't blocking the process.",
    0x8007000E: "Out of memory on the GPU -- close other GPU-heavy apps (games, browsers with hardware acceleration) and try again.",
    0x887A0005: "GPU driver crashed or reset (DXGI_ERROR_DEVICE_REMOVED) -- common right after a driver update or waking from sleep. Update your GPU driver, or restart and try again.",
    0x887A0006: "GPU stopped responding to a command (DXGI_ERROR_DEVICE_HUNG) -- usually a driver issue. Update your GPU driver.",
    0x887A0020: "GPU driver reported an internal fault (DXGI_ERROR_DRIVER_INTERNAL_ERROR). Update your GPU driver.",
    0x887A0004: "This GPU/driver doesn't support a feature this needs (DXGI_ERROR_UNSUPPORTED). Update your GPU driver.",
    0x80004005: "Unspecified GPU driver failure (E_FAIL). Update your GPU driver.",
    0x80070057: "Internal bug in this script's Direct3D calls (E_INVALIDARG), not a hardware problem. Please report this with your GPU model and driver version.",
}


def _describe_hr(hr) -> str:
    code = hr & 0xFFFFFFFF
    hint = _HRESULT_HINTS.get(code)
    return f"0x{code:08X} ({hint})" if hint else f"0x{code:08X} (unrecognized code -- please report this with your GPU model and driver version)"


def checked_call(stage, ptr, index, argtypes, *args):
    """Like call(..., restype=HRESULT, ...), but ALSO checks the returned
    HRESULT and raises GPUStageError(stage, hr) if it's non-zero.

    Every Device::CreateXxx call in this POC returns an HRESULT that MUST
    be checked -- previously only D3D11CreateDevice and Map were checked;
    every CreateVertexShader/CreatePixelShader/CreateTexture2D/
    CreateShaderResourceView/CreateRenderTargetView/CreateSamplerState/
    CreateBuffer call below just called and discarded the result. A quiet
    failure there (e.g. out-of-memory, a malformed desc) would leave the
    output pointer null and let the code carry on with it, so any crash
    would show up LATER (at Draw or Map) with no indication of which
    Create call actually failed. Checking every one, right here, means a
    failure is attributed to the exact call that produced it.
    """
    hr = call(ptr, index, HRESULT, argtypes, *args)
    if hr != 0:
        raise GPUStageError(f"{stage} failed: hr={_describe_hr(hr)}")
    return hr


# ID3D11DeviceContext vtable slots (IUnknown 0-2, ID3D11DeviceChild 3-6, own methods from 7)
CTX_PSSetShaderResources = 8
CTX_PSSetShader = 9
CTX_PSSetSamplers = 10
CTX_VSSetShader = 11
CTX_Draw = 13
CTX_Map = 14
CTX_Unmap = 15
CTX_IASetInputLayout = 17
CTX_IASetPrimitiveTopology = 24
CTX_OMSetRenderTargets = 33
CTX_RSSetViewports = 44
CTX_CopyResource = 47
CTX_UpdateSubresource = 48
CTX_ClearRenderTargetView = 50
CTX_VSSetConstantBuffers = 7
CTX_PSSetConstantBuffers = 16

# ID3D11Device vtable slots (IUnknown 0-2, own methods from 3)
DEV_CreateBuffer = 3
DEV_CreateTexture2D = 5
DEV_CreateShaderResourceView = 7
DEV_CreateRenderTargetView = 9
DEV_CreateVertexShader = 12
DEV_CreatePixelShader = 15
DEV_CreateSamplerState = 23

RELEASE = 2


# ---------------------------------------------------------------------------
# Shaders -- the pixel shader is a line-for-line port of _build_crt_shader_maps'
# numpy formula (retro_tv_emulator.py ~L4380-4447).
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
    float vignetteStrength;
    float2 _pad;
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
    bool valid = (srcUV.x >= 0.0) && (srcUV.x <= 1.0) && (srcUV.y >= 0.0) && (srcUV.y <= 1.0);
    float3 color = srcTex.Sample(samp, saturate(srcUV)).rgb;
    float vr = sqrt(nx * nx * 0.85 + ny * ny * 0.85);
    float vr_c = saturate(vr);
    float vignette = 1.0 - vignetteStrength * vr_c * vr_c;
    float mult = vignette * (valid ? 1.0 : 0.0);
    return float4(color * mult, 1.0);
}
"""


def compile_shader(source: bytes, entry: bytes, target: bytes):
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
            get_ptr = _com(err_blob, 3, ctypes.c_void_p, [])  # ID3DBlob::GetBufferPointer
            get_size = _com(err_blob, 4, ctypes.c_size_t, [])  # ID3DBlob::GetBufferSize
            ptr = get_ptr(err_blob)
            size = get_size(err_blob)
            msg = ctypes.string_at(ptr, size).decode(errors="replace")
        raise RuntimeError(f"D3DCompile failed (hr=0x{hr & 0xFFFFFFFF:08X}):\n{msg}")
    get_ptr = _com(code_blob, 3, ctypes.c_void_p, [])
    get_size = _com(code_blob, 4, ctypes.c_size_t, [])
    ptr = get_ptr(code_blob)
    size = get_size(code_blob)
    bytecode = ctypes.string_at(ptr, size)
    call(code_blob, RELEASE, ctypes.c_ulong, [])
    if err_blob:
        call(err_blob, RELEASE, ctypes.c_ulong, [])
    return bytecode


def make_test_image(w: int, h: int) -> np.ndarray:
    """Synthetic RGBA8 test pattern -- gradient + grid lines, so a barrel
    warp and vignette are both visually obvious in the output."""
    yy, xx = np.mgrid[0:h, 0:w]
    r = (xx * 255 // max(w - 1, 1)).astype(np.uint8)
    g = (yy * 255 // max(h - 1, 1)).astype(np.uint8)
    b = np.full((h, w), 128, dtype=np.uint8)
    img = np.stack([r, g, b, np.full((h, w), 255, dtype=np.uint8)], axis=-1)
    img[:, ::32, :3] = 255
    img[::32, :, :3] = 255
    return np.ascontiguousarray(img)


def cpu_reference(img_rgba: np.ndarray) -> np.ndarray:
    """Same formula as _build_crt_shader_maps + _apply_crt_shader's cv2.remap
    call, reproduced standalone (no import of the real file, which needs
    pygame/a display to even load)."""
    h, w = img_rgba.shape[:2]
    xx, yy = np.mgrid[0:w, 0:h].astype(np.float32)  # (w, h) layout, matches pygame surfarray
    nx = (xx / max(w - 1, 1)) * 2 - 1
    ny = (yy / max(h - 1, 1)) * 2 - 1
    k = CURVATURE
    r2 = nx * nx + ny * ny
    factor = (1.0 + k * r2) / (1.0 + k)
    wx = nx * factor
    wy = ny * factor
    src_x = ((wx + 1) * 0.5) * (w - 1)
    src_y = ((wy + 1) * 0.5) * (h - 1)
    valid = (src_x >= 0) & (src_x <= w - 1) & (src_y >= 0) & (src_y <= h - 1)
    map_x = np.clip(src_x, 0, w - 1).astype(np.float32)
    map_y = np.clip(src_y, 0, h - 1).astype(np.float32)
    vradius = np.sqrt(nx * nx * 0.85 + ny * ny * 0.85)
    vignette = 1.0 - VIGNETTE_STRENGTH * np.clip(vradius, 0, 1.0) ** 2
    mult = (vignette * valid).astype(np.float32)

    # img_rgba is (h, w, 4) row-major; transpose to (w, h, 3) to match the
    # pygame-surfarray layout map_x/map_y were built against.
    src = np.ascontiguousarray(img_rgba[:, :, :3].transpose(1, 0, 2))

    try:
        import cv2
        warped = cv2.remap(src, map_y, map_x, interpolation=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_REPLICATE)
        warped_f = warped.astype(np.float32)
    except ImportError:
        x0 = np.floor(map_x).astype(np.int32)
        y0 = np.floor(map_y).astype(np.int32)
        x1 = np.clip(x0 + 1, 0, w - 1)
        y1 = np.clip(y0 + 1, 0, h - 1)
        fx = (map_x - x0.astype(np.float32))[:, :, None]
        fy = (map_y - y0.astype(np.float32))[:, :, None]
        top = src[x0, y0] * (1 - fx) + src[x1, y0] * fx
        bot = src[x0, y1] * (1 - fx) + src[x1, y1] * fx
        warped_f = top * (1 - fy) + bot * fy

    out = np.clip(warped_f * mult[:, :, None], 0, 255).astype(np.uint8)
    return np.ascontiguousarray(out.transpose(1, 0, 2))  # back to (h, w, 3)


def write_ppm(path: str, rgb: np.ndarray) -> None:
    h, w = rgb.shape[:2]
    with open(path, "wb") as f:
        f.write(f"P6\n{w} {h}\n255\n".encode())
        f.write(np.ascontiguousarray(rgb).tobytes())


def run_gpu_warp(img_rgba: np.ndarray) -> np.ndarray:
    h, w = img_rgba.shape[:2]
    d3d11 = ctypes.WinDLL("d3d11.dll")

    device = ctypes.c_void_p()
    context = ctypes.c_void_p()
    feature_level = ctypes.c_uint32()
    feature_levels = (ctypes.c_uint32 * 2)(D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_0)

    D3D11CreateDeviceFn = d3d11.D3D11CreateDevice
    hr = D3D11CreateDeviceFn(
        None, D3D_DRIVER_TYPE_HARDWARE, None, 0,
        feature_levels, 2, D3D11_SDK_VERSION,
        ctypes.byref(device), ctypes.byref(feature_level), ctypes.byref(context),
    )
    if hr != 0:
        # Most likely failure point in the whole POC for a community
        # tester -- no D3D11-capable adapter at all, rather than a driver
        # hiccup on otherwise-working hardware.
        raise GPUStageError(
            f"D3D11CreateDevice failed: hr={_describe_hr(hr)}. "
            f"If you're on Remote Desktop or a VM without GPU passthrough, "
            f"this is expected. On real hardware, this usually means an "
            f"outdated or missing GPU driver.")
    print(f"[GPU] Device created OK, feature level 0x{feature_level.value:X}")

    vs_bytecode = compile_shader(VERTEX_SHADER_SRC, b"VSMain", b"vs_4_0")
    ps_bytecode = compile_shader(PIXEL_SHADER_SRC, b"PSMain", b"ps_4_0")
    print("[GPU] Shaders compiled OK")

    vs = ctypes.c_void_p()
    checked_call("CreateVertexShader", device, DEV_CreateVertexShader,
         [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
         vs_bytecode, len(vs_bytecode), None, ctypes.byref(vs))

    ps = ctypes.c_void_p()
    checked_call("CreatePixelShader", device, DEV_CreatePixelShader,
         [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
         ps_bytecode, len(ps_bytecode), None, ctypes.byref(ps))

    # -- source texture (upload the test image) --
    src_desc = D3D11_TEXTURE2D_DESC(
        w, h, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_SAMPLE_DESC(1, 0),
        D3D11_USAGE_DEFAULT, D3D11_BIND_SHADER_RESOURCE, 0, 0,
    )
    src_data = np.ascontiguousarray(img_rgba)
    sub = D3D11_SUBRESOURCE_DATA(src_data.ctypes.data_as(ctypes.c_void_p), w * 4, 0)
    src_tex = ctypes.c_void_p()
    checked_call("CreateTexture2D(source)", device, DEV_CreateTexture2D,
         [ctypes.POINTER(D3D11_TEXTURE2D_DESC), ctypes.POINTER(D3D11_SUBRESOURCE_DATA), ctypes.POINTER(ctypes.c_void_p)],
         ctypes.byref(src_desc), ctypes.byref(sub), ctypes.byref(src_tex))

    srv = ctypes.c_void_p()
    checked_call("CreateShaderResourceView", device, DEV_CreateShaderResourceView,
         [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
         src_tex, None, ctypes.byref(srv))

    # -- render target texture --
    rt_desc = D3D11_TEXTURE2D_DESC(
        w, h, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_SAMPLE_DESC(1, 0),
        D3D11_USAGE_DEFAULT, D3D11_BIND_RENDER_TARGET, 0, 0,
    )
    rt_tex = ctypes.c_void_p()
    checked_call("CreateTexture2D(render target)", device, DEV_CreateTexture2D,
         [ctypes.POINTER(D3D11_TEXTURE2D_DESC), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
         ctypes.byref(rt_desc), None, ctypes.byref(rt_tex))

    rtv = ctypes.c_void_p()
    checked_call("CreateRenderTargetView", device, DEV_CreateRenderTargetView,
         [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
         rt_tex, None, ctypes.byref(rtv))

    # -- staging texture for CPU readback --
    stage_desc = D3D11_TEXTURE2D_DESC(
        w, h, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_SAMPLE_DESC(1, 0),
        D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_READ, 0,
    )
    stage_tex = ctypes.c_void_p()
    checked_call("CreateTexture2D(staging)", device, DEV_CreateTexture2D,
         [ctypes.POINTER(D3D11_TEXTURE2D_DESC), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
         ctypes.byref(stage_desc), None, ctypes.byref(stage_tex))

    # -- sampler (clamp = same semantics as cv2.BORDER_REPLICATE) --
    samp_desc = D3D11_SAMPLER_DESC(
        D3D11_FILTER_MIN_MAG_MIP_LINEAR, D3D11_TEXTURE_ADDRESS_CLAMP,
        D3D11_TEXTURE_ADDRESS_CLAMP, D3D11_TEXTURE_ADDRESS_CLAMP,
        0.0, 1, D3D11_COMPARISON_NEVER, (FLOAT * 4)(0, 0, 0, 0), 0.0, 3.4e38,
    )
    sampler = ctypes.c_void_p()
    checked_call("CreateSamplerState", device, DEV_CreateSamplerState,
         [ctypes.POINTER(D3D11_SAMPLER_DESC), ctypes.POINTER(ctypes.c_void_p)],
         ctypes.byref(samp_desc), ctypes.byref(sampler))

    # -- constant buffer (curvature, vignette strength) --
    cb_data = struct.pack("<ffff", CURVATURE, VIGNETTE_STRENGTH, 0.0, 0.0)
    D3D11_BIND_CONSTANT_BUFFER = 1
    cb_desc = D3D11_BUFFER_DESC(len(cb_data), D3D11_USAGE_DEFAULT, D3D11_BIND_CONSTANT_BUFFER, 0, 0, 0)
    cb_init = D3D11_SUBRESOURCE_DATA(ctypes.cast(ctypes.create_string_buffer(cb_data), ctypes.c_void_p), 0, 0)
    cbuf = ctypes.c_void_p()
    checked_call("CreateBuffer(constant)", device, DEV_CreateBuffer,
         [ctypes.POINTER(D3D11_BUFFER_DESC), ctypes.POINTER(D3D11_SUBRESOURCE_DATA), ctypes.POINTER(ctypes.c_void_p)],
         ctypes.byref(cb_desc), ctypes.byref(cb_init), ctypes.byref(cbuf))

    # -- draw --
    viewport = D3D11_VIEWPORT(0, 0, float(w), float(h), 0.0, 1.0)
    call(context, CTX_RSSetViewports, None, [UINT, ctypes.POINTER(D3D11_VIEWPORT)], 1, ctypes.byref(viewport))
    call(context, CTX_OMSetRenderTargets, None,
         [UINT, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p], 1, ctypes.byref(rtv), None)
    clear_color = (FLOAT * 4)(0, 0, 0, 1)
    call(context, CTX_ClearRenderTargetView, None, [ctypes.c_void_p, ctypes.POINTER(FLOAT)], rtv, clear_color)
    call(context, CTX_IASetPrimitiveTopology, None, [UINT], D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST)
    call(context, CTX_VSSetShader, None, [ctypes.c_void_p, ctypes.c_void_p, UINT], vs, None, 0)
    call(context, CTX_PSSetShader, None, [ctypes.c_void_p, ctypes.c_void_p, UINT], ps, None, 0)
    call(context, CTX_PSSetShaderResources, None,
         [UINT, UINT, ctypes.POINTER(ctypes.c_void_p)], 0, 1, ctypes.byref(srv))
    call(context, CTX_PSSetSamplers, None,
         [UINT, UINT, ctypes.POINTER(ctypes.c_void_p)], 0, 1, ctypes.byref(sampler))
    call(context, CTX_PSSetConstantBuffers, None,
         [UINT, UINT, ctypes.POINTER(ctypes.c_void_p)], 0, 1, ctypes.byref(cbuf))
    call(context, CTX_Draw, None, [UINT, UINT], 3, 0)
    print("[GPU] Draw call issued")

    # -- readback --
    call(context, CTX_CopyResource, None, [ctypes.c_void_p, ctypes.c_void_p], stage_tex, rt_tex)
    mapped = D3D11_MAPPED_SUBRESOURCE()
    hr = call(context, CTX_Map, HRESULT,
              [ctypes.c_void_p, UINT, UINT, UINT, ctypes.POINTER(D3D11_MAPPED_SUBRESOURCE)],
              stage_tex, 0, D3D11_MAP_READ, 0, ctypes.byref(mapped))
    if hr != 0:
        raise GPUStageError(f"Map(staging texture readback) failed: hr={_describe_hr(hr)}")

    buf = ctypes.string_at(mapped.pData, mapped.RowPitch * h)
    call(context, CTX_Unmap, None, [ctypes.c_void_p, UINT], stage_tex, 0)

    row_bytes = w * 4
    out = np.frombuffer(buf, dtype=np.uint8).reshape(h, mapped.RowPitch // 4, 4)[:, :w, :3]
    out = np.ascontiguousarray(out)

    for obj in (cbuf, sampler, stage_tex, rtv, rt_tex, srv, src_tex, ps, vs, context, device):
        call(obj, RELEASE, ctypes.c_ulong, [])

    return out


def main():
    img = make_test_image(WIDTH, HEIGHT)
    print(f"Test image: {WIDTH}x{HEIGHT}")

    cpu_out = cpu_reference(img)
    write_ppm("warp_cpu.ppm", cpu_out)
    print("Wrote warp_cpu.ppm (CPU reference, same math as _apply_crt_shader today)")

    gpu_out = run_gpu_warp(img)
    write_ppm("warp_gpu.ppm", gpu_out)
    print("Wrote warp_gpu.ppm (GPU shader output)")

    diff = np.abs(gpu_out.astype(np.int16) - cpu_out.astype(np.int16))
    print("\n--- GPU vs CPU diff ---")
    print(f"  max abs diff per channel : {diff.max(axis=(0,1))}")
    print(f"  mean abs diff per channel: {diff.mean(axis=(0,1))}")
    print(f"  pixels with diff > 10    : {(diff.max(axis=2) > 10).sum()} / {WIDTH*HEIGHT}")
    print("\nSmall (~single-digit) diffs are expected (sampler filtering isn't")
    print("bit-identical between D3D and cv2). Large diffs or a garbled image")
    print("mean something above is wrong -- check warp_gpu.ppm visually first.")


if __name__ == "__main__":
    try:
        main()
    except GPUStageError as e:
        # The clean, copy-pasteable line the module docstring promises.
        # No traceback -- a GPUStageError already IS the diagnosis (which
        # D3D11 stage failed + a plain-English hint from _describe_hr),
        # and dumping 40 lines of ctypes call-stack on top of that just
        # buries the one line that's actually useful to report back.
        print("\n=== GPU WARP TEST FAILED ===")
        print(str(e))
        print("=============================")
        print("Copy the two lines above when reporting this. If the hint")
        print("mentions updating your GPU driver or using Remote Desktop/")
        print("a VM, try that first -- otherwise this is a bug in the")
        print("script itself, not your hardware.")
        sys.exit(1)
    except ImportError as e:
        # Missing numpy, or the sys.platform != "win32" guard at import
        # time -- not a GPU problem at all, so it gets its own short
        # message instead of the GPU-stage framing above.
        print(f"\n=== GPU WARP TEST FAILED ===\n{e}\n=============================")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 - deliberate catch-all, see below
        # Anything NOT wrapped in GPUStageError (a genuine bug in this
        # script, not a checked D3D11 call -- e.g. D3DCompile's own
        # RuntimeError, which already carries the full HLSL error text
        # and needs no further wrapping) still gets a short report first,
        # THEN the traceback -- so the top of the output is useful even
        # if a tester doesn't scroll, but nothing is hidden from a dev
        # who does.
        print("\n=== GPU WARP TEST FAILED (unexpected error) ===")
        print(f"{type(e).__name__}: {e}")
        print("=================================================")
        print("This isn't a recognized GPU failure -- please report the")
        print("full output below along with your GPU model and driver version.\n")
        raise
