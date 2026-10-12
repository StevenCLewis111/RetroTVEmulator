"""
D3D11 / DXGI core for the GPU frame path: adapter choice, device, swapchain on the app window, resize, present.

Owns: the one copy of the COM call helpers and HRESULT hints for the GPU files, adapter enumeration and choice, the D3D11
device and immediate context, the DXGI swapchain bound to the pygame window, the back-buffer render target view, back-buffer
readback, and the device-lost signal.
Does not own: shaders, textures for picture content, the bezel, or the decision to use the GPU (see d3d11_gpu_frame.py and
the GPU gate in retro_tv_emulator.py).

Main thread only: the device is created single-threaded.
"""
from __future__ import annotations

import ctypes
import logging
import sys

import numpy as np

if sys.platform != "win32":
    raise ImportError("d3d11_gpu_core is Windows-only (D3D11 via ctypes).")

log = logging.getLogger(__name__)

# ============================================================================
# SECTION: ctypes bindings & constants
# ============================================================================
HRESULT = ctypes.c_long
UINT = ctypes.c_uint32
INT = ctypes.c_int32
FLOAT = ctypes.c_float
BOOL = ctypes.c_int32
VOIDP = ctypes.c_void_p

D3D_DRIVER_TYPE_UNKNOWN = 0
D3D_FEATURE_LEVEL_11_0 = 0xB000
D3D_FEATURE_LEVEL_10_0 = 0xA000
D3D11_SDK_VERSION = 7
D3D11_CREATE_DEVICE_SINGLETHREADED = 0x1
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20

DXGI_FORMAT_UNKNOWN = 0
DXGI_FORMAT_B8G8R8A8_UNORM = 87
DXGI_USAGE_RENDER_TARGET_OUTPUT = 0x20
DXGI_SWAP_EFFECT_DISCARD = 0
DXGI_MWA_NO_WINDOW_CHANGES = 0x1
DXGI_MWA_NO_ALT_ENTER = 0x2
DXGI_ADAPTER_FLAG_SOFTWARE = 0x2
MONITOR_DEFAULTTONEAREST = 2

D3D11_USAGE_STAGING = 3
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_MAP_READ = 1

VENDOR_MICROSOFT = 0x1414
RELEASE = 2
_MAX_ADAPTERS = 16
_MAX_OUTPUTS = 16

ADAPTER_POLICY_MONITOR = "monitor"
ADAPTER_POLICY_FASTEST = "fastest"
# WARNING: must equal ADAPTER_POLICY_NAMED_PREFIX in d3d11_gpu_gate.py; the gate builds the policy text and this module reads it.
ADAPTER_POLICY_NAMED_PREFIX = "adapter:"


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_uint8 * 8)]


def _guid(a, b, c, d):
    return GUID(a, b, c, (ctypes.c_uint8 * 8)(*d))


IID_IDXGIFactory1 = _guid(0x770aae78, 0xf26f, 0x4dba, (0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))
IID_ID3D11Texture2D = _guid(0x6f15aaf2, 0xd208, 0x4e89, (0x9a, 0xb4, 0x48, 0x95, 0x35, 0xd3, 0x4f, 0x9c))


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class DXGI_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", UINT), ("Denominator", UINT)]


class DXGI_MODE_DESC(ctypes.Structure):
    _fields_ = [("Width", UINT), ("Height", UINT), ("RefreshRate", DXGI_RATIONAL),
                ("Format", UINT), ("ScanlineOrdering", UINT), ("Scaling", UINT)]


class DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", UINT), ("Quality", UINT)]


class DXGI_SWAP_CHAIN_DESC(ctypes.Structure):
    _fields_ = [("BufferDesc", DXGI_MODE_DESC), ("SampleDesc", DXGI_SAMPLE_DESC),
                ("BufferUsage", UINT), ("BufferCount", UINT), ("OutputWindow", VOIDP),
                ("Windowed", BOOL), ("SwapEffect", UINT), ("Flags", UINT)]


class DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", UINT), ("DeviceId", UINT),
                ("SubSysId", UINT), ("Revision", UINT), ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t), ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", LUID), ("Flags", UINT)]


