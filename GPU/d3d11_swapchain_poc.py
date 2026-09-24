"""
D3D11/DXGI swapchain proof-of-coexistence (build step 4, Stage 1).

STATUS: UNTESTED -- handed off for a live run, same as d3d11_warp_poc.py
was before it got validated. This is a different question from that POC:
d3d11_warp_poc.py proved the warp *shader math* matches the CPU path on
real hardware, off-screen, with no window involved at all. This module
proves (or disproves) something orthogonal: can a DXGI swapchain Present
directly to the *same HWND* pygame/SDL already owns and is still calling
pygame.display.flip() on elsewhere in the app -- through resize, maximize,
alt-tab, and fullscreen toggle -- without the two presentation paths
fighting each other for the window.

That's the actual go/no-go gate for the whole GPU-warp project: if SDL
and a DXGI swapchain can't peacefully co-own one HWND, direct-to-swapchain
presentation (Stage 2+) isn't viable at all and the project needs a
different shape (e.g. render-to-texture then hand the result back to SDL
some other way, with all the round-trip cost that was trying to avoid).

WHAT THIS DOES
    SwapchainPresenter binds a DXGI swapchain to an existing native window
    handle (the same HWND pygame_hwnd already points at in
    retro_tv_emulator.py) and Present()s an animated solid-color test
    pattern on demand -- nothing read from or drawn by pygame is touched.
    A caller drives it by calling .present() once per frame instead of (or
    interleaved with -- see retro_tv_emulator.py's dev-hotkey wiring)
    pygame.display.flip().

    This intentionally does NOT reuse d3d11_warp_poc.py's device -- a
    device created via plain D3D11CreateDevice has no swapchain attached,
    and DXGI's own rules make retrofitting one onto an existing device
    more ceremony (walk device -> IDXGIDevice -> IDXGIAdapter -> IDXGIFactory,
    then IDXGIFactory::CreateSwapChain) than just creating device+swapchain
    together via D3D11CreateDeviceAndSwapChain, which takes the target HWND
    directly. Simpler surface area for a throwaway coexistence test.

NO NEW DEPENDENCIES: same as d3d11_warp_poc.py -- d3d11.dll and DXGI are
both already part of every Windows 10/11 install.

HOW THIS GETS RUN
    Not meant to run standalone in the normal case (there's no window to
    bind to without a real app). __main__ below creates its own throwaway
    Win32 window with ctypes for a quick manual smoke test
    (`python d3d11_swapchain_poc.py`) -- close the window or Ctrl+C to
    stop. The real test is via retro_tv_emulator.py's hidden dev hotkey
    (Ctrl+Shift+F9), which binds this to the actual running app window
    instead of a throwaway one.

WHAT TO CHECK WHEN IT RUNS (real-app hotkey test)
    - Does the window immediately start pulsing through colors, replacing
      whatever pygame had drawn, with no crash/black flash/flicker storm?
    - Drag/resize the window, maximize it, alt-tab away and back, toggle
      fullscreen -- does the pulse keep animating smoothly through all of
      that, or does it freeze / go black / throw?
    - Toggle the hotkey off again -- does pygame's own rendering resume
      cleanly on the next frame with no leftover swapchain content stuck
      on screen?
    - Check the log for `[SWAPCHAIN POC]` lines -- device/swapchain
      creation and any Present() failures are logged there.

If any of those fail, that's the real signal about whether direct-to-
swapchain presentation is viable at all here -- resolve it before writing
any more Stage 2+ code (the actual content upload/shader path), since
Stage 2 depends entirely on this working.
"""
from __future__ import annotations

import ctypes
import sys
import time

if sys.platform != "win32":
    raise SystemExit("This POC is Windows-only (D3D11/DXGI via ctypes).")

# ---------------------------------------------------------------------------
# ctypes bindings -- deliberately duplicated from d3d11_warp_poc.py rather
# than imported from it (same reasoning as that file gives for duplicating
# its own bindings rather than depending on the real codebase): this needs
# to run standalone, and "swapchain POC imports warp POC" would make two
# throwaway scripts depend on each other for no real benefit.
# ---------------------------------------------------------------------------
HRESULT = ctypes.c_long
UINT = ctypes.c_uint32
INT = ctypes.c_int32
FLOAT = ctypes.c_float
BOOL = ctypes.c_int32
HWND = ctypes.c_void_p


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
DXGI_USAGE_RENDER_TARGET_OUTPUT = 0x20
DXGI_SWAP_EFFECT_DISCARD = 0  # legacy bitblt model -- broadest compat, no flip-model constraints
DXGI_MODE_SCANLINE_ORDER_UNSPECIFIED = 0
DXGI_MODE_SCALING_UNSPECIFIED = 0

