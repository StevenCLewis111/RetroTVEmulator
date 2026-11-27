"""Windows tray icon for the headless Retro TV server service.

The icon sits by the clock whenever the server service is running, even when the
program itself is closed, so the server can always be seen and stopped. It is
built on Windows' own tray calls (ctypes), with no extra packages to install.

Owns: the tray icon, its right-click menu, and its message-loop thread.
Does not own: what "Open Retro TV Emulator" and "Stop Server" do;
those are callbacks supplied by server_service.py.
"""
from __future__ import annotations

import colorsys
import ctypes
import logging
import os
import sys
import threading

log = logging.getLogger(__name__)

# ============================================================================
# SECTION: Windows constants
# ============================================================================

WM_DESTROY = 0x0002
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_APP_TRAY = 0x8001
WM_APP_CLOSE = 0x8002
NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
MF_GRAYED, MF_OWNERDRAW = 0x1, 0x100
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_RIGHTALIGN, TPM_BOTTOMALIGN = 0x2, 0x100, 0x8, 0x20
WM_MEASUREITEM, WM_DRAWITEM = 0x002C, 0x002B
ODS_SELECTED = 0x1
MIM_BACKGROUND = 0x2
DT_LEFT, DT_VCENTER, DT_SINGLELINE, DT_NOPREFIX = 0x0, 0x4, 0x20, 0x800
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
SM_CXSMICON, SM_CYSMICON = 49, 50
IDI_APPLICATION = 32512
MENU_OPEN_APP, MENU_STOP, MENU_CHECK_UPDATES, MENU_SEND_LOGS, MENU_LAUNCH_ON_BOOT = 1, 3, 4, 5, 6
TIP_TEXT = "Retro TV Server - running"

# ============================================================================
# SECTION: Menu look (the program's own menu colors)
# ============================================================================

# WARNING: the values below come from performance_governor.py (its VALUES CHANGE HERE notes describe them); the fallbacks must match it and are used only when that module fails to import. The menu is the menus' button blue with white text, the title line in the menus' gold, the row under the pointer in the lighter blue; sizes are in pixels.
try:
    import performance_governor as _gov
except Exception as _governor_error:
    _gov = None
    log.warning("[FAILURE] server_tray: performance_governor did not import, so the built-in numbers are used: %s", _governor_error)


def _log_swallowed(where, error):
    """Writes the shared, counted [FAILURE] line through performance_governor (a plain warning when it did not import)."""
    if _gov is not None:
        return _gov.log_swallowed(where, error)
    log.warning("[FAILURE] %s: %s: %s", where, type(error).__name__, error)
    return None


COLOR_MENU_BACK = tuple(_gov.COLOR_MENU_BUTTON_BLUE) if _gov is not None else (40, 78, 145)
COLOR_MENU_HOVER = tuple(_gov.COLOR_MENU_ON_BLUE) if _gov is not None else (40, 100, 180)
COLOR_MENU_EDGE = tuple(_gov.COLOR_MENU_ON_BLUE_EDGE) if _gov is not None else (60, 140, 220)
COLOR_MENU_TITLE = tuple(_gov.COLOR_ARCADE_GOLD) if _gov is not None else (255, 220, 0)
COLOR_MENU_TEXT = (255, 255, 255)
TRAY_MENU_FONT_PX = _gov.TRAY_MENU_FONT_PX if _gov is not None else 16
TRAY_MENU_PAD_X_PX = _gov.TRAY_MENU_PAD_X_PX if _gov is not None else 10
TRAY_MENU_PAD_Y_PX = _gov.TRAY_MENU_PAD_Y_PX if _gov is not None else 5
TRAY_MENU_GAP_PX = _gov.TRAY_MENU_GAP_PX if _gov is not None else 6
TRAY_MENU_FALLBACK_LIFT_PX = _gov.TRAY_MENU_FALLBACK_LIFT_PX if _gov is not None else 28
TRAY_MENU_CHECKBOX_PX = _gov.TRAY_MENU_CHECKBOX_PX if _gov is not None else 12
COLOR_MENU_CHECKBOX_GREY = tuple(_gov.COLOR_MENU_BUTTON_GREYED) if _gov is not None else (32, 32, 40)