class RECT(ctypes.Structure):
    _fields_ = [("left", INT), ("top", INT), ("right", INT), ("bottom", INT)]


class DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [("DeviceName", ctypes.c_wchar * 32), ("DesktopCoordinates", RECT),
                ("AttachedToDesktop", BOOL), ("Rotation", UINT), ("Monitor", VOIDP)]


class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [("Width", UINT), ("Height", UINT), ("MipLevels", UINT), ("ArraySize", UINT),
                ("Format", UINT), ("SampleDesc", DXGI_SAMPLE_DESC), ("Usage", UINT),
                ("BindFlags", UINT), ("CPUAccessFlags", UINT), ("MiscFlags", UINT)]


class D3D11_VIEWPORT(ctypes.Structure):
    _fields_ = [("TopLeftX", FLOAT), ("TopLeftY", FLOAT), ("Width", FLOAT),
                ("Height", FLOAT), ("MinDepth", FLOAT), ("MaxDepth", FLOAT)]


class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [("pData", VOIDP), ("RowPitch", UINT), ("DepthPitch", UINT)]


# ============================================================================
# SECTION: COM vtable slots
# ============================================================================
# WARNING: a wrong slot number calls a different method and can crash the app; slots below follow the d3d11.h / dxgi.h method order.
FACTORY_MakeWindowAssociation = 8
FACTORY_CreateSwapChain = 10
FACTORY_EnumAdapters1 = 12
ADAPTER_EnumOutputs = 7
ADAPTER1_GetDesc1 = 10
OUTPUT_GetDesc = 7
SWAP_Present = 8
SWAP_GetBuffer = 9
SWAP_ResizeBuffers = 13
DEV_CreateTexture2D = 5
DEV_CreateRenderTargetView = 9
CTX_Map = 14
CTX_Unmap = 15
CTX_OMSetRenderTargets = 33
CTX_RSSetViewports = 44
CTX_CopyResource = 47
CTX_ClearRenderTargetView = 50


# ============================================================================
# SECTION: COM call helpers
# ============================================================================
_proto_cache: dict = {}


def _com(ptr, index, restype, argtypes):
    """Returns the `index`-th vtable method of a COM pointer as a callable; prototypes are cached per vtable slot."""
    # WARNING: two dereferences are required (object -> lpVtbl -> slot); one hop calls into object data and crashes.
    lp_vtbl = ctypes.cast(ptr, ctypes.POINTER(VOIDP)).contents.value
    key = (lp_vtbl, index, restype, tuple(argtypes))
    func = _proto_cache.get(key)
    if func is None:
        vtbl = ctypes.cast(lp_vtbl, ctypes.POINTER(VOIDP * (index + 1))).contents
        func_ty = ctypes.WINFUNCTYPE(restype, VOIDP, *argtypes)
        func = ctypes.cast(vtbl[index], func_ty)
        _proto_cache[key] = func
    return func


def call(ptr, index, restype, argtypes, *args):
    return _com(ptr, index, restype, argtypes)(ptr, *args)


def release(ptr) -> None:
    """Releases a COM pointer if it is set; never raises."""
    try:
        if ptr is not None and (ptr.value if isinstance(ptr, VOIDP) else ptr):
            call(ptr, RELEASE, ctypes.c_ulong, [])
    except Exception:
        pass


class GPUCoreError(RuntimeError):
    """A D3D11 / DXGI stage failed."""


class DeviceLostError(GPUCoreError):
    """The GPU device was removed, reset or hung; the caller must drop back to the CPU path."""


_DEVICE_LOST_CODES = {0x887A0005, 0x887A0006, 0x887A0007, 0x887A0020}

