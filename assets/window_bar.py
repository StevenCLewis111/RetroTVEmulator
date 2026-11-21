"""The app's own blue top bar for the windowed TV, with the minimize, maximize or restore, and close pictures; Windows itself still moves, minimizes, maximizes and closes the window."""
import ctypes
import logging
import os
import sys

import pygame

try:
    import performance_governor as _governor
except Exception:
    _governor = None

log = logging.getLogger(__name__)

# WARNING: the fallback numbers below must match WINDOW_BAR_HEIGHT_PX, WINDOW_BAR_BUTTON_HEIGHT_PX, WINDOW_BAR_BUTTON_WIDTH_PX, WINDOW_BAR_BUTTON_GAP_PX, WINDOW_BAR_EDGE_PX, WINDOW_BAR_TITLE_PX, WINDOW_BAR_HOVER_LIGHTEN, WINDOW_BAR_PRESS_DARKEN, COLOR_MENU_BUTTON_BLUE and COLOR_WINDOW_BAR_TITLE in performance_governor.py; they are used only when that module fails to import.
BAR_HEIGHT = _governor.WINDOW_BAR_HEIGHT_PX if _governor is not None else 34
BUTTON_HEIGHT = _governor.WINDOW_BAR_BUTTON_HEIGHT_PX if _governor is not None else 28
BUTTON_WIDTH = _governor.WINDOW_BAR_BUTTON_WIDTH_PX if _governor is not None else 30
BUTTON_GAP = _governor.WINDOW_BAR_BUTTON_GAP_PX if _governor is not None else 6
EDGE = _governor.WINDOW_BAR_EDGE_PX if _governor is not None else 8
TITLE_PX = _governor.WINDOW_BAR_TITLE_PX if _governor is not None else 20
HOVER_LIGHTEN = _governor.WINDOW_BAR_HOVER_LIGHTEN if _governor is not None else 45
PRESS_DARKEN = _governor.WINDOW_BAR_PRESS_DARKEN if _governor is not None else 60
BAR_COLOR = _governor.COLOR_MENU_BUTTON_BLUE if _governor is not None else (40, 78, 145)
TITLE_COLOR = _governor.COLOR_WINDOW_BAR_TITLE if _governor is not None else (255, 255, 255)

TITLE = "Retro TV Emulator"
# WARNING: owner 2026-11-18: the single square (Windows4) sits on a normal window and makes it full screen windowed (maximized); the two squares (Windows3) sit on a full screen windowed TV and take it to fullscreen, the owner's manual way into fullscreen; minimize (-) is how a full screen windowed TV comes back to the normal window.
BUTTON_FILES = {"minimize": "Windows1.png", "close": "Windows2.png", "to_maximized": "Windows4.png", "to_fullscreen": "Windows3.png"}
BUTTON_ORDER = ("minimize", "size", "close")

# WARNING: Windows message and hit-test codes the bar answers; every other message goes on to the window's own handler unchanged.
_WM_GETMINMAXINFO = 0x0024
_WM_NCHITTEST = 0x0084
_WM_NCMOUSEMOVE = 0x00A0
_WM_NCLBUTTONDOWN = 0x00A1
_WM_NCLBUTTONUP = 0x00A2
_WM_NCLBUTTONDBLCLK = 0x00A3
_WM_MOUSEMOVE = 0x0200
_WM_NCMOUSELEAVE = 0x02A2
_WM_SYSCOMMAND = 0x0112
_HTCAPTION = 0x02
_HTMINBUTTON = 0x08
_HTMAXBUTTON = 0x09
_HTCLOSE = 0x14
_SC_MINIMIZE = 0xF020
_SC_MAXIMIZE = 0xF030
_SC_RESTORE = 0xF120
_SC_CLOSE = 0xF060
_GWLP_WNDPROC = -4
_TME_LEAVE = 0x00000002
_TME_NONCLIENT = 0x00000010
_MONITOR_DEFAULTTONEAREST = 0x02
_SWP_FLAGS = 0x0002 | 0x0001 | 0x0004 | 0x0020