def colorref(rgb):
    """Windows color value (0x00BBGGRR) for an (r, g, b) tuple."""
    return rgb[0] | (rgb[1] << 8) | (rgb[2] << 16)


def _rehued(rgb, hue_degrees):
    """The same colour turned to another hue (0 to 360), its saturation and brightness kept."""
    _h, sat, val = colorsys.rgb_to_hsv(*(c / 255.0 for c in rgb))
    r, g, b = colorsys.hsv_to_rgb((float(hue_degrees) % 360.0) / 360.0, sat, val)
    return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))


def menu_theme_colors(bg_hue, ui_hue):
    """The menu's colours for the program's theme: the background and hover rows follow the Background Color hue, the border and the ticked checkbox the Border/Text Color hue; a hue that is missing or not a number keeps the built-in colour. Title and text colours never change."""
    colors = {"back": COLOR_MENU_BACK, "hover": COLOR_MENU_HOVER, "edge": COLOR_MENU_EDGE,
              "title": COLOR_MENU_TITLE, "text": COLOR_MENU_TEXT, "box": COLOR_MENU_CHECKBOX_GREY}
    for hue, names in ((bg_hue, ("back", "hover")), (ui_hue, ("edge",))):
        if isinstance(hue, bool) or not isinstance(hue, (int, float)):
            continue
        for name in names:
            colors[name] = _rehued(colors[name], hue)
    return colors


def menu_anchor(icon_rect, pointer, gap=None, fallback_lift=None):
    """(x, y) the menu's bottom right corner goes at: the icon's right edge, `gap` above its top, so the menu never covers the icon. icon_rect is (left, top, right, bottom) or None when Windows will not give it, then the corner sits `fallback_lift` above the pointer."""
    gap = TRAY_MENU_GAP_PX if gap is None else gap
    lift = TRAY_MENU_FALLBACK_LIFT_PX if fallback_lift is None else fallback_lift
    if icon_rect:
        return icon_rect[2], icon_rect[1] - gap
    return pointer[0], pointer[1] - lift


def item_size(text_width, text_height):
    """(width, height) of one menu row: the text with its padding all round and nothing more."""
    return text_width + 2 * TRAY_MENU_PAD_X_PX, text_height + 2 * TRAY_MENU_PAD_Y_PX


# ============================================================================
# SECTION: Icon file
# ============================================================================


def icon_candidates(base_dir):
    """Where the Retro TV icon file may be: main/images/loading/icon.ico inside the install folder (a built copy keeps main under _internal), beside the app's other icons."""
    # WARNING: owner 2026-11-25: "icon.ico is in main/images/loading"; the tray looked in main/images and found no icon.
    return [os.path.join(base_dir, "_internal", "main", "images", "loading", "icon.ico"),
            os.path.join(base_dir, "main", "images", "loading", "icon.ico")]


# ============================================================================
# SECTION: Tray icon
# ============================================================================