_HRESULT_HINTS = {
    0x80070005: "Access denied -- run as your normal user (not admin) or check antivirus is not blocking the process.",
    0x8007000E: "Out of memory on the GPU -- close other GPU-heavy apps.",
    0x887A0005: "GPU device removed (DXGI_ERROR_DEVICE_REMOVED) -- driver crash, driver update or wake from sleep.",
    0x887A0006: "GPU stopped responding (DXGI_ERROR_DEVICE_HUNG) -- usually a driver issue.",
    0x887A0007: "GPU device reset (DXGI_ERROR_DEVICE_RESET).",
    0x887A0001: "Invalid call (DXGI_ERROR_INVALID_CALL) -- a bug in how the app calls DXGI, not hardware.",
    0x887A0004: "Feature not supported by this GPU or driver (DXGI_ERROR_UNSUPPORTED).",
    0x887A0020: "GPU driver internal fault (DXGI_ERROR_DRIVER_INTERNAL_ERROR) -- update the GPU driver.",
    0x80004005: "Unspecified GPU driver failure (E_FAIL) -- update the GPU driver.",
    0x80070057: "Invalid argument (E_INVALIDARG) -- a bug in how the app calls Direct3D, not hardware.",
}


def describe_hr(hr) -> str:
    code = hr & 0xFFFFFFFF
    hint = _HRESULT_HINTS.get(code)
    return f"0x{code:08X} ({hint})" if hint else f"0x{code:08X} (unrecognized code)"


def check_hr(hr, stage) -> int:
    """Raises on a failed HRESULT (negative); success codes such as DXGI_STATUS_OCCLUDED pass."""
    if hr < 0:
        text = f"{stage} failed: hr={describe_hr(hr)}"
        if (hr & 0xFFFFFFFF) in _DEVICE_LOST_CODES:
            raise DeviceLostError(text)
        raise GPUCoreError(text)
    return hr


def checked_call(stage, ptr, index, argtypes, *args):
    return check_hr(call(ptr, index, HRESULT, argtypes, *args), stage)


# ============================================================================
# SECTION: Adapter enumeration and choice
# ============================================================================


class AdapterInfo:
    """One DXGI adapter: its COM pointer, name, memory, software flag and the monitors it drives."""

    def __init__(self, ptr, desc, monitors):
        self.ptr = ptr
        self.name = desc.Description
        self.vendor_id = int(desc.VendorId)
        self.device_id = int(desc.DeviceId)
        self.dedicated_mb = int(desc.DedicatedVideoMemory) // (1024 * 1024)
        self.software = bool(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) or self.vendor_id == VENDOR_MICROSOFT
        self.monitors = monitors


def _enumerate_adapters(factory):
    adapters = []
    for index in range(_MAX_ADAPTERS):
        ptr = VOIDP()
        hr = call(factory, FACTORY_EnumAdapters1, HRESULT, [UINT, ctypes.POINTER(VOIDP)], index, ctypes.byref(ptr))
        if hr < 0:
            break
        desc = DXGI_ADAPTER_DESC1()
        hr = call(ptr, ADAPTER1_GetDesc1, HRESULT, [ctypes.POINTER(DXGI_ADAPTER_DESC1)], ctypes.byref(desc))
        if hr < 0:
            release(ptr)
            continue
        monitors = []
        for out_index in range(_MAX_OUTPUTS):
            out = VOIDP()
            hr = call(ptr, ADAPTER_EnumOutputs, HRESULT, [UINT, ctypes.POINTER(VOIDP)], out_index, ctypes.byref(out))
            if hr < 0:
                break
            odesc = DXGI_OUTPUT_DESC()
            if call(out, OUTPUT_GetDesc, HRESULT, [ctypes.POINTER(DXGI_OUTPUT_DESC)], ctypes.byref(odesc)) >= 0:
                if odesc.Monitor:
                    monitors.append(int(odesc.Monitor))
            release(out)
        adapters.append(AdapterInfo(ptr, desc, monitors))
    return adapters


def choose_adapter(adapters, monitor, policy):
    """Returns (adapter, reason): the adapter named by an "adapter:<name>" policy, else the hardware adapter that drives `monitor`, else the one with the most dedicated memory."""
    usable = [a for a in adapters if not a.software]
    if not usable:
        raise GPUCoreError("no hardware GPU adapter found (only software adapters)")
    if policy.startswith(ADAPTER_POLICY_NAMED_PREFIX):
        wanted = policy[len(ADAPTER_POLICY_NAMED_PREFIX):]
        for a in usable:
            if a.name == wanted:
                return a, "chosen in the GPU/CPU button"
        policy = ADAPTER_POLICY_MONITOR
        reason_prefix = "chosen adapter %r not found; " % wanted
    else:
        reason_prefix = ""
    if policy == ADAPTER_POLICY_MONITOR and monitor:
        driving = [a for a in usable if monitor in a.monitors]
        if driving:
            return driving[0], reason_prefix + "drives the window's monitor"
    best = max(usable, key=lambda a: a.dedicated_mb)
    return best, reason_prefix + "largest dedicated memory"