RELEASE = 2


class DXGI_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", UINT), ("Denominator", UINT)]


class DXGI_MODE_DESC(ctypes.Structure):
    _fields_ = [
        ("Width", UINT), ("Height", UINT), ("RefreshRate", DXGI_RATIONAL),
        ("Format", UINT), ("ScanlineOrdering", UINT), ("Scaling", UINT),
    ]


class DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", UINT), ("Quality", UINT)]


class DXGI_SWAP_CHAIN_DESC(ctypes.Structure):
    _fields_ = [
        ("BufferDesc", DXGI_MODE_DESC), ("SampleDesc", DXGI_SAMPLE_DESC),
        ("BufferUsage", UINT), ("BufferCount", UINT), ("OutputWindow", HWND),
        ("Windowed", BOOL), ("SwapEffect", UINT), ("Flags", UINT),
    ]


def _com(ptr, index, restype, argtypes):
    """Call the `index`-th vtable slot on a COM interface pointer.

    Two dereferences: ptr -> lpVtbl -> slot. See d3d11_warp_poc.py's
    matching helper for the access-violation history behind that comment
    -- kept here verbatim since it's exactly this bug class that a fresh
    set of vtable-slot bindings is most likely to reintroduce.
    """
    lpVtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    vtbl = ctypes.cast(lpVtbl, ctypes.POINTER(ctypes.c_void_p * (index + 1))).contents
    func_ty = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    func = ctypes.cast(vtbl[index], func_ty)
    return func


def call(ptr, index, restype, argtypes, *args):
    return _com(ptr, index, restype, argtypes)(ptr, *args)


class GPUStageError(RuntimeError):
    """Raised on any failed D3D11/DXGI stage, tagged with which one."""


_HRESULT_HINTS = {
    0x80070005: "Access denied -- try running as your normal user (not elevated/admin), or check antivirus/Defender isn't blocking the process.",
    0x8007000E: "Out of memory on the GPU -- close other GPU-heavy apps and try again.",
    0x887A0005: "GPU driver crashed or reset (DXGI_ERROR_DEVICE_REMOVED) -- common right after a driver update or waking from sleep.",
    0x887A0006: "GPU stopped responding to a command (DXGI_ERROR_DEVICE_HUNG) -- usually a driver issue.",
    0x887A0001: "Invalid call (DXGI_ERROR_INVALID_CALL) -- likely a bug in this script's swapchain setup, not a hardware problem.",
    0x887A0020: "GPU driver reported an internal fault (DXGI_ERROR_DRIVER_INTERNAL_ERROR). Update your GPU driver.",
    0x80004005: "Unspecified GPU driver failure (E_FAIL). Update your GPU driver.",
    0x80070057: "Internal bug in this script's Direct3D/DXGI calls (E_INVALIDARG), not a hardware problem.",
}


def _describe_hr(hr) -> str:
    code = hr & 0xFFFFFFFF
    hint = _HRESULT_HINTS.get(code)
    return f"0x{code:08X} ({hint})" if hint else f"0x{code:08X} (unrecognized code)"


def checked_call(stage, ptr, index, argtypes, *args):
    hr = call(ptr, index, HRESULT, argtypes, *args)
    if hr != 0:
        raise GPUStageError(f"{stage} failed: hr={_describe_hr(hr)}")
    return hr


# ID3D11DeviceContext vtable slots (same layout as d3d11_warp_poc.py -- only
# the subset this POC actually calls)
CTX_OMSetRenderTargets = 33
CTX_ClearRenderTargetView = 50

# ID3D11Device vtable slots
DEV_CreateRenderTargetView = 9

# IDXGISwapChain vtable slots (IUnknown 0-2, IDXGIObject 3-6, own methods from 7)
SWAP_Present = 8
SWAP_GetBuffer = 9
SWAP_ResizeBuffers = 13


