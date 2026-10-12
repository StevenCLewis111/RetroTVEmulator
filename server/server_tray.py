"""Optional Windows tray icon for the headless Retro TV server service.

The service deliberately keeps its networking dependency-free.  ``pystray``
and Pillow are optional UI dependencies: when they are installed the service
gets a real tray icon; when they are not, the headless broadcaster still runs
and records a useful warning instead of failing to serve schedules.

Owns: the tray icon, its menu, and its worker thread.
Does not own: what "Stop" or "Open folder" do -- those are callbacks supplied
by server_service.py.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


# ============================================================================
# SECTION: Tray icon
# ============================================================================


class ServerTray:
    """Own a small tray icon without making the server loop UI-dependent."""

    def __init__(self, *, title="Retro TV Server", on_stop=None,
                 on_open_folder=None):
        self.title = title
        self.on_stop = on_stop
        self.on_open_folder = on_open_folder
        self.icon = None
        self._thread = None
        self.available = False

    @staticmethod
    def _build_image(Image, ImageDraw):
        image = Image.new("RGBA", (64, 64), (8, 14, 27, 255))
        draw = ImageDraw.Draw(image)
        # CRT body and screen.
        draw.rounded_rectangle((7, 12, 57, 48), radius=6,
                               fill=(23, 43, 59, 255),
                               outline=(68, 255, 167, 255), width=3)
        draw.rectangle((14, 19, 50, 40), fill=(4, 12, 22, 255),
                       outline=(68, 255, 167, 255), width=2)
        # Signal line and antenna.
        draw.line((20, 29, 44, 29), fill=(68, 255, 167, 255), width=2)
        draw.line((26, 12, 32, 4), fill=(68, 255, 167, 255), width=3)
        draw.line((38, 12, 32, 4), fill=(68, 255, 167, 255), width=3)
        draw.line((24, 52, 40, 52), fill=(68, 255, 167, 255), width=3)
        return image

    def start(self):
        if self._thread and self._thread.is_alive():
            return self.available
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception as exc:
            log.warning("[SERVER TRAY] pystray/Pillow unavailable; "
                        "headless service will continue without an icon: %s", exc)
            return False

        try:
            image = self._build_image(Image, ImageDraw)

            def _stop(icon, item):
                if self.on_stop:
                    self.on_stop()

            def _open_folder(icon, item):
                if self.on_open_folder:
                    self.on_open_folder()

            menu = pystray.Menu(
                pystray.MenuItem("Open server folder", _open_folder),
                pystray.MenuItem("Stop Server", _stop),
            )
            self.icon = pystray.Icon("RetroTVServer", image, self.title, menu)
            self._thread = threading.Thread(
                target=self.icon.run,
                name="retro-tv-server-tray",
                daemon=True,
            )
            self._thread.start()
            self.available = True
            return True
        except Exception:
            log.exception("[SERVER TRAY] Could not start tray icon")
            self.icon = None
            return False

    def stop(self):
        icon = self.icon
        self.icon = None
        self.available = False
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                log.debug("[SERVER TRAY] icon stop failed", exc_info=True)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None



# ============================================================================
# SECTION: Exports
# ============================================================================
__all__ = ["ServerTray"]