_HIT_FOR = {"minimize": _HTMINBUTTON, "size": _HTMAXBUTTON, "close": _HTCLOSE}
_BUTTON_HITS = frozenset(_HIT_FOR.values())

_state = {"hwnd": None, "orig": None, "proc": None, "installed": False, "active": False, "width": 0,
          "hover": None, "pressed": None, "folder": None, "failed_once": False}
_pictures = {}
_bar_cache = {"key": None, "surface": None}
_title_font = {"font": None}


def supported():
    """True on Windows, where the bar can stand in for the Windows title bar."""
    return sys.platform == "win32"


def ready():
    """True once the bar is hooked to the window, so windowed modes are made without the Windows frame."""
    return _state["installed"]


def height():
    """The bar's height in pixels while it is shown (windowed), 0 otherwise."""
    return BAR_HEIGHT if (_state["installed"] and _state["active"]) else 0


def set_picture_folder(folder):
    """Sets the folder that holds the button pictures (main/images/controls)."""
    if folder != _state["folder"]:
        _state["folder"] = folder
        _pictures.clear()
        _bar_cache["key"] = None


def set_active(active, width=None):
    """Shows the bar (windowed) or takes it away (fullscreen); width is the window's width in pixels."""
    _state["active"] = bool(active) and _state["installed"]
    if width is not None:
        _state["width"] = int(width)
    if not _state["active"]:
        _state["hover"] = None
        _state["pressed"] = None


def set_width(width):
    """Tells the bar how wide the window is now, so the buttons Windows is told about sit where they are drawn."""
    _state["width"] = int(width)


def button_rects(width):
    """Where the three buttons sit on a bar `width` pixels wide, by name (minimize, size, close), as pygame rectangles."""
    w = BUTTON_WIDTH
    y = (BAR_HEIGHT - BUTTON_HEIGHT) // 2
    rects = {}
    x = int(width) - EDGE - w
    for name in reversed(BUTTON_ORDER):
        rects[name] = pygame.Rect(x, y, w, BUTTON_HEIGHT)
        x -= w + BUTTON_GAP
    return rects


def hit_code(x, y):
    """The Windows hit-test code for a point in the window's own pixels: a button, the bar (drag), or None below the bar."""
    if y < 0 or y >= BAR_HEIGHT:
        return None
    for name, rect in button_rects(_state["width"]).items():
        if rect.collidepoint(x, y):
            return _HIT_FOR[name]
    return _HTCAPTION


def _picture(name):
    """The button picture `name`, scaled to the button size, or None when it is missing (logged once)."""
    if name in _pictures:
        return _pictures[name]
    picture = None
    folder = _state["folder"]
    path = os.path.join(folder, BUTTON_FILES[name]) if folder else BUTTON_FILES[name]
    try:
        loaded = pygame.image.load(path)
        loaded = loaded.convert_alpha() if pygame.display.get_surface() is not None else loaded
        picture = pygame.transform.smoothscale(loaded, (BUTTON_WIDTH, BUTTON_HEIGHT))
    except Exception as error:
        log.warning("[WINDOW BAR] button picture %s could not be loaded, a plain sign is drawn: %s", path, error)
    _pictures[name] = picture
    return picture