class ServerTray:
    """Own one tray icon and its menu on a thread of its own, so the server loop never waits on the screen."""

    def __init__(self, *, title="Retro TV Server", on_stop=None,
                 on_open_app=None, on_check_updates=None, on_send_logs=None, launch_on_boot=None, on_toggle_launch_on_boot=None, icon_paths=(),
                 menu_colors=None):
        # WARNING: launch_on_boot() -> True when the Launch on boot row is ticked; on_toggle_launch_on_boot() flips it. menu_colors() -> (Background Color hue, Border/Text Color hue) from the program's saved theme, read each time the menu opens.
        self.menu_colors = menu_colors
        # WARNING: owner 2026-11-26: "that load bar between server streaming, profile watching on server, and the tray to all show the same load progress"; scan_status() -> the line the menu shows under its title (how many of the profiles' files are scanned), or None.
        self.scan_status = None
        self.launch_on_boot = launch_on_boot
        self.on_toggle_launch_on_boot = on_toggle_launch_on_boot
        self.title = title
        self.on_send_logs = on_send_logs
        self.on_check_updates = on_check_updates
        self.on_stop = on_stop
        self.on_open_app = on_open_app
        self.icon_paths = tuple(icon_paths)
        self._thread = None
        self._hwnd = None
        self._ready = threading.Event()
        self.available = False

    # ============================================================
    # SECTION: Start and stop
    # ============================================================

    def start(self):
        if os.name != "nt":
            log.info("[SERVER TRAY] the tray icon is for Windows; the service runs without one here")
            return False
        if self._thread and self._thread.is_alive():
            return self.available
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="retro-tv-server-tray", daemon=True)
        self._thread.start()
        self._ready.wait(5.0)
        return self.available

    def stop(self):
        hwnd = self._hwnd
        self.available = False
        if hwnd:
            try:
                ctypes.WinDLL("user32").PostMessageW(ctypes.c_void_p(hwnd), WM_APP_CLOSE, 0, 0)
            except Exception:
                log.debug("[SERVER TRAY] close message failed", exc_info=True)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    # ============================================================
    # SECTION: Message loop (Windows only)
    # ============================================================

    def _run(self):
        try:
            self._loop()
        except Exception:
            log.exception("[SERVER TRAY] the tray icon stopped")
        finally:
            self.available = False
            self._hwnd = None
            self._ready.set()

    def _loop(self):
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                        ("uTimeoutOrVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                        ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user32.LoadIconW.restype = wintypes.HICON
        user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterWindowMessageW.restype = wintypes.UINT
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        shell32.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON), wintypes.UINT]
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE

        taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        h_icon = self._load_icon(user32, shell32, wintypes)

        def add_icon(hwnd):
            data = NOTIFYICONDATAW()
            data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            data.hWnd = hwnd
            data.uID = 1
            data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            data.uCallbackMessage = WM_APP_TRAY
            data.hIcon = h_icon
            data.szTip = TIP_TEXT
            return bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)))

        def delete_icon(hwnd):
            data = NOTIFYICONDATAW()
            data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            data.hWnd = hwnd
            data.uID = 1
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))

        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

        class SIZE(ctypes.Structure):
            _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

        class MEASUREITEMSTRUCT(ctypes.Structure):
            _fields_ = [("CtlType", wintypes.UINT), ("CtlID", wintypes.UINT), ("itemID", wintypes.UINT),
                        ("itemWidth", wintypes.UINT), ("itemHeight", wintypes.UINT), ("itemData", ctypes.c_size_t)]

        class DRAWITEMSTRUCT(ctypes.Structure):
            _fields_ = [("CtlType", wintypes.UINT), ("CtlID", wintypes.UINT), ("itemID", wintypes.UINT),
                        ("itemAction", wintypes.UINT), ("itemState", wintypes.UINT), ("hwndItem", wintypes.HWND),
                        ("hDC", wintypes.HDC), ("rcItem", wintypes.RECT), ("itemData", ctypes.c_size_t)]

        class NOTIFYICONIDENTIFIER(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("guidItem", GUID)]

        shell32.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(NOTIFYICONIDENTIFIER), ctypes.POINTER(wintypes.RECT)]
        shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long

        def icon_rect(hwnd):
            """The icon's screen rectangle as (left, top, right, bottom), or None when Windows will not say."""
            try:
                ident = NOTIFYICONIDENTIFIER()
                ident.cbSize = ctypes.sizeof(NOTIFYICONIDENTIFIER)
                ident.hWnd = hwnd
                ident.uID = 1
                rect = wintypes.RECT()
                if shell32.Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(rect)) == 0 and rect.right > rect.left:
                    return rect.left, rect.top, rect.right, rect.bottom
            except Exception:
                log.debug("[SERVER TRAY] could not read the icon's place", exc_info=True)
            return None

        class MENUINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.DWORD), ("dwStyle", wintypes.DWORD),
                        ("cyMax", wintypes.UINT), ("hbrBack", wintypes.HBRUSH), ("dwContextHelpID", wintypes.DWORD),
                        ("dwMenuData", ctypes.c_size_t)]

        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t]
        user32.SetMenuInfo.argtypes = [wintypes.HMENU, ctypes.POINTER(MENUINFO)]
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
        user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT]
        gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
        gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
        gdi32.CreateFontW.restype = wintypes.HANDLE
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        gdi32.SelectObject.restype = wintypes.HANDLE
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
        gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
        gdi32.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(SIZE)]

        # WARNING: Every row is drawn by this program (owner-drawn) so the menu can use the program's own theme colours and be only as big as its text. Each row's itemData is its key in this table; the Launch on boot row is a checkbox kind with its tick third.
        rows = {1: (TIP_TEXT, "title"), 2: ("Open Retro TV Emulator", "item"), 3: ("Stop Server", "item"), 4: ("Check for updates", "item"), 5: ("Send logs", "item")}
        draw = {"font": None, "brushes": {}}
        colors = dict(menu_theme_colors(None, None))
        checkbox_span = TRAY_MENU_CHECKBOX_PX + 2 * TRAY_MENU_PAD_X_PX

        def brush(rgb):
            if rgb not in draw["brushes"]:
                draw["brushes"][rgb] = gdi32.CreateSolidBrush(colorref(rgb))
            return draw["brushes"][rgb]

        def measure_row(hwnd, info):
            label = rows.get(info.itemData, ("", ""))[0]
            hdc = user32.GetDC(hwnd)
            old = gdi32.SelectObject(hdc, draw["font"])
            size = SIZE()
            gdi32.GetTextExtentPoint32W(hdc, label, len(label), ctypes.byref(size))
            gdi32.SelectObject(hdc, old)
            user32.ReleaseDC(hwnd, hdc)
            width, height = item_size(size.cx, size.cy)
            if rows.get(info.itemData, ("", ""))[1] == "checkbox":
                width += checkbox_span
            info.itemWidth, info.itemHeight = width, height

        def draw_row(info):
            row = rows.get(info.itemData, ("", "item"))
            label, kind = row[0], row[1]
            ticked = bool(row[2]) if len(row) > 2 else False
            hdc = info.hDC
            rect = info.rcItem
            hovered = bool(info.itemState & ODS_SELECTED) and kind not in ("title", "status")
            edge_brush = brush(colors["edge"])
            user32.FillRect(hdc, ctypes.byref(rect), brush(colors["hover"] if hovered else colors["back"]))
            if kind == "title":
                line = wintypes.RECT(rect.left, rect.bottom - 1, rect.right, rect.bottom)
                user32.FillRect(hdc, ctypes.byref(line), edge_brush)
            # WARNING: Windows gives an owner-drawn popup menu no border of its own, so each row paints its share: a strip down both sides, the top strip on the first row and the bottom strip on the last row (Stop Server).
            for strip in (wintypes.RECT(rect.left, rect.top, rect.left + 1, rect.bottom), wintypes.RECT(rect.right - 1, rect.top, rect.right, rect.bottom)):
                user32.FillRect(hdc, ctypes.byref(strip), edge_brush)
            if info.itemData == 1:
                top = wintypes.RECT(rect.left, rect.top, rect.right, rect.top + 1)
                user32.FillRect(hdc, ctypes.byref(top), edge_brush)
            if info.itemData == MENU_STOP:
                bottom = wintypes.RECT(rect.left, rect.bottom - 1, rect.right, rect.bottom)
                user32.FillRect(hdc, ctypes.byref(bottom), edge_brush)
            old = gdi32.SelectObject(hdc, draw["font"])
            gdi32.SetBkMode(hdc, 1)
            gdi32.SetTextColor(hdc, colorref(colors["title"] if kind == "title" else colors["text"]))
            text_right = rect.right - checkbox_span if kind == "checkbox" else rect.right
            text_rect = wintypes.RECT(rect.left + TRAY_MENU_PAD_X_PX, rect.top, text_right, rect.bottom)
            user32.DrawTextW(hdc, label, -1, ctypes.byref(text_rect), DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX)
            label_size = SIZE()
            gdi32.GetTextExtentPoint32W(hdc, label, len(label), ctypes.byref(label_size))
            gdi32.SelectObject(hdc, old)
            if kind == "checkbox":
                # WARNING: owner 2026-11-19: the box sits one gap after the words "Launch on boot", not at the menu's right edge.
                box = TRAY_MENU_CHECKBOX_PX
                box_left = min(rect.left + TRAY_MENU_PAD_X_PX + label_size.cx + TRAY_MENU_PAD_X_PX, rect.right - TRAY_MENU_PAD_X_PX - box)
                box_top = rect.top + (rect.bottom - rect.top - box) // 2
                outer = wintypes.RECT(box_left, box_top, box_left + box, box_top + box)
                user32.FillRect(hdc, ctypes.byref(outer), edge_brush)
                centre = wintypes.RECT(box_left + 1, box_top + 1, box_left + box - 1, box_top + box - 1)
                user32.FillRect(hdc, ctypes.byref(centre), brush(colors["box"]))
                if ticked:
                    inner = wintypes.RECT(box_left + 3, box_top + 3, box_left + box - 3, box_top + box - 3)
                    user32.FillRect(hdc, ctypes.byref(inner), edge_brush)

        def show_menu(hwnd):
            try:
                colors.update(menu_theme_colors(*(self.menu_colors() if self.menu_colors else (None, None))))
            except Exception:
                log.warning("[SERVER TRAY] could not read the theme colours, the menu keeps its own", exc_info=True)
                colors.update(menu_theme_colors(None, None))
            draw["font"] = gdi32.CreateFontW(-TRAY_MENU_FONT_PX, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5, 0, "Courier New")
            menu = user32.CreatePopupMenu()
            info = MENUINFO()
            info.cbSize = ctypes.sizeof(MENUINFO)
            info.fMask = MIM_BACKGROUND
            info.hbrBack = brush(colors["back"])
            user32.SetMenuInfo(menu, ctypes.byref(info))
            user32.AppendMenuW(menu, MF_GRAYED | MF_OWNERDRAW, 0, 1)
            _status = None
            if self.scan_status:
                try:
                    _status = self.scan_status()
                except Exception:
                    log.warning("[SERVER TRAY] the scan progress line could not be made", exc_info=True)
            if _status:
                rows[7] = (str(_status), "status")
                user32.AppendMenuW(menu, MF_GRAYED | MF_OWNERDRAW, 0, 7)
            if self.on_open_app:
                user32.AppendMenuW(menu, MF_OWNERDRAW, MENU_OPEN_APP, 2)
            # WARNING: owner 2026-11-18: Launch on boot sits above Check for updates; its tick is a square on the right of the row so every label starts at the same left edge.
            if self.on_toggle_launch_on_boot:
                ticked = bool(self.launch_on_boot()) if self.launch_on_boot else False
                rows[6] = ("Launch on boot", "checkbox", ticked)
                user32.AppendMenuW(menu, MF_OWNERDRAW, MENU_LAUNCH_ON_BOOT, 6)
            if self.on_check_updates:
                user32.AppendMenuW(menu, MF_OWNERDRAW, MENU_CHECK_UPDATES, 4)
            if self.on_send_logs:
                user32.AppendMenuW(menu, MF_OWNERDRAW, MENU_SEND_LOGS, 5)
            user32.AppendMenuW(menu, MF_OWNERDRAW, MENU_STOP, 3)
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(hwnd)
            # WARNING: the menu opens with its bottom right corner just above the icon, so it grows up and to the left, away from the screen edge the tray sits against, and leaves the icon showing.
            anchor_x, anchor_y = menu_anchor(icon_rect(hwnd), (point.x, point.y))
            choice = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_RIGHTALIGN | TPM_BOTTOMALIGN, anchor_x, anchor_y, 0, hwnd, None)
            user32.PostMessageW(hwnd, 0, 0, 0)
            user32.DestroyMenu(menu)
            for handle in list(draw["brushes"].values()) + [draw["font"]]:
                if handle:
                    gdi32.DeleteObject(handle)
            draw["brushes"].clear()
            draw["font"] = None
            callback = {MENU_OPEN_APP: self.on_open_app, MENU_STOP: self.on_stop, MENU_CHECK_UPDATES: self.on_check_updates, MENU_SEND_LOGS: self.on_send_logs, MENU_LAUNCH_ON_BOOT: self.on_toggle_launch_on_boot}.get(choice)
            if callback:
                callback()

        def window_proc(hwnd, msg, wparam, lparam):
            try:
                if msg == WM_APP_TRAY:
                    mouse = lparam & 0xFFFF
                    if mouse in (WM_RBUTTONUP, WM_CONTEXTMENU):
                        show_menu(hwnd)
                    elif mouse == WM_LBUTTONDBLCLK and self.on_open_app:
                        self.on_open_app()
                    return 0
                if msg == WM_MEASUREITEM and draw["font"]:
                    measure_row(hwnd, ctypes.cast(lparam, ctypes.POINTER(MEASUREITEMSTRUCT)).contents)
                    return 1
                if msg == WM_DRAWITEM and draw["font"]:
                    draw_row(ctypes.cast(lparam, ctypes.POINTER(DRAWITEMSTRUCT)).contents)
                    return 1
                if msg == taskbar_created:
                    add_icon(hwnd)
                    return 0
                if msg == WM_APP_CLOSE:
                    user32.DestroyWindow(hwnd)
                    return 0
                if msg == WM_DESTROY:
                    delete_icon(hwnd)
                    user32.PostQuitMessage(0)
                    return 0
            except Exception:
                log.exception("[SERVER TRAY] a tray message failed")
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        proc = WNDPROC(window_proc)
        instance = kernel32.GetModuleHandleW(None)
        class_name = "RetroTVServerTray"
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = proc
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            log.warning("[SERVER TRAY] could not register the tray window: error %s", ctypes.get_last_error())
            return
        hwnd = user32.CreateWindowExW(0, class_name, self.title, 0, 0, 0, 0, 0, None, None, instance, None)
        if not hwnd:
            log.warning("[SERVER TRAY] could not make the tray window: error %s", ctypes.get_last_error())
            return
        if not add_icon(hwnd):
            log.warning("[SERVER TRAY] Windows would not add the tray icon: error %s", ctypes.get_last_error())
            user32.DestroyWindow(hwnd)
            return
        self._hwnd = hwnd
        self.available = True
        self._ready.set()
        log.info("[SERVER TRAY] tray icon added")
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        user32.UnregisterClassW(class_name, instance)

    def _load_icon(self, user32, shell32, wintypes):
        """The Retro TV icon file when it is there, else the program's own icon, else Windows' plain one."""
        width, height = user32.GetSystemMetrics(SM_CXSMICON), user32.GetSystemMetrics(SM_CYSMICON)
        for path in self.icon_paths:
            if os.path.isfile(path):
                handle = user32.LoadImageW(None, path, IMAGE_ICON, width, height, LR_LOADFROMFILE)
                if handle:
                    return handle
                log.warning("[SERVER TRAY] could not read the icon file %s", path)
        small = wintypes.HICON()
        if shell32.ExtractIconExW(sys.executable, 0, None, ctypes.byref(small), 1) and small:
            return small
        return user32.LoadIconW(None, IDI_APPLICATION)


# ============================================================================
# SECTION: Exports
# ============================================================================
__all__ = ["ServerTray", "icon_candidates", "menu_theme_colors"]