def _monitor_of_window(hwnd) -> int:
    user32 = ctypes.WinDLL("user32")
    user32.MonitorFromWindow.argtypes = [VOIDP, UINT]
    user32.MonitorFromWindow.restype = VOIDP
    handle = user32.MonitorFromWindow(VOIDP(hwnd), MONITOR_DEFAULTTONEAREST)
    return int(handle) if handle else 0


# ============================================================================
# SECTION: GPUCore
# ============================================================================


class GPUCore:
    """Device, context and swapchain bound to the window `hwnd`; Present() shows the back buffer on that window."""

    def __init__(self, hwnd: int, width: int, height: int,
                 adapter_policy: str = ADAPTER_POLICY_MONITOR, sync_interval: int = 0):
        if not hwnd:
            raise GPUCoreError("GPUCore: no window handle")
        self._hwnd = hwnd
        self._width = max(1, int(width))
        self._height = max(1, int(height))
        self._sync_interval = int(sync_interval)
        self._closed = False
        self._factory = VOIDP()
        self._adapters = []
        self._device = VOIDP()
        self._context = VOIDP()
        self._swapchain = VOIDP()
        self._backbuffer = VOIDP()
        self._rtv = VOIDP()
        self._staging = VOIDP()
        self._staging_size = None
        self._listeners = []
        self.info = {}
        try:
            self._create(adapter_policy)
        except Exception:
            self.close()
            raise

    # -- setup ---------------------------------------------------------------
    def _create(self, policy):
        dxgi = ctypes.WinDLL("dxgi")
        dxgi.CreateDXGIFactory1.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(VOIDP)]
        dxgi.CreateDXGIFactory1.restype = HRESULT
        check_hr(dxgi.CreateDXGIFactory1(ctypes.byref(IID_IDXGIFactory1), ctypes.byref(self._factory)),
                 "CreateDXGIFactory1")

        self._adapters = _enumerate_adapters(self._factory)
        monitor = _monitor_of_window(self._hwnd)
        adapter, reason = choose_adapter(self._adapters, monitor, policy)

        d3d11 = ctypes.WinDLL("d3d11")
        d3d11.D3D11CreateDevice.argtypes = [VOIDP, UINT, VOIDP, UINT, ctypes.POINTER(ctypes.c_uint32), UINT, UINT,
                                            ctypes.POINTER(VOIDP), ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(VOIDP)]
        d3d11.D3D11CreateDevice.restype = HRESULT
        levels = (ctypes.c_uint32 * 2)(D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_0)
        feature_level = ctypes.c_uint32()
        flags = D3D11_CREATE_DEVICE_SINGLETHREADED | D3D11_CREATE_DEVICE_BGRA_SUPPORT
        hr = d3d11.D3D11CreateDevice(adapter.ptr, D3D_DRIVER_TYPE_UNKNOWN, None, flags, levels, 2, D3D11_SDK_VERSION,
                                     ctypes.byref(self._device), ctypes.byref(feature_level), ctypes.byref(self._context))
        check_hr(hr, "D3D11CreateDevice(%s)" % adapter.name)

        desc = DXGI_SWAP_CHAIN_DESC(
            DXGI_MODE_DESC(self._width, self._height, DXGI_RATIONAL(0, 0), DXGI_FORMAT_B8G8R8A8_UNORM, 0, 0),
            DXGI_SAMPLE_DESC(1, 0), DXGI_USAGE_RENDER_TARGET_OUTPUT, 1, VOIDP(self._hwnd), 1,
            DXGI_SWAP_EFFECT_DISCARD, 0)
        checked_call("Factory::CreateSwapChain", self._factory, FACTORY_CreateSwapChain,
                     [VOIDP, ctypes.POINTER(DXGI_SWAP_CHAIN_DESC), ctypes.POINTER(VOIDP)],
                     self._device, ctypes.byref(desc), ctypes.byref(self._swapchain))
        checked_call("Factory::MakeWindowAssociation", self._factory, FACTORY_MakeWindowAssociation,
                     [VOIDP, UINT], VOIDP(self._hwnd), DXGI_MWA_NO_WINDOW_CHANGES | DXGI_MWA_NO_ALT_ENTER)
        self._create_back_buffer_view()

        self.info = {
            "adapter": adapter.name, "vendor_id": adapter.vendor_id, "dedicated_mb": adapter.dedicated_mb,
            "feature_level": "0x%X" % feature_level.value, "chosen_because": reason, "policy": policy,
            "monitor_matched": monitor in adapter.monitors if monitor else False,
            "adapters": ["%s%s (%d MB, monitors=%d)" % (a.name, " [software]" if a.software else "",
                                                       a.dedicated_mb, len(a.monitors)) for a in self._adapters],
        }
        for a in self._adapters:
            if a is not adapter:
                release(a.ptr)
        self._adapters = [adapter]

    def _create_back_buffer_view(self):
        checked_call("SwapChain::GetBuffer", self._swapchain, SWAP_GetBuffer,
                     [UINT, ctypes.POINTER(GUID), ctypes.POINTER(VOIDP)],
                     0, ctypes.byref(IID_ID3D11Texture2D), ctypes.byref(self._backbuffer))
        checked_call("CreateRenderTargetView(back buffer)", self._device, DEV_CreateRenderTargetView,
                     [VOIDP, VOIDP, ctypes.POINTER(VOIDP)], self._backbuffer, None, ctypes.byref(self._rtv))

    # -- properties ------------------------------------------------------------
    @property
    def device(self):
        return self._device

    @property
    def context(self):
        return self._context

    @property
    def size(self):
        return self._width, self._height

    @property
    def back_buffer_rtv(self):
        return self._rtv

    def describe(self) -> str:
        i = self.info
        return ("adapter=%s dedicated=%dMB feature_level=%s chosen_because=%s monitor_matched=%s all=[%s]" % (
            i.get("adapter"), i.get("dedicated_mb", 0), i.get("feature_level"), i.get("chosen_because"),
            i.get("monitor_matched"), "; ".join(i.get("adapters", []))))

    # -- resize ------------------------------------------------------------------
    def add_resize_listener(self, on_release, on_recreate) -> None:
        """Registers callbacks: on_release() before the buffers are resized, on_recreate(width, height) after."""
        self._listeners.append((on_release, on_recreate))

    def _unbind_targets(self):
        call(self._context, CTX_OMSetRenderTargets, None, [UINT, ctypes.POINTER(VOIDP), VOIDP], 0, None, None)

    def resize(self, width: int, height: int) -> None:
        """Resizes the back buffer to the window's client size; a no-op when the size is unchanged."""
        self._require_open()
        width, height = max(1, int(width)), max(1, int(height))
        if (width, height) == (self._width, self._height):
            return
        self._unbind_targets()
        for on_release, _ in self._listeners:
            on_release()
        self._release_staging()
        release(self._rtv)
        release(self._backbuffer)
        self._rtv = VOIDP()
        self._backbuffer = VOIDP()
        checked_call("SwapChain::ResizeBuffers", self._swapchain, SWAP_ResizeBuffers,
                     [UINT, UINT, UINT, UINT, UINT], 0, width, height, DXGI_FORMAT_UNKNOWN, 0)
        self._width, self._height = width, height
        self._create_back_buffer_view()
        for _, on_recreate in self._listeners:
            on_recreate(width, height)

    # -- per frame -----------------------------------------------------------------
    def begin_frame(self):
        """Binds the back buffer as the render target with a full-size viewport and returns its view."""
        self._require_open()
        call(self._context, CTX_OMSetRenderTargets, None, [UINT, ctypes.POINTER(VOIDP), VOIDP],
             1, ctypes.byref(self._rtv), None)
        vp = D3D11_VIEWPORT(0.0, 0.0, float(self._width), float(self._height), 0.0, 1.0)
        call(self._context, CTX_RSSetViewports, None, [UINT, ctypes.POINTER(D3D11_VIEWPORT)], 1, ctypes.byref(vp))
        return self._rtv

    def present(self) -> int:
        """Presents the back buffer; returns the success HRESULT (an occluded window is not an error)."""
        self._require_open()
        return checked_call("SwapChain::Present", self._swapchain, SWAP_Present, [UINT, UINT], self._sync_interval, 0)

    # -- readback ----------------------------------------------------------------------
    def _release_staging(self):
        release(self._staging)
        self._staging = VOIDP()
        self._staging_size = None

    def _ensure_staging(self):
        if self._staging_size == (self._width, self._height):
            return
        self._release_staging()
        desc = D3D11_TEXTURE2D_DESC(self._width, self._height, 1, 1, DXGI_FORMAT_B8G8R8A8_UNORM,
                                    DXGI_SAMPLE_DESC(1, 0), D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_READ, 0)
        checked_call("CreateTexture2D(readback staging)", self._device, DEV_CreateTexture2D,
                     [ctypes.POINTER(D3D11_TEXTURE2D_DESC), VOIDP, ctypes.POINTER(VOIDP)],
                     ctypes.byref(desc), None, ctypes.byref(self._staging))
        self._staging_size = (self._width, self._height)

    def read_back_buffer(self) -> np.ndarray:
        """Copies the current back buffer to the CPU as a (height, width, 4) uint8 BGRA array; for the self-check only."""
        self._require_open()
        self._ensure_staging()
        call(self._context, CTX_CopyResource, None, [VOIDP, VOIDP], self._staging, self._backbuffer)
        mapped = D3D11_MAPPED_SUBRESOURCE()
        checked_call("Map(readback staging)", self._context, CTX_Map,
                     [VOIDP, UINT, UINT, UINT, ctypes.POINTER(D3D11_MAPPED_SUBRESOURCE)],
                     self._staging, 0, D3D11_MAP_READ, 0, ctypes.byref(mapped))
        try:
            raw = ctypes.string_at(mapped.pData, mapped.RowPitch * self._height)
        finally:
            call(self._context, CTX_Unmap, None, [VOIDP, UINT], self._staging, 0)
        rows = np.frombuffer(raw, dtype=np.uint8).reshape(self._height, mapped.RowPitch // 4, 4)
        return rows[:, :self._width, :].copy()

    def probe(self) -> dict:
        """Clears the back buffer to a known colour and reads it back; raises GPUCoreError if the device does not return it."""
        self.begin_frame()
        color = (FLOAT * 4)(0.25, 0.5, 0.75, 1.0)
        call(self._context, CTX_ClearRenderTargetView, None, [VOIDP, ctypes.POINTER(FLOAT)], self._rtv, color)
        px = self.read_back_buffer()
        got = tuple(int(v) for v in px[self._height // 2, self._width // 2])
        want = (191, 128, 64, 255)
        if any(abs(g - w) > 1 for g, w in zip(got, want)):
            raise GPUCoreError("back-buffer probe returned BGRA %s, expected about %s" % (got, want))
        return {"pixel_bgra": got}

    # -- teardown ------------------------------------------------------------------------
    def _require_open(self):
        if self._closed:
            raise GPUCoreError("GPUCore is closed")

    def close(self) -> None:
        """Releases everything in reverse order; safe to call twice and after a failed constructor."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._context:
                self._unbind_targets()
        except Exception:
            pass
        for on_release, _ in self._listeners:
            try:
                on_release()
            except Exception:
                pass
        self._listeners = []
        for obj in (self._staging, self._rtv, self._backbuffer, self._swapchain, self._context, self._device,
                    self._factory):
            release(obj)
        for a in self._adapters:
            release(a.ptr)
        self._adapters = []
        self._staging = self._rtv = self._backbuffer = self._swapchain = VOIDP()
        self._context = self._device = self._factory = VOIDP()