def _make_swap_chain_desc(hwnd: int, width: int, height: int) -> DXGI_SWAP_CHAIN_DESC:
    return DXGI_SWAP_CHAIN_DESC(
        DXGI_MODE_DESC(max(1, width), max(1, height), DXGI_RATIONAL(0, 0),
                       DXGI_FORMAT_R8G8B8A8_UNORM,
                       DXGI_MODE_SCANLINE_ORDER_UNSPECIFIED, DXGI_MODE_SCALING_UNSPECIFIED),
        DXGI_SAMPLE_DESC(1, 0),
        DXGI_USAGE_RENDER_TARGET_OUTPUT, 1, HWND(hwnd), 1,
        DXGI_SWAP_EFFECT_DISCARD, 0,
    )


class SwapchainPresenter:
    """Binds a DXGI swapchain directly to an existing HWND and Presents an
    animated solid-color test pattern. See module docstring -- this is
    Stage 1 of the GPU-warp project: proving SDL and a DXGI swapchain can
    coexist on one window, nothing more. Not wired to any real content.

    Every public method is exception-safe in the sense that failures raise
    GPUStageError (or let ImportError/OSError through for a missing DLL) --
    callers (the dev-hotkey wiring in retro_tv_emulator.py) are expected to
    catch broadly, log, and fall back to plain pygame.display.flip() on any
    failure rather than let a Present() hiccup take down the main loop.
    """

    def __init__(self, hwnd: int, width: int, height: int):
        if not hwnd:
            raise GPUStageError("SwapchainPresenter: no HWND given (pygame_hwnd was None/0)")
        self._hwnd = hwnd
        self._d3d11 = ctypes.WinDLL("d3d11.dll")
        self._device = ctypes.c_void_p()
        self._context = ctypes.c_void_p()
        self._swapchain = ctypes.c_void_p()
        self._rtv = ctypes.c_void_p()
        self._backbuffer = ctypes.c_void_p()
        self._width = max(1, width)
        self._height = max(1, height)
        self._t0 = time.perf_counter()
        self._create_device_and_swapchain()
        self._create_render_target_view()

    # -- setup -----------------------------------------------------------
    def _create_device_and_swapchain(self) -> None:
        desc = _make_swap_chain_desc(self._hwnd, self._width, self._height)
        feature_level = ctypes.c_uint32()
        feature_levels = (ctypes.c_uint32 * 2)(D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_0)
        D3D11CreateDeviceAndSwapChain = self._d3d11.D3D11CreateDeviceAndSwapChain
        hr = D3D11CreateDeviceAndSwapChain(
            None, D3D_DRIVER_TYPE_HARDWARE, None, 0,
            feature_levels, 2, D3D11_SDK_VERSION,
            ctypes.byref(desc), ctypes.byref(self._swapchain),
            ctypes.byref(self._device), ctypes.byref(feature_level), ctypes.byref(self._context),
        )
        if hr != 0:
            raise GPUStageError(
                f"D3D11CreateDeviceAndSwapChain failed: hr={_describe_hr(hr)}. "
                f"If you're on Remote Desktop or a VM without GPU passthrough, "
                f"this is expected.")

    def _create_render_target_view(self) -> None:
        checked_call("SwapChain::GetBuffer", self._swapchain, SWAP_GetBuffer,
                     [UINT, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                     0, ctypes.byref(IID_ID3D11Texture2D), ctypes.byref(self._backbuffer))
        checked_call("CreateRenderTargetView", self._device, DEV_CreateRenderTargetView,
                     [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     self._backbuffer, None, ctypes.byref(self._rtv))

    # -- per-frame ---------------------------------------------------------
    def present(self) -> None:
        """Clear the backbuffer to an animated color and Present it. This
        is the entire "test pattern" -- see module docstring for why a
        plain animated clear is enough to answer Stage 1's actual
        question (does presentation coexist), without needing a shader
        pipeline at all.
        """
        call(self._context, CTX_OMSetRenderTargets, None,
             [UINT, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p],
             1, ctypes.byref(self._rtv), None)
        t = time.perf_counter() - self._t0
        # Smooth RGB pulse, ~6s period per channel, phase-offset per
        # channel so it reads as an obvious color cycle rather than a
        # simple fade -- deliberately gaudy so it's unmistakable which
        # presentation path is currently driving the window.
        import math
        r = 0.5 + 0.5 * math.sin(t * 1.1)
        g = 0.5 + 0.5 * math.sin(t * 1.1 + 2.094)
        b = 0.5 + 0.5 * math.sin(t * 1.1 + 4.188)
        clear_color = (FLOAT * 4)(r, g, b, 1.0)
        call(self._context, CTX_ClearRenderTargetView, None,
             [ctypes.c_void_p, ctypes.POINTER(FLOAT)], self._rtv, clear_color)
        checked_call("SwapChain::Present", self._swapchain, SWAP_Present, [UINT, UINT], 1, 0)

    def resize(self, width: int, height: int) -> None:
        """Must be called whenever the bound window's client size changes
        (resize, maximize/restore, fullscreen toggle) -- the backbuffer
        and its view have to be released and recreated at the new size
        before the next Present(), or DXGI either stretches stale content
        or refuses the call outright.
        """
        width, height = max(1, width), max(1, height)
        if width == self._width and height == self._height:
            return
        call(self._rtv, RELEASE, ctypes.c_ulong, [])
        call(self._backbuffer, RELEASE, ctypes.c_ulong, [])
        self._rtv = ctypes.c_void_p()
        self._backbuffer = ctypes.c_void_p()
        checked_call("SwapChain::ResizeBuffers", self._swapchain, SWAP_ResizeBuffers,
                     [UINT, UINT, UINT, UINT, UINT], 0, width, height, DXGI_FORMAT_R8G8B8A8_UNORM, 0)
        self._width, self._height = width, height
        self._create_render_target_view()

    def close(self) -> None:
        for obj in (self._rtv, self._backbuffer, self._swapchain, self._context, self._device):
            try:
                if obj and obj.value:
                    call(obj, RELEASE, ctypes.c_ulong, [])
            except Exception:
                pass
        self._rtv = self._backbuffer = self._swapchain = self._context = self._device = ctypes.c_void_p()


# ---------------------------------------------------------------------------
# Standalone smoke test -- creates its own throwaway window rather than
# needing the real app running. Only exercises presenter creation + a
# present loop, NOT the resize path (retro_tv_emulator.py's hotkey test is
# what exercises resize/maximize/alt-tab/fullscreen against the real
# window and its real event loop).
# ---------------------------------------------------------------------------
def _run_standalone_smoke_test() -> None:
    import ctypes.wintypes as wt

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    WM_DESTROY = 0x0002
    WM_CLOSE = 0x0010

    def _wndproc(hwnd, msg, wparam, lparam):
        if msg in (WM_DESTROY, WM_CLOSE):
            ctypes.windll.user32.PostQuitMessage(0)
            return 0
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    class WNDCLASS(ctypes.Structure):
        _fields_ = [
            ("style", UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", INT), ("cbWndExtra", INT),
            ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
            ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
        ]

    hinstance = ctypes.windll.kernel32.GetModuleHandleW(None)
    wndproc_cb = WNDPROC(_wndproc)
    wc = WNDCLASS(0, wndproc_cb, 0, 0, hinstance, None, None, None, None, "SwapchainPOCWindow")
    ctypes.windll.user32.RegisterClassW(ctypes.byref(wc))
    hwnd = ctypes.windll.user32.CreateWindowExW(
        0, "SwapchainPOCWindow", "D3D11 Swapchain Coexistence POC",
        0x00CF0000,  # WS_OVERLAPPEDWINDOW
        100, 100, 640, 480, None, None, hinstance, None)
    ctypes.windll.user32.ShowWindow(hwnd, 5)

    print(f"[SWAPCHAIN POC] throwaway window created, hwnd={hwnd}")
    presenter = SwapchainPresenter(hwnd, 640, 480)
    print("[SWAPCHAIN POC] device + swapchain created OK, presenting -- close the window to stop")

    msg = wt.MSG()
    running = True
    try:
        while running:
            while ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == 0x0012:  # WM_QUIT
                    running = False
                    break
                ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
                ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
            if not running:
                break
            presenter.present()
            time.sleep(1 / 60)
    finally:
        presenter.close()
        print("[SWAPCHAIN POC] closed cleanly")


if __name__ == "__main__":
    try:
        _run_standalone_smoke_test()
    except GPUStageError as e:
        print("\n=== SWAPCHAIN POC FAILED ===")
        print(str(e))
        print("=============================")
        sys.exit(1)