def _draw_sign(surface, name, rect):
    """A plain white sign for a button whose picture is missing."""
    inner = rect.inflate(-rect.width // 2, -rect.height // 2)
    if name == "minimize":
        pygame.draw.line(surface, TITLE_COLOR, (inner.left, inner.bottom), (inner.right, inner.bottom), 2)
    elif name == "to_fullscreen":
        pygame.draw.rect(surface, TITLE_COLOR, inner.move(inner.width // 4, -inner.height // 4).inflate(-inner.width // 3, -inner.height // 3), 2)
        pygame.draw.rect(surface, TITLE_COLOR, inner.move(-inner.width // 8, inner.height // 8).inflate(-inner.width // 3, -inner.height // 3), 2)
    elif name == "close":
        pygame.draw.line(surface, TITLE_COLOR, inner.topleft, inner.bottomright, 2)
        pygame.draw.line(surface, TITLE_COLOR, inner.bottomleft, inner.topright, 2)
    else:
        pygame.draw.rect(surface, TITLE_COLOR, inner, 2)


def bar_surface(width, maximized):
    """The bar picture for a window `width` pixels wide: blue, the title, and the three buttons (the single square on a normal window, the two squares while full screen windowed), lit while the mouse is over one; made again only when something on it changes."""
    key = (int(width), bool(maximized), _state["hover"], _state["pressed"], _state["folder"])
    if _bar_cache["key"] == key:
        return _bar_cache["surface"]
    surface = pygame.Surface((max(1, int(width)), BAR_HEIGHT), pygame.SRCALPHA)
    surface.fill(tuple(BAR_COLOR) + (255,))
    if _title_font["font"] is None:
        try:
            _title_font["font"] = pygame.font.Font(None, TITLE_PX)
        except Exception as error:
            log.warning("[WINDOW BAR] the title font could not be made, the bar has no title: %s", error)
            _title_font["font"] = False
    if _title_font["font"]:
        text = _title_font["font"].render(TITLE, True, TITLE_COLOR)
        surface.blit(text, (EDGE, (BAR_HEIGHT - text.get_height()) // 2))
    for name, rect in button_rects(width).items():
        picture_name = name if name != "size" else ("to_fullscreen" if maximized else "to_maximized")
        picture = _picture(picture_name)
        if picture is None:
            _draw_sign(surface, picture_name, rect)
        else:
            surface.blit(picture, rect.topleft)
        hit = _HIT_FOR[name]
        if _state["pressed"] == hit:
            surface.fill((PRESS_DARKEN, PRESS_DARKEN, PRESS_DARKEN, 0), rect, special_flags=pygame.BLEND_RGBA_SUB)
        elif _state["hover"] == hit:
            surface.fill((HOVER_LIGHTEN, HOVER_LIGHTEN, HOVER_LIGHTEN, 0), rect, special_flags=pygame.BLEND_RGBA_ADD)
    _bar_cache["key"] = key
    _bar_cache["surface"] = surface
    return surface


def cache_key():
    """What the last bar picture was made for, so a caller keeping a copy of it knows when to make its copy again."""
    return _bar_cache["key"]


def draw(window_surface, maximized):
    """Draws the bar along the top of the window's own picture while it is shown."""
    if height() <= 0:
        return
    window_surface.blit(bar_surface(window_surface.get_width(), maximized), (0, 0))


def content_surface(surface):
    """The part of the window below the bar (the whole window when no bar is shown, or when `surface` is not the window)."""
    top = height()
    if top <= 0 or surface is None or surface is not pygame.display.get_surface():
        return surface
    w, h = surface.get_size()
    return surface.subsurface((0, top, w, max(1, h - top)))


def _command_for(hit, hwnd):
    """The Windows command a button click stands for."""
    if hit == _HTMINBUTTON:
        return _SC_MINIMIZE
    if hit == _HTCLOSE:
        return _SC_CLOSE
    return _SC_RESTORE if ctypes.windll.user32.IsZoomed(hwnd) else _SC_MAXIMIZE


def _fit_to_work_area(hwnd, lparam):
    """Makes a maximized window cover the screen it is on without the taskbar, as a window with the Windows frame does."""
    from ctypes import wintypes

    class _MINMAXINFO(ctypes.Structure):
        _fields_ = [("ptReserved", wintypes.POINT), ("ptMaxSize", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                    ("ptMinTrackSize", wintypes.POINT), ("ptMaxTrackSize", wintypes.POINT)]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
    user32 = ctypes.windll.user32
    monitor = user32.MonitorFromWindow(wintypes.HWND(hwnd), _MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return
    mmi = _MINMAXINFO.from_address(lparam)
    work, whole = info.rcWork, info.rcMonitor
    mmi.ptMaxPosition.x = work.left - whole.left
    mmi.ptMaxPosition.y = work.top - whole.top
    mmi.ptMaxSize.x = work.right - work.left
    mmi.ptMaxSize.y = work.bottom - work.top
    mmi.ptMaxTrackSize.x = max(mmi.ptMaxTrackSize.x, mmi.ptMaxSize.x)
    mmi.ptMaxTrackSize.y = max(mmi.ptMaxTrackSize.y, mmi.ptMaxSize.y)


def _track_leave(hwnd):
    """Asks Windows to say when the mouse leaves the bar, so a lit button goes dark again."""
    from ctypes import wintypes

    class _TRACKMOUSEEVENT(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]
    track = _TRACKMOUSEEVENT(ctypes.sizeof(_TRACKMOUSEEVENT), _TME_LEAVE | _TME_NONCLIENT, hwnd, 0)
    ctypes.windll.user32.TrackMouseEvent(ctypes.byref(track))


def _handle(hwnd, msg, wparam, lparam):
    """The bar's answer to one window message, or None to let the window's own handler answer."""
    if not _state["active"]:
        return None
    if msg == _WM_NCHITTEST:
        from ctypes import wintypes
        point = wintypes.POINT(ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value)
        ctypes.windll.user32.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(point))
        return hit_code(point.x, point.y)
    if msg in (_WM_NCLBUTTONDOWN, _WM_NCLBUTTONDBLCLK) and wparam in _BUTTON_HITS:
        _state["pressed"] = wparam
        return 0
    if msg == _WM_NCLBUTTONUP and wparam in _BUTTON_HITS:
        pressed, _state["pressed"] = _state["pressed"], None
        if pressed == wparam:
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd), _WM_SYSCOMMAND, _command_for(wparam, hwnd), 0)
        return 0
    if msg == _WM_NCMOUSEMOVE:
        _state["hover"] = wparam if wparam in _BUTTON_HITS else None
        if _state["pressed"] is not None and _state["pressed"] != wparam:
            _state["pressed"] = None
        _track_leave(hwnd)
        return None
    if msg in (_WM_NCMOUSELEAVE, _WM_MOUSEMOVE):
        _state["hover"] = None
        if msg == _WM_NCMOUSELEAVE:
            _state["pressed"] = None
        return None
    return None


def install(hwnd):
    """Hooks the bar to the window `hwnd`; True when it is hooked (or already was), False when Windows refused (logged), and the Windows frame stays."""
    if not supported() or not hwnd:
        return False
    if _state["installed"] and _state["hwnd"] == hwnd:
        return True
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        lresult = ctypes.c_ssize_t
        proc_type = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.CallWindowProcW.restype = lresult
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]

        def _proc(h, msg, wparam, lparam):
            try:
                if msg == _WM_GETMINMAXINFO and _state["active"]:
                    result = user32.CallWindowProcW(_state["orig"], h, msg, wparam, lparam)
                    _fit_to_work_area(h, lparam)
                    return result
                answer = _handle(h, msg, wparam, lparam)
                if answer is not None:
                    return answer
            except Exception as error:
                if not _state["failed_once"]:
                    _state["failed_once"] = True
                    log.warning("[WINDOW BAR] a window message could not be answered, the window's own handler answered it: %s", error)
            return user32.CallWindowProcW(_state["orig"], h, msg, wparam, lparam)
        proc = proc_type(_proc)
        orig = user32.SetWindowLongPtrW(hwnd, _GWLP_WNDPROC, ctypes.cast(proc, ctypes.c_void_p))
        if not orig:
            log.warning("[WINDOW BAR] Windows did not hook the bar to the window (error %d); the Windows title bar stays", ctypes.windll.kernel32.GetLastError())
            return False
        _state.update(hwnd=hwnd, orig=orig, proc=proc, installed=True)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, _SWP_FLAGS)
        log.info("[WINDOW BAR] the app's own top bar is hooked to the window")
        return True
    except Exception as error:
        log.warning("[WINDOW BAR] the bar could not be hooked to the window, the Windows title bar stays: %s", error)
        return False
