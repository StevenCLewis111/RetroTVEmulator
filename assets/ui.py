"""Screen drawing for the TV guide, the settings overlay menu and the popups.

WARNING: guide and menu canvases are filled with (1, 1, 1), never pure black. (0, 0, 0) is the
game-overlay transparency key.
"""
import os
import pygame
import logging
import threading
import time
import media
import broadcast_clock
import menu_nav
try:
    import performance_governor
except Exception as _governor_import_error:
    performance_governor = None
    logging.getLogger(__name__).warning("[UI] could not import performance_governor, using the fallback values written in ui.py: %s", _governor_import_error)

log = logging.getLogger(__name__)


# ============================================================================
# SECTION: Swallowed-failure lines
# ============================================================================
try:
    from performance_governor import log_swallowed as _log_swallowed
except Exception:
    def _log_swallowed(where, error):
        """Fallback used only when performance_governor cannot be imported: the failure line is lost, nothing else changes."""
        return None


# ============================================================================
# SECTION: Colours
# ============================================================================
# WARNING: the fallback colours below must match the same names in performance_governor.py; they are used only when that module fails to import.
_DEFAULT_THEME = performance_governor.DEFAULT_THEME if performance_governor is not None else {"ui": (0, 255, 128), "bg": (10, 15, 30), "text": (255, 255, 255)}
COLOR_TEXT_WHITE = performance_governor.COLOR_TEXT_WHITE if performance_governor is not None else (255, 255, 255)
COLOR_ARCADE_GOLD = performance_governor.COLOR_ARCADE_GOLD if performance_governor is not None else (255, 220, 0)
COLOR_ACTION_RED = performance_governor.COLOR_ACTION_RED if performance_governor is not None else (255, 60, 60)
COLOR_SCREEN_BLACK = performance_governor.COLOR_SCREEN_BLACK if performance_governor is not None else (1, 1, 1)
COLOR_MENU_BUTTON = performance_governor.COLOR_MENU_BUTTON if performance_governor is not None else (40, 78, 145)
COLOR_MENU_BUTTON_GREYED = performance_governor.COLOR_MENU_BUTTON_GREYED if performance_governor is not None else (32, 32, 40)
COLOR_MENU_TEXT_GREYED = performance_governor.COLOR_MENU_TEXT_GREYED if performance_governor is not None else (105, 105, 115)
COLOR_MENU_BUTTON_BLUE = performance_governor.COLOR_MENU_BUTTON_BLUE if performance_governor is not None else (40, 78, 145)
COLOR_MENU_BUTTON_BLUE_DIM = performance_governor.COLOR_MENU_BUTTON_BLUE_DIM if performance_governor is not None else (32, 58, 105)
COLOR_MENU_ON_BLUE = performance_governor.COLOR_MENU_ON_BLUE if performance_governor is not None else (40, 100, 180)
COLOR_MENU_ON_BLUE_EDGE = performance_governor.COLOR_MENU_ON_BLUE_EDGE if performance_governor is not None else (60, 140, 220)
COLOR_MENU_OFF_BUTTON = performance_governor.COLOR_MENU_OFF_BUTTON if performance_governor is not None else (35, 45, 60)
COLOR_MENU_OFF_RED = performance_governor.COLOR_MENU_OFF_RED if performance_governor is not None else (220, 53, 69)
COLOR_MENU_ON_GREEN = performance_governor.COLOR_MENU_ON_GREEN if performance_governor is not None else (40, 167, 69)
COLOR_BUTTON_CLOSE = performance_governor.COLOR_BUTTON_CLOSE if performance_governor is not None else (220, 53, 69)
COLOR_BUTTON_NO = performance_governor.COLOR_BUTTON_NO if performance_governor is not None else (220, 53, 69)
COLOR_BUTTON_YES = performance_governor.COLOR_BUTTON_YES if performance_governor is not None else (40, 167, 69)
COLOR_BUTTON_CLEAR = performance_governor.COLOR_BUTTON_CLEAR if performance_governor is not None else (130, 28, 38)
COLOR_BUTTON_ADD = performance_governor.COLOR_BUTTON_ADD if performance_governor is not None else (22, 100, 42)
COLOR_BUTTON_TOGGLE_ADD = performance_governor.COLOR_BUTTON_TOGGLE_ADD if performance_governor is not None else (0, 0, 0)
COLOR_BUTTON_AUDIO_TRACK = performance_governor.COLOR_BUTTON_AUDIO_TRACK if performance_governor is not None else (230, 190, 40)
COLOR_BUTTON_AUDIO_TRACK_TEXT = performance_governor.COLOR_BUTTON_AUDIO_TRACK_TEXT if performance_governor is not None else (20, 20, 20)
MENU_CLOSE_BUTTON_PLACE = performance_governor.MENU_CLOSE_BUTTON_PLACE if performance_governor is not None else (25, 52, 110, 40)
# WARNING: the fallback number below must match GUIDE_HIGHLIGHT_NUDGE_SECONDS in performance_governor.py; it is used only when that module fails to import.
_GUIDE_HIGHLIGHT_NUDGE_SECONDS = performance_governor.GUIDE_HIGHLIGHT_NUDGE_SECONDS if performance_governor is not None else 0.75
MENU_CLEAR_BUTTON_PLACE = performance_governor.MENU_CLEAR_BUTTON_PLACE if performance_governor is not None else (25, 52, 240, 40)
POPUP_YES_NO_OFFSET = performance_governor.POPUP_YES_NO_OFFSET if performance_governor is not None else 100
POPUP_YES_NO_SIZE = performance_governor.POPUP_YES_NO_SIZE if performance_governor is not None else (110, 36)
COLOR_MENU_SWITCH_OFF = performance_governor.COLOR_MENU_SWITCH_OFF if performance_governor is not None else (45, 65, 100)
COLOR_MENU_SWITCH_GREYED = performance_governor.COLOR_MENU_SWITCH_GREYED if performance_governor is not None else (35, 35, 45)
COLOR_MENU_TRACK_OFF = performance_governor.COLOR_MENU_TRACK_OFF if performance_governor is not None else (55, 55, 60)
COLOR_MENU_VALUE_MINT = performance_governor.COLOR_MENU_VALUE_MINT if performance_governor is not None else (200, 255, 200)
COLOR_MENU_DISABLED_GREY = performance_governor.COLOR_MENU_DISABLED_GREY if performance_governor is not None else (100, 100, 100)
COLOR_MENU_TEXT_DISABLED_LIGHT = performance_governor.COLOR_MENU_TEXT_DISABLED_LIGHT if performance_governor is not None else (140, 140, 140)
COLOR_MENU_OFF_LABEL_GREY = performance_governor.COLOR_MENU_OFF_LABEL_GREY if performance_governor is not None else (110, 110, 110)
COLOR_MENU_DIM_TEXT = performance_governor.COLOR_MENU_DIM_TEXT if performance_governor is not None else (150, 160, 180)
COLOR_MENU_HINT_TEXT = performance_governor.COLOR_MENU_HINT_TEXT if performance_governor is not None else (120, 120, 140)
COLOR_MENU_WARNING_RED = performance_governor.COLOR_MENU_WARNING_RED if performance_governor is not None else (255, 80, 80)
COLOR_MENU_FLASH_TEXT = performance_governor.COLOR_MENU_FLASH_TEXT if performance_governor is not None else (255, 90, 90)
COLOR_MENU_LAST_ITEM_TEXT = performance_governor.COLOR_MENU_LAST_ITEM_TEXT if performance_governor is not None else (90, 90, 90)
COLOR_MENU_TEXT_ON_LIT = performance_governor.COLOR_MENU_TEXT_ON_LIT if performance_governor is not None else (20, 20, 20)
COLOR_MENU_SECTION_LABEL = performance_governor.COLOR_MENU_SECTION_LABEL if performance_governor is not None else (120, 120, 120)
COLOR_MENU_TEXT_GREYED_LILAC = performance_governor.COLOR_MENU_TEXT_GREYED_LILAC if performance_governor is not None else (130, 130, 150)
COLOR_MENU_TEXT_GREYED_FOCUS = performance_governor.COLOR_MENU_TEXT_GREYED_FOCUS if performance_governor is not None else (160, 160, 175)
COLOR_MENU_VALUE_BOX = performance_governor.COLOR_MENU_VALUE_BOX if performance_governor is not None else (10, 10, 10)
COLOR_MENU_SWATCH_EDGE = performance_governor.COLOR_MENU_SWATCH_EDGE if performance_governor is not None else (90, 90, 100)
COLOR_MENU_EDGE_GREYED = performance_governor.COLOR_MENU_EDGE_GREYED if performance_governor is not None else (45, 45, 55)
COLOR_MENU_DIM_GREY = performance_governor.COLOR_MENU_DIM_GREY if performance_governor is not None else (60, 60, 80)
COLOR_MENU_PADLOCK = performance_governor.COLOR_MENU_PADLOCK if performance_governor is not None else (80, 80, 80)
COLOR_MENU_LOCKED_SWITCH_ON = performance_governor.COLOR_MENU_LOCKED_SWITCH_ON if performance_governor is not None else (55, 90, 55)
COLOR_MENU_LOCKED_SWITCH_KNOB = performance_governor.COLOR_MENU_LOCKED_SWITCH_KNOB if performance_governor is not None else (130, 130, 130)
COLOR_MENU_KNOB_GREYED = performance_governor.COLOR_MENU_KNOB_GREYED if performance_governor is not None else (80, 80, 90)
COLOR_MENU_TOGGLE_GREYED = performance_governor.COLOR_MENU_TOGGLE_GREYED if performance_governor is not None else (22, 22, 30)
COLOR_MENU_TOGGLE_GREYED_EDGE = performance_governor.COLOR_MENU_TOGGLE_GREYED_EDGE if performance_governor is not None else (150, 150, 170)
COLOR_MENU_KEY_CONFLICT_FILL = performance_governor.COLOR_MENU_KEY_CONFLICT_FILL if performance_governor is not None else (120, 20, 20)
COLOR_MENU_KEY_CONFLICT_TEXT = performance_governor.COLOR_MENU_KEY_CONFLICT_TEXT if performance_governor is not None else (255, 100, 100)
COLOR_MENU_KEY_CAPTURE_FILL = performance_governor.COLOR_MENU_KEY_CAPTURE_FILL if performance_governor is not None else (90, 30, 30)
COLOR_MENU_CARD = performance_governor.COLOR_MENU_CARD if performance_governor is not None else (35, 45, 65)
COLOR_MENU_CARD_EDGE = performance_governor.COLOR_MENU_CARD_EDGE if performance_governor is not None else (90, 90, 110)
COLOR_MENU_CARD_GREYED = performance_governor.COLOR_MENU_CARD_GREYED if performance_governor is not None else (24, 27, 34)
COLOR_MENU_CARD_EDGE_GREYED = performance_governor.COLOR_MENU_CARD_EDGE_GREYED if performance_governor is not None else (60, 62, 70)
COLOR_MENU_CARD_TEXT = performance_governor.COLOR_MENU_CARD_TEXT if performance_governor is not None else (210, 210, 220)
COLOR_MENU_CARD_TEXT_GREYED = performance_governor.COLOR_MENU_CARD_TEXT_GREYED if performance_governor is not None else (95, 97, 105)
COLOR_MENU_UNNAMED_TEXT = performance_governor.COLOR_MENU_UNNAMED_TEXT if performance_governor is not None else (110, 112, 120)
COLOR_MENU_DELETE_ARMED = performance_governor.COLOR_MENU_DELETE_ARMED if performance_governor is not None else (60, 25, 25)
COLOR_MENU_RADIO_FILL = performance_governor.COLOR_MENU_RADIO_FILL if performance_governor is not None else (15, 15, 20)
COLOR_MENU_RADIO_EDGE = performance_governor.COLOR_MENU_RADIO_EDGE if performance_governor is not None else (140, 140, 160)
COLOR_MENU_RADIO_SELECTED_EDGE = performance_governor.COLOR_MENU_RADIO_SELECTED_EDGE if performance_governor is not None else (90, 160, 230)
COLOR_MENU_NO_SERVER_TEXT = performance_governor.COLOR_MENU_NO_SERVER_TEXT if performance_governor is not None else (200, 160, 60)
COLOR_MENU_LOCKED_CELL = performance_governor.COLOR_MENU_LOCKED_CELL if performance_governor is not None else (48, 48, 55)
COLOR_MENU_LOCKED_TEXT = performance_governor.COLOR_MENU_LOCKED_TEXT if performance_governor is not None else (150, 150, 160)
COLOR_MENU_EDGE_LOCKED = performance_governor.COLOR_MENU_EDGE_LOCKED if performance_governor is not None else (145, 145, 155)
COLOR_MENU_SERVER_LABEL = performance_governor.COLOR_MENU_SERVER_LABEL if performance_governor is not None else (175, 175, 185)
COLOR_MENU_LOCK_DIM = performance_governor.COLOR_MENU_LOCK_DIM if performance_governor is not None else (0, 0, 0, 145)
COLOR_MENU_LOCK_TEXT = performance_governor.COLOR_MENU_LOCK_TEXT if performance_governor is not None else (190, 190, 200)
COLOR_MENU_PLAYER_SQUARE = performance_governor.COLOR_MENU_PLAYER_SQUARE if performance_governor is not None else (80, 220, 120)
COLOR_MENU_PLAYER_ROW = performance_governor.COLOR_MENU_PLAYER_ROW if performance_governor is not None else (25, 35, 55)
COLOR_MENU_DISC_EDGE = performance_governor.COLOR_MENU_DISC_EDGE if performance_governor is not None else (40, 60, 90)
COLOR_MENU_DISC_GREYED = performance_governor.COLOR_MENU_DISC_GREYED if performance_governor is not None else (20, 20, 28)
COLOR_MENU_DISC_TEXT_OFF = performance_governor.COLOR_MENU_DISC_TEXT_OFF if performance_governor is not None else (120, 120, 130)
COLOR_MENU_PROFILE_FOCUS = performance_governor.COLOR_MENU_PROFILE_FOCUS if performance_governor is not None else (30, 50, 30)
COLOR_MENU_PROFILE = performance_governor.COLOR_MENU_PROFILE if performance_governor is not None else (20, 30, 50)
COLOR_MENU_PROFILE_EDGE = performance_governor.COLOR_MENU_PROFILE_EDGE if performance_governor is not None else (50, 70, 100)
COLOR_MENU_STEP_FOCUS = performance_governor.COLOR_MENU_STEP_FOCUS if performance_governor is not None else (34, 34, 46)
COLOR_MENU_STEP_FOCUS_EDGE = performance_governor.COLOR_MENU_STEP_FOCUS_EDGE if performance_governor is not None else (110, 110, 125)
COLOR_MENU_SPLASH_FILE_OFF = performance_governor.COLOR_MENU_SPLASH_FILE_OFF if performance_governor is not None else (75, 75, 82)
COLOR_MENU_DOT_RING = performance_governor.COLOR_MENU_DOT_RING if performance_governor is not None else (170, 255, 195)
COLOR_MENU_DOT_EDGE = performance_governor.COLOR_MENU_DOT_EDGE if performance_governor is not None else (90, 150, 220)
COLOR_MENU_POPUP_HINT = performance_governor.COLOR_MENU_POPUP_HINT if performance_governor is not None else (160, 160, 160)
COLOR_MENU_POPUP_SUBTEXT = performance_governor.COLOR_MENU_POPUP_SUBTEXT if performance_governor is not None else (200, 200, 210)
COLOR_MENU_POPUP_SUBTEXT_DIM = performance_governor.COLOR_MENU_POPUP_SUBTEXT_DIM if performance_governor is not None else (140, 140, 150)
COLOR_MENU_CHECKBOX = performance_governor.COLOR_MENU_CHECKBOX if performance_governor is not None else (200, 200, 200)
COLOR_MENU_DVD_MAP_OFF_RED = performance_governor.COLOR_MENU_DVD_MAP_OFF_RED if performance_governor is not None else (150, 45, 45)
COLOR_MENU_DVD_MAP_PANEL = performance_governor.COLOR_MENU_DVD_MAP_PANEL if performance_governor is not None else (16, 22, 40)
COLOR_MENU_DVD_MAP_KEY = performance_governor.COLOR_MENU_DVD_MAP_KEY if performance_governor is not None else (12, 16, 30)
# WARNING: the fallback numbers below must match the same names in performance_governor.py; they are used only when that module fails to import.
GUIDE_VISIBLE_ROWS_16_9 = performance_governor.GUIDE_VISIBLE_ROWS_16_9 if performance_governor is not None else 8
GUIDE_VISIBLE_ROWS_4_3 = performance_governor.GUIDE_VISIBLE_ROWS_4_3 if performance_governor is not None else 5
GUIDE_NUM_COLS_16_9 = performance_governor.GUIDE_NUM_COLS_16_9 if performance_governor is not None else 5
GUIDE_NUM_COLS_4_3 = performance_governor.GUIDE_NUM_COLS_4_3 if performance_governor is not None else 4
GUIDE_HDR_FONT_PX = performance_governor.GUIDE_HDR_FONT_PX if performance_governor is not None else 17
GUIDE_CLOCK_FONT_PX = performance_governor.GUIDE_CLOCK_FONT_PX if performance_governor is not None else 17
GUIDE_MIN_FONT_PX = performance_governor.GUIDE_MIN_FONT_PX if performance_governor is not None else 17
CONTROLS_BODY_FONT_PX = performance_governor.CONTROLS_BODY_FONT_PX if performance_governor is not None else 22
CONTROLS_ICON_KEY_W_PX = performance_governor.CONTROLS_ICON_KEY_W_PX if performance_governor is not None else 64
CONTROLS_TITLE_SECTION_GAP_PX = performance_governor.CONTROLS_TITLE_SECTION_GAP_PX if performance_governor is not None else 18
CONTROLS_SECTION_BODY_GAP_PX = performance_governor.CONTROLS_SECTION_BODY_GAP_PX if performance_governor is not None else 24
CONTROLS_SUB_BODY_GAP_PX = performance_governor.CONTROLS_SUB_BODY_GAP_PX if performance_governor is not None else 20
OPTIONS_LEFT_TOGGLE_OFFSET_PX = performance_governor.OPTIONS_LEFT_TOGGLE_OFFSET_PX if performance_governor is not None else 370
OPTIONS_RIGHT_TOGGLE_OFFSET_PX = performance_governor.OPTIONS_RIGHT_TOGGLE_OFFSET_PX if performance_governor is not None else 260
SERVER_PICKER_VISIBLE_ROWS = performance_governor.SERVER_PICKER_VISIBLE_ROWS if performance_governor is not None else 6
CHANNEL_GRID_GROUP_SHIFT_LEFT_PX = performance_governor.CHANNEL_GRID_GROUP_SHIFT_LEFT_PX if performance_governor is not None else 40
CHANNEL_GRID_LEFT_COL_SHIFT_PX = performance_governor.CHANNEL_GRID_LEFT_COL_SHIFT_PX if performance_governor is not None else 50


# ============================================================================
# SECTION: Drawing & canvas-fit helpers
# ============================================================================


SELECTION_BORDER_WIDTH = media.SELECTION_BORDER_WIDTH

# WARNING: the fallback numbers below must match CHANNEL_SCAN_BAR_HEIGHT_PX, CHANNEL_SCAN_BAR_HEAD_PX, CHANNEL_SCAN_BAR_HALO_MIX in performance_governor.py; they are used only when that module fails to import.
CHANNEL_SCAN_BAR_HEIGHT_PX = performance_governor.CHANNEL_SCAN_BAR_HEIGHT_PX if performance_governor is not None else 3
CHANNEL_SCAN_BAR_HEAD_PX = performance_governor.CHANNEL_SCAN_BAR_HEAD_PX if performance_governor is not None else 6
CHANNEL_SCAN_BAR_HALO_MIX = performance_governor.CHANNEL_SCAN_BAR_HALO_MIX if performance_governor is not None else 0.35
# WARNING: the 12 fallback must match CHANNEL_SCAN_BAR_MIN_PX in performance_governor.py; it is used only when that module fails to import.
CHANNEL_SCAN_BAR_MIN_PX = performance_governor.CHANNEL_SCAN_BAR_MIN_PX if performance_governor is not None else 12


def _menu_close_rect(cx, content_w, content_y, content_h):
    """The one place of the Close button in every settings menu and sub-menu: bottom right of the panel."""
    inset, up, w, h = MENU_CLOSE_BUTTON_PLACE
    return pygame.Rect(cx + content_w - inset - w, content_y + content_h - up, w, h)


def _menu_clear_rect(cx, content_y, content_h):
    """The one place of the Clear Media button in every menu that has one: bottom left of the panel."""
    inset, up, w, h = MENU_CLEAR_BUTTON_PLACE
    return pygame.Rect(cx + inset, content_y + content_h - up, w, h)


def _draw_shared_button(canvas, rect, color, label, focused, font, focus_color, text_color=None):
    """Draws one shared button (Close, Clear Media, Yes, No): the filled shape, the focus border and the centred label."""
    pygame.draw.rect(canvas, color, rect, border_radius=4)
    if focused:
        _aa_rounded_rect(canvas, focus_color, rect, SELECTION_BORDER_WIDTH, border_radius=4)
    lbl = font.render(label, True, text_color or COLOR_TEXT_WHITE)
    canvas.blit(lbl, (rect.x + rect.w // 2 - lbl.get_width() // 2, rect.y + rect.h // 2 - lbl.get_height() // 2))


def _draw_menu_close(canvas, cx, content_w, content_y, content_h, focused, font, focus_color):
    """Draws the Close button of a settings menu at its one place, in its one colour."""
    _draw_shared_button(canvas, _menu_close_rect(cx, content_w, content_y, content_h), COLOR_BUTTON_CLOSE, "Close", focused, font, focus_color)


def _draw_menu_clear(canvas, cx, content_y, content_h, focused, has_media, font, focus_color):
    """Draws the Clear Media button at its one place: darker red, greyed when there is nothing to clear."""
    rect = _menu_clear_rect(cx, content_y, content_h)
    color = COLOR_BUTTON_CLEAR if has_media else COLOR_MENU_BUTTON_GREYED
    _draw_shared_button(canvas, rect, color, "Clear Media", focused, font, focus_color,
                        text_color=None if has_media else COLOR_MENU_TEXT_GREYED)
    return rect


def popup_yes_no_rects(center_x, btn_y):
    """(yes rect, no rect) of every Yes/No popup: Yes left and No right of the centre."""
    w, h = POPUP_YES_NO_SIZE
    return (pygame.Rect(center_x - POPUP_YES_NO_OFFSET - w // 2, btn_y, w, h),
            pygame.Rect(center_x + POPUP_YES_NO_OFFSET - w // 2, btn_y, w, h))


def draw_popup_yes_no(surface, center_x, btn_y, selection, focused, font, focus_color=None):
    """Draws the Yes and No buttons of a popup in their one place and colours: Yes green on the left, No red (the Close colour) on the right; the picked one has a white edge, which shows on both colours."""
    focus_color = COLOR_TEXT_WHITE
    yes_rect, no_rect = popup_yes_no_rects(center_x, btn_y)
    _draw_shared_button(surface, yes_rect, COLOR_BUTTON_YES, "YES", focused and selection == "Yes", font, focus_color)
    _draw_shared_button(surface, no_rect, COLOR_BUTTON_NO, "NO", focused and selection == "No", font, focus_color)
    return yes_rect, no_rect


def _aa_rounded_rect(surface, color, rect, width=0, border_radius=0, supersample=4):
    """Anti-aliased replacement for pygame.draw.rect with border_radius."""
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return
    if not media.CRT_WARP_ACTIVE:
        pygame.draw.rect(surface, color, rect, width, border_radius=border_radius)
        return
    ss = max(1, int(supersample))
    big_w, big_h = max(1, round(w * ss)), max(1, round(h * ss))
    big_surf = pygame.Surface((big_w, big_h), pygame.SRCALPHA)
    big_width = int(width) * ss if width else 0
    big_radius = int(border_radius) * ss if border_radius else 0
    pygame.draw.rect(big_surf, color, (0, 0, big_w, big_h), big_width, border_radius=big_radius)
    small_surf = pygame.transform.smoothscale(big_surf, (max(1, round(w)), max(1, round(h))))
    surface.blit(small_surf, (x, y))


def fit_design_canvas(canvas_w, canvas_h, real_w, real_h, margin=20):
    """Placement math for centering a fixed-design-size overlay in the window: native size if it fits, else shrunk to fit."""
    if real_w >= canvas_w and real_h >= canvas_h:
        return canvas_w, canvas_h, (real_w - canvas_w) // 2, (real_h - canvas_h) // 2
    shrink_w = min(canvas_w, real_w - margin)
    shrink_h = int(shrink_w * (canvas_h / canvas_w))
    if shrink_h > real_h - margin:
        shrink_h = real_h - margin
        shrink_w = int(shrink_h * (canvas_w / canvas_h))
    return shrink_w, shrink_h, (real_w - shrink_w) // 2, (real_h - shrink_h) // 2


def fit_menu_envelope(real_w, real_h, db=None, margin=16):
    """The on-screen envelope the Main Menu, Controls splash, and File Explorer are all boxed into."""
    _bezel_on = bool(db is not None and db.config.get("fake_43_border_enabled", False))
    if _bezel_on:
        env_w = max(1, real_w - margin * 2)
        env_h = max(1, real_h - margin * 2)
        return env_w, env_h, (real_w - env_w) // 2, (real_h - env_h) // 2, True
    aspect = 4.0 / 3.0
    if real_h and abs((real_w / real_h) - aspect) < 0.02:
        env_w = max(1, real_w - margin * 2)
        env_h = max(1, real_h - margin * 2)
        return env_w, env_h, (real_w - env_w) // 2, (real_h - env_h) // 2, False
    if real_w / real_h > aspect:
        box_h = real_h
        box_w = int(box_h * aspect)
    else:
        box_w = real_w
        box_h = int(box_w / aspect)
    box_x = (real_w - box_w) // 2
    box_y = (real_h - box_h) // 2
    env_w = max(1, box_w - margin * 2)
    env_h = max(1, box_h - margin * 2)
    env_x = box_x + margin
    env_y = box_y + margin
    return env_w, env_h, env_x, env_y, False


# ============================================================================
# SECTION: Menu-fit shrink for bezel styles
# ============================================================================


_BEZEL_STYLES_WITH_MENU_SHRINK = (1, 2)
# WARNING: the fallback number below must match MENU_FIT_SHRINK_FACTOR in performance_governor.py; it is used only when that module fails to import.
_MENU_FIT_SHRINK_FACTOR = performance_governor.MENU_FIT_SHRINK_FACTOR if performance_governor is not None else 0.94


def _menu_fit_shrink_factor(db):
    """Return _MENU_FIT_SHRINK_FACTOR while the Bezel toggle is on with style 1 or 2, else 1.0."""
    if db is None:
        return 1.0
    if not bool(db.config.get("fake_43_border_enabled", False)):
        return 1.0
    style = db.config.get("bezel_style", 1)
    try:
        style = int(style)
    except (TypeError, ValueError) as _swallowed_error:
        _log_swallowed("_menu_fit_shrink_factor: style = int(style)", _swallowed_error)
        style = 1
    if style in _BEZEL_STYLES_WITH_MENU_SHRINK:
        return _MENU_FIT_SHRINK_FACTOR
    return 1.0


# ============================================================================
# SECTION: Main Menu panel rectangle
# ============================================================================

# WARNING: the fallback numbers below must match MENU_CANVAS_W, MENU_CANVAS_H in performance_governor.py; they are used only when that module fails to import.
MENU_CANVAS_W = performance_governor.MENU_CANVAS_W if performance_governor is not None else 1036
MENU_CANVAS_H = performance_governor.MENU_CANVAS_H if performance_governor is not None else 680


def menu_panel_rect(real_w, real_h, db=None):
    """Returns (x, y, w, h): where the Main Menu panel sits on a surface of real_w by real_h; the File Explorer is sized by this same function."""
    # WARNING: the Main Menu and the File Explorer must both take their size from here; a second copy of this math makes them drift apart.
    env_w, env_h, env_x, env_y, stretch_fill = fit_menu_envelope(real_w, real_h, db)
    if stretch_fill:
        fit_w, fit_h = env_w, env_h
    else:
        fit_w = env_w
        fit_h = int(fit_w * (float(MENU_CANVAS_H) / MENU_CANVAS_W))
        if fit_h > env_h:
            fit_h = env_h
            fit_w = int(fit_h * (float(MENU_CANVAS_W) / MENU_CANVAS_H))
    shrink = _menu_fit_shrink_factor(db)
    if shrink != 1.0:
        fit_w = max(1, int(fit_w * shrink))
        fit_h = max(1, int(fit_h * shrink))
    return env_x + (env_w - fit_w) // 2, env_y + (env_h - fit_h) // 2, fit_w, fit_h


# ============================================================================
# SECTION: Remote & DVD key bindings
# ============================================================================


REMOTE_REMAP_ACTIONS = [
    ("w",     "Nav Up"),
    ("s",     "Nav Dwn"),
    ("a",     "Nav Lft"),
    ("d",     "Nav Rt"),
    ("enter", "Select"),
    ("up",    "Ch Up"),
    ("down",  "Ch Dwn"),
    ("right", "Volume Up"),
    ("left",  "Volume Dwn"),
    ("n",     "Mute"),
    ("m",     "Menu"),
    ("c",     "Controls Menu"),
    ("esc",   "Back/Minimize"),
]

NUMBER_REMAP_ACTIONS = [(str(i), str(i)) for i in range(10)]

DEFAULT_REMOTE_BINDINGS = {
    "w": pygame.K_w, "a": pygame.K_a, "s": pygame.K_s, "d": pygame.K_d,
    "up": pygame.K_UP, "down": pygame.K_DOWN, "left": pygame.K_LEFT, "right": pygame.K_RIGHT,
    "enter": pygame.K_RETURN, "esc": pygame.K_ESCAPE,
    "n": pygame.K_n, "m": pygame.K_m, "c": pygame.K_c,
}
for _i in range(10):
    DEFAULT_REMOTE_BINDINGS[str(_i)] = getattr(pygame, f"K_{_i}")
del _i

_SDLK_MASK = 0x40000000

_REMOTE_KEY_NAME_OVERRIDES = {
    pygame.K_RETURN:    "ENTER",   pygame.K_ESCAPE: "ESC",      pygame.K_SPACE:  "SPACE",
    pygame.K_UP:        "UP",      pygame.K_DOWN:   "DOWN",      pygame.K_LEFT:   "LEFT",
    pygame.K_RIGHT:     "RIGHT",   pygame.K_BACKSPACE: "BKSP",   pygame.K_TAB:    "TAB",
    pygame.K_LSHIFT:    "L-SHIFT", pygame.K_RSHIFT: "R-SHIFT",
    pygame.K_LCTRL:     "L-CTRL",  pygame.K_RCTRL:  "R-CTRL",
    pygame.K_LALT:      "L-ALT",   pygame.K_RALT:   "R-ALT",
    pygame.K_F1:  "F1",  pygame.K_F2:  "F2",  pygame.K_F3:  "F3",  pygame.K_F4:  "F4",
    pygame.K_F5:  "F5",  pygame.K_F6:  "F6",  pygame.K_F7:  "F7",  pygame.K_F8:  "F8",
    pygame.K_F9:  "F9",  pygame.K_F10: "F10", pygame.K_F11: "F11", pygame.K_F12: "F12",
    pygame.K_INSERT:   "INS",   pygame.K_DELETE: "DEL",
    pygame.K_HOME:     "HOME",  pygame.K_END:    "END",
    pygame.K_PAGEUP:   "PGUP",  pygame.K_PAGEDOWN: "PGDN",
    pygame.K_NUMLOCK:  "NUMLK", pygame.K_CAPSLOCK: "CAPSLK",
    pygame.K_PRINT:    "PRINT", pygame.K_PAUSE:    "PAUSE",
    pygame.K_KP_ENTER:    "KP-ENT", pygame.K_KP_PERIOD:   "KP-.",
    pygame.K_KP_PLUS:     "KP-+",   pygame.K_KP_MINUS:    "KP--",
    pygame.K_KP_MULTIPLY: "KP-*",   pygame.K_KP_DIVIDE:   "KP-/",
    pygame.K_KP0: "KP-0", pygame.K_KP1: "KP-1", pygame.K_KP2: "KP-2",
    pygame.K_KP3: "KP-3", pygame.K_KP4: "KP-4", pygame.K_KP5: "KP-5",
    pygame.K_KP6: "KP-6", pygame.K_KP7: "KP-7", pygame.K_KP8: "KP-8",
    pygame.K_KP9: "KP-9",
    _SDLK_MASK | 258: "MED-NEXT",
    _SDLK_MASK | 259: "MED-PREV",
    _SDLK_MASK | 260: "MED-STOP",
    _SDLK_MASK | 261: "MED-PLAY",
    _SDLK_MASK | 262: "MED-MUTE",
    _SDLK_MASK | 285: "MED-REW",
    _SDLK_MASK | 286: "MED-FF",
}


def _remote_key_label(key_code):
    if key_code is None:
        return "---"
    if key_code in _REMOTE_KEY_NAME_OVERRIDES:
        return _REMOTE_KEY_NAME_OVERRIDES[key_code]
    try:
        name = pygame.key.name(key_code)
        if name and name not in ("", "unknown key"):
            return name.upper()[:8]
    except Exception as _swallowed_error:
        _log_swallowed("_remote_key_label: name = pygame.key.name(key_code)", _swallowed_error)
    return f"#{key_code & 0xFFFF}"[:8]


def _get_remote_binding(db, action):
    bindings = db.config.get("remote_bindings", {}) if db is not None else {}
    return bindings.get(action, DEFAULT_REMOTE_BINDINGS.get(action))


DVD_MAP_ACTIONS = [
    (menu_nav.DVD_PLAY_PAUSE,   "Play / Pause"),
    (menu_nav.DVD_REWIND,       "Rewind  (hold = fast)"),
    (menu_nav.DVD_FAST_FORWARD, "Fast Forward  (hold = fast)"),
    (menu_nav.DVD_RETURN_MENU,  "Return to Main Menu"),
]
DVD_MAP_DEFAULTS = {action: menu_nav.dvd_default_binding(action) for action in menu_nav.DVD_ACTIONS}


def get_dvd_binding(db, action):
    """Current key bound to a DVD transport action (falls back to default)."""
    bindings = db.config.get("dvd_bindings", {}) if db is not None else {}
    return bindings.get(action, DVD_MAP_DEFAULTS.get(action))


# ============================================================================
# SECTION: DVD button-mapping popup
# ============================================================================


def render_dvd_button_mapping_popup(surface, app_state, db):
    import colorsys

    cursor      = app_state.get("dvd_map_cursor", 0)
    capturing   = app_state.get("dvd_map_capturing", False)
    capture_idx = app_state.get("dvd_map_capture_idx", 0)

    ui_hue = db.config.get("theme_ui_hue", 140) / 360.0 if db is not None else 140 / 360.0
    r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.9, 1.0)
    NEON_GREEN = (int(r * 255), int(g * 255), int(b * 255))
    TEXT_WHITE = COLOR_TEXT_WHITE
    OFF_RED    = COLOR_MENU_DVD_MAP_OFF_RED
    PANEL_BG   = COLOR_MENU_DVD_MAP_PANEL
    ROW_BG     = COLOR_MENU_BUTTON
    KEY_BG     = COLOR_MENU_DVD_MAP_KEY
    DIM        = COLOR_MENU_DIM_TEXT

    sw, sh = surface.get_size()
    win_w, win_h = 640, 460
    win_x = (sw - win_w) // 2
    win_y = (sh - win_h) // 2

    pygame.draw.rect(surface, PANEL_BG, (win_x, win_y, win_w, win_h), border_radius=10)
    _aa_rounded_rect(surface, NEON_GREEN, (win_x, win_y, win_w, win_h), 2, border_radius=10)

    f_title = _get_font("Courier New", 24, bold=True)
    f_body  = _get_font("Courier New", 22, bold=True)
    f_key   = f_body
    f_btn   = _get_font("Courier New", 19, bold=True)
    f_small = _get_font("Courier New", 12, bold=False)

    title = f_title.render("DVD BUTTON MAPPING", True, COLOR_ARCADE_GOLD)
    surface.blit(title, (win_x + win_w // 2 - title.get_width() // 2, win_y + 18))

    hint_txt = ("Press a button for the highlighted action"
                if capturing else
                "Select an action to see its key  •  Close to leave")
    hint = f_small.render(hint_txt, True, DIM)
    surface.blit(hint, (win_x + win_w // 2 - hint.get_width() // 2, win_y + 52))

    _reject_flash = app_state.get("_dvd_map_reject_flash")
    if _reject_flash is not None and (pygame.time.get_ticks() - _reject_flash) < 2000:
        _reject_key  = app_state.get("_dvd_map_reject_key")
        _reject_name = _remote_key_label(_reject_key) if _reject_key is not None else "KEY"
        _warn_txt = f"  {_reject_name} is already used — remap it first  "
        _warn_surf = f_small.render(_warn_txt, True, COLOR_MENU_WARNING_RED)
        _warn_x = win_x + win_w // 2 - _warn_surf.get_width() // 2
        surface.blit(_warn_surf, (_warn_x, win_y + 68))

    list_x = win_x + 30
    list_w = win_w - 60
    row_h  = 44
    row_y  = win_y + 104
    for idx, (action, label) in enumerate(DVD_MAP_ACTIONS):
        ry = row_y + idx * (row_h + 8)
        is_cap_row = capturing and idx == capture_idx
        pygame.draw.rect(surface, ROW_BG, (list_x, ry, list_w, row_h), border_radius=6)
        if is_cap_row:
            _aa_rounded_rect(surface, NEON_GREEN, (list_x, ry, list_w, row_h), SELECTION_BORDER_WIDTH, border_radius=6)
        lbl = f_body.render(label, True, NEON_GREEN if is_cap_row else TEXT_WHITE)
        surface.blit(lbl, (list_x + 14, ry + row_h // 2 - lbl.get_height() // 2))

        key_txt = "PRESS…" if is_cap_row else _remote_key_label(get_dvd_binding(db, action))
        kb_w, kb_h = 110, 30
        kb_x = list_x + list_w - kb_w - 12
        kb_y = ry + row_h // 2 - kb_h // 2
        pygame.draw.rect(surface, KEY_BG, (kb_x, kb_y, kb_w, kb_h), border_radius=4)
        _aa_rounded_rect(surface, NEON_GREEN if is_cap_row else DIM, (kb_x, kb_y, kb_w, kb_h), SELECTION_BORDER_WIDTH, border_radius=4)
        ks = f_key.render(key_txt, True, NEON_GREEN if is_cap_row else TEXT_WHITE)
        surface.blit(ks, (kb_x + kb_w // 2 - ks.get_width() // 2, kb_y + kb_h // 2 - ks.get_height() // 2))

    btn_h = 40
    btn_pad_x = 28
    btn_y = win_y + win_h - MENU_CLOSE_BUTTON_PLACE[1]
    map_focused = (not capturing) and cursor == 0
    mb_lbl = f_btn.render("Map Buttons", True, NEON_GREEN if map_focused else TEXT_WHITE)
    mb_w = mb_lbl.get_width() + (btn_pad_x * 2)
    mb_x = win_x + 30
    pygame.draw.rect(surface, ROW_BG, (mb_x, btn_y, mb_w, btn_h), border_radius=5)
    if map_focused:
        _aa_rounded_rect(surface, NEON_GREEN, (mb_x, btn_y, mb_w, btn_h), SELECTION_BORDER_WIDTH, border_radius=5)
    surface.blit(mb_lbl, (mb_x + mb_w // 2 - mb_lbl.get_width() // 2, btn_y + btn_h // 2 - mb_lbl.get_height() // 2))

    close_focused = (not capturing) and cursor == 1
    _draw_menu_close(surface, win_x, win_w, win_y, win_h, close_focused, f_btn, NEON_GREEN)


# ============================================================================
# SECTION: Font cache & logo fitting helpers
# ============================================================================


_font_cache = {}

def _crop_logo_to_artwork(logo_surface):
    if logo_surface is None:
        return None
    try:
        bounds = logo_surface.get_bounding_rect(min_alpha=10)
        if bounds.width > 0 and bounds.height > 0:
            if (bounds.width != logo_surface.get_width()
                    or bounds.height != logo_surface.get_height()):
                return logo_surface.subsurface(bounds).copy()
    except Exception as _swallowed_error:
        _log_swallowed("_crop_logo_to_artwork: bounds = logo_surface.get_bounding_rect(min_alpha=10)", _swallowed_error)
    return logo_surface

def _fit_logo_to_box(logo_surface, box_w, box_h):
    logo_surface = _crop_logo_to_artwork(logo_surface)
    if logo_surface is None:
        return None
    try:
        raw_w = logo_surface.get_width()
        raw_h = logo_surface.get_height()
        if raw_w <= 0 or raw_h <= 0:
            return None
        target_w = max(1, int(box_w * 0.84))
        target_h = max(1, int(box_h * 0.76))
        ratio = min(target_w / raw_w, target_h / raw_h)
        scaled_w = max(1, int(round(raw_w * ratio)))
        scaled_h = max(1, int(round(raw_h * ratio)))
        return pygame.transform.smoothscale(logo_surface, (scaled_w, scaled_h))
    except Exception as _swallowed_error:
        _log_swallowed("_fit_logo_to_box: raw_w = logo_surface.get_width()", _swallowed_error)
        return None


# ============================================================================
# SECTION: TV guide: layout, canvases & time math
# ============================================================================


_guide_timeline_cache = {}


_guide_exists_state = {"token": None, "cache": {}}

def _guide_visualizer_admitted(ch_info, app_state, db):
    """Returns the songs the station's Visualizer lineup admits for this channel right now."""
    _inputs = app_state.get("visualizer_admission_inputs") if app_state is not None else None
    _unseekable, _unseekable_exts = _inputs() if callable(_inputs) else ({}, set())
    return media.admit_visualizer_assets(
        ch_info.get("schedules", {}).get("Music Tracks", []), _unseekable, _unseekable_exts)


def _guide_path_exists(path, app_state):
    import os
    token = app_state.get("guide_refresh_token", 0) if app_state is not None else 0
    if token != _guide_exists_state["token"]:
        _guide_exists_state["cache"].clear()
        _guide_exists_state["token"] = token
    cache = _guide_exists_state["cache"]
    v = cache.get(path)
    if v is None:
        v = os.path.exists(path)
        cache[path] = v
    return v

GUIDE_DESIGN_W, GUIDE_DESIGN_H = 1920, 1080
_guide_canvas = None

# WARNING: the fallback number must match GUIDE_HUB_FILL_CACHE_SECONDS in performance_governor.py; it is used only when that module fails to import.
_GUIDE_HUB_FILL_TTL_SECONDS = performance_governor.GUIDE_HUB_FILL_CACHE_SECONDS if performance_governor is not None else 2.0

_guide_hub_fill_cache = {}


def _guide_entry_parts(entry):
    """The files one guide entry airs, in order, as (normalised path, duration) pairs: a pair gives both episodes, a leftovers bundle all of its files, anything else its one file; empty when the entry has no path."""
    import os
    if not isinstance(entry, dict):
        return []
    if entry.get("is_pair"):
        raw = [(entry.get("pair_ep1_path", entry.get("path", "")), entry.get("pair_ep1_duration", 0)),
               (entry.get("pair_ep2_path", ""), entry.get("pair_ep2_duration", 0))]
    elif entry.get("is_bundle"):
        _paths = entry.get("bundle_item_paths") or media.lineup_paths([entry])
        _durs = list(entry.get("bundle_item_durations") or [])
        _durs += [0] * (len(_paths) - len(_durs))
        raw = list(zip(_paths, _durs))
    else:
        raw = [(entry.get("path", ""), entry.get("duration", 0))]
    return [(os.path.normcase(os.path.normpath(str(p))), d) for p, d in raw if p]


def _guide_entry_airs_file(entry, file_path):
    """True when the guide entry airs the given file, as any of its episodes."""
    import os
    _want = os.path.normcase(os.path.normpath(str(file_path)))
    return any(p == _want for p, _d in _guide_entry_parts(entry))


def _guide_aired_rows_in_view(aired_rows, view_start, view_end):
    """Hub rows that touch the guide viewport, past, airing and upcoming alike."""
    return [_r for _r in aired_rows if _r[2] > view_start and _r[1] < view_end + 3600]


def _guide_splice_aired(timeline_entries, aired_rows):
    """The timeline with the aired rows laid over it; an entry they only partly cover keeps its uncovered part."""
    # WARNING: the rows must cover their whole span; cutting them at the clock leaves a hole where the guide's own break differs from the scheduler's, and that splits pairs and draws OFF-AIR.
    if not aired_rows:
        return timeline_entries
    _rows = sorted(aired_rows, key=lambda _p: _p[1])
    _runs = []
    for _p in _rows:
        if _runs and _p[1] <= _runs[-1][1] + 0.5:
            _runs[-1][1] = max(_runs[-1][1], _p[2])
        else:
            _runs.append([_p[1], _p[2]])
    _kept = []
    for _entry, _t_start, _t_end, _t_block in timeline_entries:
        _pieces = [(_t_start, _t_end)]
        for _rs, _re in _runs:
            _next = []
            for _ps, _pe in _pieces:
                if not (_ps < _re - 0.5 and _rs < _pe - 0.5):
                    _next.append((_ps, _pe))
                    continue
                if _ps < _rs - 0.5:
                    _next.append((_ps, min(_pe, _rs)))
                if _pe > _re + 0.5:
                    _next.append((max(_ps, _re), _pe))
            _pieces = _next
        for _ps, _pe in _pieces:
            if _pe - _ps > 0.5:
                _kept.append((_entry, _ps, _pe, _t_block))
    return sorted(_kept + _rows, key=lambda _t: _t[1])


def _guide_merge_cells(timeline_entries):
    """The timeline with each commercial joined to the show before it and each pair's parts joined into one cell."""
    _merged_entries = []
    for _entry, _e_start, _e_end, _e_block in timeline_entries:
        if _entry is None:
            _merged_entries.append((_entry, _e_start, _e_end, _e_block))
            continue
        _is_com = isinstance(_entry, dict) and _entry.get("is_commercial")
        if _merged_entries:
            _p_entry, _p_start, _p_end, _p_block = _merged_entries[-1]
            _p_valid = _p_entry is not None
            _same_show = (not _is_com and _p_valid
                          and isinstance(_entry, dict) and isinstance(_p_entry, dict)
                          and not _p_entry.get("is_commercial")
                          and _entry.get("path") == _p_entry.get("path")
                          and _entry.get("seek_offset", 0) > 0)
            _contiguous = _p_valid and abs(_e_start - _p_end) < 1
            if _contiguous and (_is_com or _same_show):
                _merged_entries[-1] = (_p_entry, _p_start, _e_end, _p_block)
                continue
        _merged_entries.append((_entry, _e_start, _e_end, _e_block))
    return _merged_entries


_guide_hub_past_cache = {}


def _same_channel_number(a, b):
    """True when two channel labels name the same channel number (4 and 04 are the same)."""
    try:
        return int(str(a)) == int(str(b))
    except (TypeError, ValueError):
        return str(a) == str(b)


def visualizer_gain_file(state, channel_key):
    """The song whose loudness gain the music menu may change: the one playing on this channel, only while this channel is the one on screen, else an empty string."""
    if not _same_channel_number(state.get("current_channel", ""), channel_key):
        return ""
    return state.get("_vis_target_filepath", "") or ""


def _guide_hub_past_signature(ch_str, now_dt, hub=None):
    """A small value that changes when the hub's aired, airing or Static rows of the channel change, for the guide's timeline cache key; None when the hub cannot be read."""
    try:
        _bucket = int(now_dt.timestamp() // _GUIDE_HUB_FILL_TTL_SECONDS)
        _key = (ch_str, _bucket, id(hub))
        if _key in _guide_hub_past_cache:
            return _guide_hub_past_cache[_key]
        if hub is None:
            import media_library as _ml
            hub = _ml.shared_schedule_hub()
        _sig = hub.past_signature(ch_str)
        if len(_guide_hub_past_cache) > 64:
            _guide_hub_past_cache.clear()
        _guide_hub_past_cache[_key] = _sig
        return _sig
    except Exception as error:
        log.warning("[GUIDE HUB PAST] ch%s: could not read the hub's past rows signature; the generated guide fallback remains active: %s", ch_str, error)
        return None


def _guide_hub_aired_cells(ch_str, now_dt, view_start, view_end, hub=None):
    """The hub's airing, aired and Static rows that touch the viewport, as (entry, start, end, block) rows built from each row's saved guide facts, commercials included and in start order; rows without guide facts are left to the generated fallback timeline, and overlaps are dropped."""
    # WARNING: only the guide's drawing is read from the hub; a failure must return no rows so the generated timeline remains the safe fallback.
    try:
        now_secs = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second + now_dt.microsecond / 1_000_000.0
        now_epoch = now_dt.timestamp()
        midnight = now_epoch - now_secs
        if hub is None:
            import media_library as _ml
            hub = _ml.shared_schedule_hub()
        found = hub.rows_between(ch_str, midnight + view_start, midnight + view_end + 3600, now_epoch, full=True)
        out, last_end = [], None
        for row in found:
            facts = row.get("guide_entry")
            if not facts or not row.get("path"):
                continue
            start = float(row["planned_start"]) - midnight
            length = float(row["used_length"] or 0)
            if length <= 0:
                continue
            if last_end is not None and start < last_end - 0.5:
                continue
            entry = dict(facts)
            entry.setdefault("path", row["path"])
            entry["duration"] = length
            entry.setdefault("is_commercial", row.get("kind") == "commercial")
            out.append((entry, start, start + length, ""))
            last_end = start + length
        return out
    except Exception as error:
        log.warning("[GUIDE HUB PAST] ch%s: could not read the guide's past from the hub, the ledger and the rebuilt timeline draw it: %s", ch_str, error)
        return []


_guide_hub_future_sig_cache = {}


def _guide_hub_future_signature(ch_str, now_dt, hub=None):
    """A small value that changes when the hub's projected future rows of the channel are replaced, for the guide's timeline cache key; None when the hub cannot be read."""
    try:
        _bucket = int(now_dt.timestamp() // _GUIDE_HUB_FILL_TTL_SECONDS)
        _key = (ch_str, _bucket, id(hub))
        if _key in _guide_hub_future_sig_cache:
            return _guide_hub_future_sig_cache[_key]
        if hub is None:
            import media_library as _ml
            hub = _ml.shared_schedule_hub()
        _sig = hub.future_signature(ch_str)
        if len(_guide_hub_future_sig_cache) > 64:
            _guide_hub_future_sig_cache.clear()
        _guide_hub_future_sig_cache[_key] = _sig
        return _sig
    except Exception as error:
        log.warning("[GUIDE HUB FUTURE] ch%s: could not read the hub's future rows signature, the guide's saved future is not refreshed from the hub: %s", ch_str, error)
        return None


def _guide_hub_future_cells(ch_str, now_dt, view_start, view_end, hub=None):
    """The hub's projected future rows that touch the viewport, as (entry, start, end, block) rows built from each row's saved guide facts, commercials included and in start order; a row without readable facts is left out so the guide's own future covers it, and an overlapping row is dropped."""
    # WARNING: only the guide's drawing is read from the hub; a failure must return no rows so the guide draws its own future as before.
    try:
        now_secs = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second + now_dt.microsecond / 1_000_000.0
        midnight = now_dt.timestamp() - now_secs
        if hub is None:
            import media_library as _ml
            hub = _ml.shared_schedule_hub()
        found = hub.future_rows_between(ch_str, midnight + view_start, midnight + view_end + 3600)
        out, last_end = [], None
        for row in found:
            facts = row.get("guide_entry")
            if not facts or not row.get("path"):
                continue
            start = float(row["planned_start"]) - midnight
            length = float(row["used_length"] or 0)
            if length <= 0:
                continue
            if last_end is not None and start < last_end - 0.5:
                continue
            entry = dict(facts)
            entry.setdefault("path", row["path"])
            entry["duration"] = length
            entry.setdefault("is_commercial", row.get("kind") == "commercial")
            out.append((entry, start, start + length, ""))
            last_end = start + length
        return out
    except Exception as error:
        log.warning("[GUIDE HUB FUTURE] ch%s: could not read the guide's future from the hub, the guide draws its own future: %s", ch_str, error)
        return []


def _guide_fill_from_hub(timeline_entries, ch_str, now_dt, view_start, view_end, app_state=None, hub=None):
    """Adds the hub's airing, aired and Static show rows that no guide entry covers, so the show on air at boot, the shows before it and a slot that went to Static are drawn as the show the clock scheduled and not as OFF-AIR; returns the entries sorted by start."""
    # WARNING: only the guide's drawing is filled; nothing here may change what airs, and a failure must leave the entries as they were.
    try:
        now_secs = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second + now_dt.microsecond / 1_000_000.0
        now_epoch = now_dt.timestamp()
        midnight = now_epoch - now_secs
        cache_key = (ch_str, int(now_epoch // _GUIDE_HUB_FILL_TTL_SECONDS), int(view_start), int(view_end), id(hub))
        rows = _guide_hub_fill_cache.get(cache_key)
        if rows is None:
            if hub is None:
                import media_library as _ml
                hub = _ml.shared_schedule_hub()
            rows = hub.rows_between(ch_str, midnight + view_start, midnight + view_end, now_epoch)
            if len(_guide_hub_fill_cache) > 64:
                _guide_hub_fill_cache.clear()
            _guide_hub_fill_cache[cache_key] = rows
        added = []
        for row in rows:
            start = float(row["planned_start"]) - midnight
            end = start + float(row["used_length"] or 0)
            if end - start <= 0 or not row["path"]:
                continue
            if any(entry is not None and es < end - 0.5 and start < ee - 0.5 for entry, es, ee, _eb in list(timeline_entries) + added):
                continue
            if not _guide_path_exists(row["path"], app_state):
                continue
            added.append(({"path": row["path"], "duration": end - start, "is_commercial": False, "_guide_live_override": True,
                           "_guide_hub_fill": True}, start, end, ""))
        if not added:
            return timeline_entries
        return sorted(list(timeline_entries) + added, key=lambda item: item[1])
    except Exception as error:
        log.warning("[GUIDE HUB FILL] ch%s: could not fill the guide row from the hub, the row is drawn as it was: %s", ch_str, error)
        return timeline_entries


def _get_guide_canvas():
    global _guide_canvas
    if _guide_canvas is None or _guide_canvas.get_size() != (GUIDE_DESIGN_W, GUIDE_DESIGN_H):
        _guide_canvas = pygame.Surface((GUIDE_DESIGN_W, GUIDE_DESIGN_H))
    return _guide_canvas

GUIDE_DESIGN_W_4_3, GUIDE_DESIGN_H_4_3 = 1920, 1440

_guide_canvas_43 = None

def _get_guide_canvas_43():
    global _guide_canvas_43
    if _guide_canvas_43 is None or _guide_canvas_43.get_size() != (GUIDE_DESIGN_W_4_3, GUIDE_DESIGN_H_4_3):
        _guide_canvas_43 = pygame.Surface((GUIDE_DESIGN_W_4_3, GUIDE_DESIGN_H_4_3))
    return _guide_canvas_43

def _guide_aspect_is_4_3(db):
    """Whether the TV guide should render in its 4:3 layout right now."""
    if db is None:
        return False
    return getattr(db, "_detected_aspect_ratio", None) == "4:3"


def get_guide_visible_rows(db):
    return GUIDE_VISIBLE_ROWS_4_3 if _guide_aspect_is_4_3(db) else GUIDE_VISIBLE_ROWS_16_9

def get_guide_num_cols(db):
    return GUIDE_NUM_COLS_4_3 if _guide_aspect_is_4_3(db) else GUIDE_NUM_COLS_16_9

# WARNING: the fallback numbers below must match GUIDE_TOTAL_HALF_HOURS, GUIDE_PAST_HALF_HOURS in performance_governor.py; they are used only when that module fails to import.
GUIDE_TOTAL_HALF_HOURS = performance_governor.GUIDE_TOTAL_HALF_HOURS if performance_governor is not None else 336
GUIDE_PAST_HALF_HOURS = performance_governor.GUIDE_PAST_HALF_HOURS if performance_governor is not None else 48

def clamp_guide_time_idx(raw_idx, num_cols):
    """Keeps the guide's first column between the previous midnight and the last visible half hour; the 7 visible days run from the previous midnight."""
    return max(-GUIDE_PAST_HALF_HOURS, min(raw_idx, GUIDE_TOTAL_HALF_HOURS - GUIDE_PAST_HALF_HOURS - num_cols))


# WARNING: the fallback number below must match NAME_CELL_SIDE_PAD in performance_governor.py; it is used only when that module fails to import.
_NAME_CELL_SIDE_PAD = performance_governor.NAME_CELL_SIDE_PAD if performance_governor is not None else 10


# WARNING: the fallback numbers below must match GUIDE_43_CLOCK_CELL_PAD_PX, GUIDE_43_HDR_CELL_PAD_PX, GUIDE_43_CLOCK_PAIR_GAP_PX, GUIDE_43_HDR_TO_CLOCK_RATIO in performance_governor.py; they are used only when that module fails to import.
GUIDE_43_CLOCK_CELL_PAD_PX = performance_governor.GUIDE_43_CLOCK_CELL_PAD_PX if performance_governor is not None else 10
GUIDE_43_HDR_CELL_PAD_PX = performance_governor.GUIDE_43_HDR_CELL_PAD_PX if performance_governor is not None else 12
GUIDE_43_CLOCK_PAIR_GAP_PX = performance_governor.GUIDE_43_CLOCK_PAIR_GAP_PX if performance_governor is not None else 8
GUIDE_43_HDR_TO_CLOCK_RATIO = performance_governor.GUIDE_43_HDR_TO_CLOCK_RATIO if performance_governor is not None else 22.0 / 17.0


def compute_channel_name_char_limit(real_h=None):
    """Return how many characters of a channel name fit in the guide's name cell."""
    import math as _math

    w, h = GUIDE_DESIGN_W_4_3, GUIDE_DESIGN_H_4_3
    num_cols = GUIDE_NUM_COLS_4_3
    num_rows = GUIDE_VISIBLE_ROWS_4_3
    col_width = w // (num_cols + 1)

    _CLOCK_CELL_PAD = GUIDE_43_CLOCK_CELL_PAD_PX
    _HDR_CELL_PAD = GUIDE_43_HDR_CELL_PAD_PX
    _CLOCK_PAIR_GAP = GUIDE_43_CLOCK_PAIR_GAP_PX
    _HDR_TO_CLOCK_RATIO = GUIDE_43_HDR_TO_CLOCK_RATIO
    _HDR_FONT_PX_16_9 = GUIDE_HDR_FONT_PX
    _CLOCK_FONT_PX_16_9 = GUIDE_CLOCK_FONT_PX
    _CLOCK_FONT_PX_4_3 = _CLOCK_FONT_PX_16_9
    _HDR_FONT_PX_4_3 = _HDR_FONT_PX_16_9
    for _clock_pt in range(60, 16, -1):
        _hdr_pt = max(_clock_pt, round(_clock_pt * _HDR_TO_CLOCK_RATIO))
        _fc = _get_font("Courier New", _clock_pt, bold=True)
        _fh = _get_font("Courier New", _hdr_pt, bold=True)
        _clock_total_w = (_fc.size("00/00/0000")[0] + _CLOCK_PAIR_GAP
                           + _fc.size("00:00:00 AM")[0])
        _hdr_total_w = _fh.size("WED 12:00 PM")[0]
        if (_clock_total_w <= col_width - _CLOCK_CELL_PAD
                and _hdr_total_w <= col_width - _HDR_CELL_PAD):
            _CLOCK_FONT_PX_4_3 = _clock_pt
            _HDR_FONT_PX_4_3 = _hdr_pt
            break

    font_hdr = _get_font("Courier New", _HDR_FONT_PX_4_3, bold=True)
    font_clock = _get_font("Courier New", _CLOCK_FONT_PX_4_3, bold=True)

    _MIN_GUIDE_FONT_PX = GUIDE_MIN_FONT_PX
    _guide_scale_y = (real_h / h) if real_h else 1.0
    if 0 < _guide_scale_y < 1.0:
        _min_font_pt = max(_MIN_GUIDE_FONT_PX,
                            int(_math.ceil(_MIN_GUIDE_FONT_PX / _guide_scale_y)))
    else:
        _min_font_pt = _MIN_GUIDE_FONT_PX

    _hdr_text_h = max(font_hdr.get_height(), font_clock.get_height())
    header_height = max(32, _hdr_text_h + 14)

    _pw_ratio = 900.0 / 1920.0
    _base_pw = int(w * _pw_ratio)
    _base_ph = int(_base_pw * (9.0 / 16.0))
    _header_growth = header_height - 32
    ph = _base_ph - _header_growth
    py = 15
    grid_top_y = py + ph + 10
    start_y = grid_top_y + header_height
    row_height = (h - start_y) // num_rows

    dynamic_font_size = max(13, int(row_height * 0.32))
    dynamic_font_size = max(dynamic_font_size, _min_font_pt)
    font_row_sm = _get_font("Courier New", dynamic_font_size, bold=True)

    max_txt_w = col_width - (2 * _NAME_CELL_SIDE_PAD)

    _probe_len = 20
    _char_w = font_row_sm.size("M" * _probe_len)[0] / _probe_len
    if _char_w <= 0:
        return 18

    return max(4, int(max_txt_w // _char_w))

def get_current_guide_time_idx(app_state, db):
    """Return the half-hour column index of the guide's current time window."""
    num_cols = get_guide_num_cols(db)
    now_dt = broadcast_clock.now().replace(tzinfo=None)
    system_live_c_idx = (now_dt.hour * 2) + (1 if now_dt.minute >= 30 else 0)
    user_shift = app_state.get("selected_guide_time_idx", 0)
    return clamp_guide_time_idx(system_live_c_idx + user_shift, num_cols)


# ============================================================================
# SECTION: Settings menu: feature flags & row metadata
# ============================================================================


UPDATER_AND_SERVER_FEATURES_ENABLED = True

# WARNING: the fallback number below must match SERVER_TOGGLE_ENABLED in performance_governor.py; it is used only when that module fails to import.
SERVER_TOGGLE_ENABLED = performance_governor.SERVER_TOGGLE_ENABLED if performance_governor is not None else False

GPU_RENDER_MODE_ENABLED = True

SYSTEM_ROW_META = {
    "game_ch":            {"kind": "toggle", "label": "Games (Channel 03):"},
    "guide":              {"kind": "toggle", "label": "TV Guide (Channel 04):"},
    "controls_on_start":  {"kind": "toggle", "label": "Controls Menu (Nav Info):"},
    "boot_on_start":      {"kind": "toggle", "label": "Custom Shell Launcher:"},
    "kiosk_mode":         {"kind": "toggle", "label": "Kiosk (Hide Scheduling):"},
    "server_option":      {"kind": "toggle", "label": "Server Mode:"},
    "loudness_scan":      {"kind": "toggle", "label": "Loudness Scan:"},
    "pixel_orbiter":      {"kind": "toggle", "label": "Pixel Orbiter:"},
    "bezel_toggle":       {"kind": "toggle", "label": "CRT TV Bezels:"},
    "captions_toggle":    {"kind": "toggle", "label": "Closed Captions:"},
    "mono_stereo_toggle": {"kind": "toggle", "label": "Mono Audio:"},
    "warp_screen":        {"kind": "toggle", "label": "Warp Screen:"},
    "boot_anim":          {"kind": "toggle", "label": "Boot Anim:"},
    "static_noise":       {"kind": "toggle", "label": "Static Noise:"},
    "reception_lines":    {"kind": "toggle", "label": "Reception Lines:"},
    "scan_lines":         {"kind": "toggle", "label": "Scan Lines:"},
    "bezel_style_select": {"kind": "button", "label": "Bezel Style: Option 1", "color": COLOR_MENU_BUTTON_BLUE},
    "update_check":       {"kind": "button", "label": "Look for Updates", "color": COLOR_MENU_BUTTON_BLUE},
    "remote_remap":       {"kind": "button", "label": "Remote Remapping", "color": COLOR_MENU_BUTTON_BLUE},
    "gpu_render_mode":    {"kind": "button", "label": "GPU/CPU: Disabled (CPU Only)", "color": COLOR_MENU_BUTTON_BLUE},
    "performance_mode":   {"kind": "button", "label": "Performance Mode: Standard", "color": COLOR_MENU_BUTTON_BLUE},
    "advanced_settings":  {"kind": "button", "label": "Advanced Settings", "color": COLOR_MENU_BUTTON_BLUE},
    "reset_settings":     {"kind": "button", "label": "Reset All Settings"},
    "exit_program":       {"kind": "button", "label": "Exit Program"},
    "poweroff":           {"kind": "button", "label": "Turn Off Computer"},
}


def _gpu_render_mode_options(db):
    """Return ordered (value, label) pairs for the GPU/CPU button: detected adapters by VRAM, then 'CPU Only'."""
    adapters = getattr(db, "_detected_gpu_adapters", None) if db is not None else None
    real_adapters = [a for a in (adapters or []) if not a.get("is_software")]
    opts = []
    for a in real_adapters:
        name = str(a.get("name", "")).strip()
        if not name:
            continue
        short = name.replace("(R)", "").replace("(TM)", "")
        short = " ".join(short.split())
        opts.append((name, short))
    opts.append(("cpu_only", "CPU Only"))
    return opts


def _gpu_render_mode_label(db):
    """Return the 'GPU/CPU: <choice>' button text from render_mode_override, falling back to CPU Only."""
    if not GPU_RENDER_MODE_ENABLED:
        return "GPU/CPU: Disabled (CPU Only)"
    opts = _gpu_render_mode_options(db)
    saved = db.config.get("render_mode_override", "cpu_only") if db is not None else "cpu_only"
    by_value = dict(opts)
    if saved not in by_value:
        saved = "cpu_only"
    return f"GPU/CPU: {by_value[saved]}"


def _warp_needs_a_bezel(db):
    """True while no bezel that warps is on (CRT TV Bezels off and no home console's own bezel on): Warp Screen is greyed out and shown off, and its saved choice is kept."""
    try:
        from game_deck import warp_bezel_available
        return not warp_bezel_available(db)
    except Exception as _swallowed_error:
        _log_swallowed("_warp_needs_a_bezel: from game_deck import warp_bezel_available", _swallowed_error)
        return False


def _mode_locks(config_key):
    """True while the current performance mode greys out and switches off this setting."""
    try:
        return performance_governor is not None and performance_governor.feature_locked(config_key)
    except Exception as _swallowed_error:
        _log_swallowed("_mode_locks: return performance_governor is not None and performance_gove", _swallowed_error)
        return False


def _performance_mode_label(db):
    """Return the 'Performance Mode: <mode>' button text from the live governor mode."""
    if performance_governor is None:
        return "Performance Mode: Standard"
    mode = performance_governor.get_mode()
    return "Performance Mode: " + str(performance_governor.MODE_TABLE.get(mode, {}).get("label", mode.title()))


# ============================================================================
# SECTION: Settings menu: server picker entries
# ============================================================================


SERVER_MENU_BTN_SEL    = 500
CAPTIONS_COLOR_BOX_SEL = 502


def get_mock_discovered_servers():
    """Return the hardcoded list of {name, host} entries used as mock server discovery results."""
    return [
        {"name": "Mom",   "host": "Living Room PC"},
        {"name": "Dad",   "host": "Living Room PC"},
        {"name": "Steve", "host": "Steve-PC"},
    ]


def get_server_picker_entries(db):
    """Alphabetically-sorted (by display name) *live* discovered-server entries for SERVER_PICKER_MENU."""
    listener = getattr(db, "server_listener", None) if db is not None else None
    if listener is None:
        return []
    try:
        entries = listener.get_discovered_entries()
    except Exception as _swallowed_error:
        _log_swallowed("get_server_picker_entries: entries = listener.get_discovered_entries()", _swallowed_error)
        entries = []
    return sorted(entries, key=lambda e: e["name"].lower())


def get_server_picker_display_rows(entries, include_close=True):
    """Build the SERVER_PICKER_MENU render list: letter headers, entry rows and, unless windowed, a Close row."""
    rows = []
    last_letter = None
    for entry in entries:
        letter = entry["name"][:1].upper() if entry["name"] else "#"
        if letter != last_letter:
            rows.append({"type": "header", "text": letter})
            last_letter = letter
            rows.append({
                "type": "entry",
                "name": entry["name"],
                "host": entry["host"],
                "slot_id": entry.get("slot_id"),
            })
    if include_close:
        rows.append({"type": "close"})
    return rows


def get_server_picker_selectable_count(entries):
    """Return the number of selectable SERVER_PICKER_MENU rows: the entries plus Close, headers excluded."""
    return len(entries) + 1


# ============================================================================
# SECTION: Settings menu: row lists per tab
# ============================================================================


# VALUES CHANGE HERE: the config key each Channels > Settings option writes; the System tab shows these options while Kiosk is on.
SYSTEM_KIOSK_SETTING_KEYS = {
    "bezel_toggle": "fake_43_border_enabled",
    "warp_screen": "warp_screen_enabled",
    "boot_anim": "boot_anim_enabled",
    "static_noise": "fake_static_noise_enabled",
    "reception_lines": "fake_reception_lines_enabled",
    "scan_lines": "fake_43_scanlines_enabled",
}


def _update_button_text(app_state):
    """Returns the Look for Updates button text for the current update state."""
    if not UPDATER_AND_SERVER_FEATURES_ENABLED:
        return "Updates Disabled"
    if app_state.get("_update_check_inflight", False):
        return "Checking..."
    if app_state.get("_update_ready", False):
        return "Install Update"
    if app_state.get("_update_available", False):
        return "Update Now"
    if app_state.get("_update_up_to_date", False):
        return "Up to Date"
    status = str(app_state.get("_update_status", "")).lower()
    if "not configured" in status:
        return "Updates Unconfigured"
    if "failed" in status:
        return "Retry Updates"
    return SYSTEM_ROW_META["update_check"]["label"]


def get_system_menu_rows(db, app_state):
    kiosk_on = db.config.get("kiosk_mode_enabled", False) if db is not None else False

    poweroff_visible = app_state.get("shell_takeover_active", False) if app_state is not None else False

    if kiosk_on:
        # WARNING: with Kiosk on the Channels tab is hidden, so its Settings options (and Look for Updates) are shown here instead; the toggles must stay in SYSTEM_KIOSK_SETTING_KEYS and the same two column groups as menu_nav.SYSTEM_LEFT_TOGGLES / SYSTEM_RIGHT_TOGGLES.
        kiosk_rows = ["kiosk_mode", "bezel_toggle", "warp_screen", "boot_anim",
                      "static_noise", "reception_lines", "scan_lines", "update_check"]
        if media.bezel_style_count() > 1:
            kiosk_rows.append("bezel_style_select")
        kiosk_rows.append("poweroff" if poweroff_visible else "exit_program")
        return kiosk_rows

    rows = ["game_ch", "guide", "controls_on_start", "kiosk_mode",
            "loudness_scan", "pixel_orbiter", "captions_toggle", "mono_stereo_toggle"]
    rows += ["advanced_settings", "reset_settings", "exit_program"]
    if poweroff_visible:
        rows.append("poweroff")
    return rows


def get_advanced_settings_menu_rows(db, app_state):
    """Return the row order for SYSTEM_ADVANCED_SUB_MENU: shortest label first, bars with a side switch last, the Server Menu button right after its row."""
    return ["update_check", "remote_remap", "performance_mode", "gpu_render_mode",
            "boot_on_start", "server_option", "server_menu_open"]


def get_channels_settings_menu_rows(db, app_state):
    """Return the row order for CHANNEL_SETTINGS_SUB_MENU."""
    rows = ["bezel_toggle"]
    if media.bezel_style_count() > 1:
        rows.append("bezel_style_select")
    rows.append("warp_screen")
    rows.append("boot_anim")
    rows.append("static_noise")
    rows.append("reception_lines")
    rows.append("scan_lines")
    return rows


def get_games_sub_menu_rows(db, app_state, console_key, is_on):
    if console_key == "MENU":
        return ["controller_map", "stick_deadzone", "scan_for_roms", "clear_roms", "scan_for_cores", "clear_cores", "clear_bios",
                "menu_bg_select_image", "multitap_toggle", "scroll_style", "text_color",
                "game_screen_mode"]

    if not is_on:
        return []

    if console_key == "DVD":
        splash_rows = ["splash_toggle", "splash_select_image", "splash_select_video"]
        return (splash_rows + ["dvd_captions_toggle", "dvd_resume_toggle"]
                + ["dvd_button_mapping", "dvd_screen_mode", "dvd_audio_track"])

    kiosk_on = db.config.get("kiosk_mode_enabled", False) if db is not None else False

    rows = []
    if not kiosk_on:
        rows.append("core_select")
    rows.append("controller_map")
    rows.extend(["profile1", "profile2", "profile3"])
    if console_key == "PSX":
        rows.append("disc")

    splash_rows = ["splash_toggle", "splash_select_image", "splash_select_video"]

    from game_deck import BIOS_REQUIRED_CONSOLES
    if console_key in BIOS_REQUIRED_CONSOLES:
        splash_rows.append("add_bios")

    splash_rows += ["relocate_roms_folder", "clear_console_roms"]

    return rows + splash_rows


# ============================================================================
# SECTION: Games tab: splash panel & asset preload
# ============================================================================


def _draw_console_splash_panel(menu_canvas, db, console_key, cx, content_w, top_y,
                                cell_h, y_gap, full_rows, sub_row, f_body, get_font_fn,
                                neon_green, text_white, off_green):
    import os

    if console_key == "DVD":
        splash_on   = db.config.get("dvd_splash_enabled", False) if db else False
        splash_path = db.config.get("dvd_splash_path", "") if db else ""
    else:
        splash_on   = db.config.get("console_splash_enabled", {}).get(console_key, False) if db else False
        splash_path = db.config.get("console_splash_path", {}).get(console_key, "") if db else ""
    splash_path = media.console_splash_file(console_key, splash_path)

    from game_deck import DVD_SPLASH_VIDEO_EXTS
    _current_kind = None
    if splash_path:
        _current_kind = "video" if os.path.splitext(splash_path)[1].lower() in DVD_SPLASH_VIDEO_EXTS else "image"

    right_col_w = content_w // 3
    right_margin = 40
    right_col_x = cx + content_w - right_col_w - right_margin

    splash_row_y = top_y
    is_splash_focused = (sub_row == full_rows.index("splash_toggle"))
    lbl_splash_txt = f_body.render("Splash Screen:", True, neon_green if is_splash_focused else text_white)
    menu_canvas.blit(lbl_splash_txt, (right_col_x, splash_row_y + 4))

    sw_x = right_col_x + 210
    sw_bg = off_green if splash_on else COLOR_MENU_SWITCH_OFF
    pygame.draw.rect(menu_canvas, sw_bg, (sw_x, splash_row_y, 55, 24), border_radius=12)
    if is_splash_focused:
        pygame.draw.rect(menu_canvas, neon_green, (sw_x, splash_row_y, 55, 24), 2, border_radius=12)
    sw_handle_x = sw_x + 42 if splash_on else sw_x + 12
    pygame.draw.circle(menu_canvas, text_white, (sw_handle_x, splash_row_y + 12), 9)
    if is_splash_focused:
        pygame.draw.circle(menu_canvas, neon_green, (sw_handle_x, splash_row_y + 12), 11, 2)

    has_pickers = ("splash_select_image" in full_rows or "splash_select_video" in full_rows)

    btn_row_y = splash_row_y + cell_h + y_gap
    btn_gap   = 14
    btn_w     = (right_col_w - btn_gap) // 2

    vid_x = right_col_x
    img_x = right_col_x + btn_w + btn_gap

    _dim_bg  = COLOR_MENU_BUTTON_GREYED
    _dim_txt = COLOR_MENU_TEXT_GREYED

    if has_pickers:
        is_vid_focused = (sub_row == full_rows.index("splash_select_video"))
        _vid_bg = COLOR_MENU_BUTTON if splash_on else _dim_bg
        pygame.draw.rect(menu_canvas, _vid_bg, (vid_x, btn_row_y, btn_w, cell_h), border_radius=6)
        if is_vid_focused:
            pygame.draw.rect(menu_canvas, neon_green, (vid_x, btn_row_y, btn_w, cell_h), 2, border_radius=6)
        _vid_txt_col = neon_green if (is_vid_focused and splash_on) else (text_white if splash_on else _dim_txt)
        lbl_vid = f_body.render("Video", True, _vid_txt_col)
        menu_canvas.blit(lbl_vid, (vid_x + btn_w // 2 - lbl_vid.get_width() // 2,
                                    btn_row_y + cell_h // 2 - lbl_vid.get_height() // 2))

        is_img_focused = (sub_row == full_rows.index("splash_select_image"))
        _img_bg = COLOR_MENU_BUTTON if splash_on else _dim_bg
        pygame.draw.rect(menu_canvas, _img_bg, (img_x, btn_row_y, btn_w, cell_h), border_radius=6)
        if is_img_focused:
            pygame.draw.rect(menu_canvas, neon_green, (img_x, btn_row_y, btn_w, cell_h), 2, border_radius=6)
        _img_txt_col = neon_green if (is_img_focused and splash_on) else (text_white if splash_on else _dim_txt)
        lbl_img = f_body.render("Image", True, _img_txt_col)
        menu_canvas.blit(lbl_img, (img_x + btn_w // 2 - lbl_img.get_width() // 2,
                                    btn_row_y + cell_h // 2 - lbl_img.get_height() // 2))

        if _current_kind is not None:
            f_fname = get_font_fn("Courier New", 12, bold=False)
            _fname_col = COLOR_MENU_TEXT_DISABLED_LIGHT if splash_on else COLOR_MENU_SPLASH_FILE_OFF
            fname_lbl = f_fname.render(os.path.basename(splash_path), True, _fname_col)
            cap_x = vid_x if _current_kind == "video" else img_x
            menu_canvas.blit(fname_lbl, (cap_x, btn_row_y + cell_h + 4))

    bios_row_y = (btn_row_y + cell_h + y_gap) if has_pickers else btn_row_y
    if "add_bios" in full_rows:
        is_bios_focused = (sub_row == full_rows.index("add_bios"))
        pygame.draw.rect(menu_canvas, COLOR_BUTTON_ADD, (right_col_x, bios_row_y, right_col_w, cell_h), border_radius=6)
        if is_bios_focused:
            pygame.draw.rect(menu_canvas, neon_green, (right_col_x, bios_row_y, right_col_w, cell_h), 2, border_radius=6)
        lbl_bios = f_body.render("Add Bios", True, neon_green if is_bios_focused else text_white)
        menu_canvas.blit(lbl_bios, (right_col_x + right_col_w // 2 - lbl_bios.get_width() // 2,
                                     bios_row_y + cell_h // 2 - lbl_bios.get_height() // 2))
        bios_row_y += cell_h + y_gap

    if "relocate_roms_folder" in full_rows:
        reloc_row_y = bios_row_y
        is_reloc_focused = (sub_row == full_rows.index("relocate_roms_folder"))
        pygame.draw.rect(menu_canvas, COLOR_BUTTON_ADD, (right_col_x, reloc_row_y, right_col_w, cell_h), border_radius=6)
        if is_reloc_focused:
            pygame.draw.rect(menu_canvas, neon_green, (right_col_x, reloc_row_y, right_col_w, cell_h), 2, border_radius=6)
        lbl_reloc = f_body.render("Add/Remove Roms", True, neon_green if is_reloc_focused else text_white)
        menu_canvas.blit(lbl_reloc, (right_col_x + right_col_w // 2 - lbl_reloc.get_width() // 2,
                                      reloc_row_y + cell_h // 2 - lbl_reloc.get_height() // 2))

        import sys as _sys
        _main = _sys.modules.get("__main__")
        _gd   = getattr(_main, "game_deck", None) if _main else None
        _manual_n = len(_gd.console_roms_manual.get(console_key, [])) if _gd is not None else 0

        if "clear_console_roms" in full_rows:
            clear_row_y = reloc_row_y + cell_h + y_gap
            is_clear_focused = (sub_row == full_rows.index("clear_console_roms"))
            if _manual_n:
                _clear_bg  = COLOR_BUTTON_CLEAR
                _clear_txt = neon_green if is_clear_focused else COLOR_TEXT_WHITE
            else:
                _clear_bg  = COLOR_MENU_BUTTON_GREYED
                _clear_txt = COLOR_MENU_TEXT_GREYED
            pygame.draw.rect(menu_canvas, _clear_bg, (right_col_x, clear_row_y, right_col_w, cell_h), border_radius=6)
            if is_clear_focused:
                pygame.draw.rect(menu_canvas, neon_green, (right_col_x, clear_row_y, right_col_w, cell_h), 2, border_radius=6)
            lbl_clear = f_body.render("Clear Roms", True, _clear_txt)
            menu_canvas.blit(lbl_clear, (right_col_x + right_col_w // 2 - lbl_clear.get_width() // 2,
                                          clear_row_y + cell_h // 2 - lbl_clear.get_height() // 2))


_games_tab_logo_raw_preload = {}
_games_tab_logo_raw_preload_lock = threading.Lock()


def preload_games_tab_assets(app_state):
    if app_state.get("_games_tab_preloaded"):
        return
    app_state["_games_tab_preloaded"] = True

    import os
    from game_deck import CONSOLE_ORDER

    logos_dir = os.path.join(media.resolve_main_dir(), "images", "logos")
    _menu_logo_raw_cache = app_state.setdefault("_menu_logo_raw_cache", {})

    for console_key in CONSOLE_ORDER:
        if console_key in _menu_logo_raw_cache:
            continue
        with _games_tab_logo_raw_preload_lock:
            preloaded = _games_tab_logo_raw_preload.pop(console_key, None)
        if preloaded is not None:
            try:
                _menu_logo_raw_cache[console_key] = preloaded.convert_alpha()
            except Exception as _swallowed_error:
                _log_swallowed("preload_games_tab_assets: _menu_logo_raw_cache[console_key] = preloaded.convert_alpha()", _swallowed_error)
                _menu_logo_raw_cache[console_key] = None
            continue
        logo_path = os.path.join(logos_dir, f"{console_key}.png")
        try:
            logo_raw = (
                pygame.image.load(logo_path).convert_alpha()
                if os.path.exists(logo_path) else None
            )
            _menu_logo_raw_cache[console_key] = _crop_logo_to_artwork(logo_raw)
        except Exception as _swallowed_error:
            _log_swallowed("preload_games_tab_assets: logo_raw = pygame.image.load(logo_path).convert_alpha() if os.path....", _swallowed_error)
            _menu_logo_raw_cache[console_key] = None


# ============================================================================
# SECTION: Loading screen & controls splash
# ============================================================================


def _get_font(name, size, bold=False):
    """Return a cached SysFont. Creates it only on first call per (name,size,bold)."""
    key = (name, size, bold)
    if key not in _font_cache:
        _font_cache[key] = pygame.font.SysFont(name, size, bold=bold)
    return _font_cache[key]

def render_loading_screen(surface, progress, theme):
    surface.fill(theme.get("bg", _DEFAULT_THEME["bg"]))


# WARNING: the fallback numbers below must match CONTROLS_ROW_KEY_GAP_PX, CONTROLS_COL_KEY_GAP_PX, CONTROLS_COL_GAP_PX, CONTROLS_SECTION_EXTRA_PX in performance_governor.py; they are used only when that module fails to import.
CONTROLS_ROW_KEY_GAP_PX = performance_governor.CONTROLS_ROW_KEY_GAP_PX if performance_governor is not None else 48
CONTROLS_COL_KEY_GAP_PX = performance_governor.CONTROLS_COL_KEY_GAP_PX if performance_governor is not None else 40
CONTROLS_COL_GAP_PX = performance_governor.CONTROLS_COL_GAP_PX if performance_governor is not None else 90
CONTROLS_SECTION_EXTRA_PX = performance_governor.CONTROLS_SECTION_EXTRA_PX if performance_governor is not None else 22


# WARNING: one title per page of the Controls splash, in page order; the main script's page keys stop at the last of them, and render_controls_splash pairs each title with its rows.
# WARNING: the fallback numbers below must match the CONTROLS_KEY_ values in performance_governor.py; they are used only when that module fails to import.
CONTROLS_KEY_H_PX = performance_governor.CONTROLS_KEY_H_PX if performance_governor is not None else 54
CONTROLS_KEY_GAP_PX = performance_governor.CONTROLS_KEY_GAP_PX if performance_governor is not None else 10
CONTROLS_KEY_ROW_GAP_PX = performance_governor.CONTROLS_KEY_ROW_GAP_PX if performance_governor is not None else 14
CONTROLS_FOOTER_KEY_H_PX = performance_governor.CONTROLS_FOOTER_KEY_H_PX if performance_governor is not None else 30
CONTROLS_KEY_ALPHA_CUTOFF = performance_governor.CONTROLS_KEY_ALPHA_CUTOFF if performance_governor is not None else 200
CONTROLS_LONG_KEY_MIN_RATIO = performance_governor.CONTROLS_LONG_KEY_MIN_RATIO if performance_governor is not None else 1.25
CONTROLS_HINT_KEY_SCALE = performance_governor.CONTROLS_HINT_KEY_SCALE if performance_governor is not None else 1.45
CONTROLS_PAD_LONG_BUTTON_SCALE = performance_governor.CONTROLS_PAD_LONG_BUTTON_SCALE if performance_governor is not None else 0.62
CONTROLS_HINT_KEY_WORD_GAP_PX = performance_governor.CONTROLS_HINT_KEY_WORD_GAP_PX if performance_governor is not None else 8
CONTROLS_HINT_GROUP_GAP_PX = performance_governor.CONTROLS_HINT_GROUP_GAP_PX if performance_governor is not None else 26

# ============================================================================
# SECTION: Controls splash key pictures
# ============================================================================

CONTROL_KEY_FILES = {
    "up": "key_up.png", "down": "key_down.png", "left": "key_left.png", "right": "key_right.png",
    "w": "key_w.png", "a": "key_a.png", "s": "key_s.png", "d": "key_d.png",
    "c": "key_c.png", "m": "key_m.png", "n": "key_n.png",
    "enter": "key_enter.png", "esc": "key_esc.png", "backspace": "key_backspace.png",
    "play": "key_play.png", "rewind": "key_rewind.png", "fastforward": "key_fastforward.png",
    "pad_dpad": "pad_dpad.png", "pad_up": "pad_up.png", "pad_down": "pad_down.png", "pad_left": "pad_left.png",
    "pad_right": "pad_right.png", "pad_a": "pad_a.png", "pad_b": "pad_b.png", "pad_x": "pad_x.png", "pad_y": "pad_y.png",
    "pad_select": "pad_select.png", "pad_start": "pad_start.png",
}
CONTROL_KEY_TEXT = {
    "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT", "w": "W", "a": "A", "s": "S", "d": "D",
    "c": "C", "m": "M", "n": "N", "enter": "ENTER", "esc": "ESC", "backspace": "BACKSPACE",
    "play": "PLAY", "rewind": "REWIND", "fastforward": "FAST FORWARD",
    "pad_dpad": "D-PAD", "pad_up": "UP", "pad_down": "DOWN", "pad_left": "LEFT", "pad_right": "RIGHT",
    "pad_a": "A", "pad_b": "B", "pad_x": "X", "pad_y": "Y", "pad_select": "SELECT", "pad_start": "START",
}
_control_key_raw = {}
_control_key_scaled = {}


def _control_key_trimmed(name):
    """The key picture `name` from main/images/controls, trimmed to the key's own outline (its faint glow and empty border cut away); None when it is missing or unreadable, which is logged once."""
    if name in _control_key_raw:
        return _control_key_raw[name]
    path = os.path.join(media.resolve_main_dir(), "images", "controls", CONTROL_KEY_FILES[name])
    picture = None
    try:
        loaded = pygame.image.load(path).convert_alpha()
        rects = pygame.mask.from_surface(loaded, CONTROLS_KEY_ALPHA_CUTOFF).get_bounding_rects()
        if rects:
            bound = rects[0].unionall(rects[1:])
            picture = loaded.subsurface(bound).copy()
        else:
            log.warning("[CONTROLS] key picture %s has no solid pixels, the key is shown as text", path)
    except Exception as e:
        log.warning("[CONTROLS] key picture %s could not be loaded, the key is shown as text: %s", path, e)
    _control_key_raw[name] = picture
    return picture


def control_key_picture(name, height):
    """The key picture `name` at `height` pixels, whatever size the file is: every square key is exactly height by height, and a long key (ENTER, ESC, BACKSPACE) is the same height and as wide as its own shape, so every key looks like it comes off one keyboard; None when the picture is missing."""
    cache_key = (name, int(height))
    if cache_key in _control_key_scaled:
        return _control_key_scaled[cache_key]
    trimmed = _control_key_trimmed(name)
    scaled = None
    if trimmed is not None and trimmed.get_height() > 0:
        ratio = trimmed.get_width() / float(trimmed.get_height())
        if ratio >= CONTROLS_LONG_KEY_MIN_RATIO and name.startswith("pad_"):
            # WARNING: a controller's long buttons (SELECT, START) drawn as tall as a key look far bigger than the round buttons beside them; they are drawn smaller and centred in the key's height so rows still line up.
            pill_h = max(1, int(round(height * CONTROLS_PAD_LONG_BUTTON_SCALE)))
            inner = pygame.transform.smoothscale(trimmed, (max(1, int(round(pill_h * ratio))), pill_h))
            scaled = pygame.Surface((inner.get_width(), int(height)), pygame.SRCALPHA)
            scaled.blit(inner, (0, (int(height) - pill_h) // 2))
        elif ratio >= CONTROLS_LONG_KEY_MIN_RATIO:
            scaled = pygame.transform.smoothscale(trimmed, (max(int(height), int(round(height * ratio))), int(height)))
        elif name.startswith("pad_"):
            # WARNING: controller buttons are round or arrow shaped; they keep their own shape inside the square instead of being squeezed to it.
            fit = float(height) / max(trimmed.get_width(), trimmed.get_height())
            inner = pygame.transform.smoothscale(trimmed, (max(1, int(round(trimmed.get_width() * fit))), max(1, int(round(trimmed.get_height() * fit)))))
            scaled = pygame.Surface((int(height), int(height)), pygame.SRCALPHA)
            scaled.blit(inner, ((int(height) - inner.get_width()) // 2, (int(height) - inner.get_height()) // 2))
        else:
            scaled = pygame.transform.smoothscale(trimmed, (int(height), int(height)))
        # WARNING: pure black (0, 0, 0) is the game overlay's see-through colour (see the top of this script); the keys' black faces are lifted to (1, 1, 1) after scaling, or they show as holes over a game.
        rgb = pygame.surfarray.pixels3d(scaled)
        rgb[(rgb[:, :, 0] == 0) & (rgb[:, :, 1] == 0) & (rgb[:, :, 2] == 0)] = 1
        del rgb
    _control_key_scaled[cache_key] = scaled
    return scaled


def control_keys_size(keys, height, gap):
    """(width, height) of a group of key pictures drawn together, or None when one of them is missing; "wasd" is the W key over S with A and D at its sides."""
    if keys == ("wasd",):
        if any(control_key_picture(k, height) is None for k in ("w", "a", "s", "d")):
            return None
        return 3 * height + 2 * gap, 2 * height + gap
    pictures = [control_key_picture(k, height) for k in keys]
    if any(p is None for p in pictures):
        return None
    return sum(p.get_width() for p in pictures) + gap * (len(pictures) - 1), height


def draw_control_keys(target, keys, x, y, height, gap):
    """Draws a group of key pictures with its top-left at (x, y); returns its (width, height)."""
    if keys == ("wasd",):
        step = height + gap
        for name, dx, dy in (("w", step, 0), ("a", 0, step), ("s", step, step), ("d", 2 * step, step)):
            target.blit(control_key_picture(name, height), (x + dx, y + dy))
        return 3 * height + 2 * gap, 2 * height + gap
    cx = x
    for name in keys:
        picture = control_key_picture(name, height)
        target.blit(picture, (cx, y))
        cx += picture.get_width() + gap
    return cx - gap - x, height


def key_hint_size(parts, font, key_h, gap=None):
    """(width, height) of a hint line (see draw_key_hint) without drawing it."""
    return draw_key_hint(None, parts, 0, 0, font, (0, 0, 0), key_h, gap)


def draw_key_hint(target, parts, x, center_y, font, color, key_h=None, gap=None, center=False):
    """Draws a hint line: parts is a list of key groups (tuples of key picture names, drawn side by side), words (strings) and joining words (a list, such as ["OR"], kept close on both sides), e.g. [("a", "d"), "TO NAVIGATE", ("enter",), "TO SELECT"]; a key whose picture is missing is written as its name instead. The line is centred up and down on center_y, and side to side on x when center is True; returns its (width, height). With target None it only measures."""
    key_h = int(key_h or round(font.get_height() * CONTROLS_HINT_KEY_SCALE))
    gap = max(2, key_h // 8) if gap is None else gap
    pieces = []
    after_keys = False
    for n, part in enumerate(parts):
        nxt = parts[n + 1] if n + 1 < len(parts) else None
        if isinstance(part, tuple):
            for i, name in enumerate(part):
                picture = control_key_picture(name, key_h)
                if picture is None:
                    picture = font.render("[%s]" % CONTROL_KEY_TEXT.get(name, name.upper()), True, color)
                last = i == len(part) - 1
                pieces.append((picture, gap if not last else (CONTROLS_HINT_GROUP_GAP_PX if isinstance(nxt, tuple) else CONTROLS_HINT_KEY_WORD_GAP_PX)))
            after_keys = True
        elif isinstance(part, list):
            pieces.append((font.render(" ".join(part), True, color), CONTROLS_HINT_KEY_WORD_GAP_PX))
            after_keys = False
        else:
            pieces.append((font.render(part, True, color), CONTROLS_HINT_GROUP_GAP_PX if after_keys else CONTROLS_HINT_KEY_WORD_GAP_PX))
            after_keys = False
    if not pieces:
        return 0, 0
    width = sum(p.get_width() for p, _g in pieces) + sum(g for _p, g in pieces[:-1])
    height = max(p.get_height() for p, _g in pieces)
    if target is not None:
        cx = x - width // 2 if center else x
        for picture, after in pieces:
            target.blit(picture, (cx, center_y - picture.get_height() // 2))
            cx += picture.get_width() + after
    return width, height


def draw_key_hint_fitted(target, parts, x, center_y, font_name, font_px, color, max_w, center=True, min_px=11):
    """draw_key_hint with the largest font (and keys to match) from font_px down to min_px whose line fits max_w; returns its (width, height)."""
    size = font_px
    while True:
        font = _get_font(font_name, size, bold=True)
        if key_hint_size(parts, font, None)[0] <= max_w or size <= min_px:
            return draw_key_hint(target, parts, x, center_y, font, color, center=center)
        size -= 1


CONTROLS_SPLASH_TITLES = ("TV CONTROLS", "TV GUIDE", "MENU NAVIGATION", "GAME CHANNEL — KEYBOARD", "GAME CHANNEL — CONTROLLER",
                          "DVD PLAYER — KEYBOARD", "DVD PLAYER — CONTROLLER")
NUM_CONTROL_PAGES = len(CONTROLS_SPLASH_TITLES)


def render_controls_splash(surface, theme, db=None, app_state=None):
    import colorsys

    CANVAS_W, CANVAS_H = 1036, 680
    real_w, real_h = surface.get_size()
    canvas = pygame.Surface((CANVAS_W, CANVAS_H), pygame.SRCALPHA)

    bg_hue = db.config.get("theme_bg_hue", 220) / 360.0 if db is not None else 220/360.0
    ui_hue = db.config.get("theme_ui_hue", 140) / 360.0 if db is not None else 140/360.0
    r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.9, 1.0)
    NEON_GREEN    = (int(r*255), int(g*255), int(b*255))
    r, g, b = colorsys.hsv_to_rgb(bg_hue, 0.6, 0.15)
    MIDNIGHT_NAVY = (int(r*255), int(g*255), int(b*255))
    TEXT_WHITE    = COLOR_TEXT_WHITE
    ARCADE_GOLD   = COLOR_ARCADE_GOLD
    DIM_GREY      = COLOR_MENU_DIM_GREY

    slider_val   = db.config.get("menu_opacity", 50) if db is not None else 50
    menu_opacity = int((slider_val / 100.0) * 255)

    bg_surf = pygame.Surface((CANVAS_W, CANVAS_H), pygame.SRCALPHA)
    bg_surf.fill(MIDNIGHT_NAVY + (menu_opacity,))
    canvas.blit(bg_surf, (0, 0))
    pygame.draw.rect(canvas, NEON_GREEN, (0, 0, CANVAS_W, CANVAS_H), 3)

    f_title  = _get_font("Courier New", 44, bold=True)
    f_sect   = _get_font("Courier New", 32, bold=True)
    f_footer = _get_font("Courier New", 19, bold=True)

    if app_state is None:
        app_state = {}
    page  = app_state.get("controls_splash_page",  0)
    focus = app_state.get("controls_splash_focus", 1)

    tv_controls = [
        (("up", "down"),    "UP / DOWN",    "CHANNEL UP / CHANNEL DOWN"),
        (("left", "right"), "LEFT / RIGHT", "VOLUME DOWN / VOLUME UP"),
        (("n",),            "N",            "MUTE / UNMUTE"),
        (("m",),            "M",            "OPEN / CLOSE MAIN MENU"),
        (("c",),            "C",            "OPEN / CLOSE CONTROLS MENU"),
        (("esc", "enter"),  "ESC, ENTER",   "MINIMIZE / FULLSCREEN  (ESC, THEN ENTER)"),
        (None,              "NUMBER KEYS",  "TUNE STRAIGHT TO A CHANNEL"),
    ]
    guide_controls = [
        (("w", "s"), "W / S", "SCROLL CHANNELS UP / DOWN"),
        (("a", "d"), "A / D", "SCROLL TIME SLOTS LEFT / RIGHT"),
        (("enter",), "ENTER", "TUNE TO SELECTED CHANNEL"),
    ]
    menu_controls = [
        (("w", "s"), "W / S",  "NAVIGATE UP / DOWN"),
        (("a", "d"), "A / D",  "NAVIGATE LEFT / RIGHT"),
        (("enter",), "ENTER",  "SELECT / CONFIRM"),
        (("esc",),   "ESCAPE", "BACK / QUIT"),
        (None,       "MOUSE",  "FILE EXPLORER  (MOUSE ONLY)"),
    ]
    game_controls_kb = [
        (("wasd",),  "W / A / S / D", "NAVIGATE MENUS"),
        (("enter",), "ENTER",         "SELECT / CONFIRM"),
        (("esc",),   "ESCAPE",        "BACK / QUIT GAME"),
        (None,       "LAUNCHED GAMES", "PLAYING  (CONTROLLER ONLY)"),
    ]
    game_controls_pad = [
        (("pad_dpad",),               "D-PAD",          "NAVIGATE MENUS"),
        (("pad_a",),                  "A BUTTON",       "SELECT / CONFIRM"),
        (("pad_b",),                  "B BUTTON",       "BACK"),
        (("pad_start", "pad_select"), "START / SELECT", "QUIT GAME "),
    ]
    dvd_controls_kb = [
        (("play",),        ("ICON", "play"), "PLAY / PAUSE"),
        (("rewind",),      ("ICON", "rew"),  "TAP TO SKIP BACK  •  HOLD TO SEEK"),
        (("fastforward",), ("ICON", "ff"),   "TAP TO SKIP AHEAD  •  HOLD TO SEEK"),
        (("backspace",),   "BACKSPACE",      "RETURN TO MAIN MENU  (DVD OPTION)"),
        (("esc",),         "ESCAPE",         "QUIT DVD"),
        (("wasd",),        "W / A / S / D",  "NAVIGATION  (DISC MENUS, YES / NO)"),
        (("enter",),       "ENTER",          "CONFIRM YES / NO SELECTION"),
    ]
    dvd_controls_pad = [
        (("pad_down",),               "DOWN",           "PLAY / PAUSE"),
        (("pad_left",),               "LEFT",           "TAP TO SKIP BACK  •  HOLD TO SEEK"),
        (("pad_right",),              "RIGHT",          "TAP TO SKIP AHEAD  •  HOLD TO SEEK"),
        (("pad_dpad",),               "D-PAD",          "NAVIGATION  (DISC MENUS, YES / NO)"),
        (("pad_y",),                  "Y BUTTON",       "RETURN TO MAIN MENU  (DVD OPTION)"),
        (("pad_a",),                  "A BUTTON",       "SELECT"),
        (("pad_b",),                  "B BUTTON",       "BACK"),
        (("pad_start", "pad_select"), "START / SELECT", "QUIT DVD"),
    ]

    PAGES = [{"title": title, "rows": rows} for title, rows in zip(
        CONTROLS_SPLASH_TITLES,
        (tv_controls, guide_controls, menu_controls, game_controls_kb, game_controls_pad, dvd_controls_kb, dvd_controls_pad))]
    page = max(0, min(page, NUM_CONTROL_PAGES - 1))
    page_data = PAGES[page]

    margin  = 40
    ROW_KEY_GAP  = CONTROLS_ROW_KEY_GAP_PX
    COL_KEY_GAP  = CONTROLS_COL_KEY_GAP_PX
    COL_GAP      = CONTROLS_COL_GAP_PX

    BODY_SIZE = CONTROLS_BODY_FONT_PX
    f_body = _get_font("Courier New", BODY_SIZE, bold=True)
    f_sub  = _get_font("Courier New", BODY_SIZE + 9, bold=True)
    ROW_STEP = f_body.get_height() + 16
    SECT_H   = f_sect.get_height() + CONTROLS_SECTION_EXTRA_PX

    def _is_icon_key(k):
        return isinstance(k, tuple) and k[0] == "ICON"

    ICON_KEY_W = CONTROLS_ICON_KEY_W_PX

    def draw_icon(kind, x, y, h, color):
        cy = y + h // 2
        s = h * 0.34
        if kind == "play":
            pygame.draw.polygon(canvas, color, [
                (x, cy - s), (x, cy + s), (x + s * 1.7, cy),
            ])
        elif kind == "rew":
            pygame.draw.polygon(canvas, color, [
                (x + s * 1.1, cy - s), (x + s * 1.1, cy + s), (x, cy),
            ])
            pygame.draw.polygon(canvas, color, [
                (x + s * 2.6, cy - s), (x + s * 2.6, cy + s), (x + s * 1.5, cy),
            ])
        elif kind == "ff":
            pygame.draw.polygon(canvas, color, [
                (x, cy - s), (x, cy + s), (x + s * 1.1, cy),
            ])
            pygame.draw.polygon(canvas, color, [
                (x + s * 1.5, cy - s), (x + s * 1.5, cy + s), (x + s * 2.6, cy),
            ])

    def measure_rows(rows):
        key_w  = max(ICON_KEY_W if _is_icon_key(k) else f_body.size(k)[0] for k, _ in rows)
        desc_w = max(f_body.size(d)[0] for _, d in rows)
        return key_w, desc_w

    KEY_GAP = CONTROLS_KEY_GAP_PX

    def row_key_size(row, key_h):
        """(width, height) of a row's keys: its key pictures, or its text label when a picture is missing or the row has none."""
        keys, text_key, _desc = row
        size = control_keys_size(keys, key_h, KEY_GAP) if keys else None
        if size is not None:
            return size
        if _is_icon_key(text_key):
            return ICON_KEY_W, f_body.get_height()
        return f_body.size(text_key)[0], f_body.get_height()

    def rows_layout(rows, room_h):
        """The key height for a page and each row's key size, the key height made smaller only when the rows with at least CONTROLS_KEY_ROW_GAP_PX above, between and below them would not fit the room."""
        key_h = CONTROLS_KEY_H_PX
        while True:
            sizes = [row_key_size(row, key_h) for row in rows]
            total = sum(max(h, key_h) for _w, h in sizes)
            if total + CONTROLS_KEY_ROW_GAP_PX * (len(rows) + 1) <= room_h or key_h <= f_body.get_height():
                return key_h, sizes, total
            key_h -= 2

    def draw_rows_block(rows, top_y, room_h, gap=ROW_KEY_GAP):
        key_h, sizes, total = rows_layout(rows, room_h)
        key_w = max(w for w, _h in sizes)
        desc_w = max(f_body.size(d)[0] for _k, _t, d in rows)
        key_x = CANVAS_W // 2 - (key_w + gap + desc_w) // 2
        desc_x = key_x + key_w + gap
        # WARNING: the rows are spread evenly: the same space above the first row, between every two rows and below the last (owner 2026-11-04 03:53).
        even_gap = max(0.0, (room_h - total) / float(len(rows) + 1))
        yy = top_y + even_gap
        for (keys, text_key, desc_lbl), (w, h) in zip(rows, sizes):
            row_h = max(h, key_h)
            kx = key_x + (key_w - w) // 2
            ky = int(round(yy)) + (row_h - h) // 2
            if keys and control_keys_size(keys, key_h, KEY_GAP) is not None:
                draw_control_keys(canvas, keys, kx, ky, key_h, KEY_GAP)
            elif _is_icon_key(text_key):
                draw_icon(text_key[1], kx, ky, f_body.get_height(), ARCADE_GOLD)
            else:
                canvas.blit(f_body.render(text_key, True, ARCADE_GOLD), (kx, ky))
            canvas.blit(f_body.render(desc_lbl, True, TEXT_WHITE), (desc_x, int(round(yy)) + (row_h - f_body.get_height()) // 2))
            yy += row_h + even_gap
        return total

    def draw_sect_title_centered(text, y):
        lbl = f_sect.render(text, True, TEXT_WHITE)
        canvas.blit(lbl, (CANVAS_W // 2 - lbl.get_width() // 2, y))
        uy = y + lbl.get_height() + 8
        uw = max(lbl.get_width() + 60, 260)
        ux = CANVAS_W // 2 - uw // 2
        pygame.draw.line(canvas, TEXT_WHITE, (ux, uy), (ux + uw, uy), 1)
        return SECT_H

    def draw_footer(page_num):
        footer_y = CANVAS_H - 52
        pygame.draw.line(canvas, NEON_GREEN, (40, footer_y - 8), (CANVAS_W - 40, footer_y - 8), 1)
        draw_key_hint(canvas, [("a", "d"), "TO NAVIGATE", ("enter",), "TO SELECT", ("c",), ["OR"], ("esc",), "TO QUIT"], margin,
                      footer_y + 6 + f_footer.get_height() // 2, f_footer, TEXT_WHITE, key_h=CONTROLS_FOOTER_KEY_H_PX)

        if page_num == 0:
            lbl   = f_footer.render("  \u25ba  ", True, MIDNIGHT_NAVY)
            btn_w = lbl.get_width() + 8
            btn_h = lbl.get_height() + 4
            bx    = CANVAS_W - margin - btn_w
            by    = footer_y + 4
            _aa_rounded_rect(canvas, NEON_GREEN, (bx - 4, by, btn_w + 4, btn_h), border_radius=4)
            canvas.blit(lbl, (bx, by + 2))
        elif page_num == NUM_CONTROL_PAGES - 1:
            left_focused = (focus == 0)
            cl  = f_footer.render("  CLOSE  ", True, MIDNIGHT_NAVY if not left_focused else TEXT_WHITE)
            cw  = cl.get_width() + 8
            ch  = cl.get_height() + 4
            cx_ = CANVAS_W - margin - cw
            cy_ = footer_y + 4
            pygame.draw.rect(canvas, NEON_GREEN if not left_focused else DIM_GREY,
                             (cx_ - 4, cy_, cw + 4, ch), border_radius=4)
            canvas.blit(cl, (cx_, cy_ + 2))
            ll  = f_footer.render("  \u25c4  ", True, MIDNIGHT_NAVY if left_focused else TEXT_WHITE)
            lw  = ll.get_width() + 8
            lh  = ll.get_height() + 4
            lx_ = cx_ - lw - 12
            ly_ = footer_y + 4
            pygame.draw.rect(canvas, NEON_GREEN if left_focused else DIM_GREY,
                             (lx_ - 4, ly_, lw + 4, lh), border_radius=4)
            canvas.blit(ll, (lx_, ly_ + 2))
        else:
            left_focused  = (focus == 0)
            right_focused = not left_focused
            rl  = f_footer.render("  \u25ba  ", True, MIDNIGHT_NAVY if right_focused else TEXT_WHITE)
            rw  = rl.get_width() + 8
            rh  = rl.get_height() + 4
            rx_ = CANVAS_W - margin - rw
            ry_ = footer_y + 4
            pygame.draw.rect(canvas, NEON_GREEN if right_focused else DIM_GREY,
                             (rx_ - 4, ry_, rw + 4, rh), border_radius=4)
            canvas.blit(rl, (rx_, ry_ + 2))
            ll  = f_footer.render("  \u25c4  ", True, MIDNIGHT_NAVY if left_focused else TEXT_WHITE)
            lw  = ll.get_width() + 8
            lh  = ll.get_height() + 4
            lx_ = rx_ - lw - 12
            ly_ = footer_y + 4
            pygame.draw.rect(canvas, NEON_GREEN if left_focused else DIM_GREY,
                             (lx_ - 4, ly_, lw + 4, lh), border_radius=4)
            canvas.blit(ll, (lx_, ly_ + 2))

    title_lbl = f_title.render("CONTROLS MENU", True, ARCADE_GOLD)
    canvas.blit(title_lbl, (CANVAS_W//2 - title_lbl.get_width()//2, 15))
    title_bottom = 15 + title_lbl.get_height() + 4
    pygame.draw.line(canvas, ARCADE_GOLD, (40, title_bottom), (CANVAS_W - 40, title_bottom), 1)

    GAP_TITLE_SECTION = CONTROLS_TITLE_SECTION_GAP_PX
    SECTION_TITLE_Y = title_bottom + GAP_TITLE_SECTION

    GAP_SECTION_BODY = CONTROLS_SECTION_BODY_GAP_PX
    GAP_SUB_BODY = CONTROLS_SUB_BODY_GAP_PX

    if "rows" in page_data:
        rows = page_data["rows"]
        draw_sect_title_centered(page_data["title"], SECTION_TITLE_Y)
        # WARNING: these two lines must match the underline drawn by draw_sect_title_centered and the footer line drawn by draw_footer; the rows are centred between them.
        body_top = SECTION_TITLE_Y + f_sect.get_height() + 9
        body_bottom = CANVAS_H - 60
        draw_rows_block(rows, body_top, body_bottom - body_top)

    else:
        cols = page_data["cols"]
        p_col_gap     = page_data.get("col_gap", COL_GAP)
        p_col_key_gap = page_data.get("col_key_gap", COL_KEY_GAP)
        col_metrics = []
        for label, rows in cols:
            key_w, desc_w = measure_rows(rows)
            sub_lbl_w = f_sub.size(label)[0]
            col_w = max(key_w + p_col_key_gap + desc_w, sub_lbl_w)
            col_metrics.append((label, rows, key_w, col_w))
        total_cols_w = sum(cw for *_, cw in col_metrics) + p_col_gap * (len(col_metrics) - 1)

        sub_h = f_sub.get_height() + 14 + GAP_SUB_BODY

        th = draw_sect_title_centered(page_data["title"], SECTION_TITLE_Y)
        sub_y = SECTION_TITLE_Y + th + GAP_SECTION_BODY
        cx = CANVAS_W // 2 - total_cols_w // 2
        for label, rows, key_w, col_w in col_metrics:
            sub_lbl = f_sub.render(label, True, ARCADE_GOLD)
            canvas.blit(sub_lbl, (cx + col_w // 2 - sub_lbl.get_width() // 2, sub_y))
            yy = sub_y + sub_h
            for key_lbl, desc_lbl in rows:
                canvas.blit(f_body.render(key_lbl,  True, ARCADE_GOLD), (cx, yy))
                canvas.blit(f_body.render(desc_lbl, True, TEXT_WHITE),  (cx + key_w + p_col_key_gap, yy))
                yy += ROW_STEP
            cx += col_w + p_col_gap

    draw_footer(page)

    dest_x, dest_y, dest_w, dest_h = menu_panel_rect(real_w, real_h, db)

    if dest_w == CANVAS_W and dest_h == CANVAS_H:
        surface.blit(canvas, (dest_x, dest_y))
    else:
        scaled = pygame.transform.smoothscale(canvas, (dest_w, dest_h))
        surface.blit(scaled, (dest_x, dest_y))


# ============================================================================
# SECTION: TV guide: frame cache, locking & async rendering
# ============================================================================


_guide_frame_cache = {"key": None, "canvas": None, "preview_bounds": None, "clock_box": None, "serial": 0}
_guide_scaled_cache = {"key": None, "surf": None}

# WARNING: the fallback numbers below must match GUIDE_ROW_CACHE_TTL_S, GUIDE_ROW_CACHE_MAX in performance_governor.py; they are used only when that module fails to import.
_GUIDE_ROW_CACHE_TTL_S = performance_governor.GUIDE_ROW_CACHE_TTL_S if performance_governor is not None else 20.0
_GUIDE_ROW_CACHE_MAX = performance_governor.GUIDE_ROW_CACHE_MAX if performance_governor is not None else 24
_guide_row_cache = {}
_guide_row_cache_state = {"gen": 0}


class _TrackedLock:
    """threading.Lock wrapper that records its holder and when it took the lock."""
    def __init__(self, name):
        self._lock = threading.Lock()
        self._name = name
        self._holder = None
        self._held_since = None

    def __enter__(self):
        self._lock.acquire()
        self._holder = threading.current_thread().name
        self._held_since = time.monotonic()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._holder = None
        self._held_since = None
        self._lock.release()
        return False

    def describe(self):
        if self._holder is None:
            return f"{self._name}: free"
        held_for = time.monotonic() - self._held_since
        return f"{self._name}: held by {self._holder} for {held_for:.2f}s"


_guide_build_lock = _TrackedLock("_guide_build_lock")
_guide_cache_lock = threading.Lock()

_guide_row_stops_cache = {}


def invalidate_guide_cache():
    """Force the next guide render to do a real rebuild instead of serving a stale cached frame/row-stops."""
    with _guide_cache_lock:
        _guide_frame_cache["key"] = None
        _guide_frame_cache["canvas"] = None
        _guide_frame_cache["preview_bounds"] = None
        _guide_frame_cache["clock_box"] = None
        _guide_row_cache_state["gen"] += 1
    _guide_scaled_cache["key"] = None
    _guide_scaled_cache["surf"] = None
    _guide_row_cache.clear()
    _guide_row_stops_cache.clear()


def get_cached_row_stops(ch_str, expected_time_idx=None):
    """Return the cached (blocks, stops) for ch_str if built for the active time window, else None."""
    entry = _guide_row_stops_cache.get(str(ch_str).zfill(2))
    if entry is None:
        return None
    if expected_time_idx is not None and entry.get("time_idx") != expected_time_idx:
        return None
    return entry["blocks"], entry["stops"]

def _clean_show_title(raw_title):
    """Return the guide display title via media.clean_video_display_title()."""
    return media.clean_video_display_title(raw_title)


def _compute_guide_cache_key(app_state, db, real_w, real_h, tx, ty, tw, th):
    aspect_mode = "4:3" if _guide_aspect_is_4_3(db) else "16:9"
    _now = broadcast_clock.now().replace(tzinfo=None)
    _now_bucket = (_now.hour * 2) + (1 if _now.minute >= 30 else 0)
    return (
        aspect_mode, real_w, real_h, tx, ty, tw, th,
        app_state.get("selected_guide_channel"),
        app_state.get("guide_col_pos"),
        app_state.get("guide_row_offset"),
        app_state.get("selected_guide_time_idx"),
        app_state.get("guide_selected_abs_time"),
        db.config.get("theme_bg_hue") if db is not None else None,
        db.config.get("theme_ui_hue") if db is not None else None,
        app_state.get("guide_refresh_token", 0),
        repr(sorted((app_state.get("guide_channel_stamps") or {}).items())),
        _now.date(),
        _now_bucket,
    )


def _guide_target_geometry(surface, target_rect):
    real_w, real_h = surface.get_size()
    if target_rect is not None:
        tx, ty, tw, th = target_rect.x, target_rect.y, target_rect.width, target_rect.height
    else:
        tx, ty, tw, th = 0, 0, real_w, real_h
    return real_w, real_h, tx, ty, tw, th


def _build_and_cache_guide_frame(surface, app_state, db, theme, target_rect=None):
    """Rebuild the guide from media.build_block_timeline for every visible row and cache the frame."""
    real_w, real_h, tx, ty, tw, th = _guide_target_geometry(surface, target_rect)

    with _guide_build_lock:
        is_4_3 = _guide_aspect_is_4_3(db)
        cache_key = _compute_guide_cache_key(app_state, db, real_w, real_h, tx, ty, tw, th)

        with _guide_cache_lock:
            cached = _guide_frame_cache
            if cached["key"] == cache_key and cached["canvas"] is not None:

                return cache_key, cached["canvas"], cached["preview_bounds"]

        _guide_build_started = time.perf_counter()
        _render_tv_guide_impl(surface, app_state, db, theme, target_rect=target_rect, present=False)
        log.info("[GUIDE BUILD] elapsed_ms=%.1f selected_channel=%s row_offset=%s col_pos=%s",
                 (time.perf_counter() - _guide_build_started) * 1000.0,
                 app_state.get("selected_guide_channel"),
                 app_state.get("guide_row_offset"),
                 app_state.get("guide_col_pos"))
        design_canvas = _get_guide_canvas_43() if is_4_3 else _get_guide_canvas()
        canvas_copy = design_canvas.copy()
        preview_bounds = app_state.get("guide_preview_bounds")
        clock_box = app_state.get("_guide_clock_box")

        with _guide_cache_lock:
            _guide_frame_cache["key"] = cache_key
            _guide_frame_cache["canvas"] = canvas_copy
            _guide_frame_cache["preview_bounds"] = preview_bounds
            _guide_frame_cache["clock_box"] = clock_box
            _guide_frame_cache["serial"] += 1

    return cache_key, canvas_copy, preview_bounds


def _guide_clock_texts(clock_box):
    """Return the (clock, date) strings the guide clock box shows right now."""
    now_dt = broadcast_clock.now().replace(tzinfo=None)
    clock_txt = now_dt.strftime("%I:%M:%S %p")
    if clock_box["is_4_3"]:
        date_txt = now_dt.strftime('%m/%d/%Y')
    else:
        date_txt = f"{now_dt.strftime('%a').upper()} {now_dt.strftime('%m/%d/%Y')}"
    return clock_txt, date_txt


def _draw_guide_clock_overlay(canvas, clock_box, texts=None):
    """Repaint the live clock and date box on a cached guide canvas."""
    if canvas is None or not clock_box:
        return
    clock_txt, date_txt = texts if texts is not None else _guide_clock_texts(clock_box)

    font = clock_box["font"]
    lbl_clock = font.render(clock_txt, True, clock_box["text_color"])
    lbl_date = font.render(date_txt, True, clock_box["text_color"])

    bx, by, bw, bh = clock_box["rect"]
    pygame.draw.rect(canvas, clock_box["bg_color"], (bx, by, bw, bh))
    pygame.draw.rect(canvas, clock_box["border_color"], (bx, by, bw, bh), 1)

    pair_gap = 8
    pair_total_w = lbl_date.get_width() + pair_gap + lbl_clock.get_width()
    pair_left_x = bx + (bw - pair_total_w) // 2
    pair_y = by + (bh - lbl_clock.get_height()) // 2

    canvas.blit(lbl_date, (pair_left_x, pair_y))
    canvas.blit(lbl_clock, (pair_left_x + lbl_date.get_width() + pair_gap, pair_y))


def render_tv_guide_async(surface, app_state, db, theme, target_rect=None):
    """Non-blocking guide render for the main per-frame draw path."""
    real_w, real_h, tx, ty, tw, th = _guide_target_geometry(surface, target_rect)

    with _guide_cache_lock:
        cached = _guide_frame_cache
        _canvas = cached["canvas"]
        _preview_bounds = cached["preview_bounds"]
        _clock_box = cached["clock_box"]
        _serial = cached["serial"]

    if _canvas is None:
        _, _canvas, _preview_bounds = _build_and_cache_guide_frame(
            surface, app_state, db, theme, target_rect=target_rect)
        with _guide_cache_lock:
            _clock_box = _guide_frame_cache["clock_box"]
            _serial = _guide_frame_cache["serial"]

    if tw > 0 and th > 0 and _canvas.get_width() > 0 and _canvas.get_height() > 0:
        _clock_texts = _guide_clock_texts(_clock_box) if _clock_box else None
        _scaled_key = (_serial, _clock_texts, tw, th)
        _scaled = _guide_scaled_cache["surf"]
        if _scaled is None or _guide_scaled_cache["key"] != _scaled_key:
            _draw_guide_clock_overlay(_canvas, _clock_box, _clock_texts)
            _scaled = pygame.transform.smoothscale(_canvas, (tw, th))
            _guide_scaled_cache["surf"] = _scaled
            _guide_scaled_cache["key"] = _scaled_key
        surface.blit(_scaled, (tx, ty))
    if _preview_bounds is not None:
        app_state["guide_preview_bounds"] = _preview_bounds


def guide_preload_tick(scratch_surface, app_state, db, theme, target_rect=None):
    """One iteration of background guide preloading."""
    # WARNING: call only from a daemon thread, never from the main render loop.
    if db is None:
        return
    _build_and_cache_guide_frame(scratch_surface, app_state, db, theme, target_rect=target_rect)


# ============================================================================
# SECTION: TV guide: render implementation
# ============================================================================


def _render_tv_guide_impl(surface, app_state, db, theme, target_rect=None, present=True):
    import os
    import datetime
    import math

    real_surface = surface
    real_w, real_h = real_surface.get_size()
    is_4_3 = _guide_aspect_is_4_3(db)

    # ========================================================================
    # SECTION: Canvas & layout setup
    # ========================================================================

    num_rows = GUIDE_VISIBLE_ROWS_4_3 if is_4_3 else GUIDE_VISIBLE_ROWS_16_9
    num_cols = GUIDE_NUM_COLS_4_3 if is_4_3 else GUIDE_NUM_COLS_16_9
    last_col = num_cols - 1
    viewport_seconds = num_cols * 1800
    surface = _get_guide_canvas_43() if is_4_3 else _get_guide_canvas()
    w, h = (GUIDE_DESIGN_W_4_3, GUIDE_DESIGN_H_4_3) if is_4_3 else (GUIDE_DESIGN_W, GUIDE_DESIGN_H)
    # WARNING: not pure black; (0,0,0) is the game-overlay transparency key.
    surface.fill(COLOR_SCREEN_BLACK)
    
    import colorsys
    bg_hue = db.config.get("theme_bg_hue", 220) / 360.0 if db is not None else 220/360.0
    ui_hue = db.config.get("theme_ui_hue", 140) / 360.0 if db is not None else 140/360.0
    r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.9, 1.0)
    MINT_GREEN = (int(r * 255), int(g * 255), int(b * 255))
    r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.9, 0.55)
    HIGHLIGHT_BG = (int(r * 255), int(g * 255), int(b * 255))
    r, g, b = colorsys.hsv_to_rgb(bg_hue, 0.6, 0.15)
    UNSELECTED_BLUE = (int(r * 255), int(g * 255), int(b * 255))
    r, g, b = colorsys.hsv_to_rgb(bg_hue, 0.7, 0.08)
    DARKER_TIME_BLUE = (int(r * 255), int(g * 255), int(b * 255))
    CYAN_GRID = COLOR_TEXT_WHITE
    TEXT_WHITE = COLOR_TEXT_WHITE
    
    col_width = w // (num_cols + 1)

    _HDR_FONT_PX_16_9 = GUIDE_HDR_FONT_PX
    _CLOCK_FONT_PX_16_9 = GUIDE_CLOCK_FONT_PX

    if is_4_3:
        _CLOCK_CELL_PAD = GUIDE_43_CLOCK_CELL_PAD_PX
        _HDR_CELL_PAD = GUIDE_43_HDR_CELL_PAD_PX
        _CLOCK_PAIR_GAP = GUIDE_43_CLOCK_PAIR_GAP_PX
        _HDR_TO_CLOCK_RATIO = GUIDE_43_HDR_TO_CLOCK_RATIO
        _CLOCK_FONT_PX_4_3 = _CLOCK_FONT_PX_16_9
        _HDR_FONT_PX_4_3 = _HDR_FONT_PX_16_9
        for _clock_pt in range(60, 16, -1):
            _hdr_pt = max(_clock_pt, round(_clock_pt * _HDR_TO_CLOCK_RATIO))
            _fc = _get_font("Courier New", _clock_pt, bold=True)
            _fh = _get_font("Courier New", _hdr_pt, bold=True)
            _clock_total_w = (_fc.size("00/00/0000")[0] + _CLOCK_PAIR_GAP
                               + _fc.size("00:00:00 AM")[0])
            _hdr_total_w = _fh.size("WED 12:00 PM")[0]
            if (_clock_total_w <= col_width - _CLOCK_CELL_PAD
                    and _hdr_total_w <= col_width - _HDR_CELL_PAD):
                _CLOCK_FONT_PX_4_3 = _clock_pt
                _HDR_FONT_PX_4_3 = _hdr_pt
                break
    else:
        _HDR_FONT_PX_4_3 = _HDR_FONT_PX_16_9
        _CLOCK_FONT_PX_4_3 = _CLOCK_FONT_PX_16_9

    font_hdr = _get_font("Courier New", _HDR_FONT_PX_4_3 if is_4_3 else _HDR_FONT_PX_16_9, bold=True)
    font_row_lg = _get_font("Courier New", 17, bold=True)
    font_clock = _get_font("Courier New", _CLOCK_FONT_PX_4_3 if is_4_3 else _CLOCK_FONT_PX_16_9, bold=True)

    _MIN_GUIDE_FONT_PX = GUIDE_MIN_FONT_PX
    _guide_scale_y = (real_h / h) if h else 1.0
    if is_4_3 and 0 < _guide_scale_y < 1.0:
        _min_font_pt = max(_MIN_GUIDE_FONT_PX, int(math.ceil(_MIN_GUIDE_FONT_PX / _guide_scale_y)))
    else:
        _min_font_pt = _MIN_GUIDE_FONT_PX

    if is_4_3:
        font_row_lg = _get_font("Courier New", _min_font_pt * 2, bold=True)
        font_ch_num = _get_font("Courier New", max(13, int(_min_font_pt * 1.35)), bold=True)
    else:
        font_ch_num = font_row_lg

    _pw_ratio = (900.0 / 1920.0) if is_4_3 else (800.0 / 1920.0)
    _base_pw = int(w * _pw_ratio)
    _base_ph = int(_base_pw * (9.0 / 16.0))

    if is_4_3:
        _hdr_text_h = max(font_hdr.get_height(), font_clock.get_height())
        header_height = max(32, _hdr_text_h + 14)
    else:
        header_height = 32

    _header_growth = header_height - 32
    ph = _base_ph - _header_growth
    pw = int(ph * (16.0 / 9.0)) if is_4_3 else _base_pw
    px = (w - pw) // 2
    py = 15
    app_state["guide_preview_bounds"] = (px, py, pw, ph)

    grid_top_y = py + ph + 10

    pygame.draw.rect(surface, UNSELECTED_BLUE, (0, grid_top_y, w, h - grid_top_y))
    
    now_dt = broadcast_clock.now().replace(tzinfo=None)
    clock_txt = now_dt.strftime("%I:%M:%S %p")
    if is_4_3:
        date_txt = now_dt.strftime('%m/%d/%Y')
    else:
        date_txt = f"{now_dt.strftime('%a').upper()} {now_dt.strftime('%m/%d/%Y')}"
    lbl_clock = font_clock.render(clock_txt, True, MINT_GREEN)
    lbl_date = font_clock.render(date_txt, True, MINT_GREEN)

    pygame.draw.rect(surface, DARKER_TIME_BLUE, (0, grid_top_y, col_width, header_height))
    pygame.draw.rect(surface, CYAN_GRID, (0, grid_top_y, col_width, header_height), 1)

    pair_gap = 8
    pair_total_w = lbl_date.get_width() + pair_gap + lbl_clock.get_width()
    pair_left_x = (col_width - pair_total_w) // 2
    pair_y = grid_top_y + (header_height - lbl_clock.get_height()) // 2

    surface.blit(lbl_date, (pair_left_x, pair_y))
    surface.blit(lbl_clock, (pair_left_x + lbl_date.get_width() + pair_gap, pair_y))

    app_state["_guide_clock_box"] = {
        "rect": (0, grid_top_y, col_width, header_height),
        "bg_color": DARKER_TIME_BLUE,
        "border_color": CYAN_GRID,
        "text_color": MINT_GREEN,
        "font": font_clock,
        "is_4_3": is_4_3,
    }

    system_live_c_idx = (now_dt.hour * 2) + (1 if now_dt.minute >= 30 else 0)
    user_shift = app_state.get("selected_guide_time_idx", 0)
    time_idx = clamp_guide_time_idx(system_live_c_idx + user_shift, num_cols)
    viewport_start_seconds = time_idx * 1800

    # ========================================================================
    # SECTION: Time header row
    # ========================================================================

    for col in range(num_cols):
        c_idx = time_idx + col
        hr = (c_idx // 2) % 24
        mn = "00" if (c_idx % 2 == 0) else "30"
        ampm = "AM" if hr < 12 else "PM"
        hr_12 = hr if (0 < hr <= 12) else (12 if hr == 0 or hr == 12 else hr - 12)

        _day_offset = c_idx // 48
        _day_date = now_dt.date() + datetime.timedelta(days=_day_offset)
        hdr_txt = f"{_day_date.strftime('%a').upper()} {hr_12:02d}:{mn} {ampm}"
        h_lbl = font_hdr.render(hdr_txt, True, MINT_GREEN)
        cell_x = col_width + (col * col_width)
        
        current_block_w = (w - cell_x) if col == last_col else col_width

        _is_now_col = (c_idx == system_live_c_idx)
        _hdr_bg = HIGHLIGHT_BG if _is_now_col else DARKER_TIME_BLUE
        _hdr_txt_col = (0, 0, 0) if _is_now_col else MINT_GREEN
        if _is_now_col:
            h_lbl = font_hdr.render(hdr_txt, True, _hdr_txt_col)

        pygame.draw.rect(surface, _hdr_bg, (cell_x, grid_top_y, current_block_w, header_height))
        pygame.draw.rect(surface, CYAN_GRID, (cell_x, grid_top_y, current_block_w, header_height), 1)
        
        hdr_lbl_x = cell_x + (current_block_w // 2 - h_lbl.get_width() // 2)
        hdr_lbl_y = grid_top_y + (header_height // 2 - h_lbl.get_height() // 2)
        surface.blit(h_lbl, (hdr_lbl_x, hdr_lbl_y))
        
        if _is_now_col:
            pygame.draw.rect(surface, MINT_GREEN, (cell_x, grid_top_y, current_block_w, header_height), 2)
        
    pygame.draw.rect(surface, CYAN_GRID, (0, grid_top_y, col_width, header_height), 1)

    # ========================================================================
    # SECTION: Visible channel selection
    # ========================================================================

    allowed_channels = []
    for i in range(5, 45):
        if db.channels_db.get(str(i).zfill(2), {}).get("active", True):
            allowed_guide_set = str(i).zfill(2)
            allowed_channels.append(allowed_guide_set)
            
    row_offset = app_state.get("guide_row_offset", 0)
    visible_channels = allowed_channels[row_offset:row_offset + num_rows]
    
    start_y = grid_top_y + header_height
    row_height = (h - start_y) // num_rows
    
    viewport_start_seconds = time_idx * 1800

    _guide_target_abs = app_state.get("guide_selected_abs_time")
    if _guide_target_abs is None:
        _guide_target_abs = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second
    else:
        # WARNING: a show's start can move a fraction of a second between two drawings; the highlight time is nudged forward by that much so a start that drifted just past it still holds the highlight instead of handing it to the cell on its left.
        _guide_target_abs = _guide_target_abs + _GUIDE_HIGHLIGHT_NUDGE_SECONDS

    # ========================================================================
    # SECTION: Channel rows (selected row drawn first)
    # ========================================================================

    _row_start_y_map = {
        str(visible_channels[_ri]).zfill(2): start_y + _ri * row_height
        for _ri in range(len(visible_channels))
    }
    _sel_guide_ch = str(app_state.get("selected_guide_channel", "05")).zfill(2)
    _guide_proc_order = sorted(
        range(len(visible_channels)),
        key=lambda _i: (0 if str(visible_channels[_i]).zfill(2) == _sel_guide_ch else 1, _i)
    )
    _row_cache_now = time.monotonic()
    _row_cache_gen = _guide_row_cache_state["gen"]
    _row_cache_base = (
        is_4_3, real_w, real_h, w, h, col_width, row_height, num_cols, time_idx,
        now_dt.date(), (now_dt.hour * 2) + (1 if now_dt.minute >= 30 else 0),
        app_state.get("guide_refresh_token", 0),
        repr(sorted((app_state.get("guide_channel_stamps") or {}).items())),
        db.config.get("theme_bg_hue"), db.config.get("theme_ui_hue"),
    )

    def _row_cache_store(cache_key, y, blocks, stops):
        if y < 0 or y + row_height > h or _row_cache_gen != _guide_row_cache_state["gen"]:
            return
        strip = surface.subsurface((0, y, w, row_height)).copy()
        while len(_guide_row_cache) >= _GUIDE_ROW_CACHE_MAX:
            _guide_row_cache.pop(next(iter(_guide_row_cache)), None)
        _guide_row_cache.pop(cache_key, None)
        _guide_row_cache[cache_key] = {
            "strip": strip, "blocks": list(blocks), "stops": list(stops),
            "gen": _row_cache_gen, "ts": _row_cache_now,
        }

    for _pi in _guide_proc_order:
        ch_num = visible_channels[_pi]
        start_y = _row_start_y_map.get(str(ch_num).zfill(2), grid_top_y + header_height)
        if start_y + row_height > h + 5:
            continue

        ch_str = str(ch_num).zfill(2)
        ch_info = db.channels_db.get(ch_str, {})
        is_highlighted_row = (ch_str == str(app_state["selected_guide_channel"]).zfill(2))

        _row_cache_key = None
        if not is_highlighted_row:
            _row_viz_admitted = (bool(_guide_visualizer_admitted(ch_info, app_state, db))
                                 if ch_info.get("is_visualizer", False) else None)
            _row_cache_key = _row_cache_base + (ch_str, str(ch_info.get("name")), repr(ch_info.get("playback_anchor")), _row_viz_admitted)
            _row_hit = _guide_row_cache.get(_row_cache_key)
            if (_row_hit is not None and _row_hit["gen"] == _row_cache_gen
                    and _row_cache_now - _row_hit["ts"] < _GUIDE_ROW_CACHE_TTL_S):
                surface.blit(_row_hit["strip"], (0, start_y))
                _guide_row_stops_cache[ch_str] = {
                    "blocks": list(_row_hit["blocks"]),
                    "stops": list(_row_hit["stops"]),
                    "ts": time.time(),
                    "time_idx": time_idx,
                }
                continue
        
        pygame.draw.rect(surface, UNSELECTED_BLUE, (0, start_y, col_width, row_height))
        pygame.draw.rect(surface, CYAN_GRID, (0, start_y, col_width, row_height), 1)
        
        display_station_name = str(ch_info.get("name", f"STATION {ch_str}")).upper()
        
        dynamic_font_size = max(13, int(row_height * 0.32))
        if is_4_3:
            dynamic_font_size = max(dynamic_font_size, _min_font_pt)
        font_row_sm = _get_font("Courier New", dynamic_font_size, bold=True)
        
        lbl_ch_top = font_ch_num.render(f"CH {ch_str}", True, MINT_GREEN)
        lbl_ch_bot = font_row_sm.render(display_station_name, True, MINT_GREEN)
        
        top_x = (col_width // 2) - (lbl_ch_top.get_width() // 2)
        bot_x = (col_width // 2) - (lbl_ch_bot.get_width() // 2)
        
        line_gap = max(2, int(row_height * 0.08))
        total_text_height = lbl_ch_top.get_height() + lbl_ch_bot.get_height() + line_gap
        center_y = start_y + (row_height // 2) - (total_text_height // 2)
        
        surface.blit(lbl_ch_top, (top_x, center_y))
        surface.blit(lbl_ch_bot, (bot_x, center_y + lbl_ch_top.get_height() + line_gap))
        
        current_row_blocks_list = []
        current_row_stops_list = []
        
        if ch_info.get("is_visualizer", False):
            s_list = _guide_visualizer_admitted(ch_info, app_state, db)
            for col in range(num_cols):
                _viz_block_abs = viewport_start_seconds + col * 1800
                current_row_blocks_list.append(col)
                current_row_stops_list.append({"col": col, "abs": _viz_block_abs, "key": ("col", round(_viz_block_abs))})
                is_selected_cell = (is_highlighted_row and
                                     _viz_block_abs <= _guide_target_abs < _viz_block_abs + 1800)
                cell_bg = HIGHLIGHT_BG if is_selected_cell else UNSELECTED_BLUE
                cell_txt_col = (0, 0, 0) if is_selected_cell else TEXT_WHITE
                
                cell_x = col_width + (col * col_width)
                if cell_x >= w:
                    continue
                    
                current_cell_w = (w - cell_x) if col == last_col else col_width
                
                pygame.draw.rect(surface, cell_bg, (cell_x, start_y, current_cell_w, row_height))
                pygame.draw.rect(surface, CYAN_GRID, (cell_x, start_y, current_cell_w, row_height), 1)
                
                txt_label = "PLAYING MUSIC" if s_list else "OFF-AIR"
                
                if cell_x + 8 < w:
                    max_txt_w = current_cell_w - 15
                    if max_txt_w > 5:
                        if is_selected_cell:
                            full_lbl = font_row_lg.render(txt_label, True, cell_txt_col)
                            v_off = row_height // 2 - full_lbl.get_height() // 2
                            if full_lbl.get_width() > max_txt_w:
                                scroll_range = full_lbl.get_width() - max_txt_w
                                offset_x = int((pygame.time.get_ticks() * 0.04) % (scroll_range + 60))
                                if offset_x > scroll_range: offset_x = scroll_range
                                
                                text_sub = pygame.Surface((max_txt_w, row_height), pygame.SRCALPHA)
                                text_sub.blit(full_lbl, (-offset_x, v_off))
                                surface.blit(text_sub, (cell_x + 6, start_y))
                            else:
                                cell_text_x = cell_x + (current_cell_w // 2 - full_lbl.get_width() // 2)
                                surface.blit(full_lbl, (cell_text_x, start_y + v_off))
                        else:
                            max_chars = max(3, max_txt_w // 10)
                            if max_txt_w >= 15:
                                truncated_label = txt_label[:max_chars]
                                if len(truncated_label) < len(txt_label) and max_chars > 3:
                                    truncated_label = truncated_label[:-1] + "."
                                lbl_cell = font_row_lg.render(truncated_label, True, cell_txt_col)
                                while lbl_cell.get_width() > max_txt_w and len(truncated_label) > 1:
                                    truncated_label = truncated_label[:-1]
                                    lbl_cell = font_row_lg.render(truncated_label, True, cell_txt_col)
                                cell_text_x = cell_x + (current_cell_w // 2 - lbl_cell.get_width() // 2)
                                cell_text_x = max(cell_x + 2, cell_text_x)
                                surface.blit(lbl_cell, (cell_text_x, start_y + (row_height // 2 - lbl_cell.get_height() // 2)))
                
            import time as _time
            _guide_row_stops_cache[ch_str] = {
                "blocks": list(current_row_blocks_list),
                "stops": list(current_row_stops_list),
                "ts": _time.time(),
                "time_idx": time_idx,
            }
            if _row_cache_key is not None:
                _row_cache_store(_row_cache_key, start_y, current_row_blocks_list, current_row_stops_list)
            if is_highlighted_row:
                app_state["guide_current_row_blocks"] = current_row_blocks_list
                app_state["guide_current_row_stops"] = current_row_stops_list
                if app_state.get("_guide_stops_only_probe"):
                    return
        else:
            last_drawn_pixel_x = col_width
            has_rendered_any_video = False
            
            _is_six_block_guide = (not ch_info.get("is_visualizer", False)
                                   and ch_info.get("scheduling_mode", "random_slots") != "marathon"
                                   and ch_info.get("block_mode", "full_day") == "six_block")
            
            viewport_adjusted_start = viewport_start_seconds
            viewport_adjusted_end = viewport_adjusted_start + viewport_seconds
            last_drawn_abs_time = viewport_adjusted_start
            
            _is_full_day_guide = (not ch_info.get("is_visualizer", False)
                                  and ch_info.get("scheduling_mode", "random_slots") != "marathon"
                                  and ch_info.get("block_mode", "full_day") == "full_day")

            _fd_anchor_secs = None
            _seg_yesterday = now_dt - datetime.timedelta(days=1)
            if _is_full_day_guide:
                _fd_pl = ch_info.get("playback_log", {})
                _fd_key = f"{now_dt.year}_{now_dt.timetuple().tm_yday}_Full Day"
                _fd_a = _fd_pl.get("block_anchors", {}).get(_fd_key)
                if _fd_a is not None:
                    try:
                        _fd_anchor_secs = int(_fd_a) + int(_fd_pl.get("sched_cycle_boundary", 0) or 0)
                    except (TypeError, ValueError) as _swallowed_error:
                        _log_swallowed("_render_tv_guide_impl: _fd_anchor_secs = int(_fd_a) + int(_fd_pl.get('sched_cycle_boundary...", _swallowed_error)
                        _fd_anchor_secs = None

            _seg_anchor_delta = {}
            _seg_has_anchor = set()
            if not _is_full_day_guide and not ch_info.get("is_visualizer", False) \
                    and ch_info.get("scheduling_mode", "random_slots") != "marathon":
                _seg_pl = ch_info.get("playback_log", {})
                _seg_anchors = _seg_pl.get("block_anchors", {})
                _seg_cycle = 0
                try:
                    _seg_cycle = int(_seg_pl.get("sched_cycle_boundary", 0) or 0)
                except (TypeError, ValueError) as _swallowed_error:
                    _log_swallowed("_render_tv_guide_impl: _seg_cycle = int(_seg_pl.get('sched_cycle_boundary', 0) or 0)", _swallowed_error)
                    _seg_cycle = 0
                if _is_six_block_guide:
                    _seg_nominal = {_n: _s * 3600 for (_n, _s, _e) in media.SIX_HOUR_BLOCKS}
                else:
                    _seg_nominal = {"Morning": 4 * 3600, "Evening": 12 * 3600, "Night": 20 * 3600}
                _seg_yday = now_dt.timetuple().tm_yday
                for _bn, _nom in _seg_nominal.items():
                    _av = _seg_anchors.get(f"{now_dt.year}_{_seg_yday}_{_bn}")
                    if (_av is None
                            and not _is_six_block_guide
                            and _bn == "Night"
                            and now_dt.hour < 4):
                        _av = _seg_anchors.get(
                            f"{_seg_yesterday.year}_"
                            f"{_seg_yesterday.timetuple().tm_yday}_Night"
                        )
                    if _av is None:
                        continue
                    _seg_has_anchor.add(_bn)
                    try:
                        _seg_anchor_delta[_bn] = (int(_av) + _seg_cycle) - _nom
                    except (TypeError, ValueError) as _swallowed_error:
                        _log_swallowed("_render_tv_guide_impl: _seg_anchor_delta[_bn] = int(_av) + _seg_cycle - _nom", _swallowed_error)
                        pass

            def get_block_for_hour(hour):
                if _is_full_day_guide:
                    return "Full Day", hour * 3600
                if _is_six_block_guide:
                    _bn, _bs, _be = media.SIX_HOUR_BLOCKS[hour // 4]
                    return _bn, (hour - _bs) * 3600
                if 4 <= hour < 12:
                    return "Morning", (hour - 4) * 3600
                elif 12 <= hour < 20:
                    return "Evening", (hour - 12) * 3600
                else:  
                    if hour >= 20:
                        return "Night", (hour - 20) * 3600
                    else:  
                        return "Night", (hour + 4) * 3600  
                        
            def _guide_block_seed_offset(bn):
                _m = {"Morning": 100, "Evening": 200, "Night": 300, "Full Day": 500}
                for _i, _n in enumerate(media.SIX_HOUR_BLOCK_NAMES):
                    _m[_n] = 600 + _i * 100
                return _m.get(bn, 400)

            def _build_commercial_tracks():
                commercials_enabled = ch_info.get("commercials_enabled", False)
                commercial_items = ch_info.get("schedules", {}).get("Commercials", [])
                if not commercials_enabled or not commercial_items:
                    return []
                return media.build_commercial_tracks(
                    commercial_items,
                    lambda _p: _guide_path_exists(_p, app_state),
                )

            def get_block_content(block_name, _override_sp=None):
                schedules = ch_info.get("schedules", {})
                folder_items = schedules.get(block_name, [])
                if not folder_items:
                    return [], {}

                _start_seed = lambda _info, _offset: "%s:%d" % (
                    _info.get("playback_log", {}).get(
                        "intro_seed",
                        _info.get("playback_log", {}).get("start_number", 0),
                    ),
                    int(_offset),
                )
                _lineup, _lineup_block = media.read_block_lineup(
                    ch_str, ch_info, block_name, now_dt,
                    lambda _p: _guide_path_exists(_p, app_state), _start_seed,
                    peek_only=True,
                )
                return _lineup, _lineup_block

            def get_block_timeline(block_name, _override_sp=None):
                block_content, _new_sp = get_block_content(block_name, _override_sp)
                if not block_content:
                    return [], 0, _new_sp
                block_offset = _guide_block_seed_offset(block_name)
                day_seed = now_dt.year * 1000 + now_dt.timetuple().tm_yday + int(ch_str) + block_offset
                commercial_placement = ch_info.get("commercial_placement", "interrupt_half_hour")
                _pair_thresh_min = ch_info.get("pair_threshold_minutes", 15)
                interrupt_floor_seconds = (_pair_thresh_min if _pair_thresh_min > 0 else 15) * 60
                _fade_in, _fade_out = media.bumper_tracks_for_channel(ch_info)
                _tl, _tc, _ = media.build_block_timeline(
                    block_content, _build_commercial_tracks(), commercial_placement, day_seed,
                    interrupt_floor_seconds=interrupt_floor_seconds,
                    commercial_state=ch_info.get("playback_log", {}).get("commercial_pos"),
                    fade_in_tracks=_fade_in, fade_out_tracks=_fade_out,
                )
                return _tl, _tc, _new_sp

            def get_marathon_content():
                folder_items = ch_info.get("schedules", {}).get("Marathon", [])
                _marathon_seed = ch_info.get("playback_log", {}).get(
                    "intro_seed",
                    ch_info.get("playback_log", {}).get("start_number", 0),
                )
                ordered = media.build_ordered_tracks(folder_items, "marathon", "%s:marathon" % _marathon_seed)
                if not ordered:
                    return []
                _mara_cached_durs = ch_info.get("playback_log", {}).get("marathon_cached_durations", {})
                if _mara_cached_durs:
                    ordered = [
                        {**t, "duration": _mara_cached_durs[t["path"]]}
                        if t.get("path") in _mara_cached_durs and _mara_cached_durs[t["path"]] > 0
                        else t
                        for t in ordered
                    ]
                return ordered

            _is_marathon = (not ch_info.get("is_visualizer", False)
                            and ch_info.get("scheduling_mode", "random_slots") == "marathon")

            _holiday_forced_guide = media.holiday_forced_key_for_channel(db.config, ch_str) if db is not None else ""

            def _get_holiday_key_guide(d):
                return media.holiday_key_for_date(d, _holiday_forced_guide, now_dt.date())

            _holiday_key_guide = (None if ch_info.get("is_visualizer", False)
                                   else _get_holiday_key_guide(now_dt.date()))
            _holiday_entries_guide = (
                ch_info.get("holiday_schedules", {}).get(_holiday_key_guide, [])
                if _holiday_key_guide else []
            )
            _is_holiday_guide = bool(_holiday_entries_guide) and not _is_marathon

            viewport_start_hour = viewport_adjusted_start // 3600
            viewport_end_hour = (viewport_adjusted_end - 1) // 3600
            
            # WARNING: the guide cache key must include the live schedule contents, or a fresh retro_config.json would paint the previous run's programs.
            _guide_schedule_signature = tuple(
                repr(_entry) for _entry in (
                    list(_holiday_entries_guide) if _is_holiday_guide else list(
                        _item
                        for _block_items in (ch_info.get("schedules", {}) or {}).values()
                        for _item in (_block_items if isinstance(_block_items, (list, tuple)) else [])
                    )
                )
            )
            _tl_key = (ch_str, now_dt.year, now_dt.timetuple().tm_yday, viewport_start_seconds,
                       app_state.get("guide_refresh_token", 0),
                       (app_state.get("guide_channel_stamps") or {}).get(ch_str, 0),
                       _guide_schedule_signature,
                       tuple(ch_info.get(_opt) for _opt in ("scheduling_mode", "block_mode", "pair_threshold_minutes", "commercials_enabled",
                                                            "commercial_placement", "active", "marathon_anchor_epoch")),
                       _guide_hub_past_signature(ch_str, now_dt),
                       _guide_hub_future_signature(ch_str, now_dt))
            _tl_hit = _guide_timeline_cache.get(_tl_key)
            if _tl_hit is not None:
                _cached_sources_ok = True
                for _cached_row in _tl_hit:
                    _cached_entry = _cached_row[0] if isinstance(_cached_row, tuple) else _cached_row
                    if (isinstance(_cached_entry, dict)
                            and not _cached_entry.get("is_commercial")
                            and _cached_entry.get("path")
                            and not _guide_path_exists(_cached_entry["path"], app_state)):
                        _cached_sources_ok = False
                        break
                if not _cached_sources_ok:
                    _tl_hit = None
            # WARNING: the cached timeline must never be edited in place; the anchor and live-preview passes below rewrite entries and would move every later render of this row.
            timeline_entries = list(_tl_hit) if _tl_hit is not None else []
            _block_overflow_end = {}
            if _is_six_block_guide:
                _sbn = media.SIX_HOUR_BLOCK_NAMES
                _next_block_map = {_sbn[i]: _sbn[(i + 1) % len(_sbn)] for i in range(len(_sbn))}
            else:
                _next_block_map = {"Morning": "Evening", "Evening": "Night", "Night": "Morning", "Full Day": "Full Day"}


            if _tl_hit is None and _is_holiday_guide:
                _h_log = ch_info.get("holiday_playback_log", {}).get(_holiday_key_guide, {})
                _h_raw_tracks, _h_initial_sp = media.read_holiday_lineup(
                    ch_str, ch_info, _holiday_key_guide, now_dt,
                    lambda _p: _guide_path_exists(_p, app_state),
                    show_pos=_h_log.get("show_pos", {}),
                    fixed_show_order=_h_log.get("show_order"),
                )

                if _h_raw_tracks:
                    _h_pair_thresh_min = ch_info.get("pair_threshold_minutes", 15)
                    _h_interrupt_floor = (_h_pair_thresh_min if _h_pair_thresh_min > 0 else 15) * 60
                    _h_day_seed = (now_dt.year * 1000 + now_dt.timetuple().tm_yday + int(ch_str)
                                   + 9000 + (sum(ord(c) for c in _holiday_key_guide) % 1000))
                    _h_commercial_placement = ch_info.get("commercial_placement", "interrupt_half_hour")

                    def _get_holiday_content(_override_sp=None, _seed_bump=0):
                        _sp_copy = dict(_override_sp if _override_sp is not None
                                        else _h_log.get("show_pos", {}))
                        _fixed_order = None if _override_sp is not None else _h_log.get("show_order")
                        return media.read_holiday_lineup(
                            ch_str, ch_info, _holiday_key_guide, now_dt,
                            lambda _p: _guide_path_exists(_p, app_state),
                            show_pos=_sp_copy, fixed_show_order=_fixed_order,
                            seed_bump=_seed_bump,
                        )

                    def _get_holiday_timeline(_override_sp=None, _seed_bump=0):
                        _content, _new_sp = _get_holiday_content(_override_sp, _seed_bump)
                        if not _content:
                            return [], 0, _new_sp
                        _tl, _tc, _ = media.build_block_timeline(
                            _content, _build_commercial_tracks(), _h_commercial_placement,
                            _h_day_seed + _seed_bump, interrupt_floor_seconds=_h_interrupt_floor,
                            commercial_state=_h_log.get("commercial_pos"),
                        )
                        return _tl, _tc, _new_sp

                    _h_block_key = f"{now_dt.year}_{now_dt.timetuple().tm_yday}_Holiday_{_holiday_key_guide}"
                    _h_anchors = ch_info.get("playback_log", {}).get("block_anchors", {})
                    _h_block_start = _h_anchors.get(_h_block_key)
                    if _h_block_start is None:
                        _h_now_secs = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second
                        _h_block_start = (int(_h_now_secs) // 1800) * 1800

                    _win_lo = viewport_adjusted_start
                    _win_hi = viewport_adjusted_end + 3600

                    _h_cycle_start_secs = _h_block_start
                    _h_sp = None
                    _h_seed_bump = 0
                    _h_guard = 0
                    while _h_cycle_start_secs < _win_hi and _h_guard < 10000:
                        _h_tl, _h_total, _h_next_sp = _get_holiday_timeline(_h_sp, _h_seed_bump)
                        if not _h_tl or _h_total <= 0:
                            break
                        for entry in _h_tl:
                            _s_start = _h_cycle_start_secs + entry["start_time"]
                            _s_end = _s_start + entry["duration"]
                            if _s_end > _win_lo and _s_start < _win_hi:
                                timeline_entries.append((entry, _s_start, _s_end, "Holiday"))
                            _h_guard += 1
                        _h_cycle_start_secs += _h_total
                        _h_sp = _h_next_sp
                        _h_seed_bump += 1
            elif _tl_hit is None and _is_marathon:
                marathon_content = get_marathon_content()
                commercial_placement = ch_info.get("commercial_placement", "interrupt_half_hour")
                _pair_thresh_min = ch_info.get("pair_threshold_minutes", 15)
                _interrupt_floor = (_pair_thresh_min if _pair_thresh_min > 0 else 15) * 60
                m_timeline, m_total, _ = media.build_block_timeline(
                    marathon_content, _build_commercial_tracks(), commercial_placement,
                    day_seed=0, interrupt_floor_seconds=_interrupt_floor,
                    commercial_state=ch_info.get("playback_log", {}).get("marathon_commercial_pos"),
                ) if marathon_content else ([], 0, None)
                if m_total > 0:
                    view_start_epoch = (now_dt.toordinal() * 86400
                                        + (system_live_c_idx + user_shift) * 1800)
                    anchor = ch_info.get("marathon_anchor_epoch")
                    if not anchor:
                        _cs0 = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second
                        anchor = now_dt.toordinal() * 86400 + (_cs0 // 1800) * 1800
                    win_lo = view_start_epoch
                    win_hi = view_start_epoch + viewport_seconds + 3600
                    off = (win_lo - anchor) % m_total
                    cycle_start_epoch = win_lo - off
                    _guard = 0
                    while cycle_start_epoch < win_hi and _guard < 10000:
                        for entry in m_timeline:
                            _seg_dur = entry["duration"]
                            s_start = cycle_start_epoch + entry["start_time"]
                            s_end = s_start + _seg_dur
                            if s_end > win_lo and s_start < win_hi:
                                timeline_entries.append((
                                    entry,
                                    viewport_adjusted_start + (s_start - view_start_epoch),
                                    viewport_adjusted_start + (s_end   - view_start_epoch),
                                    "Marathon"))
                            _guard += 1
                            if s_end >= win_hi:
                                break
                        cycle_start_epoch += m_total
            elif _tl_hit is None:
              _NIGHT_SECS_BEFORE_MIDNIGHT = 86400 - 20 * 3600

              def _walk_block_overflow(block_name, block_abs_start, block_abs_end, seg_delta=0):
                  _tl, _total, _ = get_block_timeline(block_name)
                  if not _tl or _total <= 0:
                      return None
                  if block_name == "Night" and block_abs_start == seg_delta:
                      abs_pos = -_NIGHT_SECS_BEFORE_MIDNIGHT + seg_delta
                  elif block_name in _seg_has_anchor:
                      abs_pos = block_abs_start
                  else:
                      abs_pos = max(block_abs_start,
                                     _block_overflow_end.get(block_name, block_abs_start))
                  _guard = 0
                  while abs_pos < block_abs_end and _guard < 10000:
                      cycle_start = abs_pos
                      _kept_tl, _kept_total, _crossing_end = media.guide_cycle_through_block_end(
                          _tl, _total, cycle_start, block_abs_end)
                      if _crossing_end is not None:
                          return _crossing_end
                      _guard += len(_kept_tl) + 1
                      abs_pos = cycle_start + _total
                  return None

              if not _is_full_day_guide:
                  if _is_six_block_guide:
                      _sb = media.SIX_HOUR_BLOCKS
                      _overflow_ring = [
                          (_sb[i][0], _sb[i][1] * 3600, _sb[i][2] * 3600,
                           _sb[(i + 1) % len(_sb)][0])
                          for i in range(len(_sb))
                      ]
                  else:
                      _overflow_ring = [
                          ("Night",   0,           4 * 3600,            "Morning"),
                          ("Morning", 4 * 3600,    12 * 3600,           "Evening"),
                          ("Evening", 12 * 3600,   20 * 3600,           "Night"),
                      ]
                  for _bname, _bstart, _bend, _nb in _overflow_ring:
                      _bd = _seg_anchor_delta.get(_bname, 0)
                      _overflow_end = _walk_block_overflow(_bname, _bstart + _bd, _bend, _bd)
                      if _overflow_end is not None:
                          _block_overflow_end[_nb] = max(
                              _block_overflow_end.get(_nb, 0), _overflow_end)

              for hour in range(viewport_start_hour - 1, viewport_end_hour + 1):
                hour_mod = hour % 24
                block_name, _ = get_block_for_hour(hour_mod)
                
                _seg_d = _seg_anchor_delta.get(block_name, 0)
                if _is_six_block_guide:
                    _sbw = next((b for b in media.SIX_HOUR_BLOCKS if b[0] == block_name), None)
                    if _sbw is None:
                        continue
                    block_abs_start = _sbw[1] * 3600 + _seg_d
                    block_abs_end = _sbw[2] * 3600
                elif block_name == "Morning":
                    block_abs_start = 4 * 3600 + _seg_d
                    block_abs_end = 12 * 3600
                elif block_name == "Evening":
                    block_abs_start = 12 * 3600 + _seg_d
                    block_abs_end = 20 * 3600
                elif block_name == "Full Day":
                    if _fd_anchor_secs is not None:
                        block_abs_start = _fd_anchor_secs
                        block_abs_end = 24 * 3600
                    else:
                        block_abs_start = 0
                        block_abs_end = 24 * 3600
                else:  
                    if hour_mod >= 20:
                        block_abs_start = 20 * 3600 + _seg_d
                        block_abs_end = 24 * 3600 + 4 * 3600
                    else:  
                        block_abs_start = 0 + _seg_d
                        block_abs_end = 4 * 3600
                
                block_already_rendered = any(
                    e[3] == block_name and e[0] is not None
                    and e[2] > block_abs_start and e[1] < block_abs_end
                    for e in timeline_entries
                )
                if block_already_rendered:
                    continue

                block_timeline, total_content, _guide_cycle_sp = get_block_timeline(block_name)
                if not block_timeline or total_content <= 0:
                    continue
                
                if block_name == "Night" and block_abs_start == _seg_d:
                    _night_secs_before_midnight = 86400 - 20 * 3600
                    abs_pos = -_night_secs_before_midnight + _seg_d
                elif block_name in _seg_has_anchor:
                    abs_pos = block_abs_start
                else:
                    abs_pos = max(block_abs_start,
                                  _block_overflow_end.get(block_name, block_abs_start))
                
                while abs_pos < block_abs_end and abs_pos < viewport_adjusted_end + 3600:
                    # WARNING: loop_start_pos guards against an endless loop; every pass must advance abs_pos.
                    loop_start_pos = abs_pos
                    cycle_start = abs_pos
                    hit_block_boundary = False
                    _cycle_tl, _cycle_total, _crossing_end = media.guide_cycle_through_block_end(
                        block_timeline, total_content, cycle_start, block_abs_end)
                    if _crossing_end is not None:
                        _nb = _next_block_map.get(block_name)
                        if _nb is not None:
                            _block_overflow_end[_nb] = max(
                                _block_overflow_end.get(_nb, 0), _crossing_end)
                    for entry in _cycle_tl:
                        entry_abs_start = cycle_start + entry["start_time"]
                        entry_abs_end = entry_abs_start + entry["duration"]

                        timeline_entries.append((entry, entry_abs_start, entry_abs_end, block_name))

                        if entry_abs_end >= viewport_adjusted_end + 3600:
                            abs_pos = entry_abs_end
                            break
                    else:
                        if _crossing_end is not None:
                            hit_block_boundary = True
                            abs_pos = _crossing_end
                        else:
                            abs_pos = cycle_start + total_content
                        if _crossing_end is None and abs_pos < block_abs_end:
                            _next_tl, _next_tc, _guide_cycle_sp = get_block_timeline(
                                block_name, _override_sp=_guide_cycle_sp)
                            if _next_tl and _next_tc > 0:
                                block_timeline = _next_tl
                                total_content  = _next_tc

                    if hit_block_boundary:
                        break
                    
                    if abs_pos <= loop_start_pos:
                        break
                    
                    if abs_pos < block_abs_end:
                        continue
                    else:
                        break

              if _tl_hit is None and _is_full_day_guide:
                  _DAY_SPAN = 86400
                  _base_day_start = _fd_anchor_secs if _fd_anchor_secs is not None else 0
                  _base_day_end = _DAY_SPAN
                  _base_day_entries = [
                      (_e, _es, _ee, _eb) for _e, _es, _ee, _eb in timeline_entries
                      if _e is not None
                  ]
                  _base_overflow = max(0, _block_overflow_end.get("Full Day", _base_day_end) - _base_day_end)
                  _real_day_len = max(1, _base_day_end + _base_overflow - _base_day_start)
                  _guide_span_end = min(viewport_adjusted_end + 3600,
                                         GUIDE_TOTAL_HALF_HOURS * 1800)
                  _day_start = _base_day_end + _base_overflow
                  _day_num = 2
                  while _day_start < _guide_span_end and _day_num <= 7:
                      _shift = _day_start - _base_day_start
                      _shifted = []
                      for _e, _es, _ee, _eb in _base_day_entries:
                          _ns, _ne = _es + _shift, _ee + _shift
                          if _ne > viewport_adjusted_start and _ns < _guide_span_end:
                              _shifted.append((_e, _ns, _ne, _eb))
                      timeline_entries = timeline_entries + _shifted
                      _day_start += _real_day_len
                      _day_num += 1

            if _tl_hit is None:
                timeline_entries = _guide_splice_aired(
                    timeline_entries,
                    _guide_aired_rows_in_view(
                        _guide_hub_future_cells(ch_str, now_dt, viewport_adjusted_start, viewport_adjusted_end),
                        viewport_adjusted_start, viewport_adjusted_end))
            if _tl_hit is None and not _is_holiday_guide:
                timeline_entries = _guide_splice_aired(
                    timeline_entries,
                    _guide_aired_rows_in_view(
                        _guide_hub_aired_cells(ch_str, now_dt, viewport_adjusted_start, viewport_adjusted_end),
                        viewport_adjusted_start, viewport_adjusted_end))

            if _tl_hit is None:
                timeline_entries = _guide_merge_cells(timeline_entries)
                if len(_guide_timeline_cache) > 80:
                    _guide_timeline_cache.clear()
                _guide_timeline_cache[_tl_key] = list(timeline_entries)

            _anchor = {} if ch_info.get("is_visualizer", False) else ch_info.get("playback_anchor", {})
            _ch_snaps_grid = bool(
                ch_info.get("commercials_enabled", False)
                and ch_info.get("commercial_placement", "interrupt_half_hour")
                    in ("interrupt_half_hour", "end_of_show"))
            _anchor_file = _anchor.get("file") if isinstance(_anchor, dict) else None
            _anchor_is_commercial = (isinstance(_anchor, dict)
                                     and _anchor.get("mode") == "Commercial")
            if _anchor_file and os.path.exists(str(_anchor_file)) and not _anchor_is_commercial:
                _now_secs = (
                    now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second
                    + now_dt.microsecond / 1_000_000.0
                )
                _wall_start = _anchor.get("wall_start", 0)
                _seek_off = _anchor.get("seek_offset", 0)
                _a_dur = _anchor.get("duration", 1800)
                if db is not None:
                    _probed_real_dur, _probed_kind = media.read_track_duration(_anchor_file, 0)
                    if _probed_kind == media.DURATION_MEASURED and _probed_real_dur and _probed_real_dur > _a_dur:
                        _a_dur = _probed_real_dur
                _elapsed = _now_secs - _wall_start
                if _elapsed < 0:
                    _elapsed += 86400
                _slot_start_seek_a = _anchor.get("slot_start_seek", 0)
                _seek_into_slot_a  = _seek_off - _slot_start_seek_a
                if (_a_dur - _seek_into_slot_a - _elapsed) > 0:
                    _a_start_abs_seg = _wall_start - _seek_into_slot_a
                    _a_end_abs = _a_start_abs_seg + _a_dur
                    while _a_start_abs_seg < _now_secs - 43200:
                        _a_start_abs_seg += 86400
                        _a_end_abs       += 86400
                    while _a_start_abs_seg > _now_secs + 43200:
                        _a_start_abs_seg -= 86400
                        _a_end_abs       -= 86400
                    for _idx in range(len(timeline_entries)):
                        _e, _es, _ee, _eb = timeline_entries[_idx]
                        if _e is None:
                            continue
                        _covers_now = (_es <= _now_secs < _ee) or (_es <= _now_secs + 86400 < _ee)
                        if not _covers_now:
                            continue
                        _now_is_wrong_title = not _guide_entry_airs_file(_e, _anchor_file)
                        _multi_parts = _guide_entry_parts(_e)
                        if len(_multi_parts) > 1 and not _now_is_wrong_title:
                            _want_a = os.path.normcase(os.path.normpath(str(_anchor_file)))
                            _before = 0.0
                            for _pp, _pd in _multi_parts:
                                if _pp == _want_a:
                                    break
                                _before += float(_pd or 0)
                            _a_start_abs = _a_start_abs_seg - _before
                            _shift = _a_start_abs - _es
                            _tail_start = _ee + _shift
                            if _ch_snaps_grid and (_tail_start % 1800) != 0:
                                _tail_start = ((_tail_start // 1800) + 1) * 1800
                            timeline_entries[_idx] = (_e, _a_start_abs, _tail_start, _anchor.get("block", _eb))
                            _cursor = _tail_start
                            _j = _idx + 1
                            while _j < len(timeline_entries):
                                _ne, _nes, _nee, _neb = timeline_entries[_j]
                                _seg_len = max(0, _nee - _nes)
                                timeline_entries[_j] = (_ne, _cursor, _cursor + _seg_len, _neb)
                                _cursor += _seg_len
                                _j += 1
                            break
                        if _now_is_wrong_title:
                            _now_entry = {
                                "path": _anchor_file, "duration": _a_dur,
                                "is_commercial": (_anchor.get("mode") == "Commercial"),
                            }
                            _a_start_abs = _a_start_abs_seg
                        else:
                            _now_entry = dict(_e)
                            _now_entry["duration"] = _a_dur
                            _a_start_abs = min(_es, _a_start_abs_seg)
                        _tail_start = _a_end_abs
                        if _ch_snaps_grid and (_tail_start % 1800) != 0:
                            _tail_start = ((_tail_start // 1800) + 1) * 1800
                        timeline_entries[_idx] = (
                            _now_entry, _a_start_abs, _tail_start,
                            _anchor.get("block", _eb))
                        _cursor = _tail_start
                        _j = _idx + 1
                        while _j < len(timeline_entries):
                            _ne, _nes, _nee, _neb = timeline_entries[_j]
                            _seg_len = _nee - _nes
                            if _seg_len < 0:
                                _seg_len = 0
                            timeline_entries[_j] = (_ne, _cursor, _cursor + _seg_len, _neb)
                            _cursor += _seg_len
                            _j += 1
                        break

            if not ch_info.get("is_visualizer", False):
                _guide_live_state = None
                if str(app_state.get("current_channel", "")).zfill(2) == ch_str:
                    _candidate_live = app_state.get("live_now_playing") or {}
                    if (isinstance(_candidate_live, dict)
                            and _candidate_live.get("channel") == ch_str
                            and _candidate_live.get("mode") in ("Video", "Commercial")):
                        _guide_live_state = _candidate_live

                if _guide_live_state is None:
                    _candidate_preview = app_state.get("guide_preview_live_state") or {}
                    if (isinstance(_candidate_preview, dict)
                            and _candidate_preview.get("channel") == ch_str
                            and _candidate_preview.get("mode") in ("Video", "Commercial")):
                        _guide_live_state = _candidate_preview

                if _guide_live_state is not None:
                    _lnp_file = _guide_live_state.get("file")
                    if _lnp_file and os.path.exists(str(_lnp_file)):
                        try:
                            _lnp_seek = max(0.0, float(_guide_live_state.get("seek", 0) or 0))
                        except (TypeError, ValueError) as _swallowed_error:
                            _log_swallowed("_render_tv_guide_impl: _lnp_seek = max(0.0, float(_guide_live_state.get('seek', 0) or 0))", _swallowed_error)
                            _lnp_seek = 0.0
                        _lnp_dur = int(_guide_live_state.get("duration", 0) or 0)
                        _lnp_real, _lnp_real_kind = media.read_track_duration(_lnp_file, 0)
                        if _lnp_real_kind == media.DURATION_MEASURED and _lnp_real and _lnp_real > 0:
                            _lnp_dur = max(_lnp_dur, int(_lnp_real))
                        if _lnp_dur <= 0:
                            _lnp_dur = 1800

                        _lnp_now_secs = _guide_live_state.get("_resolved_clock_seconds")
                        if _lnp_now_secs is None:
                            _lnp_now_secs = (
                                now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second
                                + now_dt.microsecond / 1_000_000.0
                            )
                        try:
                            _lnp_now_secs = float(_lnp_now_secs)
                        except (TypeError, ValueError) as _swallowed_error:
                            _log_swallowed("_render_tv_guide_impl: _lnp_now_secs = float(_lnp_now_secs)", _swallowed_error)
                            _lnp_now_secs = (
                                now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second
                            )

                        _lnp_mode = _guide_live_state.get("mode")
                        _lnp_is_commercial = (_lnp_mode == "Commercial")
                        _lnp_start = _lnp_now_secs - _lnp_seek
                        _lnp_end = _lnp_start + _lnp_dur
                        _lnp_found = False

                        _lnp_now_candidates = [_lnp_now_secs]
                        if _lnp_now_secs < 4 * 3600:
                            _lnp_now_candidates.append(_lnp_now_secs + 86400)

                        for _li in range(len(timeline_entries)):
                            _le, _les, _lee, _leb = timeline_entries[_li]
                            if _le is None:
                                continue
                            _matched_now = next(
                                (_candidate_now for _candidate_now in _lnp_now_candidates
                                 if _les <= _candidate_now < _lee),
                                None,
                            )
                            if _matched_now is None:
                                continue
                            _lnp_found = True
                            if _lnp_is_commercial and not (
                                    isinstance(_le, dict) and _le.get("is_commercial")):
                                break
                            _same_path = _guide_entry_airs_file(_le, _lnp_file)
                            _lnp_parts = _guide_entry_parts(_le)
                            if _same_path and len(_lnp_parts) > 1 and not _lnp_is_commercial:
                                _want_l = os.path.normcase(os.path.normpath(str(_lnp_file)))
                                _before_l = 0.0
                                for _pp, _pd in _lnp_parts:
                                    if _pp == _want_l:
                                        break
                                    _before_l += float(_pd or 0)
                                _new_start = _matched_now - _lnp_seek - _before_l
                                _lc = _lee + (_new_start - _les)
                                if _ch_snaps_grid and (_lc % 1800) != 0:
                                    _lc = ((_lc // 1800) + 1) * 1800
                                timeline_entries[_li] = (_le, _new_start, _lc, _leb)
                                _lj = _li + 1
                                while _lj < len(timeline_entries):
                                    _ne2, _nes2, _nee2, _neb2 = timeline_entries[_lj]
                                    _sl2 = max(0, _nee2 - _nes2)
                                    timeline_entries[_lj] = (_ne2, _lc, _lc + _sl2, _neb2)
                                    _lc += _sl2
                                    _lj += 1
                                break
                            if _same_path:
                                _lnp_entry = dict(_le)
                                _lnp_entry["duration"] = _lnp_dur
                                _lnp_entry["is_commercial"] = _lnp_is_commercial
                                _lnp_entry["_guide_live_override"] = True
                                _lnp_start = min(_les, _matched_now - _lnp_seek)
                            else:
                                _lnp_entry = {
                                    "path": _lnp_file,
                                    "duration": _lnp_dur,
                                    "is_commercial": _lnp_is_commercial,
                                    "_guide_live_override": True,
                                }
                                _lnp_start = _matched_now - _lnp_seek
                            _lnp_end = _lnp_start + _lnp_dur
                            _lc = _lnp_end
                            if _ch_snaps_grid and (_lc % 1800) != 0:
                                _lc = ((_lc // 1800) + 1) * 1800
                            timeline_entries[_li] = (
                                _lnp_entry, _lnp_start, _lc, _leb
                            )
                            _lj = _li + 1
                            while _lj < len(timeline_entries):
                                _ne2, _nes2, _nee2, _neb2 = timeline_entries[_lj]
                                _sl2 = max(0, _nee2 - _nes2)
                                timeline_entries[_lj] = (_ne2, _lc, _lc + _sl2, _neb2)
                                _lc += _sl2
                                _lj += 1
                            break

                        if not _lnp_found:
                            _lnp_now_abs = _lnp_now_secs
                            if (_guide_live_state.get("block") == "Night"
                                    and _lnp_now_secs < 4 * 3600):
                                _lnp_now_abs += 86400
                            _lnp_start = _lnp_now_abs - _lnp_seek
                            _lnp_end = _lnp_start + _lnp_dur
                            _lnp_entry = {
                                "path": _lnp_file,
                                "duration": _lnp_dur,
                                "is_commercial": _lnp_is_commercial,
                                "_guide_live_override": True,
                            }
                            timeline_entries.append((
                                _lnp_entry, _lnp_start, _lnp_end,
                                _guide_live_state.get("block", ""),
                            ))
                            timeline_entries.sort(key=lambda _row: _row[1])

            if not ch_info.get("is_visualizer", False):
                timeline_entries = _guide_fill_from_hub(timeline_entries, ch_str, now_dt, viewport_adjusted_start, viewport_adjusted_end, app_state)

            if not ch_info.get("is_visualizer", False):
                _cutoff_g = None
                if db is not None:
                    _since_g = db.config.get("guide_onair_since", {})
                    _rec_g = _since_g.get(ch_str)
                    _ty_g, _td_g = now_dt.year, now_dt.timetuple().tm_yday
                    if _rec_g and _rec_g[0] == _ty_g and _rec_g[1] == _td_g:
                        _cutoff_g = _rec_g[2]

                def _before_cutoff_g(_row):
                    if _cutoff_g is None or _row[2] is None:
                        return False
                    if (isinstance(_row[0], dict)
                            and _row[0].get("_guide_live_override")):
                        return False
                    return _row[2] <= _cutoff_g

                _gated_g = [
                    _row_g for _row_g in timeline_entries
                    if _row_g[0] is None or not _before_cutoff_g(_row_g)
                ]
                timeline_entries = _gated_g

            # WARNING: owner 2026-11-07: every half-hour grid of an off-air stretch is its own spot and its own stop; a stop's time is the start of that grid (or the end of the show before it when that falls inside the grid) and its key is that time, never a column number, which means something else once the guide scrolls.
            _last_show_end_abs = viewport_adjusted_start
            for _pre_show, _pre_start, _pre_end, _pre_block in timeline_entries:
                if _pre_show is not None and _pre_end <= viewport_adjusted_start:
                    _last_show_end_abs = _pre_end
            for show, show_abs_start, show_abs_end, block_name in timeline_entries:
                if show is None:
                    continue
                    
                if show_abs_end <= viewport_adjusted_start:
                    continue
                if show_abs_start >= viewport_adjusted_end:
                    break
                    
                has_rendered_any_video = True
                clip_start = max(viewport_adjusted_start, show_abs_start)
                clip_end = min(viewport_adjusted_end, show_abs_end)
                
                col_start_float = float(clip_start - viewport_adjusted_start) / 1800.0
                col_end_float = float(clip_end - viewport_adjusted_start) / 1800.0
                
                rounded_start_block_col = int(col_start_float + 1e-6)
                if rounded_start_block_col not in current_row_blocks_list:
                    current_row_blocks_list.append(rounded_start_block_col)
                current_row_stops_list.append({
                    "col": rounded_start_block_col,
                    "abs": show_abs_start,
                    "key": ("show", round(show_abs_start)),
                    "kind": "break" if (isinstance(show, dict) and show.get("is_commercial")) else "show",
                })
                
                cell_x = col_width + int(col_start_float * col_width + 1e-6)
                pixel_cell_width = int((col_end_float - col_start_float) * col_width)
                pixel_cell_width = max(pixel_cell_width, 2)
                
                if cell_x >= w:
                    _is_target_show = (is_highlighted_row and
                                        show_abs_start <= _guide_target_abs < show_abs_end)
                    if not _is_target_show:
                        continue
                    cell_x = w - 3
                
                current_draw_w = min(pixel_cell_width, w - cell_x)
                current_draw_w = max(current_draw_w, 1)
                
                if cell_x > last_drawn_pixel_x:
                    _gap_abs_start = last_drawn_abs_time
                    _oa_x = last_drawn_pixel_x
                    _oa_abs_cursor = _gap_abs_start
                    while _oa_x < cell_x and _oa_x < w:
                        # WARNING: keep the +1e-6 float-truncation guard identical to rounded_start_block_col.
                        _oa_col  = max(0, int(float(_oa_x - col_width) / col_width + 1e-6))
                        _oa_next = col_width + (_oa_col + 1) * col_width
                        _oa_end  = min(cell_x, _oa_next, w)
                        _oa_bw   = _oa_end - _oa_x
                        if _oa_bw <= 0:
                            break
                        if _oa_col not in current_row_blocks_list:
                            current_row_blocks_list.append(_oa_col)
                        _oa_seg_abs = max(_last_show_end_abs, viewport_adjusted_start + _oa_col * 1800)
                        _oa_seg_end = min(show_abs_start, viewport_adjusted_start + (_oa_col + 1) * 1800)
                        current_row_stops_list.append({
                            "col": _oa_col,
                            "abs": _oa_seg_abs,
                            "key": ("offair", round(_oa_seg_abs)),
                        })
                        _oa_abs_start = _oa_abs_cursor
                        _oa_col_boundary_abs = viewport_adjusted_start + (_oa_col + 1) * 1800
                        if _oa_end == cell_x:
                            _oa_abs_end = show_abs_start
                        else:
                            _oa_abs_end = _oa_col_boundary_abs
                        _oa_sel = (is_highlighted_row and
                                   _oa_seg_abs <= _guide_target_abs < _oa_seg_end)
                        _oa_bg  = HIGHLIGHT_BG if _oa_sel else UNSELECTED_BLUE
                        _oa_tc  = (0, 0, 0) if _oa_sel else TEXT_WHITE
                        pygame.draw.rect(surface, _oa_bg, (_oa_x, start_y, _oa_bw, row_height))
                        pygame.draw.rect(surface, CYAN_GRID, (_oa_x, start_y, _oa_bw, row_height), 1)
                        if _oa_x + 8 < w and _oa_bw > 30:
                            _lbl_oa = font_row_lg.render("OFF-AIR", True, _oa_tc)
                            surface.blit(_lbl_oa, (_oa_x + 8, start_y + (row_height // 2 - _lbl_oa.get_height() // 2)))
                        _oa_abs_cursor = _oa_abs_end
                        _oa_x = _oa_end
                
                is_selected_cell = (is_highlighted_row and
                                     show_abs_start <= _guide_target_abs < show_abs_end)
                cell_bg = HIGHLIGHT_BG if is_selected_cell else UNSELECTED_BLUE
                cell_txt_col = (0, 0, 0) if is_selected_cell else TEXT_WHITE
                
                pygame.draw.rect(surface, cell_bg, (cell_x, start_y, current_draw_w, row_height))
                pygame.draw.rect(surface, CYAN_GRID, (cell_x, start_y, current_draw_w, row_height), 1)
                
                if isinstance(show, dict) and show.get("is_commercial"):
                    cleaned_title = "COMMERCIAL BREAK"
                elif isinstance(show, dict) and show.get("is_pair"):
                    cleaned_title = media.guide_pair_title(show).upper()
                else:
                    cleaned_title = media.guide_display_title(show).upper() if isinstance(show, dict) else _clean_show_title(show).upper()
                
                if cell_x + 8 < w:
                    max_txt_w = current_draw_w - 15
                    if max_txt_w > 5:
                        if is_selected_cell:
                            full_lbl = font_row_lg.render(cleaned_title, True, cell_txt_col)
                            v_off = row_height // 2 - full_lbl.get_height() // 2
                            if full_lbl.get_width() > max_txt_w:
                                scroll_range = full_lbl.get_width() - max_txt_w
                                offset_x = int((pygame.time.get_ticks() * 0.04) % (scroll_range + 60))
                                if offset_x > scroll_range: offset_x = scroll_range
                                
                                text_sub = pygame.Surface((max_txt_w, row_height), pygame.SRCALPHA)
                                text_sub.blit(full_lbl, (-offset_x, v_off))
                                surface.blit(text_sub, (cell_x + 6, start_y))
                            else:
                                surface.blit(full_lbl, (cell_x + 8, start_y + v_off))
                        else:
                            max_chars = max(3, max_txt_w // 10)
                            if max_txt_w >= 15:
                                truncated_title = cleaned_title[:max_chars]
                                if len(truncated_title) < len(cleaned_title) and max_chars > 3:
                                    truncated_title = truncated_title[:-1] + "."
                                lbl_cell = font_row_lg.render(truncated_title, True, cell_txt_col)
                                surface.blit(lbl_cell, (cell_x + 8, start_y + (row_height // 2 - lbl_cell.get_height() // 2)))
                
                last_drawn_pixel_x = cell_x + pixel_cell_width
                last_drawn_abs_time = clip_end
                _last_show_end_abs = show_abs_end
                
            viewport_absolute_max_pixel_x = col_width + (num_cols * col_width)
            if last_drawn_pixel_x < viewport_absolute_max_pixel_x and has_rendered_any_video:
                _gap_abs_start = last_drawn_abs_time
                _tr_x = last_drawn_pixel_x
                _tr_abs_cursor = _gap_abs_start
                while _tr_x < viewport_absolute_max_pixel_x and _tr_x < w:
                    _tr_col  = max(0, int(float(_tr_x - col_width) / col_width))
                    _tr_next = col_width + (_tr_col + 1) * col_width
                    _tr_end  = min(viewport_absolute_max_pixel_x, _tr_next, w)
                    _tr_bw   = _tr_end - _tr_x
                    if _tr_bw <= 0:
                        break
                    if _tr_col not in current_row_blocks_list:
                        current_row_blocks_list.append(_tr_col)
                    _tr_seg_abs = max(_last_show_end_abs, viewport_adjusted_start + _tr_col * 1800)
                    _tr_seg_end = viewport_adjusted_start + (_tr_col + 1) * 1800
                    current_row_stops_list.append({
                        "col": _tr_col,
                        "abs": _tr_seg_abs,
                        "key": ("offair", round(_tr_seg_abs)),
                    })
                    _tr_abs_start = _tr_abs_cursor
                    _tr_abs_end = viewport_adjusted_start + (_tr_col + 1) * 1800
                    _tr_sel = (is_highlighted_row and
                               _tr_seg_abs <= _guide_target_abs < _tr_seg_end)
                    _tr_bg  = HIGHLIGHT_BG if _tr_sel else UNSELECTED_BLUE
                    _tr_tc  = (0, 0, 0) if _tr_sel else TEXT_WHITE
                    pygame.draw.rect(surface, _tr_bg, (_tr_x, start_y, _tr_bw, row_height))
                    pygame.draw.rect(surface, CYAN_GRID, (_tr_x, start_y, _tr_bw, row_height), 1)
                    if _tr_x + 8 < w and _tr_bw > 30:
                        _lbl_tr = font_row_lg.render("OFF-AIR", True, _tr_tc)
                        surface.blit(_lbl_tr, (_tr_x + 8, start_y + (row_height // 2 - _lbl_tr.get_height() // 2)))
                    _tr_x = _tr_end
            if not has_rendered_any_video:
                for fallback_col in range(num_cols):
                    if fallback_col not in current_row_blocks_list:
                        current_row_blocks_list.append(fallback_col)
                    _fb_abs = max(_last_show_end_abs, viewport_adjusted_start + fallback_col * 1800)
                    _fb_end = viewport_adjusted_start + (fallback_col + 1) * 1800
                    current_row_stops_list.append({
                        "col": fallback_col,
                        "abs": _fb_abs,
                        "key": ("offair", round(_fb_abs)),
                    })
                    is_selected_cell = (is_highlighted_row and
                                        _fb_abs <= _guide_target_abs < _fb_end)
                    cell_bg = HIGHLIGHT_BG if is_selected_cell else UNSELECTED_BLUE
                    cell_txt_col = (0, 0, 0) if is_selected_cell else TEXT_WHITE
                    cell_x = col_width + (fallback_col * col_width)
                    
                    if cell_x < w:
                        current_cell_w = min(col_width, w - cell_x)
                        pygame.draw.rect(surface, cell_bg, (cell_x, start_y, current_cell_w, row_height))
                        pygame.draw.rect(surface, CYAN_GRID, (cell_x, start_y, current_cell_w, row_height), 1)
                        if cell_x + 15 < w:
                            lbl_cell = font_row_lg.render("OFF-AIR", True, cell_txt_col)
                            surface.blit(lbl_cell, (cell_x + 15, start_y + (row_height // 2 - lbl_cell.get_height() // 2)))
            
            current_row_blocks_list.sort()
            # WARNING: a commercial break cell is only a few pixels wide, so the highlight must never rest on one; the breaks stay drawn but are not stops while the row has any other stop.
            if any(s.get("kind") != "break" for s in current_row_stops_list):
                current_row_stops_list = [s for s in current_row_stops_list if s.get("kind") != "break"]
            current_row_stops_list.sort(key=lambda s: s.get("abs", 0))
            import time as _time
            _guide_row_stops_cache[ch_str] = {
                "blocks": list(current_row_blocks_list),
                "stops": list(current_row_stops_list),
                "ts": _time.time(),
                "time_idx": time_idx,
            }
            if _row_cache_key is not None:
                _row_cache_store(_row_cache_key, start_y, current_row_blocks_list, current_row_stops_list)
            if is_highlighted_row:
                app_state["guide_current_row_blocks"] = current_row_blocks_list
                app_state["guide_current_row_stops"] = current_row_stops_list
                if app_state.get("_guide_stops_only_probe"):
                    return

    # ========================================================================
    # SECTION: Fit to target rect
    # ========================================================================

    if target_rect is not None:
        tx, ty = int(target_rect.x), int(target_rect.y)
        tw, th = max(1, int(target_rect.width)), max(1, int(target_rect.height))
    else:
        tx, ty = 0, 0
        tw, th = max(1, int(real_w)), max(1, int(real_h))

    # WARNING: must match the size and placement render_tv_guide_async scales the cached canvas to.
    disp_w = max(1, int(round(tw)))
    disp_h = max(1, int(round(th)))
    off_x = int(round(tx))
    off_y = int(round(ty))

    if present:
        real_surface.blit(pygame.transform.smoothscale(surface, (disp_w, disp_h)), (off_x, off_y))

    x_scale = disp_w / float(w)
    y_scale = disp_h / float(h)
    app_state["guide_preview_bounds"] = (
        int(off_x + px * x_scale),
        int(off_y + py * y_scale),
        max(1, int(pw * x_scale)),
        max(1, int(ph * y_scale)),
    )


# ============================================================================
# SECTION: Settings menu: caches & change detection
# ============================================================================


_cached_bg_gradient_surf = None
_cached_ui_gradient_surf = None
_cached_gradient_width = -1

# WARNING: the cache key must include everything that can change what is on screen.
_settings_menu_frame_cache = {"key": None, "stamp": None, "pos": None}


def _safe_state_hash(obj):
    """Hash a possibly nested or unhashable value for cache keys, falling back to its id."""
    try:
        return hash(repr(obj))
    except Exception:
        return id(obj)


_MENU_IRRELEVANT_CONFIG_KEYS = frozenset()


def _menu_relevant_config_hash(config):
    """Hash the config with the known background-cache keys removed."""
    if config is None:
        return None
    try:
        filtered = {k: v for k, v in config.items()
                    if k not in _MENU_IRRELEVANT_CONFIG_KEYS}
    except Exception as _swallowed_error:
        _log_swallowed("_menu_relevant_config_hash: filtered = {k: v for k, v in config.items() if k not in _MENU_IRREL...", _swallowed_error)
        return _safe_state_hash(config)
    return _safe_state_hash(filtered)


def _channel_menu_fingerprint(ch_info):
    """Return a small per-channel summary of the fields the settings menu reads, with entry counts instead of schedule content."""
    if not isinstance(ch_info, dict):
        return None
    scheds = ch_info.get("schedules", {})
    holiday = ch_info.get("holiday_schedules", {})
    sched_counts = tuple(sorted(
        (k, len(v) if isinstance(v, list) else 0)
        for k, v in scheds.items())) if isinstance(scheds, dict) else ()
    holiday_counts = tuple(sorted(
        (k, len(v) if isinstance(v, list) else 0)
        for k, v in holiday.items())) if isinstance(holiday, dict) else ()
    return (
        ch_info.get("active", True),
        ch_info.get("is_visualizer", False),
        ch_info.get("visualizer_style", "Random"),
        ch_info.get("reactive_visualizer_enabled", True),
        ch_info.get("reactive_visualizer_strength", 50),
        ch_info.get("name", ""),
        menu_nav.shown_scheduling_mode(ch_info),
        ch_info.get("block_mode", "full_day"),
        ch_info.get("episode_order_mode", "sequential"),
        ch_info.get("pair_threshold_minutes", 15),
        ch_info.get("commercials_enabled", False),
        ch_info.get("commercial_placement", "interrupt_half_hour"),
        ch_info.get("intros_enabled", False),
        ch_info.get("outros_enabled", False),
        ch_info.get("server_locked", False),
        sched_counts,
        holiday_counts,
    )


def _menu_relevant_channels_hash(channels_db, app_state):
    """Hash what the settings menu shows: each channel's (active, server_locked) pair plus a full fingerprint of the channels in view."""
    if not isinstance(channels_db, dict):
        return None
    grid = tuple(sorted(
        (k, v.get("active", True), v.get("server_locked", False))
        for k, v in channels_db.items() if isinstance(v, dict)))
    focus_keys = {
        str(app_state.get("selected_channel_row", "")).zfill(2),
        str(app_state.get("audio_track_source_channel", "")).zfill(2),
    }
    focus_prints = tuple(sorted(
        (k, _channel_menu_fingerprint(channels_db.get(k)))
        for k in focus_keys if k in channels_db))
    return (grid, focus_prints)


# ============================================================================
# SECTION: Settings menu: Advanced Settings scrolling labels
# ============================================================================


_advanced_scroll = {"row": None, "t0": 0.0, "seen": 0.0, "active": False}


def _advanced_menu_value(name, default):
    """Returns one Advanced Settings tunable from the governor table."""
    if performance_governor is None:
        return default
    return getattr(performance_governor, name, default)


def _marquee_offset(overflow_px, elapsed_s, speed_px, pause_s):
    """Returns how many pixels a too-long label is shifted left: rest, scroll to the end, rest, jump back, repeat."""
    if overflow_px <= 0 or speed_px <= 0:
        return 0
    travel_s = overflow_px / float(speed_px)
    cycle_s = pause_s + travel_s + pause_s
    t = elapsed_s % cycle_s
    if t < pause_s:
        return 0
    if t < pause_s + travel_s:
        return int((t - pause_s) * speed_px)
    return int(overflow_px)


def _advanced_scroll_tick(app_state):
    """Returns a number that changes at the scroll redraw rate while a highlighted Advanced Settings label is scrolling, else 0."""
    if app_state.get("menu_layer") != "SYSTEM_ADVANCED_SUB_MENU" or not _advanced_scroll["active"]:
        return 0
    steps = max(1, int(_advanced_menu_value("ADVANCED_MENU_SCROLL_STEPS_PER_SECOND", 20)))
    return int(time.time() * steps)


def _compute_settings_menu_cache_key(app_state, db, real_w, real_h):
    import time as _time
    channels_db = getattr(db, "channels_db", None) if db is not None else None
    return (
        real_w, real_h,
        app_state.get("active_menu_tab"),
        app_state.get("menu_layer"),
        app_state.get("menu_selection_index"),
        app_state.get("sub_menu_row_index"),
        app_state.get("sub_menu_col_index"),
        app_state.get("selected_channel_row"),
        app_state.get("channels_tab_page"),
        app_state.get("channel_name_all_selected"),
        app_state.get("editing_channel_name"),
        app_state.get("current_channel"),
        app_state.get("games_profile_num"),
        app_state.get("games_profile_row"),
        app_state.get("games_selected_console"),
        app_state.get("games_sub_row"),
        app_state.get("games_tab_page"),
        app_state.get("remote_remap_index"),
        app_state.get("_remap_reject_flash"),
        app_state.get("_remap_reject_key"),
        app_state.get("_games_menu_flash_msg"),
        app_state.get("_games_menu_flash_at"),
        app_state.get("audio_track_active_id"),
        app_state.get("audio_track_gain_db"),
        app_state.get("audio_track_context"),
        app_state.get("audio_track_source_channel"),
        app_state.get("dvd_audio_track_count"),
        _safe_state_hash(app_state.get("audio_track_list")),
        _menu_relevant_config_hash(db.config) if db is not None else None,
        _menu_relevant_channels_hash(channels_db, app_state),
        int(_time.time() * 2),
        _advanced_scroll_tick(app_state),
    )


# ============================================================================
# SECTION: Settings menu: entry points
# ============================================================================


def render_settings_overlay_menu(surface, theme, app_state, db):
    """Reuse the last finished menu surface when nothing it depends on changed, otherwise rebuild it."""
    real_w, real_h = surface.get_size()
    cache_key = _compute_settings_menu_cache_key(app_state, db, real_w, real_h)
    cached = _settings_menu_frame_cache
    if cached["key"] == cache_key and cached["stamp"] is not None:
        surface.blit(cached["stamp"], cached["pos"])
        return

    stamp, pos = _build_settings_overlay_menu(surface, theme, app_state, db)
    _settings_menu_frame_cache["key"] = cache_key
    _settings_menu_frame_cache["stamp"] = stamp
    _settings_menu_frame_cache["pos"] = pos
    surface.blit(stamp, pos)


# WARNING: the fallback numbers below must match OPTIONS_TOGGLE_BOX_W_PX, REMOTE_REMAP_LABEL_COL_W_PX, REMOTE_REMAP_KEY_BOX_W_PX, REMOTE_REMAP_KEY_BOX_H_PX, REMOTE_REMAP_COL_GAP_PX, CHANNEL_SETTINGS_HEADER_GAP_PX, CHANNEL_SETTINGS_BOTTOM_RESERVED_PX, CHANNEL_SETTINGS_MIN_TOP_PX, CHANNEL_SETTINGS_TITLE_TOP_PX, HOLIDAY_ROWS_TOP_PX, HOLIDAY_ROWS_BOTTOM_PX, HOLIDAY_ROW_MIN_H_PX, HOLIDAY_ROW_MAX_H_PX, HOLIDAY_ROW_SLOT_PAD_PX, HOLIDAY_TOGGLE_W_PX, HOLIDAY_TOGGLE_GAP_PX, SUB_MENU_ROW_WIDTH_SHARE, SUB_MENU_ROW_GAP_PX, VIDEO_SLIDER_ROW_RESERVE_PX, VIDEO_SLIDER_SPACING_MAX_PX, VIDEO_SLIDER_SPACING_MIN_PX, THEME_SLIDER_LAST_BLOCK_RESERVE_PX, THEME_SLIDER_SPACING_MAX_PX, THEME_SLIDER_SPACING_MIN_PX, THEME_SLIDER_FIRST_GAP_MIN_PX, THEME_SLIDER_FIRST_GAP_TRIM_PX in performance_governor.py; they are used only when that module fails to import.
OPTIONS_TOGGLE_BOX_W_PX = performance_governor.OPTIONS_TOGGLE_BOX_W_PX if performance_governor is not None else 18 + 24
REMOTE_REMAP_LABEL_COL_W_PX = performance_governor.REMOTE_REMAP_LABEL_COL_W_PX if performance_governor is not None else 195
REMOTE_REMAP_KEY_BOX_W_PX = performance_governor.REMOTE_REMAP_KEY_BOX_W_PX if performance_governor is not None else 110
REMOTE_REMAP_KEY_BOX_H_PX = performance_governor.REMOTE_REMAP_KEY_BOX_H_PX if performance_governor is not None else 30
REMOTE_REMAP_COL_GAP_PX = performance_governor.REMOTE_REMAP_COL_GAP_PX if performance_governor is not None else 30
CHANNEL_SETTINGS_HEADER_GAP_PX = performance_governor.CHANNEL_SETTINGS_HEADER_GAP_PX if performance_governor is not None else 40
CHANNEL_SETTINGS_BOTTOM_RESERVED_PX = performance_governor.CHANNEL_SETTINGS_BOTTOM_RESERVED_PX if performance_governor is not None else 70
CHANNEL_SETTINGS_MIN_TOP_PX = performance_governor.CHANNEL_SETTINGS_MIN_TOP_PX if performance_governor is not None else 20
CHANNEL_SETTINGS_TITLE_TOP_PX = performance_governor.CHANNEL_SETTINGS_TITLE_TOP_PX if performance_governor is not None else 20
HOLIDAY_ROWS_TOP_PX = performance_governor.HOLIDAY_ROWS_TOP_PX if performance_governor is not None else 60
HOLIDAY_ROWS_BOTTOM_PX = performance_governor.HOLIDAY_ROWS_BOTTOM_PX if performance_governor is not None else 62
HOLIDAY_ROW_MIN_H_PX = performance_governor.HOLIDAY_ROW_MIN_H_PX if performance_governor is not None else 34
HOLIDAY_ROW_MAX_H_PX = performance_governor.HOLIDAY_ROW_MAX_H_PX if performance_governor is not None else 56
HOLIDAY_ROW_SLOT_PAD_PX = performance_governor.HOLIDAY_ROW_SLOT_PAD_PX if performance_governor is not None else 8
HOLIDAY_TOGGLE_W_PX = performance_governor.HOLIDAY_TOGGLE_W_PX if performance_governor is not None else 190
HOLIDAY_TOGGLE_GAP_PX = performance_governor.HOLIDAY_TOGGLE_GAP_PX if performance_governor is not None else 12
SUB_MENU_ROW_WIDTH_SHARE = performance_governor.SUB_MENU_ROW_WIDTH_SHARE if performance_governor is not None else 0.6
SUB_MENU_ROW_GAP_PX = performance_governor.SUB_MENU_ROW_GAP_PX if performance_governor is not None else 40
VIDEO_SLIDER_ROW_RESERVE_PX = performance_governor.VIDEO_SLIDER_ROW_RESERVE_PX if performance_governor is not None else 40
VIDEO_SLIDER_SPACING_MAX_PX = performance_governor.VIDEO_SLIDER_SPACING_MAX_PX if performance_governor is not None else 70
VIDEO_SLIDER_SPACING_MIN_PX = performance_governor.VIDEO_SLIDER_SPACING_MIN_PX if performance_governor is not None else 40
THEME_SLIDER_LAST_BLOCK_RESERVE_PX = performance_governor.THEME_SLIDER_LAST_BLOCK_RESERVE_PX if performance_governor is not None else 50
THEME_SLIDER_SPACING_MAX_PX = performance_governor.THEME_SLIDER_SPACING_MAX_PX if performance_governor is not None else 88
THEME_SLIDER_SPACING_MIN_PX = performance_governor.THEME_SLIDER_SPACING_MIN_PX if performance_governor is not None else 50
THEME_SLIDER_FIRST_GAP_MIN_PX = performance_governor.THEME_SLIDER_FIRST_GAP_MIN_PX if performance_governor is not None else 38
THEME_SLIDER_FIRST_GAP_TRIM_PX = performance_governor.THEME_SLIDER_FIRST_GAP_TRIM_PX if performance_governor is not None else 18


def _build_settings_overlay_menu(surface, theme, app_state, db):
    """Does the actual (expensive) menu build: every tab, every row, then fit-scale to the target surface."""
    global _cached_bg_gradient_surf, _cached_ui_gradient_surf, _cached_gradient_width
    real_w, real_h = surface.get_size()

    # ========================================================================
    # SECTION: Canvas & tab-bar setup
    # ========================================================================

    w = MENU_CANVAS_W
    h = MENU_CANVAS_H
    
    menu_canvas = pygame.Surface((w, h), pygame.SRCALPHA)
    
    import colorsys
    bg_hue = db.config.get("theme_bg_hue", 220) / 360.0 if db is not None else 220/360.0
    ui_hue = db.config.get("theme_ui_hue", 140) / 360.0 if db is not None else 140/360.0
    r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.9, 1.0)
    NEON_GREEN = (int(r * 255), int(g * 255), int(b * 255))
    r, g, b = colorsys.hsv_to_rgb(bg_hue, 0.6, 0.15)
    MIDNIGHT_NAVY = (int(r * 255), int(g * 255), int(b * 255))
    DARK_BLUE = tuple(min(255, c + 5) for c in MIDNIGHT_NAVY)
    TEXT_WHITE = COLOR_TEXT_WHITE
    OFF_RED = COLOR_MENU_OFF_RED
    OFF_GREEN = COLOR_MENU_ON_GREEN
    
    box_w, box_h = w, h
    box_x, box_y = 0, 0
    
    slider_val = db.config.get("menu_opacity", 50) if db is not None else 50
    menu_opacity = int((slider_val / 100.0) * 255) 
    
    menu_overlay = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
    menu_overlay.fill(MIDNIGHT_NAVY + (menu_opacity,))
    menu_canvas.blit(menu_overlay, (box_x, box_y))
    
    pygame.draw.rect(menu_canvas, NEON_GREEN, (box_x, box_y, box_w, box_h), 3)
    
    f_header = _get_font("Courier New", 44, bold=True)
    lbl_title = f_header.render("MENU", True, COLOR_ARCADE_GOLD)
    menu_canvas.blit(lbl_title, (box_x + (box_w // 2) - (lbl_title.get_width() // 2), box_y + 15))
    
    game_ch_on = db.config.get("game_channel_enabled", True) if db is not None else False
    kiosk_on = db.config.get("kiosk_mode_enabled", False) if db is not None else False
    tabs_list = ["System", "Channels", "Games", "Video", "Theme"] if game_ch_on else ["System", "Channels", "Video", "Theme"]
    if kiosk_on:
        tabs_list = [t for t in tabs_list if t != "Channels"]

    global f_tab, f_body, f_ch_label, f_ch_btn, f_menu_lg
    f_tab = _get_font("Courier New", 17, bold=True)
    f_body = _get_font("Courier New", 17, bold=True)
    f_ch_label = _get_font("Courier New", 22, bold=True)
    f_ch_btn   = _get_font("Courier New", 19, bold=True)
    f_nav_tab = f_ch_label
    f_menu_lg = f_ch_label

    _tab_text_w = max(f_nav_tab.size(t)[0] for t in tabs_list)
    tab_w = _tab_text_w + 36
    tab_h = 52
    tab_gap = 8

    start_tab_x = box_x + (box_w // 2) - (((tab_w * len(tabs_list)) + (tab_gap * (len(tabs_list) - 1))) // 2)
    tab_y = box_y + 75

    active_tab = app_state.get("active_menu_tab", "System")
    layer = app_state.get("menu_layer", "TAB_SELECTION")
    cur_sel = app_state.get("menu_selection_index", 0)
    
    for i, tab_name in enumerate(tabs_list):
        tx = start_tab_x + i * (tab_w + tab_gap)
        is_focused_tab = (tab_name == active_tab)
        
        t_bg = DARK_BLUE if is_focused_tab else COLOR_MENU_BUTTON
        
        tab_surface = pygame.Surface((tab_w, tab_h), pygame.SRCALPHA)
        tab_surface.fill(t_bg + (menu_opacity,))
        menu_canvas.blit(tab_surface, (tx, tab_y))
        
        if is_focused_tab and layer == "TAB_SELECTION":
            _aa_rounded_rect(menu_canvas, NEON_GREEN, (tx, tab_y, tab_w, tab_h), SELECTION_BORDER_WIDTH, border_radius=4)
            
        lbl_t = f_nav_tab.render(tab_name, True, TEXT_WHITE)
        tab_txt_y_offset = (tab_h // 2) - (lbl_t.get_height() // 2)
        menu_canvas.blit(lbl_t, (tx + (tab_w // 2) - (lbl_t.get_width() // 2), tab_y + tab_txt_y_offset))
        
    content_y = tab_y + 63
    content_margin_x = 20
    content_w = box_w - (content_margin_x * 2) 
    content_h = box_h - 155
    cx = box_x + content_margin_x
    
    inner_panel_surface = pygame.Surface((content_w, content_h), pygame.SRCALPHA)
    inner_panel_surface.fill(DARK_BLUE + (menu_opacity,))
    menu_canvas.blit(inner_panel_surface, (cx, content_y))
    
    pygame.draw.rect(menu_canvas, NEON_GREEN, (cx, content_y, content_w, content_h), 1)

    box_h_43 = 32
    pill_w, pill_h = 60, 24
    _radio_dot_r = 9

    def _draw_footer_radio(x, y, label, selected, focused):
        """Draw a screen-mode column header: an unboxed dot to the left of the label."""
        dot_r = _radio_dot_r
        dot_x = x + dot_r
        dot_y = y + box_h_43 // 2
        if selected:
            pygame.draw.circle(menu_canvas, OFF_GREEN, (dot_x, dot_y), dot_r)
            pygame.draw.circle(menu_canvas, COLOR_MENU_DOT_RING, (dot_x, dot_y), dot_r, 1)
        else:
            pygame.draw.circle(menu_canvas, COLOR_MENU_VALUE_BOX, (dot_x, dot_y), dot_r)
            pygame.draw.circle(menu_canvas, COLOR_MENU_DOT_EDGE, (dot_x, dot_y), dot_r, 2)
        if focused:
            pygame.draw.circle(menu_canvas, NEON_GREEN, (dot_x, dot_y), dot_r + 3, SELECTION_BORDER_WIDTH)
        lbl_surf = f_ch_label.render(label, True, TEXT_WHITE)
        lbl_x = dot_x + dot_r + 10
        lbl_y = y + (box_h_43 - lbl_surf.get_height()) // 2
        menu_canvas.blit(lbl_surf, (lbl_x, lbl_y))
        return lbl_x + lbl_surf.get_width()

    # ========================================================================
    # SECTION: System tab: main list
    # ========================================================================

    if active_tab == "System" and layer not in ("REMOTE_REMAP_LIST", "REMOTE_REMAP_CAPTURE", "NUMBER_REMAP_CAPTURE", "SERVER_MENU", "SYSTEM_ADVANCED_SUB_MENU"):

        toggle_w = 55
        toggle_h = 24
        pill_radius = 12
        handle_rad = 9
        focus_thickness = SELECTION_BORDER_WIDTH

        label_padding_x = 35

        sys_rows = get_system_menu_rows(db, app_state)
        toggle_rows = [r for r in sys_rows if SYSTEM_ROW_META[r]["kind"] == "toggle"]
        button_rows = [r for r in sys_rows if SYSTEM_ROW_META[r]["kind"] == "button"]

        LEFT_TOGGLE_IDS  = ("game_ch", "guide", "controls_on_start", "kiosk_mode", "bezel_toggle", "warp_screen", "boot_anim")
        RIGHT_TOGGLE_IDS = ("loudness_scan", "pixel_orbiter", "captions_toggle", "mono_stereo_toggle", "static_noise", "reception_lines", "scan_lines")
        left_toggle_rows  = [r for r in toggle_rows if r in LEFT_TOGGLE_IDS]
        right_toggle_rows = [r for r in toggle_rows if r in RIGHT_TOGGLE_IDS]

        left_col_x  = cx
        right_col_x = cx + content_w // 2 + 10

        LEFT_COL_TOGGLE_OFFSET  = OPTIONS_LEFT_TOGGLE_OFFSET_PX
        RIGHT_COL_TOGGLE_OFFSET = OPTIONS_RIGHT_TOGGLE_OFFSET_PX

        current_values = {
            "game_ch":           db.config.get("game_channel_enabled", True) if db is not None else False,
            "guide":             db.config.get("tv_guide_enabled", True) if db is not None else True,
            "controls_on_start": db.config.get("show_controls_on_launch", True) if db is not None else True,
            "boot_on_start":     db.config.get("start_on_boot", False) if db is not None else False,
            "kiosk_mode":        db.config.get("kiosk_mode_enabled", False) if db is not None else False,
            "server_option":     db.config.get("server_mode_enabled", False) if db is not None else False,
            "loudness_scan":     db.config.get("loudness_scan_enabled", True) if db is not None else True,
            "pixel_orbiter":     db.config.get("pixel_orbiter_enabled", True) if db is not None else True,
            "captions_toggle":   (db.config.get("tv_captions_enabled", False) and not _mode_locks("tv_captions_enabled")) if db is not None else False,
            "mono_stereo_toggle": (not db.config.get("audio_stereo", True)) if db is not None else False,
        }
        for _kiosk_row, _kiosk_key in SYSTEM_KIOSK_SETTING_KEYS.items():
            current_values[_kiosk_row] = bool(db.config.get(_kiosk_key, False)) if db is not None else False

        _opt_box_w = OPTIONS_TOGGLE_BOX_W_PX

        toggle_start_y = 40
        bottom_reserved = 130
        available_h = max(45, content_h - toggle_start_y - bottom_reserved)
        toggle_gap = min(95, available_h / max(1, len(left_toggle_rows)))

        def _draw_system_toggle_row(row_id, col_x, row_y_offset, toggle_offset):
            toggle_x = col_x + toggle_offset
            _opt_box_x = toggle_x + toggle_w + 80
            row_idx = sys_rows.index(row_id)
            is_focused = (layer == "SYSTEM_ROWS" and cur_sel == row_idx)
            row_enabled = current_values[row_id]
            if row_id == "server_option" and not UPDATER_AND_SERVER_FEATURES_ENABLED:
                row_enabled = False
                is_focused = False
            if row_id == "server_option" and layer == "SYSTEM_ROWS" and cur_sel == SERVER_MENU_BTN_SEL:
                is_focused = False
            if row_id == "captions_toggle" and layer == "SYSTEM_ROWS" and \
                    cur_sel == CAPTIONS_COLOR_BOX_SEL:
                is_focused = False

            _kiosk_key = SYSTEM_KIOSK_SETTING_KEYS.get(row_id)
            _mode_locked_row = (row_id == "captions_toggle" and _mode_locks("tv_captions_enabled")) or bool(
                _kiosk_key and (_mode_locks(_kiosk_key) or (row_id == "warp_screen" and _warp_needs_a_bezel(db))))
            if _mode_locked_row and _kiosk_key:
                row_enabled = False
            _row_label_col = COLOR_MENU_TEXT_GREYED if ((row_id == "server_option"
                              and not UPDATER_AND_SERVER_FEATURES_ENABLED) or _mode_locked_row) else TEXT_WHITE
            row_label = f_ch_label.render(SYSTEM_ROW_META[row_id]["label"], True, _row_label_col)
            menu_canvas.blit(row_label, (col_x + label_padding_x, content_y + row_y_offset))

            toggle_y = content_y + row_y_offset - 3

            if row_id == "server_option":
                _srv_btn_focused = (layer == "SYSTEM_ROWS" and cur_sel == SERVER_MENU_BTN_SEL
                                     and UPDATER_AND_SERVER_FEATURES_ENABLED)
                _srv_mode_on = current_values.get("server_option", False) and \
                    UPDATER_AND_SERVER_FEATURES_ENABLED
                _srv_btn_txt_col = TEXT_WHITE if _srv_mode_on else COLOR_MENU_TEXT_GREYED
                _srv_btn_lbl = f_ch_btn.render("Menu", True, _srv_btn_txt_col)
                _srv_btn_w = _srv_btn_lbl.get_width() + 28
                _srv_btn_h = box_h_43
                _opt_box_center_x = _opt_box_x + _opt_box_w // 2
                _srv_btn_x = _opt_box_center_x - _srv_btn_w // 2
                _srv_btn_y = toggle_y - (_srv_btn_h - toggle_h) // 2
                _srv_btn_col = COLOR_MENU_BUTTON_BLUE if _srv_mode_on else COLOR_MENU_BUTTON_GREYED
                pygame.draw.rect(menu_canvas, _srv_btn_col,
                                 (_srv_btn_x, _srv_btn_y, _srv_btn_w, _srv_btn_h), border_radius=5)
                if _srv_btn_focused:
                    pygame.draw.rect(menu_canvas, NEON_GREEN,
                                     (_srv_btn_x, _srv_btn_y, _srv_btn_w, _srv_btn_h), SELECTION_BORDER_WIDTH, border_radius=5)
                menu_canvas.blit(_srv_btn_lbl,
                                 (_srv_btn_x + (_srv_btn_w - _srv_btn_lbl.get_width()) // 2,
                                  _srv_btn_y + (_srv_btn_h - _srv_btn_lbl.get_height()) // 2))

            sw_bg = COLOR_MENU_BUTTON_GREYED if _mode_locked_row else (OFF_GREEN if row_enabled else COLOR_MENU_SWITCH_OFF)
            pygame.draw.rect(menu_canvas, sw_bg, (toggle_x, toggle_y, toggle_w, toggle_h), border_radius=pill_radius)
            if is_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (toggle_x, toggle_y, toggle_w, toggle_h), focus_thickness, border_radius=pill_radius)
            handle_x = toggle_x + toggle_w - 13 if row_enabled else toggle_x + 12
            pygame.draw.circle(menu_canvas, COLOR_MENU_DISABLED_GREY if _mode_locked_row else TEXT_WHITE,
                               (handle_x, toggle_y + toggle_h // 2), handle_rad)
            if is_focused:
                pygame.draw.circle(menu_canvas, NEON_GREEN, (handle_x, toggle_y + toggle_h // 2), handle_rad + 2, SELECTION_BORDER_WIDTH)

            if row_id == "captions_toggle":
                from game_deck import LIST_TEXT_COLOR_NAMES, LIST_TEXT_COLOR_RGB
                _captions_on = current_values.get("captions_toggle", False)
                _captions_color_val = db.config.get("tv_captions_color", "white") if db is not None else "white"
                if _captions_color_val not in LIST_TEXT_COLOR_NAMES:
                    _captions_color_val = "white"
                _ccolor_swatch_size = 18
                _ccolor_focused = layer == "SYSTEM_ROWS" and cur_sel == CAPTIONS_COLOR_BOX_SEL
                _ccolor_bg = COLOR_MENU_BUTTON_BLUE if _captions_on else COLOR_MENU_BUTTON_GREYED
                pygame.draw.rect(menu_canvas, _ccolor_bg,
                                 (_opt_box_x, toggle_y, _opt_box_w, box_h_43), border_radius=5)
                if _ccolor_focused:
                    pygame.draw.rect(menu_canvas, NEON_GREEN,
                                     (_opt_box_x, toggle_y, _opt_box_w, box_h_43), SELECTION_BORDER_WIDTH, border_radius=5)
                _ccolor_swatch_x = _opt_box_x + (_opt_box_w - _ccolor_swatch_size) // 2
                _ccolor_swatch_y = toggle_y + (box_h_43 - _ccolor_swatch_size) // 2
                _ccolor_swatch_rgb = LIST_TEXT_COLOR_RGB.get(_captions_color_val, COLOR_TEXT_WHITE) \
                    if _captions_on else COLOR_MENU_SWATCH_EDGE
                pygame.draw.rect(menu_canvas, _ccolor_swatch_rgb,
                                 (_ccolor_swatch_x, _ccolor_swatch_y, _ccolor_swatch_size, _ccolor_swatch_size), border_radius=4)
                pygame.draw.rect(menu_canvas, COLOR_MENU_SWATCH_EDGE,
                                 (_ccolor_swatch_x, _ccolor_swatch_y, _ccolor_swatch_size, _ccolor_swatch_size), 1, border_radius=4)

        for t_idx, row_id in enumerate(left_toggle_rows):
            _draw_system_toggle_row(row_id, left_col_x, int(toggle_start_y + (t_idx * toggle_gap)), LEFT_COL_TOGGLE_OFFSET)

        for t_idx, row_id in enumerate(right_toggle_rows):
            _draw_system_toggle_row(row_id, right_col_x, int(toggle_start_y + (t_idx * toggle_gap)), RIGHT_COL_TOGGLE_OFFSET)

        box_radius = 4
        focus_thickness = SELECTION_BORDER_WIDTH
        label_padding_x = 35

        btn_h = 40
        btn_pad_x = 28
        btn_gap = 20
        bx_left = cx + label_padding_x
        btn_text_y_offset_base = (btn_h // 2)

        bottom_row_y = content_y + content_h - 52

        import sys as _sys
        _main = _sys.modules.get("__main__")
        _any_settings_to_reset_fn = getattr(_main, "_any_settings_to_reset", None) if _main else None
        _reset_has_content = bool(_any_settings_to_reset_fn()) if _any_settings_to_reset_fn else True

        _standalone_id = "advanced_settings" if "advanced_settings" in sys_rows else ("update_check" if "update_check" in sys_rows else None)
        poweroff_standalone = "poweroff" in sys_rows and _standalone_id is not None
        bottom_left_button_rows = [r for r in button_rows
                                    if r != _standalone_id
                                    and not (r == "poweroff" and poweroff_standalone)]

        _bx = bx_left
        for row_id in bottom_left_button_rows:
            row_idx = sys_rows.index(row_id)
            is_btn_highlighted = (layer == "SYSTEM_ROWS" and cur_sel == row_idx)
            if row_id == "update_check":
                btn_txt = _update_button_text(app_state)
            elif row_id == "bezel_style_select":
                try:
                    _bs_val = max(1, min(int(db.config.get("bezel_style", 1)), media.bezel_style_count()))
                except (TypeError, ValueError, AttributeError) as _swallowed_error:
                    _log_swallowed("render_settings_overlay_menu: bezel style button number", _swallowed_error)
                    _bs_val = 1
                btn_txt = "Bezel Style: Option %d" % _bs_val
            else:
                btn_txt = SYSTEM_ROW_META[row_id]["label"]

            if row_id == "bezel_style_select" and not (db is not None and db.config.get("fake_43_border_enabled", False)):
                b_color = COLOR_MENU_BUTTON_GREYED
                btn_txt_col = COLOR_MENU_TEXT_GREYED
            elif row_id == "reset_settings" and not _reset_has_content:
                b_color = COLOR_MENU_BUTTON_GREYED
                btn_txt_col = COLOR_MENU_TEXT_GREYED
            elif row_id == "update_check" and not UPDATER_AND_SERVER_FEATURES_ENABLED:
                b_color = COLOR_MENU_BUTTON_GREYED
                btn_txt_col = COLOR_MENU_TEXT_GREYED
            else:
                b_color = SYSTEM_ROW_META[row_id].get("color", OFF_RED)
                btn_txt_col = TEXT_WHITE
            lbl_b = f_ch_btn.render(btn_txt, True, btn_txt_col)
            btn_w = lbl_b.get_width() + (btn_pad_x * 2)

            pygame.draw.rect(menu_canvas, b_color, (_bx, bottom_row_y, btn_w, btn_h), border_radius=box_radius)
            if is_btn_highlighted:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (_bx, bottom_row_y, btn_w, btn_h), focus_thickness, border_radius=box_radius)

            menu_canvas.blit(lbl_b, (_bx + (btn_w // 2) - (lbl_b.get_width() // 2),
                                      bottom_row_y + btn_text_y_offset_base - (lbl_b.get_height() // 2)))
            _bx += btn_w + btn_gap

        _standalone_order = [_standalone_id] if _standalone_id else []
        if _standalone_order:
            _left_col_bottom_y = content_y + toggle_start_y + (max(0, len(left_toggle_rows) - 1) * toggle_gap) + toggle_h
            _standalone_gap = bottom_row_y - _left_col_bottom_y
            _h = btn_h
            _y = int(_left_col_bottom_y + max(0, (_standalone_gap - _h) // 2))
            _x = bx_left
            for _row_id in _standalone_order:
                _row_idx = sys_rows.index(_row_id)
                _lbl_txt = _gpu_render_mode_label(db) if _row_id == "gpu_render_mode" else (
                    _update_button_text(app_state) if _row_id == "update_check" else SYSTEM_ROW_META[_row_id]["label"])
                _gpu_disabled = (_row_id == "gpu_render_mode" and not GPU_RENDER_MODE_ENABLED)
                _row_disabled = _gpu_disabled or (_row_id == "update_check" and not UPDATER_AND_SERVER_FEATURES_ENABLED)
                _focused = (layer == "SYSTEM_ROWS" and cur_sel == _row_idx)
                _txt_col = COLOR_MENU_TEXT_GREYED if _row_disabled else TEXT_WHITE
                _lbl = f_ch_btn.render(_lbl_txt, True, _txt_col)
                _w = _lbl.get_width() + (btn_pad_x * 2)
                _color = COLOR_MENU_BUTTON_GREYED if _row_disabled else SYSTEM_ROW_META[_row_id].get("color", OFF_RED)
                pygame.draw.rect(menu_canvas, _color, (_x, _y, _w, _h), border_radius=box_radius)
                if _focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (_x, _y, _w, _h), focus_thickness, border_radius=box_radius)
                menu_canvas.blit(_lbl, (_x + (_w // 2) - (_lbl.get_width() // 2),
                                         _y + (_h // 2) - (_lbl.get_height() // 2)))
                _x += _w + btn_gap

        if poweroff_standalone:
            _po_row_idx = sys_rows.index("poweroff")
            _po_focused = (layer == "SYSTEM_ROWS" and cur_sel == _po_row_idx)
            _po_lbl = f_ch_btn.render(SYSTEM_ROW_META["poweroff"]["label"], True, TEXT_WHITE)
            _po_w = _po_lbl.get_width() + (btn_pad_x * 2)
            _po_h = btn_h
            _right_col_bottom_y = content_y + toggle_start_y + (max(0, len(right_toggle_rows) - 1) * toggle_gap) + toggle_h
            _po_gap = bottom_row_y - _right_col_bottom_y
            _po_y = int(_right_col_bottom_y + max(0, (_po_gap - _po_h) // 2))
            _po_x = right_col_x + label_padding_x
            _po_color = SYSTEM_ROW_META["poweroff"].get("color", OFF_RED)
            pygame.draw.rect(menu_canvas, _po_color, (_po_x, _po_y, _po_w, _po_h), border_radius=box_radius)
            if _po_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (_po_x, _po_y, _po_w, _po_h), focus_thickness, border_radius=box_radius)
            menu_canvas.blit(_po_lbl, (_po_x + (_po_w // 2) - (_po_lbl.get_width() // 2),
                                        _po_y + (_po_h // 2) - (_po_lbl.get_height() // 2)))

        sys_rows_close_idx = len(sys_rows)
        is_cl_focused = (layer == "SYSTEM_ROWS" and cur_sel == sys_rows_close_idx)
        close_btn_y = bottom_row_y

        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_cl_focused, f_ch_btn, NEON_GREEN)

    # ========================================================================
    # SECTION: System tab: advanced settings
    # ========================================================================

    elif active_tab == "System" and layer == "SYSTEM_ADVANCED_SUB_MENU":

        f_sub_title = _get_font("Courier New", 20, bold=True)
        lbl_hdr = f_sub_title.render("Advanced Settings", True, TEXT_WHITE)

        _as_rows = get_advanced_settings_menu_rows(db, app_state)
        _as_cur_row = app_state.get("menu_selection_index", 0)
        _as_close_idx = len(_as_rows)

        _as_toggle_labels = {
            "server_option": "Server Mode:",
            "boot_on_start": "Custom Shell Launcher:",
        }
        _as_button_labels = {
            "update_check": None,
            "gpu_render_mode": None,
            "performance_mode": None,
            "remote_remap": SYSTEM_ROW_META["remote_remap"]["label"],
        }
        if app_state.get("_update_check_inflight", False):
            _as_update_txt = "Checking..."
        elif app_state.get("_update_ready", False):
            _as_update_txt = "Install Update"
        elif app_state.get("_update_available", False):
            _as_update_txt = "Update Now"
        elif app_state.get("_update_up_to_date", False):
            _as_update_txt = "Up to Date"
        elif "not configured" in str(app_state.get("_update_status", "")).lower():
            _as_update_txt = "Updates Unconfigured"
        elif "failed" in str(app_state.get("_update_status", "")).lower():
            _as_update_txt = "Retry Updates"
        else:
            _as_update_txt = SYSTEM_ROW_META["update_check"]["label"]
        if not UPDATER_AND_SERVER_FEATURES_ENABLED:
            _as_update_txt = "Updates Disabled"
        _as_button_labels["update_check"] = _as_update_txt
        _as_button_labels["gpu_render_mode"] = _gpu_render_mode_label(db)
        _as_button_labels["performance_mode"] = _performance_mode_label(db)

        _as_server_on = bool(db.config.get("server_mode_enabled", False)) and UPDATER_AND_SERVER_FEATURES_ENABLED
        _as_shell_on = bool(db.config.get("start_on_boot", False))

        _as_bar_w = int(_advanced_menu_value("ADVANCED_MENU_BAR_WIDTH", 360))
        _as_text_pad = int(_advanced_menu_value("ADVANCED_MENU_TEXT_PAD", 18))
        _as_side_gap = int(_advanced_menu_value("ADVANCED_MENU_SIDE_GAP", 20))
        _as_header_gap = int(_advanced_menu_value("ADVANCED_MENU_HEADER_GAP", 40))
        _as_bottom_reserved = int(_advanced_menu_value("ADVANCED_MENU_BOTTOM_RESERVED", 70))
        _as_row_gap_want = int(_advanced_menu_value("ADVANCED_MENU_ROW_GAP", 20))
        _as_row_gap_min = int(_advanced_menu_value("ADVANCED_MENU_ROW_GAP_MIN", 8))
        _as_scroll_speed = float(_advanced_menu_value("ADVANCED_MENU_SCROLL_SPEED_PX", 55))
        _as_scroll_pause = float(_advanced_menu_value("ADVANCED_MENU_SCROLL_PAUSE_SECONDS", 1.0))

        btn_h = box_h_43 + 8
        _as_row_h = 44

        _as_wm_sel_idx = _as_rows.index("server_menu_open") if "server_menu_open" in _as_rows else None
        _as_entries = [(_i, _rid) for _i, _rid in enumerate(_as_rows) if _rid != "server_menu_open"]
        _as_count = len(_as_entries)

        _as_room = content_h - _as_bottom_reserved - lbl_hdr.get_height() - _as_header_gap - _as_count * _as_row_h
        if _as_count < 2:
            _as_row_gap = _as_row_gap_want
        else:
            _as_row_gap = max(_as_row_gap_min, min(_as_row_gap_want, _as_room // (_as_count - 1)))
        _as_block_h = lbl_hdr.get_height() + _as_header_gap + _as_count * _as_row_h + max(0, _as_count - 1) * _as_row_gap
        _as_block_top = content_y + max(20, (content_h - _as_bottom_reserved - _as_block_h) // 2)
        menu_canvas.blit(lbl_hdr, (cx + content_w // 2 - lbl_hdr.get_width() // 2, _as_block_top))
        _as_top_y = _as_block_top + lbl_hdr.get_height() + _as_header_gap

        _as_bar_x = cx + content_w // 2 - _as_bar_w // 2
        _as_inner_w = max(1, _as_bar_w - _as_text_pad * 2)

        _as_now = time.time()
        _as_focus_id = None
        if 0 <= _as_cur_row < len(_as_rows) and _as_rows[_as_cur_row] != "server_menu_open":
            _as_focus_id = _as_rows[_as_cur_row]
        _as_state = _advanced_scroll
        if _as_focus_id != _as_state["row"] or _as_now - _as_state["seen"] > 0.75:
            _as_state["row"] = _as_focus_id
            _as_state["t0"] = _as_now
        _as_state["seen"] = _as_now
        _as_state["active"] = False
        _as_elapsed = _as_now - _as_state["t0"]

        def _as_draw_text(_font, _txt, _col, _by, _focused):
            _surf = _font.render(_txt, True, _col)
            _over = _surf.get_width() - _as_inner_w
            _ty = _by + (btn_h - _surf.get_height()) // 2
            if _over <= 0:
                menu_canvas.blit(_surf, (_as_bar_x + (_as_bar_w - _surf.get_width()) // 2, _ty))
                return
            _off = 0
            if _focused:
                _as_state["active"] = True
                _off = _marquee_offset(_over, _as_elapsed, _as_scroll_speed, _as_scroll_pause)
            _clip = pygame.Surface((_as_inner_w, _surf.get_height()), pygame.SRCALPHA)
            _clip.blit(_surf, (-_off, 0))
            menu_canvas.blit(_clip, (_as_bar_x + _as_text_pad, _ty))

        for _as_pos, (_as_idx, _as_row_id) in enumerate(_as_entries):
            _as_y = _as_top_y + _as_pos * (_as_row_h + _as_row_gap)
            _as_focused = _as_cur_row == _as_idx
            _as_bar_y = _as_y + (_as_row_h - btn_h) // 2

            if _as_row_id in _as_toggle_labels:
                pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON, (_as_bar_x, _as_bar_y, _as_bar_w, btn_h), border_radius=6)
                if _as_focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (_as_bar_x, _as_bar_y, _as_bar_w, btn_h), SELECTION_BORDER_WIDTH, border_radius=6)
                _as_disabled_row = (_as_row_id == "server_option" and not (UPDATER_AND_SERVER_FEATURES_ENABLED and SERVER_TOGGLE_ENABLED))
                _as_lbl_col = COLOR_MENU_TEXT_GREYED if _as_disabled_row else TEXT_WHITE
                _as_draw_text(f_ch_label, _as_toggle_labels[_as_row_id], _as_lbl_col, _as_bar_y, _as_focused)

                _as_pill_x = _as_bar_x + _as_bar_w + _as_side_gap
                _as_on = _as_server_on if _as_row_id == "server_option" else _as_shell_on
                _as_pill_col = COLOR_MENU_BUTTON_GREYED if _as_disabled_row else (OFF_GREEN if _as_on else COLOR_MENU_SWITCH_OFF)
                _as_pill_y = _as_bar_y + (btn_h - pill_h) // 2
                pygame.draw.rect(menu_canvas, _as_pill_col, (_as_pill_x, _as_pill_y, pill_w, pill_h), border_radius=12)
                _as_handle_x = _as_pill_x + pill_w - 12 if _as_on else _as_pill_x + 12
                pygame.draw.circle(menu_canvas, COLOR_MENU_DISABLED_GREY if _as_disabled_row else TEXT_WHITE,
                                   (_as_handle_x, _as_pill_y + pill_h // 2), 9)

                if _as_row_id == "server_option":
                    _as_menu_focused = (_as_wm_sel_idx is not None and _as_cur_row == _as_wm_sel_idx)
                    _as_menu_on = _as_server_on
                    _as_menu_txt_col = TEXT_WHITE if _as_menu_on else COLOR_MENU_TEXT_GREYED
                    _as_menu_lbl = f_ch_btn.render("Menu", True, _as_menu_txt_col)
                    _as_menu_w = _as_menu_lbl.get_width() + 28
                    _as_menu_x = _as_pill_x + pill_w + _as_side_gap
                    _as_menu_bg = COLOR_MENU_BUTTON_BLUE if _as_menu_on else COLOR_MENU_BUTTON_GREYED
                    pygame.draw.rect(menu_canvas, _as_menu_bg, (_as_menu_x, _as_bar_y, _as_menu_w, btn_h), border_radius=5)
                    if _as_menu_focused:
                        _aa_rounded_rect(menu_canvas, NEON_GREEN, (_as_menu_x, _as_bar_y, _as_menu_w, btn_h), SELECTION_BORDER_WIDTH, border_radius=5)
                    menu_canvas.blit(_as_menu_lbl,
                                     (_as_menu_x + (_as_menu_w - _as_menu_lbl.get_width()) // 2,
                                      _as_bar_y + (btn_h - _as_menu_lbl.get_height()) // 2))
            else:
                _as_lbl_txt = _as_button_labels.get(_as_row_id, SYSTEM_ROW_META.get(_as_row_id, {}).get("label", _as_row_id))
                _as_gpu_disabled = (_as_row_id == "gpu_render_mode" and not GPU_RENDER_MODE_ENABLED)
                _as_upd_disabled = (_as_row_id == "update_check" and not UPDATER_AND_SERVER_FEATURES_ENABLED)
                _as_btn_disabled = _as_gpu_disabled or _as_upd_disabled
                _as_txt_col = COLOR_MENU_TEXT_GREYED if _as_btn_disabled else TEXT_WHITE
                _as_btn_color = COLOR_MENU_BUTTON_GREYED if _as_btn_disabled else SYSTEM_ROW_META.get(_as_row_id, {}).get("color", COLOR_MENU_BUTTON_BLUE)
                pygame.draw.rect(menu_canvas, _as_btn_color, (_as_bar_x, _as_bar_y, _as_bar_w, btn_h), border_radius=5)
                if _as_focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (_as_bar_x, _as_bar_y, _as_bar_w, btn_h), SELECTION_BORDER_WIDTH, border_radius=5)
                _as_draw_text(f_ch_btn, _as_lbl_txt, _as_txt_col, _as_bar_y, _as_focused)

        _as_close_focused = _as_cur_row == _as_close_idx
        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, _as_close_focused, f_ch_btn, NEON_GREEN)

    # ========================================================================
    # SECTION: System tab: remote remap
    # ========================================================================

    elif active_tab == "System" and layer in ("REMOTE_REMAP_LIST", "REMOTE_REMAP_CAPTURE", "NUMBER_REMAP_CAPTURE"):
        box_radius = 4
        focus_thickness = SELECTION_BORDER_WIDTH
        label_padding_x = 35

        is_capturing = layer in ("REMOTE_REMAP_CAPTURE", "NUMBER_REMAP_CAPTURE")
        is_number_capture = (layer == "NUMBER_REMAP_CAPTURE")
        capture_idx = app_state.get("remote_remap_index", 0)
        rr_sel = cur_sel

        remote_rows_per_col = -(-len(REMOTE_REMAP_ACTIONS) // 2)
        remote_label_col_w = REMOTE_REMAP_LABEL_COL_W_PX
        key_box_w, key_box_h = REMOTE_REMAP_KEY_BOX_W_PX, REMOTE_REMAP_KEY_BOX_H_PX
        col_gap = REMOTE_REMAP_COL_GAP_PX

        remote_sub1_label_x = cx + label_padding_x
        remote_sub1_key_x   = remote_sub1_label_x + remote_label_col_w
        remote_sub2_label_x = remote_sub1_key_x + key_box_w + col_gap
        remote_sub2_key_x   = remote_sub2_label_x + remote_label_col_w

        number_label_x = remote_sub2_key_x + key_box_w + 70
        number_key_x   = cx + content_w - key_box_w - 25

        row_top = content_y + 90
        row_h = 36

        f_sub_header = _get_font("Courier New", 24, bold=True)
        hdr1 = f_sub_header.render("Remote Remapping", True, COLOR_ARCADE_GOLD)
        menu_canvas.blit(hdr1, (remote_sub1_label_x, content_y + 14))
        hdr2 = f_sub_header.render("Number Remapping", True, COLOR_ARCADE_GOLD)
        menu_canvas.blit(hdr2, (number_label_x, content_y + 14))

        _remap_reject_flash = app_state.get("_remap_reject_flash")
        _remap_reject_active = (
            _remap_reject_flash is not None
            and (pygame.time.get_ticks() - _remap_reject_flash) < 2000
        )

        def _draw_remap_group(action_set, col_positions, rows_per_col, group_is_capturing):
            for i, (action_id, action_label) in enumerate(action_set):
                col = i // rows_per_col
                row = i % rows_per_col
                label_x, key_x = col_positions[col]
                ry = row_top + row * row_h
                row_is_capturing = group_is_capturing and capture_idx == i
                row_is_rejected  = row_is_capturing and _remap_reject_active

                lbl_row = f_ch_label.render(f"{action_label}:", True, TEXT_WHITE)
                menu_canvas.blit(lbl_row, (label_x, ry + 2))

                if row_is_rejected:
                    box_bg     = COLOR_MENU_KEY_CONFLICT_FILL
                    box_border = COLOR_MENU_WARNING_RED
                elif row_is_capturing:
                    box_bg     = COLOR_MENU_KEY_CAPTURE_FILL
                    box_border = NEON_GREEN
                else:
                    box_bg     = COLOR_MENU_DIM_GREY
                    box_border = COLOR_MENU_HINT_TEXT
                pygame.draw.rect(menu_canvas, box_bg, (key_x, ry, key_box_w, key_box_h), border_radius=box_radius)
                pygame.draw.rect(menu_canvas, box_border, (key_x, ry, key_box_w, key_box_h), 2, border_radius=box_radius)

                if row_is_rejected:
                    key_text = "!DUP"
                    key_color = COLOR_MENU_KEY_CONFLICT_TEXT
                elif row_is_capturing:
                    key_text  = "..."
                    key_color = TEXT_WHITE
                else:
                    key_text  = _remote_key_label(_get_remote_binding(db, action_id))
                    key_color = TEXT_WHITE
                lbl_key = f_ch_label.render(key_text, True, key_color)
                menu_canvas.blit(lbl_key, (key_x + (key_box_w // 2) - (lbl_key.get_width() // 2),
                                            ry + (key_box_h // 2) - (lbl_key.get_height() // 2)))

        _draw_remap_group(
            REMOTE_REMAP_ACTIONS,
            [(remote_sub1_label_x, remote_sub1_key_x), (remote_sub2_label_x, remote_sub2_key_x)],
            remote_rows_per_col,
            is_capturing and not is_number_capture,
        )
        _draw_remap_group(
            NUMBER_REMAP_ACTIONS,
            [(number_label_x, number_key_x)],
            len(NUMBER_REMAP_ACTIONS),
            is_capturing and is_number_capture,
        )

        btn_h = 40
        btn_pad_x = 28
        btn_text_y_offset = (btn_h // 2) - (f_ch_btn.get_height() // 2)
        btn_y = content_y + content_h - MENU_CLOSE_BUTTON_PLACE[1]

        prompt_y = btn_y - 28
        if is_capturing:
            active_set = NUMBER_REMAP_ACTIONS if is_number_capture else REMOTE_REMAP_ACTIONS
            cur_action_label = active_set[capture_idx][1]
            f_prompt = _get_font("Courier New", 17, bold=True)
            _remap_reject_flash = app_state.get("_remap_reject_flash")
            _remap_reject_active = (
                _remap_reject_flash is not None
                and (pygame.time.get_ticks() - _remap_reject_flash) < 2000
            )
            if _remap_reject_active:
                _reject_key  = app_state.get("_remap_reject_key")
                _reject_name = _remote_key_label(_reject_key) if _reject_key is not None else "KEY"
                prompt_txt = f_prompt.render(
                    f"\u26a0  {_reject_name} is already used — remap that action first or pick a different key",
                    True, COLOR_MENU_WARNING_RED)
            else:
                prompt_txt = f_prompt.render(
                    f"Press a key for: {cur_action_label}   "
                    f"(WASD/arrows not allowed)", True, COLOR_ARCADE_GOLD)
            menu_canvas.blit(prompt_txt, (remote_sub1_label_x, prompt_y))

        bx_remote = remote_sub1_label_x
        is_remote_focused = (not is_capturing) and rr_sel == 0
        remote_bg = COLOR_MENU_BUTTON_BLUE if layer != "NUMBER_REMAP_CAPTURE" else COLOR_MENU_BUTTON_BLUE_DIM
        remote_lbl = f_ch_btn.render("Remapping..." if layer == "REMOTE_REMAP_CAPTURE" else "Remap Remote", True, TEXT_WHITE)
        remote_btn_w = remote_lbl.get_width() + (btn_pad_x * 2)
        pygame.draw.rect(menu_canvas, remote_bg, (bx_remote, btn_y, remote_btn_w, btn_h), border_radius=box_radius)
        if is_remote_focused or layer == "REMOTE_REMAP_CAPTURE":
            _aa_rounded_rect(menu_canvas, NEON_GREEN, (bx_remote, btn_y, remote_btn_w, btn_h), focus_thickness, border_radius=box_radius)
        menu_canvas.blit(remote_lbl, (bx_remote + (remote_btn_w // 2) - (remote_lbl.get_width() // 2), btn_y + btn_text_y_offset))

        is_numbers_focused = (not is_capturing) and rr_sel == 1
        numbers_bg = COLOR_MENU_BUTTON_BLUE if layer != "REMOTE_REMAP_CAPTURE" else COLOR_MENU_BUTTON_BLUE_DIM
        numbers_lbl = f_ch_btn.render("Remapping..." if layer == "NUMBER_REMAP_CAPTURE" else "Remap Numbers", True, TEXT_WHITE)
        numbers_btn_w = numbers_lbl.get_width() + (btn_pad_x * 2)
        bx_numbers = cx + (content_w // 2) - (numbers_btn_w // 2)
        pygame.draw.rect(menu_canvas, numbers_bg, (bx_numbers, btn_y, numbers_btn_w, btn_h), border_radius=box_radius)
        if is_numbers_focused or layer == "NUMBER_REMAP_CAPTURE":
            _aa_rounded_rect(menu_canvas, NEON_GREEN, (bx_numbers, btn_y, numbers_btn_w, btn_h), focus_thickness, border_radius=box_radius)
        menu_canvas.blit(numbers_lbl, (bx_numbers + (numbers_btn_w // 2) - (numbers_lbl.get_width() // 2), btn_y + btn_text_y_offset))

        is_cl_focused = (not is_capturing) and rr_sel == 2
        close_btn_y = btn_y

        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_cl_focused, f_ch_btn, NEON_GREEN)

    # ========================================================================
    # SECTION: System tab: server menu
    # ========================================================================

    elif active_tab == "System" and layer == "SERVER_MENU":
        box_radius = 4
        focus_thickness = SELECTION_BORDER_WIDTH
        label_padding_x = 35

        srv_sel = cur_sel
        server_role = db.config.get("server_role") if db is not None else None
        _manage_servers_visible = server_role == "server"
        _pick_server_visible = server_role == "user"
        _srv_extra_btn_visible = _manage_servers_visible or _pick_server_visible
        _srv_close_idx = 3 if _srv_extra_btn_visible else 2

        f_sub_header = _get_font("Courier New", 24, bold=True)
        hdr = f_sub_header.render("Server Menu", True, COLOR_ARCADE_GOLD)
        menu_canvas.blit(hdr, (cx + label_padding_x, content_y + 4))

        f_intro = f_ch_label
        intro_lines = [
            "Choose how this device takes part in scheduling sync over your WiFi network.",
        ]
        intro_y = content_y + 40
        for line in intro_lines:
            lbl_intro = f_intro.render(line, True, TEXT_WHITE)
            menu_canvas.blit(lbl_intro, (cx + label_padding_x, intro_y))
            intro_y += f_intro.get_height() + 2

        card_top = intro_y + 18
        card_gap = 20
        card_w = content_w - (label_padding_x * 2)
        card_h = 118
        card_x = cx + label_padding_x

        f_card_title = _get_font("Courier New", 20, bold=True)
        f_card_body = _get_font("Courier New", 16)

        def _wrap(text, font, max_w):
            words = text.split(" ")
            lines, cur_line = [], ""
            for word in words:
                candidate = word if not cur_line else f"{cur_line} {word}"
                if font.size(candidate)[0] <= max_w or not cur_line:
                    cur_line = candidate
                else:
                    lines.append(cur_line)
                    cur_line = word
            if cur_line:
                lines.append(cur_line)
            return lines

        def _draw_role_card(y, sel_idx, role_id, title, body_text):
            is_focused = srv_sel == sel_idx
            is_selected = server_role == role_id
            bg = COLOR_MENU_BUTTON_BLUE if is_selected else COLOR_MENU_CARD
            pygame.draw.rect(menu_canvas, bg, (card_x, y, card_w, card_h), border_radius=8)
            border_col = NEON_GREEN if is_focused else (COLOR_MENU_RADIO_SELECTED_EDGE if is_selected else COLOR_MENU_CARD_EDGE)
            pygame.draw.rect(menu_canvas, border_col, (card_x, y, card_w, card_h), focus_thickness if is_focused else 2, border_radius=8)

            radio_cx, radio_cy, radio_r = card_x + 26, y + 26, 10
            pygame.draw.circle(menu_canvas, COLOR_MENU_RADIO_FILL, (radio_cx, radio_cy), radio_r)
            pygame.draw.circle(menu_canvas, NEON_GREEN if is_selected else COLOR_MENU_RADIO_EDGE, (radio_cx, radio_cy), radio_r, SELECTION_BORDER_WIDTH)
            if is_selected:
                pygame.draw.circle(menu_canvas, NEON_GREEN, (radio_cx, radio_cy), radio_r - 5)

            title_lbl = f_card_title.render(title, True, TEXT_WHITE)
            menu_canvas.blit(title_lbl, (card_x + 48, y + 14))
            if is_selected:
                sel_tag = f_card_body.render("(ACTIVE)", True, NEON_GREEN)
                menu_canvas.blit(sel_tag, (card_x + 48 + title_lbl.get_width() + 12, y + 18))

            body_lines = _wrap(body_text, f_card_body, card_w - 64)
            body_y = y + 48
            for line in body_lines:
                lbl_body = f_card_body.render(line, True, COLOR_MENU_CARD_TEXT)
                menu_canvas.blit(lbl_body, (card_x + 48, body_y))
                body_y += f_card_body.get_height() + 3

        _draw_role_card(
            card_top, 0, "server", "Server",
            "Saves your TV station and visualizer scheduling to a .json file and streams "
            "it to other apps on the same WiFi network that are set to User.",
        )
        _draw_role_card(
            card_top + card_h + card_gap, 1, "user", "User",
            "Looks for a Server on the same WiFi network and receives the TV station and "
            "visualizer scheduling broadcast from it automatically.",
        )

        btn_h = 40
        btn_pad_x = 28
        btn_text_y_offset = (btn_h // 2) - (f_ch_btn.get_height() // 2)
        btn_y = content_y + content_h - MENU_CLOSE_BUTTON_PLACE[1]

        if _srv_extra_btn_visible:
            is_ms_focused = srv_sel == 2
            ms_label = "Manage Servers" if _manage_servers_visible else "Pick a Server"
            ms_btn_w = f_ch_btn.size(ms_label)[0] + (btn_pad_x * 2)
            ms_btn_h = btn_h
            ms_btn_x = cx + label_padding_x
            ms_btn_y = btn_y
            pygame.draw.rect(menu_canvas, COLOR_MENU_CARD, (ms_btn_x, ms_btn_y, ms_btn_w, ms_btn_h), border_radius=box_radius)
            border_col = NEON_GREEN if is_ms_focused else COLOR_MENU_CARD_EDGE
            pygame.draw.rect(menu_canvas, border_col, (ms_btn_x, ms_btn_y, ms_btn_w, ms_btn_h), focus_thickness, border_radius=box_radius)
            lbl_ms = f_ch_btn.render(ms_label, True, TEXT_WHITE)
            menu_canvas.blit(lbl_ms, (ms_btn_x + (ms_btn_w // 2) - (lbl_ms.get_width() // 2), ms_btn_y + btn_text_y_offset))

        is_cl_focused = srv_sel == _srv_close_idx
        close_btn_y = btn_y

        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_cl_focused, f_ch_btn, NEON_GREEN)

    # ========================================================================
    # SECTION: System tab: server slots
    # ========================================================================

    elif active_tab == "System" and layer == "SERVER_SLOTS_MENU":
        box_radius = 4
        focus_thickness = SELECTION_BORDER_WIDTH
        label_padding_x = 35

        f_sub_header = _get_font("Courier New", 24, bold=True)
        hdr = f_sub_header.render("Server Slots", True, COLOR_ARCADE_GOLD)
        menu_canvas.blit(hdr, (cx + label_padding_x, content_y + 4))

        slot_ids = getattr(db, "SERVER_SLOT_IDS", tuple(f"server{i}" for i in range(1, 7))) if db is not None else tuple(f"server{i}" for i in range(1, 7))
        named = dict(db.config.get("server_slots", {})) if db is not None else {}
        active_slot_id = db.config.get("active_server_slot") if db is not None else None
        can_add = len(named) < len(slot_ids)

        SLOT_ROWS = 3
        SLOT_COLS = 2
        CREATE_IDX = SLOT_ROWS * SLOT_COLS
        MANAGE_IDX = CREATE_IDX + 1
        CLOSE_IDX = MANAGE_IDX + 1

        f_row = _get_font("Courier New", 17)
        f_btn = f_ch_btn

        side_btn_w = 150
        side_gap = 24
        grid_w = content_w - (label_padding_x * 2) - side_btn_w - side_gap
        col_gap = 16
        cell_w = (grid_w - col_gap) // SLOT_COLS
        row_h = 44
        row_gap = 10
        grid_x = cx + label_padding_x
        grid_y = content_y + 46

        editing = app_state.get("editing_server_slot_name", False)
        edit_buffer = app_state.get("server_slot_name_buffer", "")
        all_selected = app_state.get("server_slot_name_all_selected", False)

        for i, slot_id in enumerate(slot_ids):
            col = i // SLOT_ROWS
            row = i % SLOT_ROWS
            x = grid_x + col * (cell_w + col_gap)
            y = grid_y + row * (row_h + row_gap)
            name = named.get(slot_id)
            is_named = bool(name)
            is_focused = cur_sel == i

            bg = COLOR_MENU_CARD if is_named else COLOR_MENU_CARD_GREYED
            if is_focused:
                border_col = NEON_GREEN if is_named else COLOR_MENU_EDGE_LOCKED
            else:
                border_col = COLOR_MENU_CARD_EDGE if is_named else COLOR_MENU_CARD_EDGE_GREYED
            pygame.draw.rect(menu_canvas, bg, (x, y, cell_w, row_h), border_radius=6)
            pygame.draw.rect(menu_canvas, border_col, (x, y, cell_w, row_h), focus_thickness if is_focused else 1, border_radius=6)

            label_prefix = f"Slot {i + 1} -- "
            text_x = x + 14

            if editing and app_state.get("server_slot_name_target_row") == i:
                display_text = edit_buffer
                if all_selected and (pygame.time.get_ticks() // 500) % 2 == 0:
                    lbl = f_row.render(display_text, True, COLOR_MENU_TEXT_ON_LIT)
                    sel_rect = pygame.Rect(text_x - 4, y + 6, f_row.size(display_text)[0] + 8, row_h - 12)
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, sel_rect, border_radius=3)
                    menu_canvas.blit(lbl, (text_x, y + (row_h // 2) - (lbl.get_height() // 2)))
                else:
                    cursor = "_" if (pygame.time.get_ticks() // 500) % 2 == 0 else ""
                    lbl = f_row.render(display_text + cursor, True, TEXT_WHITE)
                    menu_canvas.blit(lbl, (text_x, y + (row_h // 2) - (lbl.get_height() // 2)))
                continue

            if is_named:
                lbl = f_row.render(f'{label_prefix}"{name}"', True, TEXT_WHITE)
            else:
                lbl = f_row.render(f"{label_prefix}unnamed", True, COLOR_MENU_UNNAMED_TEXT)
            menu_canvas.blit(lbl, (text_x, y + (row_h // 2) - (lbl.get_height() // 2)))

            if is_named and slot_id == active_slot_id:
                tag = f_row.render("ACTIVE", True, NEON_GREEN)
                tag_x = x + cell_w - tag.get_width() - 12
                if tag_x > text_x + lbl.get_width() + 8:
                    menu_canvas.blit(tag, (tag_x, y + (row_h // 2) - (tag.get_height() // 2)))

        side_x = grid_x + grid_w + side_gap
        create_y = grid_y
        manage_y = grid_y + row_h + 16

        def _draw_side_btn(label, y, idx, enabled):
            is_focused = cur_sel == idx
            bg = COLOR_MENU_CARD if enabled else COLOR_MENU_CARD_GREYED
            txt_col = TEXT_WHITE if enabled else COLOR_MENU_CARD_TEXT_GREYED
            border_col = (NEON_GREEN if is_focused else COLOR_MENU_CARD_EDGE) if enabled else COLOR_MENU_CARD_EDGE_GREYED
            pygame.draw.rect(menu_canvas, bg, (side_x, y, side_btn_w, row_h), border_radius=box_radius)
            pygame.draw.rect(menu_canvas, border_col, (side_x, y, side_btn_w, row_h), focus_thickness if (is_focused and enabled) else 1, border_radius=box_radius)
            lbl = f_btn.render(label, True, txt_col)
            menu_canvas.blit(lbl, (side_x + (side_btn_w // 2) - (lbl.get_width() // 2), y + (row_h // 2) - (lbl.get_height() // 2)))

        _draw_side_btn("Create", create_y, CREATE_IDX, can_add)
        _draw_side_btn("Manage", manage_y, MANAGE_IDX, bool(named))

        btn_h = 40
        btn_pad_x = 28
        close_btn_y = content_y + content_h - 45
        is_close_focused = cur_sel == CLOSE_IDX

        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_close_focused, f_ch_btn, NEON_GREEN)

    # ========================================================================
    # SECTION: System tab: server slot actions
    # ========================================================================

    elif active_tab == "System" and layer == "SERVER_SLOT_ACTIONS":
        box_radius = 4
        focus_thickness = SELECTION_BORDER_WIDTH
        label_padding_x = 35

        target_row = app_state.get("server_slot_name_target_row", 0)
        slot_ids = getattr(db, "SERVER_SLOT_IDS", tuple(f"server{i}" for i in range(1, 7))) if db is not None else tuple(f"server{i}" for i in range(1, 7))
        named = dict(db.config.get("server_slots", {})) if db is not None else {}
        target_slot_id = slot_ids[target_row] if 0 <= target_row < len(slot_ids) else None
        target_name = named.get(target_slot_id, "") if target_slot_id else ""
        is_active = db is not None and target_slot_id is not None and \
            db.config.get("active_server_slot") == target_slot_id

        f_sub_header = _get_font("Courier New", 22, bold=True)
        hdr = f_sub_header.render(f'"{target_name}"', True, COLOR_ARCADE_GOLD)
        menu_canvas.blit(hdr, (cx + label_padding_x, content_y + 8))
        if is_active:
            f_active_tag = f_ch_label
            active_tag = f_active_tag.render("(ACTIVE)", True, NEON_GREEN)
            menu_canvas.blit(active_tag, (cx + label_padding_x + hdr.get_width() + 12, content_y + 12))

        pending_delete = app_state.get("server_slot_pending_delete", False)
        make_active_label = "Deactivate (back to local)" if is_active else "Make Active"
        actions = [make_active_label, "Rename", "Delete", "Cancel"]
        f_row = _get_font("Courier New", 18)
        row_h = 40
        row_gap = 8
        row_w = content_w - (label_padding_x * 2)
        row_x = cx + label_padding_x
        row_y = content_y + 50

        for i, label in enumerate(actions):
            is_focused = cur_sel == i
            y = row_y + i * (row_h + row_gap)
            bg = COLOR_MENU_DELETE_ARMED if (label == "Delete" and pending_delete and is_focused) else COLOR_MENU_CARD
            border_col = NEON_GREEN if is_focused else COLOR_MENU_CARD_EDGE
            pygame.draw.rect(menu_canvas, bg, (row_x, y, row_w, row_h), border_radius=6)
            pygame.draw.rect(menu_canvas, border_col, (row_x, y, row_w, row_h), focus_thickness if is_focused else 1, border_radius=6)
            shown = "Press Enter again to confirm delete" if (label == "Delete" and pending_delete) else label
            col = OFF_RED if label == "Delete" else TEXT_WHITE
            lbl = f_row.render(shown, True, col)
            menu_canvas.blit(lbl, (row_x + 16, y + (row_h // 2) - (lbl.get_height() // 2)))

    # ========================================================================
    # SECTION: System tab: server picker
    # ========================================================================

    elif active_tab == "System" and layer == "SERVER_PICKER_MENU":
        box_radius = 4
        focus_thickness = SELECTION_BORDER_WIDTH
        label_padding_x = 35

        f_sub_header = _get_font("Courier New", 24, bold=True)
        hdr = f_sub_header.render("Pick a Server", True, COLOR_ARCADE_GOLD)
        menu_canvas.blit(hdr, (cx + label_padding_x, content_y + 4))

        entries = get_server_picker_entries(db)

        _sp_live_count = get_server_picker_selectable_count(entries)
        if cur_sel >= _sp_live_count:
            cur_sel = max(0, _sp_live_count - 1)
            app_state["menu_selection_index"] = cur_sel

        SP_VISIBLE_ROWS = SERVER_PICKER_VISIBLE_ROWS
        total_entries = len(entries)
        max_scroll = max(0, total_entries - SP_VISIBLE_ROWS)
        focus_entry_idx = cur_sel if cur_sel < total_entries else max(0, total_entries - 1)
        scroll = min(app_state.get("server_picker_scroll", 0), max_scroll)
        if focus_entry_idx < scroll:
            scroll = focus_entry_idx
        elif focus_entry_idx > scroll + SP_VISIBLE_ROWS - 1:
            scroll = focus_entry_idx - SP_VISIBLE_ROWS + 1
        scroll = max(0, min(scroll, max_scroll))
        app_state["server_picker_scroll"] = scroll

        visible_entries = entries[scroll:scroll + SP_VISIBLE_ROWS]
        _sp_window_reaches_end = (scroll + SP_VISIBLE_ROWS) >= total_entries
        display_rows = get_server_picker_display_rows(visible_entries, include_close=False)

        f_header_row = _get_font("Courier New", 15, bold=True)
        f_row = _get_font("Courier New", 18)
        f_host = _get_font("Courier New", 13)
        row_h = 42
        row_gap = 8
        header_h = 26
        row_w = content_w - (label_padding_x * 2)
        row_x = cx + label_padding_x
        row_y = content_y + 44

        if not entries:
            f_empty = f_ch_label
            empty_lbl = f_empty.render("No server found.", True, COLOR_MENU_NO_SERVER_TEXT)
            menu_canvas.blit(empty_lbl, (row_x, row_y))
            row_y += empty_lbl.get_height() + 18
        elif total_entries > SP_VISIBLE_ROWS:
            f_scroll_hint = _get_font("Courier New", 13)
            up_txt = "^ more above" if scroll > 0 else ""
            down_txt = "more below v" if not _sp_window_reaches_end else ""
            if up_txt:
                lbl_up = f_scroll_hint.render(up_txt, True, COLOR_MENU_DIM_TEXT)
                menu_canvas.blit(lbl_up, (row_x + row_w - lbl_up.get_width(), content_y + 6))
            if down_txt:
                lbl_down = f_scroll_hint.render(down_txt, True, COLOR_MENU_DIM_TEXT)
                menu_canvas.blit(lbl_down, (row_x, content_y + content_h - 30))

        y = row_y
        selectable_i = scroll
        for disp_row in display_rows:
            if disp_row["type"] == "header":
                lbl_h = f_header_row.render(disp_row["text"], True, COLOR_MENU_DIM_TEXT)
                menu_canvas.blit(lbl_h, (row_x, y + (header_h // 2) - (lbl_h.get_height() // 2)))
                y += header_h
                continue

            is_focused = cur_sel == selectable_i
            bg = COLOR_MENU_CARD
            border_col = NEON_GREEN if is_focused else COLOR_MENU_CARD_EDGE
            pygame.draw.rect(menu_canvas, bg, (row_x, y, row_w, row_h), border_radius=6)
            pygame.draw.rect(menu_canvas, border_col, (row_x, y, row_w, row_h), focus_thickness if is_focused else 1, border_radius=6)

            if disp_row["type"] == "entry":
                if is_focused:
                    sel_mark = f_row.render("[*]", True, NEON_GREEN)
                    menu_canvas.blit(sel_mark, (row_x + 12, y + (row_h // 2) - (sel_mark.get_height() // 2)))
                    text_x = row_x + 46
                else:
                    text_x = row_x + 16
                lbl = f_row.render(disp_row["name"], True, TEXT_WHITE)
                name_y = y + (row_h // 2) - lbl.get_height() + 1
                menu_canvas.blit(lbl, (text_x, name_y))
                lbl_host = f_host.render(disp_row["host"], True, COLOR_MENU_DIM_TEXT)
                menu_canvas.blit(lbl_host, (text_x, name_y + lbl.get_height() - 2))

            y += row_h + row_gap
            selectable_i += 1

        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, cur_sel >= total_entries, f_ch_btn, NEON_GREEN)

    # ========================================================================
    # SECTION: Channels tab: channel list
    # ========================================================================

    elif active_tab == "Channels" and layer in ("CHANNEL_LIST", "TAB_SELECTION"):
        box_radius = 4
        focus_thickness = SELECTION_BORDER_WIDTH

        cols = 4
        rows_per_page = 5
        cell_w, cell_h = 225, 62
        x_pad, y_pad = 14, 13
        cell_radius = 8
        dot_radius = 7

        filtered_channels_list = [i for i in range(5, 45)]
        total_channels = len(filtered_channels_list)

        PAGE_SIZE = cols * rows_per_page
        num_pages = max(1, (total_channels + PAGE_SIZE - 1) // PAGE_SIZE)
        if layer == "CHANNEL_LIST" and cur_sel < total_channels:
            channels_page = min(cur_sel // PAGE_SIZE, num_pages - 1)
            app_state["channels_tab_page"] = channels_page
        else:
            channels_page = min(app_state.get("channels_tab_page", 0), num_pages - 1)
        page_start = channels_page * PAGE_SIZE
        page_items = filtered_channels_list[page_start:page_start + PAGE_SIZE]

        grid_w_total = (cell_w * cols) + (x_pad * (cols - 1))
        grid_start_x = cx + (content_w // 2) - (grid_w_total // 2)
        grid_start_y = content_y + 20

        cell_text_y_offset = (cell_h // 2) - (f_ch_label.get_height() // 2)
        cell_text_x_offset = 18
        cell_dot_x_offset = 26

        for local_idx, ch_num in enumerate(page_items):
            idx = page_start + local_idx
            r_pos = local_idx // cols
            c_pos = local_idx % cols

            cell_x = grid_start_x + c_pos * (cell_w + x_pad)
            cell_y = grid_start_y + r_pos * (cell_h + y_pad)

            is_ch_highlighted = (layer == "CHANNEL_LIST" and cur_sel == idx)

            current_lookup_key = str(ch_num).zfill(2)
            if db is not None and current_lookup_key in db.channels_db:
                _channel_info = db.channels_db[current_lookup_key]
                ch_active = _channel_info.get("active", True)
                ch_server_locked = bool(_channel_info.get("server_locked", False))
            else:
                ch_active = False
                ch_server_locked = False

            dot_color = (COLOR_MENU_DISABLED_GREY if ch_server_locked
                         else (OFF_GREEN if ch_active else OFF_RED))
            cell_bg = COLOR_MENU_LOCKED_CELL if ch_server_locked else COLOR_MENU_BUTTON
            cell_border = COLOR_MENU_TEXT_GREYED if ch_server_locked else None

            pygame.draw.rect(menu_canvas, cell_bg, (cell_x, cell_y, cell_w, cell_h), border_radius=cell_radius)
            if cell_border is not None:
                pygame.draw.rect(menu_canvas, cell_border,
                                 (cell_x, cell_y, cell_w, cell_h), 1,
                                 border_radius=cell_radius)
            if is_ch_highlighted:
                pygame.draw.rect(menu_canvas,
                                 COLOR_MENU_EDGE_LOCKED if ch_server_locked else NEON_GREEN,
                                 (cell_x, cell_y, cell_w, cell_h),
                                 focus_thickness, border_radius=cell_radius)

            lbl_ch_name = f_ch_label.render(
                f"Channel {str(ch_num).zfill(2)}",
                True, COLOR_MENU_LOCKED_TEXT if ch_server_locked else TEXT_WHITE)
            if ch_server_locked:
                menu_canvas.blit(lbl_ch_name,
                                 (cell_x + cell_text_x_offset, cell_y + 7))
                lbl_server = f_ch_btn.render("SERVER", True, COLOR_MENU_SERVER_LABEL)
                menu_canvas.blit(lbl_server,
                                 (cell_x + cell_w - lbl_server.get_width() - 32,
                                  cell_y + cell_h - lbl_server.get_height() - 5))
            else:
                menu_canvas.blit(lbl_ch_name,
                                 (cell_x + cell_text_x_offset, cell_y + cell_text_y_offset))

            pygame.draw.circle(menu_canvas, dot_color, (cell_x + cell_w - cell_dot_x_offset, cell_y + (cell_h // 2)), dot_radius)

            _scan_progress = app_state.get("_channel_scan_progress", {}).get(current_lookup_key)
            if _scan_progress and ch_active and not ch_server_locked:
                _scan_done, _scan_total = _scan_progress
                if _scan_total > 0 and _scan_done < _scan_total:
                    _bar_max_w = cell_w - 2 * cell_radius
                    _bar_w = min(_bar_max_w, max(CHANNEL_SCAN_BAR_MIN_PX, int(_bar_max_w * _scan_done / _scan_total)))
                    _bar_x = cell_x + cell_radius
                    _bar_y = cell_y + cell_h - CHANNEL_SCAN_BAR_HEIGHT_PX
                    _halo_color = tuple(int(cell_bg[_c] + (NEON_GREEN[_c] - cell_bg[_c]) * CHANNEL_SCAN_BAR_HALO_MIX)
                                        for _c in range(3))
                    _head_color = tuple(int(NEON_GREEN[_c] + (255 - NEON_GREEN[_c]) * 0.5) for _c in range(3))
                    pygame.draw.rect(menu_canvas, _halo_color, (_bar_x, _bar_y - 1, _bar_w, 1))
                    pygame.draw.rect(menu_canvas, NEON_GREEN, (_bar_x, _bar_y, _bar_w, CHANNEL_SCAN_BAR_HEIGHT_PX))
                    _head_w = min(CHANNEL_SCAN_BAR_HEAD_PX, _bar_w)
                    pygame.draw.rect(menu_canvas, _head_color,
                                     (_bar_x + _bar_w - _head_w, _bar_y, _head_w, CHANNEL_SCAN_BAR_HEIGHT_PX))

        grid_bottom_y = grid_start_y + (rows_per_page * (cell_h + y_pad)) - y_pad

        arrow_btn_w, arrow_btn_h = 56, 38
        arrow_btn_x = grid_start_x + grid_w_total - arrow_btn_w
        arrow_btn_y = grid_bottom_y + 14
        is_arrow_focused = (layer == "CHANNEL_LIST" and cur_sel == 42)
        arrow_bg = COLOR_MENU_ON_BLUE_EDGE if is_arrow_focused else COLOR_MENU_ON_BLUE
        pygame.draw.rect(menu_canvas, arrow_bg,
                         (arrow_btn_x, arrow_btn_y, arrow_btn_w, arrow_btn_h), border_radius=6)
        if is_arrow_focused:
            pygame.draw.rect(menu_canvas, NEON_GREEN,
                             (arrow_btn_x, arrow_btn_y, arrow_btn_w, arrow_btn_h), SELECTION_BORDER_WIDTH, border_radius=6)
        arrow_char = "\u25ba" if channels_page == 0 else "\u25c4"
        arrow_lbl  = f_ch_btn.render(arrow_char, True, TEXT_WHITE)
        menu_canvas.blit(arrow_lbl, (arrow_btn_x + (arrow_btn_w - arrow_lbl.get_width())  // 2,
                                      arrow_btn_y + (arrow_btn_h - arrow_lbl.get_height()) // 2))

        page_lbl = f_ch_btn.render(f"{channels_page + 1}/{num_pages}", True, TEXT_WHITE)
        menu_canvas.blit(page_lbl, (arrow_btn_x - page_lbl.get_width() - 14,
                                     arrow_btn_y + (arrow_btn_h - page_lbl.get_height()) // 2))

        is_close_focused = (layer == "CHANNEL_LIST" and cur_sel == 99)
        close_btn_y = content_y + content_h - 52

        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_close_focused, f_ch_btn, NEON_GREEN)

        _settings_btn_w = 150
        _settings_btn_h = box_h_43
        _settings_btn_x = grid_start_x
        _settings_btn_y = arrow_btn_y + arrow_btn_h - _settings_btn_h
        _settings_focused = layer == "CHANNEL_LIST" and cur_sel == 43
        _settings_bg = COLOR_MENU_ON_BLUE_EDGE if _settings_focused else COLOR_MENU_ON_BLUE
        pygame.draw.rect(menu_canvas, _settings_bg,
                         (_settings_btn_x, _settings_btn_y, _settings_btn_w, _settings_btn_h), border_radius=5)
        if _settings_focused:
            _aa_rounded_rect(menu_canvas, NEON_GREEN,
                             (_settings_btn_x, _settings_btn_y, _settings_btn_w, _settings_btn_h),
                             SELECTION_BORDER_WIDTH, border_radius=5)
        _settings_lbl = f_ch_btn.render("Settings", True, TEXT_WHITE)
        menu_canvas.blit(_settings_lbl,
                         (_settings_btn_x + (_settings_btn_w - _settings_lbl.get_width()) // 2,
                          _settings_btn_y + (_settings_btn_h - _settings_lbl.get_height()) // 2))

    # ========================================================================
    # SECTION: Channels tab: settings sub-menu
    # ========================================================================

    elif active_tab == "Channels" and layer == "CHANNEL_SETTINGS_SUB_MENU":

        f_sub_title = _get_font("Courier New", 20, bold=True)
        lbl_hdr = f_sub_title.render("Channels \u2014 Settings", True, TEXT_WHITE)

        _cs_row_labels = {
            "scan_lines": "Scan Lines",
            "static_noise": "Static Noise",
            "warp_screen": "Warp Screen",
            "reception_lines": "Reception Lines",
            "bezel_toggle": "CRT TV Bezels",
            "boot_anim": "Boot Anim",
        }
        _cs_rows = get_channels_settings_menu_rows(db, app_state)
        _cs_cur_row = app_state.get("menu_selection_index", 0)
        _bezel_on = bool(db.config.get("fake_43_border_enabled", False)) if db is not None else False
        _bezel_style_n = media.bezel_style_count() if db is not None else 1

        _cs_bar_pad = 36
        _cs_bar_w = max(f_ch_label.size(_lbl)[0] for _lbl in _cs_row_labels.values()) + _cs_bar_pad
        _cs_row_content_w = _cs_bar_w + 20 + pill_w
        _cs_bstyle_box_w = 18 + max(f_ch_btn.size("Option %d" % _n)[0] for _n in range(1, max(_bezel_style_n, 1) + 1))
        _cs_bezel_row_w = _cs_row_content_w + 20 + _cs_bstyle_box_w

        _cs_bezel_style_select_idx = _cs_rows.index("bezel_style_select") if "bezel_style_select" in _cs_rows else None
        _cs_columns = [
            [(_cs_rows.index(_rid), _rid) for _rid in ("bezel_toggle", "warp_screen", "boot_anim") if _rid in _cs_rows],
            [(_cs_rows.index(_rid), _rid) for _rid in ("static_noise", "reception_lines", "scan_lines") if _rid in _cs_rows],
        ]

        _cs_half_w = content_w // 2
        _cs_col_centers = [cx + _cs_half_w // 2, cx + _cs_half_w + _cs_half_w // 2]

        _cs_grid_rows_tall = max((len(_c) for _c in _cs_columns), default=0)
        _cs_total_rows = _cs_grid_rows_tall
        _cs_title_y = content_y + CHANNEL_SETTINGS_TITLE_TOP_PX
        _cs_area_top = _cs_title_y + lbl_hdr.get_height()
        _cs_area_bottom = content_y + content_h - CHANNEL_SETTINGS_BOTTOM_RESERVED_PX
        _cs_even_gap = (_cs_area_bottom - _cs_area_top - _cs_total_rows * box_h_43) // (_cs_total_rows + 1)
        _cs_gap = max(CHANNEL_SETTINGS_HEADER_GAP_PX // 2, _cs_even_gap)

        menu_canvas.blit(lbl_hdr, (cx + content_w // 2 - lbl_hdr.get_width() // 2, _cs_title_y))
        _cs_top_y = _cs_area_top + _cs_gap

        _cs_block_ws = [
            max(((_cs_bezel_row_w if (_rid == "bezel_toggle" and _cs_bezel_style_select_idx is not None) else _cs_row_content_w)
                 for _i, _rid in _col), default=0)
            for _col in _cs_columns
        ]
        _cs_col_gap = 90
        _cs_side_margin = (content_w - sum(_cs_block_ws) - _cs_col_gap) // 2

        for _cs_col_idx, _cs_col_entries in enumerate(_cs_columns):
            if not _cs_col_entries:
                continue
            _cs_block_w = _cs_block_ws[_cs_col_idx]
            if _cs_col_idx == 0:
                _cs_col_x = cx + _cs_side_margin
            else:
                _cs_col_x = cx + _cs_side_margin + _cs_block_ws[0] + _cs_col_gap
            _cs_pill_x = _cs_col_x + _cs_bar_w + 20

            for _cs_pos, (_cs_idx, _cs_row_id) in enumerate(_cs_col_entries):
                _cs_focused = _cs_cur_row == _cs_idx
                _cs_bar_y = _cs_top_y + _cs_pos * (box_h_43 + _cs_gap)

                pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON, (_cs_col_x, _cs_bar_y, _cs_bar_w, box_h_43), border_radius=6)
                if _cs_focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (_cs_col_x, _cs_bar_y, _cs_bar_w, box_h_43), SELECTION_BORDER_WIDTH, border_radius=6)

                _cs_label = _cs_row_labels.get(_cs_row_id, _cs_row_id)
                _cs_lbl_surf = f_ch_label.render(_cs_label, True, TEXT_WHITE)
                menu_canvas.blit(_cs_lbl_surf, (_cs_col_x + (_cs_bar_w - _cs_lbl_surf.get_width()) // 2,
                                                 _cs_bar_y + (box_h_43 - _cs_lbl_surf.get_height()) // 2))

                _cs_config_key = {
                    "scan_lines": "fake_43_scanlines_enabled",
                    "static_noise": "fake_static_noise_enabled",
                    "warp_screen": "warp_screen_enabled",
                    "reception_lines": "fake_reception_lines_enabled",
                    "bezel_toggle": "fake_43_border_enabled",
                    "boot_anim": "boot_anim_enabled",
                }.get(_cs_row_id)
                _cs_on = bool(db.config.get(_cs_config_key, False)) if (db is not None and _cs_config_key) else False
                _cs_disabled = _cs_config_key is not None and (_mode_locks(_cs_config_key) or (_cs_row_id == "warp_screen" and _warp_needs_a_bezel(db)))
                if _cs_disabled:
                    _cs_on = False
                _pill_y = _cs_bar_y + (box_h_43 - pill_h) // 2
                if _cs_disabled:
                    _pill_col = COLOR_MENU_BUTTON_GREYED
                else:
                    _pill_col = OFF_GREEN if _cs_on else COLOR_MENU_SWITCH_OFF
                pygame.draw.rect(menu_canvas, _pill_col, (_cs_pill_x, _pill_y, pill_w, pill_h), border_radius=12)
                _handle_x = _cs_pill_x + pill_w - 12 if _cs_on else _cs_pill_x + 12
                pygame.draw.circle(menu_canvas, COLOR_MENU_DISABLED_GREY if _cs_disabled else TEXT_WHITE,
                                   (_handle_x, _pill_y + pill_h // 2), 9)

                if _cs_row_id == "bezel_toggle" and _cs_bezel_style_select_idx is not None:
                    _cs_bstyle_focused = (_cs_cur_row == _cs_bezel_style_select_idx)
                    _bezel_style_val = db.config.get("bezel_style", 1) if db is not None else 1
                    try:
                        _bezel_style_val = int(_bezel_style_val)
                    except (TypeError, ValueError) as _swallowed_error:
                        _log_swallowed("_build_settings_overlay_menu: _bezel_style_val = int(_bezel_style_val)", _swallowed_error)
                        _bezel_style_val = 1
                    _bezel_style_val = max(1, min(_bezel_style_val, _bezel_style_n))
                    _cs_bstyle_txt_col = TEXT_WHITE if _bezel_on else COLOR_MENU_TEXT_GREYED
                    _cs_bstyle_lbl = f_ch_btn.render("Option %d" % _bezel_style_val, True, _cs_bstyle_txt_col)
                    _cs_bstyle_x = _cs_pill_x + pill_w + 20
                    _cs_bstyle_bg = COLOR_MENU_BUTTON_BLUE if _bezel_on else COLOR_MENU_BUTTON_GREYED
                    pygame.draw.rect(menu_canvas, _cs_bstyle_bg,
                                     (_cs_bstyle_x, _cs_bar_y, _cs_bstyle_box_w, box_h_43), border_radius=5)
                    if _cs_bstyle_focused:
                        _aa_rounded_rect(menu_canvas, NEON_GREEN, (_cs_bstyle_x, _cs_bar_y, _cs_bstyle_box_w, box_h_43), SELECTION_BORDER_WIDTH, border_radius=5)
                    menu_canvas.blit(_cs_bstyle_lbl,
                                     (_cs_bstyle_x + (_cs_bstyle_box_w - _cs_bstyle_lbl.get_width()) // 2,
                                      _cs_bar_y + (box_h_43 - _cs_bstyle_lbl.get_height()) // 2))

        _cs_close_idx = len(_cs_rows)
        _cs_close_focused = _cs_cur_row == _cs_close_idx
        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, _cs_close_focused, f_ch_btn, NEON_GREEN)

    # ========================================================================
    # SECTION: Channels tab: per-channel sub-menu
    # ========================================================================

    elif active_tab == "Channels" and layer == "CHANNEL_SUB_MENU":
        
        target_ch_str = str(app_state.get("selected_channel_row", 5)).zfill(2)
        ch_info = db.channels_db.get(target_ch_str, {"active": True, "is_visualizer": False, "name": f"STATION {target_ch_str}", "visualizer_style": "Random"})
        
        lbl_sub = f_ch_label.render(f"Channel {target_ch_str} Scheduling Settings", True, TEXT_WHITE)
        title_x = cx + (content_w // 2) - (lbl_sub.get_width() // 2)
        menu_canvas.blit(lbl_sub, (title_x, content_y + 20))
        
        sub_r = app_state.get("sub_menu_row_index", 0) 
        sub_c = app_state.get("sub_menu_col_index", 0)
        
        top_y = content_y + 60
        
        is_toggle_focused = (sub_r == 0 and sub_c == 0)
        t_active = ch_info.get("active", True)

        _active_ch_count = 0
        if db is not None:
            for _n in range(5, 45):
                _cs = str(_n).zfill(2)
                if db.channels_db.get(_cs, {}).get("active", _cs == "05"):
                    _active_ch_count += 1
        _is_last_active = t_active and (_active_ch_count <= 1)

        if _is_last_active and sub_c == 0:
            # WARNING: display-only remap; never write back to app_state here.
            _effective_col = 1
        else:
            _effective_col = sub_c

        is_toggle_focused = (not _is_last_active) and (sub_r == 0 and _effective_col == 0)

        _toggle_label_color = COLOR_MENU_LAST_ITEM_TEXT if _is_last_active else (NEON_GREEN if is_toggle_focused else TEXT_WHITE)
        lbl_toggle_txt = f_ch_label.render("On/Off:", True, _toggle_label_color)
        _status_lbl_x = cx + 25
        menu_canvas.blit(lbl_toggle_txt, (_status_lbl_x, top_y + 4))

        _status_pill_x = _status_lbl_x + lbl_toggle_txt.get_width() + 20

        if _is_last_active:
            sw_bg = COLOR_MENU_LOCKED_SWITCH_ON
            sw_handle_col = COLOR_MENU_LOCKED_SWITCH_KNOB
            pygame.draw.rect(menu_canvas, COLOR_MENU_PADLOCK, (_status_pill_x + 63, top_y + 4, 10, 8), border_radius=2)
            pygame.draw.circle(menu_canvas, COLOR_MENU_PADLOCK, (_status_pill_x + 68, top_y + 4), 4, 2)
        else:
            sw_bg = OFF_GREEN if t_active else COLOR_MENU_SWITCH_OFF
            sw_handle_col = TEXT_WHITE

        pygame.draw.rect(menu_canvas, sw_bg, (_status_pill_x, top_y, 55, 24), border_radius=12)
        if is_toggle_focused:
            _aa_rounded_rect(menu_canvas, NEON_GREEN, (_status_pill_x, top_y, 55, 24), SELECTION_BORDER_WIDTH, border_radius=12)
        sw_handle_x = _status_pill_x + 42 if t_active else _status_pill_x + 12
        pygame.draw.circle(menu_canvas, sw_handle_col, (sw_handle_x, top_y + 12), 9)
        if is_toggle_focused:
            pygame.draw.circle(menu_canvas, NEON_GREEN, (sw_handle_x, top_y + 12), 11, SELECTION_BORDER_WIDTH)

        t_vis = ch_info.get("is_visualizer", False)
        if t_active:
            is_vis_mode_focused = (sub_r == 0 and _effective_col == 1)
            lbl_vis_txt = f_ch_label.render("Music", True, NEON_GREEN if is_vis_mode_focused else TEXT_WHITE)
            _mode_lbl_x = _status_pill_x + 55 + 40
            menu_canvas.blit(lbl_vis_txt, (_mode_lbl_x, top_y + 4))
            
            _mode_pill_x = _mode_lbl_x + lbl_vis_txt.get_width() + 20
            v_sw_bg = OFF_GREEN if t_vis else COLOR_MENU_SWITCH_OFF
            pygame.draw.rect(menu_canvas, v_sw_bg, (_mode_pill_x, top_y, 55, 24), border_radius=12)
            if is_vis_mode_focused: _aa_rounded_rect(menu_canvas, NEON_GREEN, (_mode_pill_x, top_y, 55, 24), SELECTION_BORDER_WIDTH, border_radius=12)
            v_sw_handle_x = _mode_pill_x + 42 if t_vis else _mode_pill_x + 12
            pygame.draw.circle(menu_canvas, TEXT_WHITE, (v_sw_handle_x, top_y + 12), 9)
            if is_vis_mode_focused: pygame.draw.circle(menu_canvas, NEON_GREEN, (v_sw_handle_x, top_y + 12), 11, SELECTION_BORDER_WIDTH)

            is_name_focused = (sub_r == 0 and _effective_col == 2)
            is_editing_name = app_state.get("editing_channel_name", False) and is_name_focused
            all_selected    = is_editing_name and app_state.get("channel_name_all_selected", False)
            _name_box_x = _mode_pill_x + 55 + 40
            _name_box_w = f_ch_label.size("NAME: XXXXXXXXXXXX_")[0] + 20
            _name_box_w = min(_name_box_w, cx + content_w - 35 - _name_box_x)
            pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON, (_name_box_x, top_y - 4, _name_box_w, 36), border_radius=4)
            if is_name_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (_name_box_x, top_y - 4, _name_box_w, 36), SELECTION_BORDER_WIDTH, border_radius=4)
            ch_name_str = ch_info.get("name", f"STATION {target_ch_str}").upper()
            if all_selected:
                sel_lbl = f_ch_label.render(f"NAME: {ch_name_str[:12]}", True, (0, 0, 0))
                sel_w = sel_lbl.get_width() + 6
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (_name_box_x + 7, top_y - 1, sel_w, 30), border_radius=2)
                menu_canvas.blit(sel_lbl, (_name_box_x + 10, top_y + 3))
            else:
                display_str = f"NAME: {ch_name_str[:12]}"
                if is_editing_name and (pygame.time.get_ticks() // 500) % 2 == 0:
                    display_str += "_"
                lbl_name_txt = f_ch_label.render(display_str, True, NEON_GREEN if is_name_focused else TEXT_WHITE)
                menu_canvas.blit(lbl_name_txt, (_name_box_x + 10, top_y + 3))

        cell_w, cell_h = 340, 48
        grid_y_start = content_y + 125
        y_gap = 20

        if not t_active:
            msg_y = grid_y_start + 60
            f_msg = _get_font("Courier New", 22, bold=True)
            lbl_msg1 = f_msg.render("CHANNEL IS CURRENTLY OFF", True, OFF_RED)
            lbl_msg2 = f_msg.render("Turn ON the channel to access scheduling options", True, TEXT_WHITE)
            menu_canvas.blit(lbl_msg1, (cx + (content_w // 2) - (lbl_msg1.get_width() // 2), msg_y))
            menu_canvas.blit(lbl_msg2, (cx + (content_w // 2) - (lbl_msg2.get_width() // 2), msg_y + 30))
            
            close_row_idx = 1
            is_close_focused = (sub_r == close_row_idx)
            _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_close_focused, f_ch_btn, NEON_GREEN)
        elif t_vis:
            row_x = cx + 25
            row_w = content_w - 60
            vis_row_h = 40
            vis_gap = 20

            _audio_row_idx = 2
            _gain_row_idx = 3
            _reactive_row_idx = 4
            _reactive_strength_row_idx = 5
            _clear_row_idx = 6
            close_row_idx = 7
            is_close_focused = (sub_r == close_row_idx)

            style_y = grid_y_start
            is_style_focused = (sub_r == 1)
            _style_box_bg = COLOR_MENU_BUTTON
            _style_text_col = NEON_GREEN if is_style_focused else TEXT_WHITE
            pygame.draw.rect(menu_canvas, _style_box_bg, (row_x, style_y, row_w, vis_row_h), border_radius=6)
            if is_style_focused: _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, style_y, row_w, vis_row_h), SELECTION_BORDER_WIDTH, border_radius=6)
            style_str = ch_info.get("visualizer_style", "Random").upper()
            lbl_style = f_ch_label.render(f"VISUALIZER ENGINE STYLE: < {style_str} >", True, _style_text_col)
            menu_canvas.blit(lbl_style, (row_x + 20, style_y + vis_row_h // 2 - lbl_style.get_height() // 2))

            audio_y = style_y + vis_row_h + vis_gap
            is_audio_dir_focused = (sub_r == _audio_row_idx)
            pygame.draw.rect(menu_canvas, COLOR_BUTTON_ADD, (row_x, audio_y, row_w, vis_row_h), border_radius=6)
            if is_audio_dir_focused: _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, audio_y, row_w, vis_row_h), SELECTION_BORDER_WIDTH, border_radius=6)
            _audio_ct = len(ch_info.get("schedules", {}).get("Music Tracks", []))
            lbl_audio = f_ch_label.render(f"AUDIO FILES: ({'ADD' if _audio_ct == 0 else _audio_ct})", True, NEON_GREEN if is_audio_dir_focused else TEXT_WHITE)
            menu_canvas.blit(lbl_audio, (row_x + 20, audio_y + vis_row_h // 2 - lbl_audio.get_height() // 2))

            gain_y = audio_y + vis_row_h + vis_gap
            reactive_y = gain_y + vis_row_h + vis_gap
            reactive_enabled = ch_info.get("reactive_visualizer_enabled", True)
            # WARNING: Lite mode never produces real-music data (see _visualizer_reactive_frame in retro_tv_emulator.py), so both reactive controls are greyed out and ignore input there.
            _reactive_mode = performance_governor.get_mode() if performance_governor is not None else "lite"
            _reactive_available = _reactive_mode in ("standard", "performance")
            reactive_label = "REACTIVE VISUALIZER"
            reactive_focused = (sub_r == _reactive_row_idx)
            reactive_w = row_w
            reactive_bg = COLOR_MENU_BUTTON if _reactive_available else COLOR_MENU_BUTTON_GREYED
            pygame.draw.rect(menu_canvas, reactive_bg, (row_x, reactive_y, reactive_w, vis_row_h), border_radius=6)
            if reactive_focused: _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, reactive_y, reactive_w, vis_row_h), SELECTION_BORDER_WIDTH, border_radius=6)
            _reactive_label_col = (NEON_GREEN if reactive_focused else TEXT_WHITE) if _reactive_available else COLOR_MENU_TEXT_GREYED
            reactive_text = f_ch_label.render(reactive_label, True, _reactive_label_col)
            menu_canvas.blit(reactive_text, (row_x + 20, reactive_y + vis_row_h // 2 - reactive_text.get_height() // 2))
            toggle_w, toggle_h = 78, 24
            toggle_x = row_x + row_w - toggle_w - 18
            toggle_y = reactive_y + (vis_row_h - toggle_h) // 2
            if _reactive_available:
                _toggle_track_col = COLOR_MENU_ON_GREEN if reactive_enabled else COLOR_MENU_SWITCH_OFF
                _toggle_knob_col = TEXT_WHITE
                _toggle_text_col = TEXT_WHITE
            else:
                _toggle_track_col = COLOR_MENU_TRACK_OFF
                _toggle_knob_col = COLOR_MENU_TEXT_GREYED
                _toggle_text_col = COLOR_MENU_TEXT_GREYED
            pygame.draw.rect(menu_canvas, _toggle_track_col, (toggle_x, toggle_y, toggle_w, toggle_h), border_radius=12)
            pygame.draw.circle(menu_canvas, _toggle_knob_col, (toggle_x + toggle_w - 12 if reactive_enabled else toggle_x + 12, toggle_y + toggle_h // 2), 9)
            toggle_text = f_ch_btn.render("ON" if reactive_enabled else "OFF", True, _toggle_text_col)
            menu_canvas.blit(toggle_text, (toggle_x + toggle_w // 2 - toggle_text.get_width() // 2, toggle_y + toggle_h // 2 - toggle_text.get_height() // 2))

            strength_y = reactive_y + vis_row_h + vis_gap
            is_strength_focused = (sub_r == _reactive_strength_row_idx)
            strength = max(0, min(100, int(ch_info.get("reactive_visualizer_strength", 50))))
            reactive_enabled = reactive_enabled and _reactive_available
            strength_color = NEON_GREEN if is_strength_focused and reactive_enabled else (COLOR_MENU_TEXT_GREYED if not reactive_enabled else TEXT_WHITE)
            strength_label = "REACTIVE STRENGTH"
            label_w = min(245, max(190, row_w // 2 - vis_gap // 2))
            slider_x = row_x + label_w + vis_gap
            slider_w = row_w - label_w - vis_gap
            strength_text = f_ch_label.render(strength_label, True, strength_color)
            menu_canvas.blit(strength_text, (row_x + 14, strength_y + vis_row_h // 2 - strength_text.get_height() // 2))
            slider_y = strength_y + vis_row_h // 2
            track_x = slider_x + 18
            track_w = max(20, slider_w - 36)
            track_color = COLOR_MENU_BUTTON if reactive_enabled else COLOR_MENU_TRACK_OFF
            pygame.draw.rect(menu_canvas, track_color, (track_x, slider_y - 3, track_w, 6), border_radius=3)
            handle_x = track_x + int(track_w * (strength / 100.0))
            pygame.draw.circle(menu_canvas, TEXT_WHITE if reactive_enabled else COLOR_MENU_TEXT_GREYED, (handle_x, slider_y), 8)
            if is_strength_focused:
                pygame.draw.circle(menu_canvas, NEON_GREEN if reactive_enabled else COLOR_MENU_TEXT_GREYED, (handle_x, slider_y), 10, SELECTION_BORDER_WIDTH)



            _has_music_content = len(ch_info.get("schedules", {}).get("Music Tracks", [])) > 0
            is_clear_focused = (sub_r == _clear_row_idx)
            _draw_menu_clear(menu_canvas, cx, content_y, content_h, is_clear_focused, _has_music_content, f_ch_btn, NEON_GREEN)

            _vis_gain_db = int(app_state.get("visualizer_gain_db", 0))
            _vis_gain_path = visualizer_gain_file(app_state, target_ch_str)
            if _vis_gain_path and hasattr(__import__("__main__"), "_get_audio_gain_db"):
                _vis_gain_db = __import__("__main__")._get_audio_gain_db(_vis_gain_path)
            gain_h = 40
            is_gain_focused = (sub_r == _gain_row_idx)
            # WARNING: the row is live only with Loudness Scan on and a song of THIS channel playing; otherwise it is drawn greyed and A and D change nothing.
            _vis_scan_on = db.config.get("loudness_scan_enabled", True)
            _vis_gain_live = _vis_scan_on and bool(_vis_gain_path)
            if _vis_gain_live:
                gain_label = f_ch_label.render(f"LOUDNESS GAIN: {_vis_gain_db:+d} dB", True, NEON_GREEN if is_gain_focused else TEXT_WHITE)
            elif not _vis_scan_on:
                gain_label = f_ch_label.render("LOUDNESS GAIN: LOUDNESS SCAN IS OFF", True, COLOR_MENU_TEXT_GREYED)
            elif not _same_channel_number(app_state.get("current_channel", ""), target_ch_str):
                gain_label = f_ch_label.render("LOUDNESS GAIN: TUNE TO THIS CHANNEL TO ADJUST", True, COLOR_MENU_TEXT_GREYED)
            else:
                gain_label = f_ch_label.render("LOUDNESS GAIN: NO SONG PLAYING", True, COLOR_MENU_TEXT_GREYED)
            pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON if _vis_gain_live else COLOR_MENU_BUTTON_GREYED, (row_x, gain_y, row_w, gain_h), border_radius=6)
            if is_gain_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, gain_y, row_w, gain_h), SELECTION_BORDER_WIDTH, border_radius=6)
            menu_canvas.blit(gain_label, (row_x + 20, gain_y + gain_h // 2 - gain_label.get_height() // 2))
            if _vis_gain_live:
                draw_key_hint(menu_canvas, [("a", "d"), "TO ADJUST"], row_x + 20 + gain_label.get_width() + CONTROLS_HINT_GROUP_GAP_PX,
                              gain_y + gain_h // 2, f_ch_label, NEON_GREEN if is_gain_focused else TEXT_WHITE)

            _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_close_focused, f_ch_btn, NEON_GREEN)
        else:
            row_x = cx + 25
            row_w = content_w - 60

            SCHED_LABELS = {
                "random_slots": "RANDOM",
                "one_slot":     "ONE",
                "two_slots":    "TWO",
                "marathon":     "MARATHON",
            }
            cur_sched = menu_nav.shown_scheduling_mode(ch_info)
            _sched_is_marathon = (cur_sched == "marathon")

            _block_mode  = ch_info.get("block_mode", "full_day")
            _is_full_day = (not _sched_is_marathon) and (_block_mode == "full_day")
            _is_six_block = (not _sched_is_marathon) and (_block_mode == "six_block")
            _folder_row  = 4 if _sched_is_marathon else 5
            _holiday_row = 5 if _sched_is_marathon else 6
            _audio_track_row = _holiday_row
            _close_row   = _holiday_row + 1

            sched_r_h = 36
            sched_y   = grid_y_start
            is_sched_focused = (sub_r == 1 and sub_c == 0)
            _sched_color = NEON_GREEN if is_sched_focused else TEXT_WHITE
            _sched_prefix_surf = f_ch_label.render("NUMBER OF EPISODES:  \u25c4  ", True, _sched_color)
            _sched_word_surf   = f_ch_label.render(SCHED_LABELS.get(cur_sched, cur_sched.upper()), True, _sched_color)
            _sched_suffix_surf = f_ch_label.render("  \u25ba", True, _sched_color)
            _sched_word_slot_w = max(f_ch_label.size(w)[0] for w in SCHED_LABELS.values())
            sched_x = row_x
            _row1_gap = 20
            _row1_right = cx + content_w - 25
            if _sched_is_marathon:
                sched_w = row_w
                order_w = 0
                order_x = 0
            else:
                _order_prefix_txt = "EPISODE ORDER:  \u25c4  "
                _order_suffix_txt = "  \u25ba"
                _order_prefix_probe = f_ch_label.render(_order_prefix_txt, True, TEXT_WHITE)
                _order_suffix_probe = f_ch_label.render(_order_suffix_txt, True, TEXT_WHITE)
                _order_word_seq_probe  = f_ch_label.render("IN ORDER", True, TEXT_WHITE)
                _order_word_rand_probe = f_ch_label.render("RANDOM", True, TEXT_WHITE)
                _order_word_slot_w = max(_order_word_seq_probe.get_width(), _order_word_rand_probe.get_width())
                order_w = _order_prefix_probe.get_width() + _order_word_slot_w + _order_suffix_probe.get_width() + 40
                order_x = _row1_right - order_w
                sched_w = (order_x - _row1_gap) - sched_x
            sched_text_x = sched_x + 20
            _sched_text_y = sched_y + sched_r_h // 2 - _sched_prefix_surf.get_height() // 2
            pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON, (sched_x, sched_y, sched_w, sched_r_h), border_radius=6)
            if is_sched_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (sched_x, sched_y, sched_w, sched_r_h), SELECTION_BORDER_WIDTH, border_radius=6)
            menu_canvas.blit(_sched_prefix_surf, (sched_text_x, _sched_text_y))
            _sched_word_x = sched_text_x + _sched_prefix_surf.get_width() + (_sched_word_slot_w - _sched_word_surf.get_width()) // 2
            menu_canvas.blit(_sched_word_surf, (_sched_word_x, _sched_text_y))
            menu_canvas.blit(_sched_suffix_surf, (sched_text_x + _sched_prefix_surf.get_width() + _sched_word_slot_w, _sched_text_y))

            if not _sched_is_marathon:
                is_order_focused = (sub_r == 1 and sub_c == 1)
                _order_mode = ch_info.get("episode_order_mode", "sequential")
                _order_color = NEON_GREEN if is_order_focused else TEXT_WHITE
                _order_prefix_surf = f_ch_label.render(_order_prefix_txt, True, _order_color)
                _order_word_surf   = f_ch_label.render("IN ORDER" if _order_mode == "sequential" else _order_mode.upper(), True, _order_color)
                _order_suffix_surf = f_ch_label.render(_order_suffix_txt, True, _order_color)
                _order_content_w = _order_prefix_surf.get_width() + _order_word_slot_w + _order_suffix_surf.get_width()
                _order_content_x = order_x + (order_w - _order_content_w) // 2
                _order_text_y = sched_y + sched_r_h // 2 - _order_prefix_surf.get_height() // 2
                pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON, (order_x, sched_y, order_w, sched_r_h), border_radius=6)
                if is_order_focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (order_x, sched_y, order_w, sched_r_h), SELECTION_BORDER_WIDTH, border_radius=6)
                menu_canvas.blit(_order_prefix_surf, (_order_content_x, _order_text_y))
                _order_word_x = _order_content_x + _order_prefix_surf.get_width() + (_order_word_slot_w - _order_word_surf.get_width()) // 2
                menu_canvas.blit(_order_word_surf, (_order_word_x, _order_text_y))
                _order_suffix_x = _order_content_x + _order_prefix_surf.get_width() + _order_word_slot_w
                menu_canvas.blit(_order_suffix_surf, (_order_suffix_x, _order_text_y))

            pair_thresh_y = sched_y + sched_r_h + 16
            pair_r_h      = 36
            is_pair_focused = (sub_r == 2)
            _pt_min = ch_info.get("pair_threshold_minutes", 15)
            _pt_label = "OFF" if _pt_min <= 0 else f"{_pt_min} MIN"
            lbl_pair = f_ch_label.render(
                f"PAIR SHORTS UNDER:   \u25c4  {_pt_label}  \u25ba   (0 = OFF, max 30 MIN)",
                True, NEON_GREEN if is_pair_focused else TEXT_WHITE
            )
            pair_w = min(lbl_pair.get_width() + 40, row_w)
            pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON, (row_x, pair_thresh_y, pair_w, pair_r_h), border_radius=6)
            if is_pair_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, pair_thresh_y, pair_w, pair_r_h), SELECTION_BORDER_WIDTH, border_radius=6)
            menu_canvas.blit(lbl_pair, (row_x + 20, pair_thresh_y + pair_r_h // 2 - lbl_pair.get_height() // 2))

            com_row_y = pair_thresh_y + pair_r_h + 16
            com_h     = 36
            com_on    = ch_info.get("commercials_enabled", False)

            is_com_toggle_focused = (sub_r == 3 and sub_c == 0)
            lbl_com_txt = f_ch_label.render("Commercials:", True, NEON_GREEN if is_com_toggle_focused else TEXT_WHITE)
            menu_canvas.blit(lbl_com_txt, (row_x, com_row_y + com_h // 2 - lbl_com_txt.get_height() // 2))

            _com_pill_x = row_x + lbl_com_txt.get_width() + 20
            _com_pill_y = com_row_y + (com_h - 24) // 2
            com_sw_bg = OFF_GREEN if com_on else COLOR_MENU_SWITCH_OFF
            pygame.draw.rect(menu_canvas, com_sw_bg, (_com_pill_x, _com_pill_y, 55, 24), border_radius=12)
            if is_com_toggle_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (_com_pill_x, _com_pill_y, 55, 24), SELECTION_BORDER_WIDTH, border_radius=12)
            com_sw_handle_x = _com_pill_x + 42 if com_on else _com_pill_x + 12
            pygame.draw.circle(menu_canvas, TEXT_WHITE, (com_sw_handle_x, _com_pill_y + 12), 9)
            if is_com_toggle_focused:
                pygame.draw.circle(menu_canvas, NEON_GREEN, (com_sw_handle_x, _com_pill_y + 12), 11, SELECTION_BORDER_WIDTH)

            if com_on:
                btn_x2 = _com_pill_x + 55 + 24
                is_com_btn_focused = (sub_r == 3 and sub_c == 1)
                _com_file_ct = len(ch_info.get("schedules", {}).get("Commercials", []))
                btn_txt2 = f_ch_label.render(
                    f"COMMERCIALS ({'ADD' if _com_file_ct == 0 else _com_file_ct})",
                    True, NEON_GREEN if is_com_btn_focused else TEXT_WHITE)
                btn_w2 = btn_txt2.get_width() + 40
                pygame.draw.rect(menu_canvas,
                                 COLOR_BUTTON_ADD,
                                 (btn_x2, com_row_y, btn_w2, com_h), border_radius=8)
                if is_com_btn_focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (btn_x2, com_row_y, btn_w2, com_h), SELECTION_BORDER_WIDTH, border_radius=8)
                menu_canvas.blit(btn_txt2, (
                    btn_x2 + btn_w2 // 2 - btn_txt2.get_width() // 2,
                    com_row_y + com_h // 2 - btn_txt2.get_height() // 2
                ))

                _placement = ch_info.get("commercial_placement", "interrupt_half_hour")
                _placement_label = "END OF SHOW" if _placement == "end_of_show" else "INTERRUPT SHOW"
                is_com_mode_focused = (sub_r == 3 and sub_c == 2)
                mode_btn_x = btn_x2 + btn_w2 + 12
                _mode_lbl_surf = f_ch_label.render(f"MODE: {_placement_label}", True,
                                               NEON_GREEN if is_com_mode_focused else TEXT_WHITE)
                mode_btn_w = _mode_lbl_surf.get_width() + 40
                pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON,
                                 (mode_btn_x, com_row_y, mode_btn_w, com_h), border_radius=8)
                if is_com_mode_focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (mode_btn_x, com_row_y, mode_btn_w, com_h), SELECTION_BORDER_WIDTH, border_radius=8)
                menu_canvas.blit(_mode_lbl_surf, (
                    mode_btn_x + mode_btn_w // 2 - _mode_lbl_surf.get_width() // 2,
                    com_row_y + com_h // 2 - _mode_lbl_surf.get_height() // 2
                ))

            bl_h = 36
            if _sched_is_marathon:
                block_len_row_bottom = com_row_y + com_h + 16
            else:
                bl_y = com_row_y + com_h + 16
                is_bl_focused = (sub_r == 4 and sub_c == 0)
                if _is_full_day:
                    _bl_label = "24-HOUR (FULL DAY)"
                elif _is_six_block:
                    _bl_label = "4-HOUR BLOCKS"
                else:
                    _bl_label = "8-HOUR BLOCKS"
                _bl_labels = (
                    "SCHEDULE LENGTH:   ◄  24-HOUR (FULL DAY)  ►",
                    "SCHEDULE LENGTH:   ◄  8-HOUR BLOCKS  ►",
                    "SCHEDULE LENGTH:   ◄  4-HOUR BLOCKS  ►",
                )
                _bl_label = _bl_labels[0] if _is_full_day else (_bl_labels[2] if _is_six_block else _bl_labels[1])
                lbl_bl = f_ch_label.render(_bl_label, True, NEON_GREEN if is_bl_focused else TEXT_WHITE)
                bl_w = min(max(f_ch_label.size(label)[0] for label in _bl_labels) + 40, row_w)
                pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON, (row_x, bl_y, bl_w, bl_h), border_radius=6)
                if is_bl_focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, bl_y, bl_w, bl_h), SELECTION_BORDER_WIDTH, border_radius=6)
                menu_canvas.blit(lbl_bl, (row_x + 20, bl_y + bl_h // 2 - lbl_bl.get_height() // 2))

                _wm_enabled = bool(ch_info.get("watermark_enabled", False))
                is_wm_focused = (sub_r == 4 and sub_c == 1)
                _wm_label = f_ch_btn.render("Watermark:", True, NEON_GREEN if is_wm_focused else TEXT_WHITE)
                _wm_pill_w, _wm_pill_h = 55, 24
                _wm_add_w = 70
                _wm_label_pad = 14
                _wm_part_gap = 16
                _wm_box_w = _wm_label.get_width() + 2 * _wm_label_pad
                _wm_group_x = row_x + bl_w + 16
                _wm_label_x = _wm_group_x + _wm_label_pad
                _wm_pill_x = _wm_group_x + _wm_box_w + _wm_part_gap
                _wm_add_x = _wm_pill_x + _wm_pill_w + _wm_part_gap
                _wm_y = bl_y
                pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON, (_wm_group_x, _wm_y, _wm_box_w, bl_h), border_radius=4)
                menu_canvas.blit(_wm_label, (_wm_label_x, _wm_y + (bl_h - _wm_label.get_height()) // 2))
                _wm_pill_y = _wm_y + (bl_h - _wm_pill_h) // 2
                _wm_pill_bg = OFF_GREEN if _wm_enabled else COLOR_MENU_SWITCH_OFF
                pygame.draw.rect(menu_canvas, _wm_pill_bg, (_wm_pill_x, _wm_pill_y, _wm_pill_w, _wm_pill_h), border_radius=12)
                if is_wm_focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (_wm_pill_x, _wm_pill_y, _wm_pill_w, _wm_pill_h), SELECTION_BORDER_WIDTH, border_radius=12)
                _wm_handle_x = _wm_pill_x + 42 if _wm_enabled else _wm_pill_x + 12
                pygame.draw.circle(menu_canvas, TEXT_WHITE, (_wm_handle_x, _wm_pill_y + _wm_pill_h // 2), 9)
                is_wm_add_focused = (sub_r == 4 and sub_c == 2)
                _wm_add_label = f_ch_btn.render("Add", True, COLOR_MENU_VALUE_MINT if is_wm_add_focused else TEXT_WHITE)
                pygame.draw.rect(menu_canvas, COLOR_BUTTON_TOGGLE_ADD, (_wm_add_x, _wm_y, _wm_add_w, bl_h), border_radius=4)
                if is_wm_add_focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (_wm_add_x, _wm_y, _wm_add_w, bl_h), SELECTION_BORDER_WIDTH, border_radius=4)
                menu_canvas.blit(_wm_add_label, (_wm_add_x + (_wm_add_w - _wm_add_label.get_width()) // 2, _wm_y + (bl_h - _wm_add_label.get_height()) // 2))
                block_len_row_bottom = bl_y + bl_h + 16

            folder_y_start = block_len_row_bottom
            f_cell_h = 44
            slots_total_w = row_w
            if cur_sched == "marathon":
                is_slot_focused = (sub_r == _folder_row)
                file_count = len(ch_info.get("schedules", {}).get("Marathon", []))
                pygame.draw.rect(menu_canvas, COLOR_BUTTON_ADD, (row_x, folder_y_start, slots_total_w, f_cell_h), border_radius=6)
                if is_slot_focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, folder_y_start, slots_total_w, f_cell_h), SELECTION_BORDER_WIDTH, border_radius=6)
                lbl_m = f_ch_label.render(
                    f"24-HOUR MARATHON  ({'ADD' if file_count == 0 else file_count})",
                    True, NEON_GREEN if is_slot_focused else TEXT_WHITE
                )
                menu_canvas.blit(lbl_m, (row_x + slots_total_w // 2 - lbl_m.get_width() // 2,
                                         folder_y_start + f_cell_h // 2 - lbl_m.get_height() // 2))
            elif _is_full_day:
                is_slot_focused = (sub_r == _folder_row)
                file_count = len(ch_info.get("schedules", {}).get("Full Day", []))
                pygame.draw.rect(menu_canvas, COLOR_BUTTON_ADD, (row_x, folder_y_start, slots_total_w, f_cell_h), border_radius=6)
                if is_slot_focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, folder_y_start, slots_total_w, f_cell_h), SELECTION_BORDER_WIDTH, border_radius=6)
                lbl_fd = f_ch_label.render(
                    f"24-HOUR BLOCK  ({'ADD' if file_count == 0 else file_count})",
                    True, NEON_GREEN if is_slot_focused else TEXT_WHITE
                )
                menu_canvas.blit(lbl_fd, (row_x + slots_total_w // 2 - lbl_fd.get_width() // 2,
                                          folder_y_start + f_cell_h // 2 - lbl_fd.get_height() // 2))
            else:
                f_gap = 12
                if _is_six_block:
                    _slot_names = media.SIX_HOUR_BLOCK_NAMES
                else:
                    _slot_names = ["Morning", "Evening", "Night"]

                _n = len(_slot_names)
                _cw = (slots_total_w - (_n - 1) * f_gap) // _n

                if _is_six_block:
                    _pad = 16
                    _slot_font = f_ch_label
                    for _try_size in range(22, 9, -1):
                        _cand_font = _get_font("Courier New", _try_size, bold=True)
                        _widest = max(
                            _cand_font.render(f"{n.upper()} (9999)", True, TEXT_WHITE).get_width()
                            for n in _slot_names
                        )
                        if _widest <= _cw - _pad:
                            _slot_font = _cand_font
                            break
                    else:
                        _slot_font = _get_font("Courier New", 10, bold=True)
                else:
                    _slot_font = f_ch_label

                def _draw_slot(idx, name, item_x, item_y, cell_w, cell_h):
                    is_slot_focused = (sub_r == _folder_row and sub_c == idx)
                    file_count = len(ch_info.get("schedules", {}).get(name, []))
                    pygame.draw.rect(menu_canvas, COLOR_BUTTON_ADD, (item_x, item_y, cell_w, cell_h), border_radius=6)
                    if is_slot_focused:
                        _aa_rounded_rect(menu_canvas, NEON_GREEN, (item_x, item_y, cell_w, cell_h), SELECTION_BORDER_WIDTH, border_radius=6)
                    lbl_slot = _slot_font.render(
                        f"{name.upper()} ({'ADD' if file_count == 0 else file_count})",
                        True, NEON_GREEN if is_slot_focused else TEXT_WHITE
                    )
                    menu_canvas.blit(lbl_slot, (item_x + cell_w // 2 - lbl_slot.get_width() // 2,
                                                item_y + cell_h // 2 - lbl_slot.get_height() // 2))

                for i, name in enumerate(_slot_names):
                    item_x = row_x + i * (_cw + f_gap)
                    _draw_slot(i, name, item_x, folder_y_start, _cw, f_cell_h)

            hol_y  = folder_y_start + f_cell_h + 20
            hol_h  = 40
            if not _sched_is_marathon:
                is_hol_focused = (sub_r == _holiday_row and sub_c == 0)
                lbl_hol = f_ch_label.render("HOLIDAY SCHEDULE", True, NEON_GREEN if is_hol_focused else TEXT_WHITE)
                hol_w = lbl_hol.get_width() + 40
                pygame.draw.rect(menu_canvas, COLOR_BUTTON_ADD, (row_x, hol_y, hol_w, hol_h), border_radius=6)
                if is_hol_focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, hol_y, hol_w, hol_h), SELECTION_BORDER_WIDTH, border_radius=6)
                menu_canvas.blit(lbl_hol, (row_x + 20, hol_y + hol_h // 2 - lbl_hol.get_height() // 2))
            else:
                hol_w = 0

            _is_live_ch_for_audio = (str(app_state.get("current_channel", "")).zfill(2) == target_ch_str)
            if cur_sched == "marathon":
                _atr_has_media = len(ch_info.get("schedules", {}).get("Marathon", [])) > 0
            elif _is_full_day:
                _atr_has_media = len(ch_info.get("schedules", {}).get("Full Day", [])) > 0
            else:
                _atr_slot_names = media.SIX_HOUR_BLOCK_NAMES if _is_six_block else ["Morning", "Evening", "Night"]
                _atr_has_media = any(len(ch_info.get("schedules", {}).get(n, [])) > 0 for n in _atr_slot_names)
            _atr_enabled = _is_live_ch_for_audio and _atr_has_media
            atr_y = hol_y
            atr_h = hol_h
            _atr_col = 0 if _sched_is_marathon else 1
            is_atr_focused = (sub_r == _audio_track_row and sub_c == _atr_col)
            _atr_bg = COLOR_BUTTON_AUDIO_TRACK if _atr_enabled else COLOR_MENU_BUTTON_GREYED
            _atr_txt_color = COLOR_BUTTON_AUDIO_TRACK_TEXT if _atr_enabled else COLOR_MENU_TEXT_GREYED
            lbl_atr = f_ch_label.render(
                "CURRENT PLAYING AUDIO SETTINGS  \u25ba",
                True, _atr_txt_color
            )
            _atr_gap = 0 if _sched_is_marathon else 16
            atr_x = row_x + hol_w + _atr_gap
            atr_w = min(lbl_atr.get_width() + 56, (row_x + row_w) - atr_x)
            pygame.draw.rect(menu_canvas, _atr_bg, (atr_x, atr_y, atr_w, atr_h), border_radius=6)
            if is_atr_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (atr_x, atr_y, atr_w, atr_h), SELECTION_BORDER_WIDTH, border_radius=6)
            menu_canvas.blit(lbl_atr, (atr_x + atr_w // 2 - lbl_atr.get_width() // 2, atr_y + atr_h // 2 - lbl_atr.get_height() // 2))

            is_clear_focused = (sub_r == _close_row and sub_c == 0)
            _has_sched_content = media.channel_has_clearable_media(ch_info)
            _draw_menu_clear(menu_canvas, cx, content_y, content_h, is_clear_focused, _has_sched_content, f_ch_btn, NEON_GREEN)

            is_close_focused = (sub_r == _close_row and sub_c == 3)
            _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_close_focused, f_ch_btn, NEON_GREEN)

        if ch_info.get("server_locked", False):
            _lock_top = content_y + 55
            _lock_bottom = content_y + content_h - 60
            if _lock_bottom > _lock_top:
                _lock_dim = pygame.Surface(
                    (content_w - 16, _lock_bottom - _lock_top),
                    pygame.SRCALPHA,
                )
                _lock_dim.fill(COLOR_MENU_LOCK_DIM)
                menu_canvas.blit(_lock_dim, (cx + 8, _lock_top))
            _lock_font = _get_font("Courier New", 15, bold=True)
            _lock_label = _lock_font.render(
                "SERVER CONTROLLED  -  EDITING DISABLED",
                True, COLOR_MENU_LOCK_TEXT,
            )
            menu_canvas.blit(
                _lock_label,
                (cx + content_w // 2 - _lock_label.get_width() // 2,
                 content_y + 42),
            )

    elif layer == "AUDIO_TRACK_PICKER" and active_tab in ("Channels", "Games"):
        _audio_ctx = app_state.get("audio_track_context", "channel")
        if _audio_ctx == "dvd":
            f_sub_title = f_ch_label
            lbl_hdr = f_sub_title.render("DVD \u2014 Change Audio Selection", True, TEXT_WHITE)
        else:
            target_ch_str = str(app_state.get("audio_track_source_channel", app_state.get("selected_channel_row", 5))).zfill(2)
            f_sub_title = f_ch_label
            lbl_hdr = f_sub_title.render(
                f"Channel {target_ch_str} \u2014 Audio Settings", True, TEXT_WHITE
            )
        menu_canvas.blit(lbl_hdr, (cx + content_w // 2 - lbl_hdr.get_width() // 2, content_y + 20))

        sub_r  = app_state.get("sub_menu_row_index", 0)
        tracks = app_state.get("audio_track_list", [])
        active_id = app_state.get("audio_track_active_id")
        gain_row = len(tracks)
        close_row = gain_row + 1
        gain_db = int(app_state.get("audio_track_gain_db", 0))

        row_h  = 40
        row_gap = 8
        list_top = content_y + 65
        row_x = cx + 25
        row_w = content_w - 60

        if not tracks:
            f_msg = _get_font("Courier New", 16, bold=True)
            lbl_none = f_msg.render("No audio tracks found for the current file.", True, TEXT_WHITE)
            menu_canvas.blit(lbl_none, (cx + content_w // 2 - lbl_none.get_width() // 2, list_top + 20))
        else:
            box_size = 16
            for t_idx, (track_id, track_label) in enumerate(tracks):
                ty = list_top + t_idx * (row_h + row_gap)
                is_focused = (sub_r == t_idx)
                is_active  = (track_id == active_id)
                pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON, (row_x, ty, row_w, row_h), border_radius=6)
                if is_focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, ty, row_w, row_h), SELECTION_BORDER_WIDTH, border_radius=6)

                box_x = row_x + 15
                box_y = ty + row_h // 2 - box_size // 2
                box_color = NEON_GREEN if is_focused else TEXT_WHITE
                if is_active:
                    pygame.draw.rect(menu_canvas, box_color, (box_x, box_y, box_size, box_size), border_radius=3)
                else:
                    pygame.draw.rect(menu_canvas, box_color, (box_x, box_y, box_size, box_size), 2, border_radius=3)

                lbl_track = f_ch_label.render(
                    track_label,
                    True, NEON_GREEN if is_focused else TEXT_WHITE
                )
                menu_canvas.blit(lbl_track, (box_x + box_size + 14, ty + row_h // 2 - lbl_track.get_height() // 2))

        is_gain_focused = (sub_r == gain_row)
        gain_y = list_top + len(tracks) * (row_h + row_gap)
        # WARNING: with Loudness Scan off no gain is played, so the row is drawn greyed (reachable, not adjustable), like the other greyed rows.
        _gain_live = db.config.get("loudness_scan_enabled", True)
        pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON if _gain_live else COLOR_MENU_BUTTON_GREYED, (row_x, gain_y, row_w, row_h), border_radius=6)
        if is_gain_focused:
            _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, gain_y, row_w, row_h), SELECTION_BORDER_WIDTH, border_radius=6)
        lbl_gain = f_ch_label.render(
            f"LOUDNESS GAIN: {gain_db:+d} dB" if _gain_live else "LOUDNESS GAIN: LOUDNESS SCAN IS OFF",
            True, (NEON_GREEN if is_gain_focused else TEXT_WHITE) if _gain_live else COLOR_MENU_TEXT_GREYED
        )
        menu_canvas.blit(lbl_gain, (row_x + 15, gain_y + row_h // 2 - lbl_gain.get_height() // 2))
        if _gain_live:
            draw_key_hint(menu_canvas, [("a", "d"), "TO ADJUST"], row_x + 15 + lbl_gain.get_width() + CONTROLS_HINT_GROUP_GAP_PX,
                          gain_y + row_h // 2, f_ch_label, NEON_GREEN if is_gain_focused else TEXT_WHITE)

        is_close_focused = (sub_r == close_row)
        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_close_focused, f_ch_btn, NEON_GREEN)

    # ========================================================================
    # SECTION: Channels tab: commercials and bumper sub-menu
    # ========================================================================

    elif active_tab == "Channels" and layer == "COMMERCIALS_SUB_MENU":
        target_ch_str = str(app_state.get("selected_channel_row", 5)).zfill(2)
        ch_info = db.channels_db.get(target_ch_str, {})
        sub_r = app_state.get("sub_menu_row_index", 0)
        labels = ("Commercials", "Fade-in", "Fade-out")
        keys = ("Commercials", "Fade-in", "Fade-out")
        f_sub_title = _get_font("Courier New", 20, bold=True)
        lbl_hdr = f_sub_title.render(f"Channel {target_ch_str} — Commercials", True, TEXT_WHITE)
        menu_canvas.blit(lbl_hdr, (cx + content_w // 2 - lbl_hdr.get_width() // 2, content_y + 20))
        row_w = int(content_w * SUB_MENU_ROW_WIDTH_SHARE)
        row_x = cx + (content_w - row_w) // 2
        row_h = 44
        row_gap = SUB_MENU_ROW_GAP_PX
        _com_area_top = content_y + HOLIDAY_ROWS_TOP_PX
        _com_area_bottom = content_y + content_h - HOLIDAY_ROWS_BOTTOM_PX
        _com_block_h = row_h * len(labels) + row_gap * (len(labels) - 1)
        row_top = _com_area_top + max(0, (_com_area_bottom - _com_area_top - _com_block_h) // 2)
        for idx, (label, key) in enumerate(zip(labels, keys)):
            y = row_top + idx * (row_h + row_gap)
            focused = sub_r == idx
            count = len(ch_info.get("schedules", {}).get(key, []))
            pygame.draw.rect(menu_canvas, COLOR_BUTTON_ADD, (row_x, y, row_w, row_h), border_radius=6)
            if focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, y, row_w, row_h), SELECTION_BORDER_WIDTH, border_radius=6)
            text = f_ch_label.render(f"{label.upper()} ({'ADD' if count == 0 else count})", True, NEON_GREEN if focused else TEXT_WHITE)
            menu_canvas.blit(text, (row_x + row_w // 2 - text.get_width() // 2, y + row_h // 2 - text.get_height() // 2))
        focused = sub_r == menu_nav.COMMERCIALS_CLEAR_ROW
        _com_has_media = any(len(ch_info.get("schedules", {}).get(k, [])) > 0 for k in keys)
        _draw_menu_clear(menu_canvas, cx, content_y, content_h, focused, _com_has_media, f_ch_btn, NEON_GREEN)

        focused = sub_r == menu_nav.COMMERCIALS_CLOSE_ROW
        close_x = cx + content_w - 135
        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, focused, f_ch_btn, NEON_GREEN)

    # ========================================================================
    # SECTION: Channels tab: holiday sub-menu
    # ========================================================================

    elif active_tab == "Channels" and layer == "HOLIDAY_SUB_MENU":
        target_ch_str = str(app_state.get("selected_channel_row", 5)).zfill(2)
        ch_info = db.channels_db.get(
            target_ch_str,
            {"active": True, "is_visualizer": False, "name": f"STATION {target_ch_str}"}
        )

        f_sub_title = _get_font("Courier New", 20, bold=True)
        lbl_hdr = f_sub_title.render(
            f"Channel {target_ch_str}  —  Holiday Schedule", True, TEXT_WHITE
        )
        menu_canvas.blit(lbl_hdr, (cx + content_w // 2 - lbl_hdr.get_width() // 2, content_y + 20))

        sub_r  = app_state.get("sub_menu_row_index", 0)
        sub_c  = app_state.get("sub_menu_col_index", 0)
        HKEYS  = list(media.HOLIDAY_KEYS)
        HNAMES = ["Halloween", "Christmas", "Thanksgiving"]
        _force_states = media.holiday_force_row_states(db.config, target_ch_str) if db is not None else {}

        _hol_top = content_y + HOLIDAY_ROWS_TOP_PX
        _hol_bottom = content_y + content_h - HOLIDAY_ROWS_BOTTOM_PX
        _hol_slot_h = max(1, (_hol_bottom - _hol_top) // 3)
        cell_h_hol = max(HOLIDAY_ROW_MIN_H_PX, min(HOLIDAY_ROW_MAX_H_PX, _hol_slot_h - HOLIDAY_ROW_SLOT_PAD_PX))
        _tog_w     = HOLIDAY_TOGGLE_W_PX
        _tog_gap   = HOLIDAY_TOGGLE_GAP_PX
        _row_w     = int(content_w * SUB_MENU_ROW_WIDTH_SHARE)
        _pick_w    = _row_w - _tog_w - _tog_gap
        _pair_x    = cx + (content_w - _row_w) // 2
        _hol_block_h = cell_h_hol * 3 + SUB_MENU_ROW_GAP_PX * 2
        _hol_block_top = _hol_top + max(0, (_hol_bottom - _hol_top - _hol_block_h) // 2)

        for h_idx, (h_key, h_name) in enumerate(zip(HKEYS, HNAMES)):
            hy = _hol_block_top + h_idx * (cell_h_hol + SUB_MENU_ROW_GAP_PX)
            is_focused = (sub_r == h_idx and sub_c == 0)
            entries    = ch_info.get("holiday_schedules", {}).get(h_key, [])
            fc = len(entries)
            pygame.draw.rect(menu_canvas, COLOR_BUTTON_ADD, (_pair_x, hy, _pick_w, cell_h_hol), border_radius=6)
            if is_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (_pair_x, hy, _pick_w, cell_h_hol), SELECTION_BORDER_WIDTH, border_radius=6)
            lbl_h = f_ch_label.render(
                f"{h_name.upper()} ({'ADD' if fc == 0 else fc})",
                True, NEON_GREEN if is_focused else TEXT_WHITE
            )
            menu_canvas.blit(lbl_h, (_pair_x + _pick_w // 2 - lbl_h.get_width() // 2, hy + cell_h_hol // 2 - lbl_h.get_height() // 2))

            _tog_on = (_force_states.get(h_key) == "on")
            _tog_greyed = (_force_states.get(h_key, "greyed") == "greyed")
            _tog_focused = (sub_r == h_idx and sub_c == 1)
            _tog_x = _pair_x + _pick_w + _tog_gap
            _tog_bg = COLOR_MENU_TOGGLE_GREYED if _tog_greyed else COLOR_MENU_BUTTON
            pygame.draw.rect(menu_canvas, _tog_bg, (_tog_x, hy, _tog_w, cell_h_hol), border_radius=6)
            if _tog_focused:
                _aa_rounded_rect(menu_canvas, COLOR_MENU_TOGGLE_GREYED_EDGE if _tog_greyed else NEON_GREEN, (_tog_x, hy, _tog_w, cell_h_hol), SELECTION_BORDER_WIDTH, border_radius=6)
            elif _tog_greyed:
                pygame.draw.rect(menu_canvas, COLOR_MENU_EDGE_GREYED, (_tog_x, hy, _tog_w, cell_h_hol), 2, border_radius=6)
            _tog_txt = (NEON_GREEN if _tog_focused else TEXT_WHITE) if not _tog_greyed else COLOR_MENU_TEXT_GREYED_LILAC
            lbl_force = f_ch_label.render("FORCE", True, _tog_txt)
            menu_canvas.blit(lbl_force, (_tog_x + 14, hy + cell_h_hol // 2 - lbl_force.get_height() // 2))
            _sw_w, _sw_h = 55, 24
            _sw_x = _tog_x + _tog_w - _sw_w - 14
            _sw_y = hy + cell_h_hol // 2 - _sw_h // 2
            pygame.draw.rect(menu_canvas, COLOR_MENU_SWITCH_GREYED if _tog_greyed else (OFF_GREEN if _tog_on else COLOR_MENU_SWITCH_OFF),
                             (_sw_x, _sw_y, _sw_w, _sw_h), border_radius=12)
            _knob_x = _sw_x + _sw_w - 12 if _tog_on else _sw_x + 12
            pygame.draw.circle(menu_canvas, COLOR_MENU_KNOB_GREYED if _tog_greyed else TEXT_WHITE, (_knob_x, _sw_y + _sw_h // 2), 9)

        is_clear_focused = (sub_r == menu_nav.HOLIDAY_CLEAR_ROW)
        _hol_has_media = any(len(v or []) > 0 for v in (ch_info.get("holiday_schedules") or {}).values())
        _draw_menu_clear(menu_canvas, cx, content_y, content_h, is_clear_focused, _hol_has_media, f_ch_btn, NEON_GREEN)

        is_back_focused = (sub_r == menu_nav.HOLIDAY_CLOSE_ROW)
        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_back_focused, f_ch_btn, NEON_GREEN)

    # ========================================================================
    # SECTION: Video tab
    # ========================================================================

    if active_tab == "Video" or active_tab == "video" or active_tab.upper() == "VIDEO":
        
        top_buttons_y = content_y + 20
        close_button_y = content_y + content_h - 50

        box_radius = 4
        inner_radius = 3
        focus_thickness = SELECTION_BORDER_WIDTH

        _screen_mode_val = db.config.get("screen_mode", "normal") if db is not None else "normal"
        try:
            _srv_sm = db.remote_screen_mode_for(app_state.get("current_channel", "")) if db is not None else None
            if _srv_sm:
                _screen_mode_val = _srv_sm
        except Exception as _swallowed_error:
            _log_swallowed("_build_settings_overlay_menu: _srv_sm = db.remote_screen_mode_for(app_state.get('current_c", _swallowed_error)

        _radio_row_y = top_buttons_y + 6

        _radio_col_gap = 32
        _normal_w   = (_radio_dot_r * 2) + 10 + f_ch_label.size("Normal Mode")[0]
        _stretch_w  = (_radio_dot_r * 2) + 10 + f_ch_label.size("Stretch Fit")[0]
        _crop_w     = (_radio_dot_r * 2) + 10 + f_ch_label.size("Crop Fit")[0]
        _panscan_w  = (_radio_dot_r * 2) + 10 + f_ch_label.size("Pan & Scan")[0]
        _radio_total_w = (_normal_w + _radio_col_gap + _stretch_w + _radio_col_gap
                          + _crop_w + _radio_col_gap + _panscan_w)
        _normal_x   = cx + (content_w // 2) - (_radio_total_w // 2)
        _stretch_x  = _normal_x + _normal_w + _radio_col_gap
        _crop_x     = _stretch_x + _stretch_w + _radio_col_gap
        _panscan_x  = _crop_x + _crop_w + _radio_col_gap

        _draw_footer_radio(_normal_x, _radio_row_y, "Normal Mode",
                           _screen_mode_val == "normal",
                           layer == "VIDEO_ROWS" and cur_sel == 0)
        _draw_footer_radio(_stretch_x, _radio_row_y, "Stretch Fit",
                           _screen_mode_val == "stretch",
                           layer == "VIDEO_ROWS" and cur_sel == 1)
        _draw_footer_radio(_crop_x, _radio_row_y, "Crop Fit",
                           _screen_mode_val == "crop",
                           layer == "VIDEO_ROWS" and cur_sel == 2)
        _draw_footer_radio(_panscan_x, _radio_row_y, "Pan & Scan",
                           _screen_mode_val == "panscan",
                           layer == "VIDEO_ROWS" and cur_sel == 3)

        slider_area_top = _radio_row_y + box_h_43 + 22
        slider_area_bottom = close_button_y - 20
        _slider_row_reserve = VIDEO_SLIDER_ROW_RESERVE_PX
        slider_spacing = min(VIDEO_SLIDER_SPACING_MAX_PX, max(VIDEO_SLIDER_SPACING_MIN_PX, (slider_area_bottom - slider_area_top - _slider_row_reserve) // 4))

        sliders = [
            (4, "Brightness:", db.config.get("brightness", 50), slider_area_top + slider_spacing * 0),
            (5, "Contrast:", db.config.get("contrast", 50), slider_area_top + slider_spacing * 1),
            (6, "Color:", db.config.get("color", 50), slider_area_top + slider_spacing * 2),
            (7, "Sharpness:", db.config.get("sharpness", 50), slider_area_top + slider_spacing * 3),
            (8, "Tint:", db.config.get("tint", 50), slider_area_top + slider_spacing * 4)
        ]
        
        slider_padding_x = 35
        slider_w_total = content_w - (slider_padding_x * 2)
        slider_bar_h = 6
        handle_rad = 8
        focus_handle_rad = 10
        
        for s_idx, s_lbl, s_val, sy in sliders:
            is_sl_focused = (layer == "VIDEO_ROWS" and cur_sel == s_idx)
            t_color = NEON_GREEN if is_sl_focused else TEXT_WHITE
            
            lbl_s = f_menu_lg.render(f"{s_lbl} {s_val}", True, t_color)
            menu_canvas.blit(lbl_s, (cx + slider_padding_x, sy))
            
            pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON, (cx + slider_padding_x, sy + 30, slider_w_total, slider_bar_h), border_radius=inner_radius)
            handle_x = cx + slider_padding_x + int((slider_w_total * (s_val / 100.0)))
            
            pygame.draw.circle(menu_canvas, TEXT_WHITE, (handle_x, sy + 33), handle_rad)
            if is_sl_focused: pygame.draw.circle(menu_canvas, NEON_GREEN, (handle_x, sy + 33), focus_handle_rad, SELECTION_BORDER_WIDTH)

        is_cl_focused = (layer == "VIDEO_ROWS" and cur_sel == 9)

        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_cl_focused, f_ch_btn, NEON_GREEN)

    # ========================================================================
    # SECTION: Theme tab
    # ========================================================================

    elif active_tab == "Theme" or active_tab == "theme" or active_tab.upper() == "THEME":
        import colorsys
        
        bg_hue = db.config.get("theme_bg_hue", 220) if db is not None else 220  
        ui_hue = db.config.get("theme_ui_hue", 140) if db is not None else 140  
        
        slider_width = content_w - 100
        slider_x = cx + 50

        gradient_height = 18
        knob_rad = 10
        focus_knob_rad = 12
        box_radius = 4
        inner_radius = 3
        focus_thickness = SELECTION_BORDER_WIDTH
        
        slider_padding_x = 50
        slider_w_total = content_w - (slider_padding_x * 2)
        slider_bar_h = 6
        handle_rad = 8
        focus_handle_rad = 10

        BUTTON_BLUE = COLOR_MENU_BUTTON_BLUE
        trans_btn_h = 40

        close_btn_y = content_y + content_h - 52

        top_area_y = content_y + 20

        vol_bar_button_y = top_area_y + 6
        trans_type_button_y = vol_bar_button_y + trans_btn_h + 15
        static_button_y = vol_bar_button_y

        slider_block_top = trans_type_button_y + trans_btn_h + 34
        slider_area_bottom = close_btn_y - 15
        _last_block_reserve = THEME_SLIDER_LAST_BLOCK_RESERVE_PX + gradient_height
        block_spacing = min(THEME_SLIDER_SPACING_MAX_PX, max(THEME_SLIDER_SPACING_MIN_PX, (slider_area_bottom - slider_block_top - _last_block_reserve) // 2))
        _first_gap = max(THEME_SLIDER_FIRST_GAP_MIN_PX, block_spacing - THEME_SLIDER_FIRST_GAP_TRIM_PX)

        trans_title_y = slider_block_top
        trans_slider_y = trans_title_y + 30

        bg_title_y = trans_title_y + _first_gap
        bg_slider_y = bg_title_y + 50

        ui_title_y = bg_title_y + block_spacing
        ui_slider_y = ui_title_y + 50

        _theme_block_top = vol_bar_button_y
        _theme_block_bottom = ui_slider_y + gradient_height
        _theme_dy = (content_y + close_btn_y - _theme_block_top - _theme_block_bottom) // 2
        vol_bar_button_y += _theme_dy
        static_button_y += _theme_dy
        trans_type_button_y += _theme_dy
        trans_title_y += _theme_dy
        trans_slider_y += _theme_dy
        bg_title_y += _theme_dy
        bg_slider_y += _theme_dy
        ui_title_y += _theme_dy
        ui_slider_y += _theme_dy

        _theme_col = app_state.get("theme_row2_col", 0)
        is_vol_bar_focused = (layer == "THEME_ROWS" and cur_sel == 0 and _theme_col == 0)
        is_static_focused = (layer == "THEME_ROWS" and cur_sel == 0 and _theme_col == 1)
        is_tt_focused = (layer == "THEME_ROWS" and cur_sel == 1 and _theme_col == 0)
        is_lb_focused = (layer == "THEME_ROWS" and cur_sel == 1 and _theme_col == 1)

        vol_bar_style_val = db.config.get("volume_bar_style", "1")
        vol_bar_value_text = "VOLUME 2" if vol_bar_style_val == "2" else "VOLUME 1"

        static_color_val = db.config.get("static_noise_color", "white")
        static_value_text = {"rainbow": "RAINBOW", "theme": "THEME", "white": "WHITE"}.get(static_color_val, "GREY")

        transition_type_val = db.config.get("transition_type", "black")
        tt_value_text = "STATIC" if transition_type_val == "static" else "BLACK"

        letterbox_color_val = db.config.get("letterbox_color", "black")
        lb_value_text = "GREY" if letterbox_color_val == "grey" else "BLACK"

        _pair_btn_label_w = max(
            f_menu_lg.size("VOLUME BAR: VOLUME 1")[0],
            f_menu_lg.size("VOLUME BAR: VOLUME 2")[0],
            f_menu_lg.size("CHANNEL STATIC: RAINBOW")[0],
            f_menu_lg.size("CHANNEL STATIC: WHITE")[0],
            f_menu_lg.size("CHANNEL STATIC: THEME")[0],
            f_menu_lg.size("CHANNEL STATIC: GREY")[0],
            f_menu_lg.size("CHANNEL TRANSITION: BLACK")[0],
            f_menu_lg.size("CHANNEL TRANSITION: STATIC")[0],
            f_menu_lg.size("LETTERBOXING: BLACK")[0],
            f_menu_lg.size("LETTERBOXING: GREY")[0],
        )
        _pair_btn_w = _pair_btn_label_w + 40
        _pair_gap = 20
        _pair_total_w = (_pair_btn_w * 2) + _pair_gap
        _pair_start_x = cx + (content_w // 2) - (_pair_total_w // 2)
        _pair_right_x = _pair_start_x + _pair_btn_w + _pair_gap

        def _theme_pair_button(btn_x, btn_y, label_text, focused):
            pygame.draw.rect(menu_canvas, BUTTON_BLUE, (btn_x, btn_y, _pair_btn_w, trans_btn_h), border_radius=box_radius)
            if focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (btn_x, btn_y, _pair_btn_w, trans_btn_h), focus_thickness, border_radius=box_radius)
            _lbl = f_menu_lg.render(label_text, True, TEXT_WHITE)
            menu_canvas.blit(_lbl, (btn_x + (_pair_btn_w // 2) - (_lbl.get_width() // 2), btn_y + (trans_btn_h // 2) - (f_menu_lg.get_height() // 2)))

        _theme_pair_button(_pair_start_x, vol_bar_button_y, f"VOLUME BAR: {vol_bar_value_text}", is_vol_bar_focused)
        _theme_pair_button(_pair_right_x, static_button_y, f"CHANNEL STATIC: {static_value_text}", is_static_focused)
        _theme_pair_button(_pair_start_x, trans_type_button_y, f"CHANNEL TRANSITION: {tt_value_text}", is_tt_focused)
        _theme_pair_button(_pair_right_x, trans_type_button_y, f"LETTERBOXING: {lb_value_text}", is_lb_focused)

        is_trans_focused = (layer == "THEME_ROWS" and cur_sel == 2)
        title_color = NEON_GREEN if is_trans_focused else TEXT_WHITE
        
        trans_val = db.config.get("menu_opacity", 50)
        lbl_trans = f_menu_lg.render(f"MENU OPACITY: {trans_val}%", True, title_color)
        trans_title_x = cx + (content_w // 2) - (lbl_trans.get_width() // 2)
        menu_canvas.blit(lbl_trans, (trans_title_x, trans_title_y))
        
        pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON, (cx + slider_padding_x, trans_slider_y + 5, slider_w_total, slider_bar_h), border_radius=inner_radius)
        handle_x = cx + slider_padding_x + int((slider_w_total * (trans_val / 100.0)))
        
        pygame.draw.circle(menu_canvas, TEXT_WHITE, (handle_x, trans_slider_y + 8), handle_rad)
        if is_trans_focused:
            pygame.draw.circle(menu_canvas, NEON_GREEN, (handle_x, trans_slider_y + 8), focus_handle_rad, SELECTION_BORDER_WIDTH)

        if _cached_bg_gradient_surf is None or _cached_gradient_width != slider_width:
            _cached_gradient_width = slider_width
            _cached_bg_gradient_surf = pygame.Surface((slider_width, gradient_height))
            _cached_ui_gradient_surf = pygame.Surface((slider_width, gradient_height))
            
            for i in range(slider_width):
                hue_val = (i / float(slider_width)) * 360
                _r, _g, _b = colorsys.hsv_to_rgb(hue_val / 360.0, 0.8, 0.9)
                _pixel_color = (int(_r * 255), int(_g * 255), int(_b * 255))
                pygame.draw.line(_cached_bg_gradient_surf, _pixel_color, (i, 0), (i, gradient_height))
                pygame.draw.line(_cached_ui_gradient_surf, _pixel_color, (i, 0), (i, gradient_height))

        is_bg_focused = (layer == "THEME_ROWS" and cur_sel == 3)
        title_color = NEON_GREEN if is_bg_focused else TEXT_WHITE
        
        lbl_bg = f_menu_lg.render("BACKGROUND COLOR", True, title_color)
        bg_title_x = cx + (content_w // 2) - (lbl_bg.get_width() // 2)
        menu_canvas.blit(lbl_bg, (bg_title_x, bg_title_y))
        
        menu_canvas.blit(_cached_bg_gradient_surf, (slider_x, bg_slider_y))
        
        bg_knob_x = slider_x + int((bg_hue / 360.0) * slider_width)
        pygame.draw.circle(menu_canvas, TEXT_WHITE, (bg_knob_x, bg_slider_y - 5), knob_rad)
        if is_bg_focused:
            pygame.draw.circle(menu_canvas, NEON_GREEN, (bg_knob_x, bg_slider_y - 5), focus_knob_rad, SELECTION_BORDER_WIDTH)
        
        is_ui_focused = (layer == "THEME_ROWS" and cur_sel == 4)
        title_color = NEON_GREEN if is_ui_focused else TEXT_WHITE
        
        lbl_ui = f_menu_lg.render("BORDER/TEXT COLOR", True, title_color)
        ui_title_x = cx + (content_w // 2) - (lbl_ui.get_width() // 2)
        menu_canvas.blit(lbl_ui, (ui_title_x, ui_title_y))
        
        menu_canvas.blit(_cached_ui_gradient_surf, (slider_x, ui_slider_y))
        
        ui_knob_x = slider_x + int((ui_hue / 360.0) * slider_width)
        pygame.draw.circle(menu_canvas, TEXT_WHITE, (ui_knob_x, ui_slider_y - 5), knob_rad)
        if is_ui_focused:
            pygame.draw.circle(menu_canvas, NEON_GREEN, (ui_knob_x, ui_slider_y - 5), focus_knob_rad, SELECTION_BORDER_WIDTH)

        is_cl_focused = (layer == "THEME_ROWS" and cur_sel == 5)

        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_cl_focused, f_ch_btn, NEON_GREEN)

    # ========================================================================
    # SECTION: Games tab: console list
    # ========================================================================

    elif active_tab == "Games" and layer in ("GAMES_LIST", "TAB_SELECTION"):
        import os
        from game_deck import CONSOLE_ORDER, CONSOLE_LABELS

        cols = 4
        console_list = list(CONSOLE_ORDER)
        total = len(console_list)
        cur_sel_g  = app_state.get("menu_selection_index", 0)

        PAGE_SIZE  = cols * 3
        num_pages  = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        if layer == "GAMES_LIST" and cur_sel_g < total:
            games_page = min(cur_sel_g // PAGE_SIZE, num_pages - 1)
            app_state["games_tab_page"] = games_page
        else:
            games_page = min(app_state.get("games_tab_page", 0), num_pages - 1)
        page_start  = games_page * PAGE_SIZE
        page_items  = console_list[page_start:page_start + PAGE_SIZE]

        x_pad, y_pad = 8, 8
        cell_w = (content_w - (x_pad * (cols - 1)) - 20) // cols
        cell_h = max(52, int(content_h * 0.16))
        grid_start_x = cx + 10
        grid_start_y = content_y + 14

        logos_dir   = os.path.join(media.resolve_main_dir(), "images", "logos")
        enabled_map = db.config.get("consoles_enabled", {}) if db else {}

        for local_idx, console_key in enumerate(page_items):
            idx    = page_start + local_idx
            r_pos  = local_idx // cols
            c_pos  = local_idx % cols
            cell_x = grid_start_x + c_pos * (cell_w + x_pad)
            cell_y = grid_start_y + r_pos * (cell_h + y_pad)

            is_focused = (layer == "GAMES_LIST" and cur_sel_g == idx)
            default_on = (console_key in ("DVD", "MENU"))
            is_on      = enabled_map.get(console_key, default_on)
            dot_color  = OFF_GREEN if is_on else OFF_RED

            pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON, (cell_x, cell_y, cell_w, cell_h), border_radius=6)
            if is_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (cell_x, cell_y, cell_w, cell_h), SELECTION_BORDER_WIDTH, border_radius=6)

            inset_pad = 6
            inset_x   = cell_x + inset_pad
            inset_y   = cell_y + inset_pad
            inset_w   = cell_w - inset_pad * 2
            inset_h   = cell_h - inset_pad * 2
            pygame.draw.rect(menu_canvas, (0, 0, 0), (inset_x, inset_y, inset_w, inset_h), border_radius=4)

            logo_path           = os.path.join(logos_dir, f"{console_key}.png")
            logo_drawn          = False
            _menu_logo_cache    = app_state.setdefault("_menu_logo_cache", {})
            _menu_logo_raw_cache = app_state.setdefault("_menu_logo_raw_cache", {})
            _cache_key          = (console_key, inset_w, inset_h)
            if _cache_key not in _menu_logo_cache:
                logo_raw = _menu_logo_raw_cache.get(console_key, "MISS")
                if logo_raw == "MISS":
                    try:
                        logo_raw = (pygame.image.load(logo_path).convert_alpha()
                                    if os.path.exists(logo_path) else None)
                    except Exception as _swallowed_error:
                        _log_swallowed("_build_settings_overlay_menu: logo_raw = pygame.image.load(logo_path).convert_alpha() if os.path....", _swallowed_error)
                        logo_raw = None
                    if logo_raw is not None:
                        logo_raw = _crop_logo_to_artwork(logo_raw)
                    _menu_logo_raw_cache[console_key] = logo_raw
                if logo_raw is not None:
                    _menu_logo_cache[_cache_key] = _fit_logo_to_box(
                        logo_raw, inset_w, inset_h
                    )
                else:
                    _menu_logo_cache[_cache_key] = None
            logo_surf = _menu_logo_cache.get(_cache_key)
            if logo_surf is not None:
                lx = inset_x + (inset_w // 2) - (logo_surf.get_width()  // 2)
                ly = inset_y + (inset_h // 2) - (logo_surf.get_height() // 2)
                menu_canvas.blit(logo_surf, (lx, ly))
                logo_drawn = True
            if not logo_drawn:
                lbl_name = f_body.render(CONSOLE_LABELS.get(console_key, console_key), True, TEXT_WHITE)
                menu_canvas.blit(lbl_name, (inset_x + 8, inset_y + inset_h // 2 - lbl_name.get_height() // 2))

            pygame.draw.circle(menu_canvas, dot_color, (cell_x + cell_w - 10, cell_y + 10), 5)

        arrow_btn_y = grid_start_y + 3 * (cell_h + y_pad) - y_pad + 10
        arrow_btn_w = 44
        arrow_btn_h = 30
        arrow_btn_x = cx + content_w - arrow_btn_w - 14
        is_arrow_focused = (layer == "GAMES_LIST" and cur_sel_g == 102)
        arrow_bg = COLOR_MENU_ON_BLUE_EDGE if is_arrow_focused else COLOR_MENU_ON_BLUE
        pygame.draw.rect(menu_canvas, arrow_bg,
                         (arrow_btn_x, arrow_btn_y, arrow_btn_w, arrow_btn_h), border_radius=6)
        if is_arrow_focused:
            pygame.draw.rect(menu_canvas, NEON_GREEN,
                             (arrow_btn_x, arrow_btn_y, arrow_btn_w, arrow_btn_h), SELECTION_BORDER_WIDTH, border_radius=6)
        arrow_char = "\u25ba" if games_page == 0 else "\u25c4"
        arrow_lbl  = f_body.render(arrow_char, True, TEXT_WHITE)
        menu_canvas.blit(arrow_lbl, (arrow_btn_x + (arrow_btn_w - arrow_lbl.get_width())  // 2,
                                      arrow_btn_y + (arrow_btn_h - arrow_lbl.get_height()) // 2))

        kiosk_on_gl       = db.config.get("kiosk_mode_enabled",          False) if db else False
        mm_enabled        = db.config.get("ch03_menu_music_enabled",     True)  if db else True
        mm_playlist       = db.config.get("ch03_menu_music",             [])    if db else []
        mm_count          = len(mm_playlist)
        ss_on             = db.config.get("ch03_screensaver_enabled",    True)  if db else True
        all_profiles_sync = db.config.get("all_profiles_sync",           False) if db else False
        ss_timeout_min    = db.config.get("ch03_screensaver_timeout_min", 5)    if db else 5
        is_timer_focused  = (cur_sel_g == 100)
        is_set_focused    = (cur_sel_g == 96)

        timer_val_str = f"{ss_timeout_min} min"
        TIMER_VAL_MAX_STR = "30 min"
        if mm_count == 1:
            mm_badge_txt = "1 track"
        elif mm_count > 1:
            mm_badge_txt = f"{mm_count} tracks"
        else:
            mm_badge_txt = "Add"
        MM_BADGE_MAX_STR = "100 tracks"
        PROFILE_BADGE_MAX_STR = "Profile 8"

        is_sync_focused = (cur_sel_g == 103)
        sync_target      = int(db.config.get("all_profiles_sync_target", 1)) if db else 1
        sync_val_str     = f"Profile {sync_target}"

        BADGE_W = max(f_ch_btn.size(PROFILE_BADGE_MAX_STR)[0],
                      f_ch_btn.size(TIMER_VAL_MAX_STR)[0],
                      f_ch_btn.size(MM_BADGE_MAX_STR)[0]) + 18

        _toggle_w = lambda lbl: f_ch_btn.size(lbl)[0] + 24 + 10 + 55
        left_col_w = max(_toggle_w("All Consoles"), _toggle_w("Screen Saver:"), _toggle_w("Game Music:"))

        _all_settings_pair_w = _toggle_w("All Settings") + 10 + BADGE_W

        _timer_w = f_ch_btn.size("Screen Saver Timer")[0] + 24 + 10 + BADGE_W
        right_col_w = max(_all_settings_pair_w, _timer_w)
        if not kiosk_on_gl:
            _set_w = f_ch_btn.size("Set Music")[0] + 24 + 10 + BADGE_W
            right_col_w = max(right_col_w, _set_w)

        col_gap = 93
        GROUP_SHIFT_LEFT = CHANNEL_GRID_GROUP_SHIFT_LEFT_PX
        _col_left_x_base = grid_start_x + 169 - GROUP_SHIFT_LEFT
        # WARNING: the right column position comes from the left column's original spot; keep it ahead of the left-column nudge.
        col_right_x = _col_left_x_base + left_col_w + col_gap
        LEFT_COL_SHIFT = CHANNEL_GRID_LEFT_COL_SHIFT_PX
        col_left_x  = _col_left_x_base + LEFT_COL_SHIFT
        grid_bottom_y = grid_start_y + 3 * (cell_h + y_pad) - y_pad
        row1_y = grid_bottom_y + 40
        row_gap = 52
        row2_y = row1_y + row_gap
        row3_y = row2_y + row_gap
        row_h  = 28
        pill_w, pill_h = 55, 24

        def _draw_toggle(surf, x, y, label, is_on, focused, box_w=None):
            """Label in a blue box + sliding pill knob."""
            lbl_s = f_ch_btn.render(label, True, TEXT_WHITE)
            bw    = box_w if box_w is not None else (lbl_s.get_width() + 24)
            bcol  = COLOR_MENU_ON_BLUE_EDGE if focused else COLOR_MENU_ON_BLUE
            pygame.draw.rect(surf, bcol, (x, y, bw, row_h), border_radius=5)
            if focused:
                _aa_rounded_rect(surf, NEON_GREEN, (x, y, bw, row_h), SELECTION_BORDER_WIDTH, border_radius=5)
            surf.blit(lbl_s, (x + (bw - lbl_s.get_width()) // 2, y + (row_h - lbl_s.get_height()) // 2))
            px  = x + bw + 10
            py  = y + (row_h - pill_h) // 2
            pbg = OFF_GREEN if is_on else COLOR_MENU_SWITCH_OFF
            pygame.draw.rect(surf, pbg, (px, py, pill_w, pill_h), border_radius=12)
            if focused:
                _aa_rounded_rect(surf, NEON_GREEN, (px, py, pill_w, pill_h), SELECTION_BORDER_WIDTH, border_radius=12)
            hx = px + pill_w - 12 if is_on else px + 12
            pygame.draw.circle(surf, TEXT_WHITE, (hx, py + pill_h // 2), 9)
            if focused:
                pygame.draw.circle(surf, NEON_GREEN, (hx, py + pill_h // 2), 11, SELECTION_BORDER_WIDTH)

        left_box_w = max(f_ch_btn.size("All Consoles")[0], f_ch_btn.size("Screen Saver:")[0],
                          f_ch_btn.size("Game Music:")[0]) + 24

        allcon_others = [c for c in console_list if c != "DVD"]
        allcon_all_on = bool(allcon_others) and all(enabled_map.get(c, False) for c in allcon_others)
        _draw_toggle(menu_canvas, col_left_x, row1_y, "All Consoles", allcon_all_on, cur_sel_g == 95, box_w=left_box_w)

        _draw_toggle(menu_canvas, col_left_x, row2_y, "Screen Saver:", ss_on, cur_sel_g == 98, box_w=left_box_w)

        _draw_toggle(menu_canvas, col_left_x, row3_y, "Game Music:", mm_enabled, cur_sel_g == 97, box_w=left_box_w)

        _draw_toggle(menu_canvas, col_right_x, row1_y, "All Settings", all_profiles_sync, cur_sel_g == 101)

        all_settings_w   = _toggle_w("All Settings")
        sync_badge_x     = col_right_x + all_settings_w + 10
        if all_profiles_sync:
            sync_val_col = COLOR_MENU_VALUE_MINT if is_sync_focused else TEXT_WHITE
        else:
            sync_val_col = COLOR_MENU_OFF_LABEL_GREY
        lbl_sv           = f_ch_btn.render(sync_val_str, True, sync_val_col)
        sync_badge_w     = BADGE_W
        pygame.draw.rect(menu_canvas, (0, 0, 0),
                         (sync_badge_x, row1_y, sync_badge_w, row_h), border_radius=4)
        if is_sync_focused:
            pygame.draw.rect(menu_canvas, NEON_GREEN,
                             (sync_badge_x, row1_y, sync_badge_w, row_h), SELECTION_BORDER_WIDTH, border_radius=4)
        menu_canvas.blit(lbl_sv, (sync_badge_x + (sync_badge_w - lbl_sv.get_width()) // 2,
                                   row1_y + (row_h - lbl_sv.get_height()) // 2))

        timer_lbl_col    = TEXT_WHITE if ss_on else COLOR_MENU_OFF_LABEL_GREY
        timer_lbl_surf   = f_ch_btn.render("Screen Saver Timer", True, timer_lbl_col)
        timer_btn_w      = max(all_settings_w, timer_lbl_surf.get_width() + 24)
        timer_btn_col    = COLOR_MENU_ON_BLUE if ss_on else COLOR_MENU_OFF_BUTTON
        pygame.draw.rect(menu_canvas, timer_btn_col,
                         (col_right_x, row2_y, timer_btn_w, row_h), border_radius=5)
        if is_timer_focused:
            pygame.draw.rect(menu_canvas, NEON_GREEN,
                             (col_right_x, row2_y, timer_btn_w, row_h), SELECTION_BORDER_WIDTH, border_radius=5)
        menu_canvas.blit(timer_lbl_surf,
                         (col_right_x + (timer_btn_w - timer_lbl_surf.get_width()) // 2,
                          row2_y + (row_h - timer_lbl_surf.get_height()) // 2))
        if ss_on:
            val_col = COLOR_MENU_VALUE_MINT if is_timer_focused else TEXT_WHITE
        else:
            val_col = COLOR_MENU_OFF_LABEL_GREY
        lbl_tv      = f_ch_btn.render(timer_val_str, True, val_col)
        val_badge_x = col_right_x + timer_btn_w + 10
        val_badge_w = BADGE_W
        pygame.draw.rect(menu_canvas, (0, 0, 0),
                         (val_badge_x, row2_y, val_badge_w, row_h), border_radius=4)
        if is_timer_focused:
            pygame.draw.rect(menu_canvas, NEON_GREEN,
                             (val_badge_x, row2_y, val_badge_w, row_h), SELECTION_BORDER_WIDTH, border_radius=4)
        menu_canvas.blit(lbl_tv, (val_badge_x + (val_badge_w - lbl_tv.get_width()) // 2,
                                   row2_y + (row_h - lbl_tv.get_height()) // 2))

        if not kiosk_on_gl:
            btn_col        = COLOR_MENU_ON_BLUE if mm_enabled else COLOR_MENU_OFF_BUTTON
            btn_lbl_col    = TEXT_WHITE if mm_enabled else COLOR_MENU_OFF_LABEL_GREY
            btn_lbl        = f_ch_btn.render("Set Music", True, btn_lbl_col)
            set_btn_w      = max(all_settings_w, btn_lbl.get_width() + 24)
            pygame.draw.rect(menu_canvas, btn_col,    (col_right_x, row3_y, set_btn_w, row_h), border_radius=5)
            if is_set_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN,
                                 (col_right_x, row3_y, set_btn_w, row_h), SELECTION_BORDER_WIDTH, border_radius=5)
            menu_canvas.blit(btn_lbl, (col_right_x + (set_btn_w - btn_lbl.get_width()) // 2,
                                        row3_y + (row_h - btn_lbl.get_height()) // 2))
            badge_col  = COLOR_BUTTON_TOGGLE_ADD
            if mm_enabled:
                badge_txt_col = COLOR_MENU_VALUE_MINT if is_set_focused else TEXT_WHITE
            else:
                badge_txt_col = COLOR_MENU_OFF_LABEL_GREY
            badge_surf = f_ch_btn.render(mm_badge_txt, True, badge_txt_col)
            badge_x    = col_right_x + set_btn_w + 10
            badge_w    = BADGE_W
            pygame.draw.rect(menu_canvas, badge_col,
                             (badge_x, row3_y, badge_w, row_h), border_radius=4)
            if is_set_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN,
                                 (badge_x, row3_y, badge_w, row_h), SELECTION_BORDER_WIDTH, border_radius=4)
            menu_canvas.blit(badge_surf, (badge_x + (badge_w - badge_surf.get_width()) // 2,
                                           row3_y + (row_h - badge_surf.get_height()) // 2))

        is_close_g = (cur_sel_g == 99)
        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_close_g, f_ch_btn, NEON_GREEN)

    # ========================================================================
    # SECTION: Games tab: console sub-menu
    # ========================================================================

    elif active_tab == "Games" and layer == "GAMES_SUB_MENU":
        import os
        from game_deck import CONSOLE_LABELS

        console_key = app_state.get("games_selected_console", "DVD")
        sub_row     = app_state.get("games_sub_row", 0)
        enabled_map = db.config.get("consoles_enabled", {}) if db else {}
        default_on  = (console_key in ("DVD", "MENU"))
        is_on       = True if console_key == "MENU" else enabled_map.get(console_key, default_on)

        logos_dir = os.path.join(media.resolve_main_dir(), "images", "logos")

        header_box_h = max(52, int(content_h * 0.18))
        header_box_w = content_w // 3
        header_box_x = cx + (content_w // 2) - (header_box_w // 2)
        header_box_y = content_y + 10

        logo_path  = os.path.join(logos_dir, f"{console_key}.png")
        logo_drawn = False
        _sub_logo_cache = app_state.setdefault("_sub_logo_cache", {})
        _menu_logo_raw_cache = app_state.setdefault("_menu_logo_raw_cache", {})
        _sub_key = (console_key, header_box_w, header_box_h)
        if _sub_key not in _sub_logo_cache and os.path.exists(logo_path):
            try:
                logo_raw = _menu_logo_raw_cache.get(console_key, "MISS")
                if logo_raw == "MISS":
                    logo_raw = pygame.image.load(logo_path).convert_alpha()
                    logo_raw = _crop_logo_to_artwork(logo_raw)
                    _menu_logo_raw_cache[console_key] = logo_raw
                _sub_logo_cache[_sub_key] = _fit_logo_to_box(
                    logo_raw, header_box_w, header_box_h
                )
            except Exception as _swallowed_error:
                _log_swallowed("_build_settings_overlay_menu: logo_raw = _menu_logo_raw_cache.get(console_key, 'MISS')", _swallowed_error)
                _sub_logo_cache[_sub_key] = None
        logo_surf = _sub_logo_cache.get(_sub_key)
        if logo_surf is not None:
            lx = header_box_x + (header_box_w // 2) - (logo_surf.get_width() // 2)
            ly = header_box_y + (header_box_h // 2) - (logo_surf.get_height() // 2)
            menu_canvas.blit(logo_surf, (lx, ly))
            logo_drawn = True
        if not logo_drawn:
            f_sub_title = _get_font("Courier New", 20, bold=True)
            lbl_sub = f_sub_title.render(CONSOLE_LABELS.get(console_key, console_key), True, TEXT_WHITE)
            menu_canvas.blit(lbl_sub, (
                header_box_x + header_box_w // 2 - lbl_sub.get_width() // 2,
                header_box_y + header_box_h // 2 - lbl_sub.get_height() // 2
            ))

        top_y  = header_box_y + header_box_h + 14
        row_x  = cx + 35
        row_w  = content_w - 70
        cell_h = 48
        y_gap  = 14

        short_row_w = 420

        _active_console_count = 0
        if db is not None:
            from game_deck import CONSOLE_ORDER as _CON_ORDER
            _con_enabled_map = db.config.get("consoles_enabled", {})
            for _c in _CON_ORDER:
                if _c == "MENU":
                    continue
                if _con_enabled_map.get(_c, _c == "DVD"):
                    _active_console_count += 1
        _is_last_console = is_on and (_active_console_count <= 1)

        if console_key != "MENU":
            is_toggle_focused = (not _is_last_console) and (sub_row == 0)
            _toggle_label_color = COLOR_MENU_LAST_ITEM_TEXT if _is_last_console else (NEON_GREEN if is_toggle_focused else TEXT_WHITE)
            lbl_toggle_txt = f_ch_label.render("On/Off:", True, _toggle_label_color)
            menu_canvas.blit(lbl_toggle_txt, (cx + 25, top_y + 4))

            if _is_last_console:
                sw_bg = COLOR_MENU_LOCKED_SWITCH_ON
                sw_handle_col = COLOR_MENU_LOCKED_SWITCH_KNOB
                pygame.draw.rect(menu_canvas, COLOR_MENU_PADLOCK, (cx + 208, top_y + 4, 10, 8), border_radius=2)
                pygame.draw.circle(menu_canvas, COLOR_MENU_PADLOCK, (cx + 213, top_y + 4), 4, 2)
            else:
                sw_bg = OFF_GREEN if is_on else COLOR_MENU_SWITCH_OFF
                sw_handle_col = TEXT_WHITE

            pygame.draw.rect(menu_canvas, sw_bg, (cx + 145, top_y, 55, 24), border_radius=12)
            if is_toggle_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (cx + 145, top_y, 55, 24), SELECTION_BORDER_WIDTH, border_radius=12)
            sw_handle_x = cx + 187 if is_on else cx + 157
            pygame.draw.circle(menu_canvas, sw_handle_col, (sw_handle_x, top_y + 12), 9)
            if is_toggle_focused:
                pygame.draw.circle(menu_canvas, NEON_GREEN, (sw_handle_x, top_y + 12), 11, SELECTION_BORDER_WIDTH)

        if not is_on:
            f_msg = _get_font("Courier New", 18, bold=True)
            lbl_msg1 = f_msg.render("CONSOLE IS CURRENTLY DISABLED", True, OFF_RED)
            lbl_msg2 = f_msg.render("Enable the console to access settings", True, TEXT_WHITE)
            msg_y = top_y + cell_h + 30
            menu_canvas.blit(lbl_msg1, (cx + content_w // 2 - lbl_msg1.get_width() // 2, msg_y))
            menu_canvas.blit(lbl_msg2, (cx + content_w // 2 - lbl_msg2.get_width() // 2, msg_y + 34))

            is_close_focused = (sub_row == 1)
            _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_close_focused, f_ch_btn, NEON_GREEN)
        elif console_key == "DVD":
            dvd_sub_rows  = get_games_sub_menu_rows(db, app_state, console_key, is_on)
            dvd_full_rows = ["status"] + dvd_sub_rows + ["close"]

            is_map_focused = (sub_row == dvd_full_rows.index("dvd_button_mapping"))
            pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON, (row_x, top_y + cell_h + y_gap, short_row_w, cell_h), border_radius=6)
            if is_map_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, top_y + cell_h + y_gap, short_row_w, cell_h), SELECTION_BORDER_WIDTH, border_radius=6)
            lbl_map = f_ch_label.render("BUTTON MAPPING", True, NEON_GREEN if is_map_focused else TEXT_WHITE)
            menu_canvas.blit(lbl_map, (row_x + 20, top_y + cell_h + y_gap + cell_h // 2 - lbl_map.get_height() // 2))

            _dvd_mode_val   = db.config.get("dvd_screen_mode", "normal") if db is not None else "normal"
            _dvd_mode_labels = {"normal": "Normal Mode", "stretch": "Stretch Fit",
                                 "crop": "Crop Fit", "panscan": "Pan & Scan"}

            mode_row_y = top_y + (cell_h + y_gap) * 2
            is_mode_focused = (sub_row == dvd_full_rows.index("dvd_screen_mode"))
            pygame.draw.rect(menu_canvas, COLOR_MENU_BUTTON, (row_x, mode_row_y, short_row_w, cell_h), border_radius=6)
            if is_mode_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, mode_row_y, short_row_w, cell_h), SELECTION_BORDER_WIDTH, border_radius=6)
            lbl_mode = f_ch_label.render(
                f"SCREEN MODE: {_dvd_mode_labels.get(_dvd_mode_val, _dvd_mode_val).upper()}",
                True, NEON_GREEN if is_mode_focused else TEXT_WHITE
            )
            menu_canvas.blit(lbl_mode, (row_x + 20, mode_row_y + cell_h // 2 - lbl_mode.get_height() // 2))

            _dvd_audio_track_count = app_state.get("dvd_audio_track_count", 0)
            _dvd_audio_enabled = _dvd_audio_track_count > 1
            audio_row_y = top_y + (cell_h + y_gap) * 3
            is_audio_focused = (sub_row == dvd_full_rows.index("dvd_audio_track"))
            _audio_bg = COLOR_BUTTON_AUDIO_TRACK if _dvd_audio_enabled else COLOR_MENU_BUTTON_GREYED
            _audio_txt_color = COLOR_BUTTON_AUDIO_TRACK_TEXT if _dvd_audio_enabled else COLOR_MENU_TEXT_GREYED
            pygame.draw.rect(menu_canvas, _audio_bg, (row_x, audio_row_y, short_row_w, cell_h), border_radius=6)
            if is_audio_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (row_x, audio_row_y, short_row_w, cell_h), SELECTION_BORDER_WIDTH, border_radius=6)
            lbl_audio = f_ch_label.render(
                "CHANGE AUDIO SELECTION", True, _audio_txt_color
            )
            menu_canvas.blit(lbl_audio, (row_x + 20, audio_row_y + cell_h // 2 - lbl_audio.get_height() // 2))

            _cap_right_col_w = content_w // 3
            _cap_right_margin = 40
            _cap_right_col_x = cx + content_w - _cap_right_col_w - _cap_right_margin

            _draw_console_splash_panel(menu_canvas, db, console_key, cx, content_w, top_y,
                                        cell_h, y_gap, dvd_full_rows, sub_row, f_ch_label, _get_font,
                                        NEON_GREEN, TEXT_WHITE, OFF_GREEN)

            _captions_row_y = top_y + (cell_h + y_gap) * 2
            _cap_mode_locked = _mode_locks("dvd_captions_enabled")
            captions_on = (db.config.get("dvd_captions_enabled", False) and not _cap_mode_locked) if db is not None else False
            is_captions_focused = (sub_row == dvd_full_rows.index("dvd_captions_toggle"))
            lbl_captions_txt = f_ch_label.render("Captions:", True, NEON_GREEN if is_captions_focused else (COLOR_MENU_TEXT_GREYED if _cap_mode_locked else TEXT_WHITE))
            menu_canvas.blit(lbl_captions_txt, (_cap_right_col_x, _captions_row_y + 4))

            _cap_sw_x = _cap_right_col_x + 210
            _cap_sw_bg = COLOR_MENU_BUTTON_GREYED if _cap_mode_locked else (OFF_GREEN if captions_on else COLOR_MENU_SWITCH_OFF)
            pygame.draw.rect(menu_canvas, _cap_sw_bg, (_cap_sw_x, _captions_row_y, 55, 24), border_radius=12)
            if is_captions_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (_cap_sw_x, _captions_row_y, 55, 24), SELECTION_BORDER_WIDTH, border_radius=12)
            _cap_handle_x = _cap_sw_x + 42 if captions_on else _cap_sw_x + 12
            pygame.draw.circle(menu_canvas, COLOR_MENU_DISABLED_GREY if _cap_mode_locked else TEXT_WHITE, (_cap_handle_x, _captions_row_y + 12), 9)
            if is_captions_focused:
                pygame.draw.circle(menu_canvas, NEON_GREEN, (_cap_handle_x, _captions_row_y + 12), 11, SELECTION_BORDER_WIDTH)

            _resume_row_y = top_y + (cell_h + y_gap) * 3
            resume_on = db.config.get("dvd_resume_enabled", False) if db is not None else False
            is_resume_focused = (sub_row == dvd_full_rows.index("dvd_resume_toggle"))
            lbl_resume_txt = f_ch_label.render("Resume:", True, NEON_GREEN if is_resume_focused else TEXT_WHITE)
            menu_canvas.blit(lbl_resume_txt, (_cap_right_col_x, _resume_row_y + 4))

            _res_sw_x = _cap_sw_x
            _res_sw_bg = OFF_GREEN if resume_on else COLOR_MENU_SWITCH_OFF
            pygame.draw.rect(menu_canvas, _res_sw_bg, (_res_sw_x, _resume_row_y, 55, 24), border_radius=12)
            if is_resume_focused:
                _aa_rounded_rect(menu_canvas, NEON_GREEN, (_res_sw_x, _resume_row_y, 55, 24), SELECTION_BORDER_WIDTH, border_radius=12)
            _res_handle_x = _res_sw_x + 42 if resume_on else _res_sw_x + 12
            pygame.draw.circle(menu_canvas, TEXT_WHITE, (_res_handle_x, _resume_row_y + 12), 9)
            if is_resume_focused:
                pygame.draw.circle(menu_canvas, NEON_GREEN, (_res_handle_x, _resume_row_y + 12), 11, SELECTION_BORDER_WIDTH)

            is_close_focused = (sub_row == dvd_full_rows.index("close"))
            _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_close_focused, f_ch_btn, NEON_GREEN)
        else:
            sub_rows = get_games_sub_menu_rows(db, app_state, console_key, is_on)
            full_rows = sub_rows + ["close"] if console_key == "MENU" else ["status"] + sub_rows + ["close"]

            grid_row_ids = [r for r in sub_rows if r in ("core_select", "controller_map", "stick_deadzone", "multitap_toggle",
                                                           "menu_bg_select_image", "scroll_style", "text_color",
                                                           "scan_for_roms", "scan_for_cores", "clear_roms",
                                                           "clear_cores", "clear_bios", "game_screen_mode")]
            has_disc = "disc" in sub_rows

            grid_y = top_y + cell_h + y_gap

            from game_deck import CONSOLE_SCROLL_STYLE_LABELS

            _cur_scroll_style = db.config.get("console_scroll_style", "carousel") if db else "carousel"
            _cur_text_color   = db.config.get("menu_text_color", "white") if db else "white"
            _multitap_on      = bool(db.config.get("multitap_enabled_global", False)) if db else False

            _cur_game_mode   = db.config.get("game_screen_mode", "normal") if db else "normal"
            _game_mode_labels = {"normal": "Normal Mode", "stretch": "Stretch Fit"}

            row_labels = {
                "core_select":    "SELECT LIBRETRO CORE (.DLL)",
                "controller_map": "CONTROLLER MAPPING",
                "stick_deadzone": "STICK DEADZONE",
                "multitap_toggle": "MULTITAP (ALL CONSOLES)",
                "menu_bg_select_image": "BACKGROUND IMAGE",
                "scroll_style": f"SCROLL STYLE:  {CONSOLE_SCROLL_STYLE_LABELS.get(_cur_scroll_style, 'CAROUSEL')}",
                "text_color":   f"TEXT COLOR:  {_cur_text_color.upper()}",
                "scan_for_roms": "SCAN FOR ROMS",
                "scan_for_cores": "SCAN FOR CORES",
                "clear_roms":   "CLEAR ROMS",
                "clear_cores":  "CLEAR CORES",
                "clear_bios":   "CLEAR BIOS",
                "game_screen_mode":  f"GAME SCREEN MODE:  {_game_mode_labels.get(_cur_game_mode, _cur_game_mode).upper()}",
            }

            if console_key == "MENU":
                _menu_left_order  = ["controller_map", "menu_bg_select_image", "game_screen_mode",
                                      "scan_for_cores", "clear_cores", "clear_bios"]
                _menu_right_order = ["stick_deadzone", "multitap_toggle", "scroll_style",
                                     "scan_for_roms", "clear_roms"]
                _left_ids  = [r for r in _menu_left_order if r in grid_row_ids]
                _right_ids = [r for r in _menu_right_order if r in grid_row_ids]
                _menu_col_gap = 30
                _menu_col_w   = (content_w - 70 - _menu_col_gap) // 2
                _left_col_x   = row_x
                _right_col_x  = row_x + _menu_col_w + _menu_col_gap

                _menu_n_rows   = max(len(_left_ids), len(_right_ids))
                _menu_row_gap  = y_gap if _menu_n_rows <= 5 else 10
                _menu_block_h  = _menu_n_rows * cell_h + (_menu_n_rows - 1) * _menu_row_gap
                _menu_top_bound    = header_box_y + header_box_h
                _menu_bottom_bound = content_y + content_h - 52
                _menu_even_gap = max(14, (_menu_bottom_bound - _menu_top_bound - _menu_block_h) // 2)
                grid_y = _menu_top_bound + _menu_even_gap

                row_positions = {}
                for i, rid in enumerate(_left_ids):
                    row_positions[rid] = (_left_col_x, grid_y + i * (cell_h + _menu_row_gap), _menu_col_w)
                for i, rid in enumerate(_right_ids):
                    row_positions[rid] = (_right_col_x, grid_y + i * (cell_h + _menu_row_gap), _menu_col_w)
            else:
                row_positions = {rid: (row_x, grid_y + i * (cell_h + y_gap), short_row_w)
                                  for i, rid in enumerate(grid_row_ids)}

            import sys as _sys
            _main = _sys.modules.get("__main__")
            _any_manual_roms_fn = getattr(_main, "_any_manual_roms_exist", None) if _main else None
            _any_cores_fn       = getattr(_main, "_any_cores_exist", None) if _main else None
            _any_bios_fn        = getattr(_main, "_any_bios_exist", None) if _main else None
            _menu_clear_has_content = {
                "clear_roms":  bool(_any_manual_roms_fn()) if _any_manual_roms_fn else True,
                "clear_cores": bool(_any_cores_fn()) if _any_cores_fn else True,
                "clear_bios":  bool(_any_bios_fn()) if _any_bios_fn else True,
            }

            for row_id in grid_row_ids:
                if row_id not in row_positions:
                    continue
                rx, ry, rw = row_positions[row_id]
                if row_id == "scroll_style" and console_key == "MENU":
                    _ss_gap = 10
                    _ss_swatch_w = cell_h
                    rw = rw - _ss_swatch_w - _ss_gap
                row_idx = full_rows.index(row_id)
                is_focused = (sub_row == row_idx)
                if row_id in ("clear_roms", "clear_cores", "clear_bios"):
                    _has_content = _menu_clear_has_content[row_id]
                    _row_bg = COLOR_BUTTON_CLEAR if _has_content else COLOR_MENU_BUTTON_GREYED
                    _row_txt_col = (NEON_GREEN if is_focused else TEXT_WHITE) if _has_content else COLOR_MENU_TEXT_GREYED
                elif row_id in ("scan_for_roms", "scan_for_cores"):
                    _row_bg = COLOR_BUTTON_ADD
                    _row_txt_col = NEON_GREEN if is_focused else TEXT_WHITE
                else:
                    _row_bg = COLOR_MENU_BUTTON
                    _row_txt_col = NEON_GREEN if is_focused else TEXT_WHITE
                pygame.draw.rect(menu_canvas, _row_bg, (rx, ry, rw, cell_h), border_radius=6)
                if is_focused:
                    _aa_rounded_rect(menu_canvas, NEON_GREEN, (rx, ry, rw, cell_h), SELECTION_BORDER_WIDTH, border_radius=6)
                lbl_r = f_ch_label.render(row_labels[row_id], True, _row_txt_col)
                menu_canvas.blit(lbl_r, (rx + 20, ry + cell_h // 2 - lbl_r.get_height() // 2))

                if row_id == "scroll_style" and console_key == "MENU":
                    from game_deck import LIST_TEXT_COLOR_RGB
                    _is_tc_focused = (sub_row == full_rows.index("text_color"))
                    _tc_x = rx + rw + _ss_gap
                    _tc_bg = COLOR_MENU_BUTTON
                    pygame.draw.rect(menu_canvas, _tc_bg, (_tc_x, ry, _ss_swatch_w, cell_h), border_radius=6)
                    if _is_tc_focused:
                        _aa_rounded_rect(menu_canvas, NEON_GREEN, (_tc_x, ry, _ss_swatch_w, cell_h), SELECTION_BORDER_WIDTH, border_radius=6)
                    _swatch_col = LIST_TEXT_COLOR_RGB.get(_cur_text_color, COLOR_TEXT_WHITE)
                    _sw_size = 20
                    _sw_x = _tc_x + _ss_swatch_w // 2 - _sw_size // 2
                    _sw_y = ry + cell_h // 2 - _sw_size // 2
                    pygame.draw.rect(menu_canvas, _swatch_col, (_sw_x, _sw_y, _sw_size, _sw_size), border_radius=4)
                    pygame.draw.rect(menu_canvas, COLOR_MENU_SWATCH_EDGE, (_sw_x, _sw_y, _sw_size, _sw_size), 1, border_radius=4)

                if row_id == "multitap_toggle":
                    _mt_sw_bg = OFF_GREEN if _multitap_on else COLOR_MENU_SWITCH_OFF
                    _mt_sw_w, _mt_sw_h = 55, 24
                    _mt_sw_x = rx + rw - _mt_sw_w - 16
                    _mt_sw_y = ry + cell_h // 2 - _mt_sw_h // 2
                    pygame.draw.rect(menu_canvas, _mt_sw_bg, (_mt_sw_x, _mt_sw_y, _mt_sw_w, _mt_sw_h), border_radius=12)
                    if is_focused:
                        _aa_rounded_rect(menu_canvas, NEON_GREEN, (_mt_sw_x, _mt_sw_y, _mt_sw_w, _mt_sw_h), SELECTION_BORDER_WIDTH, border_radius=12)
                    _mt_hx = _mt_sw_x + _mt_sw_w - 14 if _multitap_on else _mt_sw_x + 14
                    pygame.draw.circle(menu_canvas, TEXT_WHITE, (_mt_hx, _mt_sw_y + _mt_sw_h // 2), 9)
                    _mt_on_lbl = _get_font("Courier New", 14, bold=True).render(
                        "ON" if _multitap_on else "OFF", True,
                        COLOR_MENU_VALUE_MINT if _multitap_on else COLOR_MENU_HINT_TEXT)
                    menu_canvas.blit(_mt_on_lbl, (_mt_sw_x - _mt_on_lbl.get_width() - 8,
                                                   _mt_sw_y + _mt_sw_h // 2 - _mt_on_lbl.get_height() // 2))

            all_profiles_cfg = db.config.get("console_profiles", {}) if db else {}
            con_prof_cfg     = all_profiles_cfg.get(console_key, {})
            active_profile   = int(con_prof_cfg.get("active", 1))

            prof_section_y = grid_y + len(grid_row_ids) * (cell_h + y_gap) + 8
            prof_cell_h = 38
            prof_gap    = 6

            if console_key != "MENU":
                f_prof_label = _get_font("Courier New", 13, bold=True)
                sec_lbl = f_prof_label.render("SAVE PROFILES", True, COLOR_MENU_SECTION_LABEL)
                menu_canvas.blit(sec_lbl, (row_x + 20, prof_section_y))
                prof_section_y += sec_lbl.get_height() + 4

                for p_num in (1, 2, 3):
                    row_idx = full_rows.index(f"profile{p_num}")
                    ry      = prof_section_y + (p_num - 1) * (prof_cell_h + prof_gap)
                    is_foc  = (sub_row == row_idx)
                    is_act  = (active_profile == p_num)

                    bg_col  = COLOR_MENU_PLAYER_ROW
                    bdr_col = NEON_GREEN if is_foc else COLOR_MENU_DISC_EDGE
                    pygame.draw.rect(menu_canvas, bg_col,  (row_x, ry, short_row_w, prof_cell_h), border_radius=5)
                    pygame.draw.rect(menu_canvas, bdr_col, (row_x, ry, short_row_w, prof_cell_h), 2, border_radius=5)

                    txt_col = NEON_GREEN if is_foc else TEXT_WHITE
                    lbl_p   = f_ch_label.render(f"PROFILE  {p_num}", True, txt_col)
                    lbl_x   = row_x + 20
                    lbl_y   = ry + prof_cell_h // 2 - lbl_p.get_height() // 2
                    menu_canvas.blit(lbl_p, (lbl_x, lbl_y))

                    sq_size = 14
                    sq_x = lbl_x + lbl_p.get_width() + 14
                    sq_y = ry + prof_cell_h // 2 - sq_size // 2
                    if is_act:
                        pygame.draw.rect(menu_canvas, COLOR_MENU_PLAYER_SQUARE, (sq_x, sq_y, sq_size, sq_size), border_radius=3)
                    else:
                        pygame.draw.rect(menu_canvas, COLOR_MENU_CARD_EDGE, (sq_x, sq_y, sq_size, sq_size), 2, border_radius=3)

                    if is_act:
                        lbl_active = f_ch_label.render("ACTIVE", True, txt_col)
                        menu_canvas.blit(lbl_active, (sq_x + sq_size + 8, lbl_y))

            disc_row_y = prof_section_y + 3 * (prof_cell_h + prof_gap) + 10
            if has_disc:
                import sys as _sys
                _main = _sys.modules.get("__main__")
                _gd   = getattr(_main, "game_deck", None) if _main else None
                discs, cur_idx = _gd.get_psx_disc_info() if (_gd is not None and hasattr(_gd, "get_psx_disc_info")) else ([], -1)

                disc_row_idx = full_rows.index("disc")
                is_disc_focused = (sub_row == disc_row_idx)
                disc_available = bool(discs)
                has_next_disc = disc_available and (cur_idx < 0 or (cur_idx + 1) < len(discs))
                bg_col  = COLOR_MENU_BUTTON if has_next_disc else COLOR_MENU_DISC_GREYED
                bdr_col = NEON_GREEN if is_disc_focused else (COLOR_MENU_DISC_EDGE if has_next_disc else COLOR_MENU_EDGE_GREYED)
                disc_row_w = 620
                pygame.draw.rect(menu_canvas, bg_col, (row_x, disc_row_y, disc_row_w, cell_h), border_radius=6)
                pygame.draw.rect(menu_canvas, bdr_col, (row_x, disc_row_y, disc_row_w, cell_h), 2, border_radius=6)
                txt_col = NEON_GREEN if (is_disc_focused and has_next_disc) else (TEXT_WHITE if has_next_disc else COLOR_MENU_DISC_TEXT_OFF)
                lbl_disc = f_ch_label.render("CHANGE DISC", True, txt_col)
                menu_canvas.blit(lbl_disc, (row_x + 20, disc_row_y + cell_h // 2 - lbl_disc.get_height() // 2))

                if not disc_available:
                    val_str = "NO MULTI-DISC GAME LOADED"
                    val_col = COLOR_MENU_DISC_TEXT_OFF
                elif cur_idx < 0:
                    val_str = f"{len(discs)} DISCS FOUND"
                    if is_disc_focused:
                        val_str = [val_str + "  --", ("enter",), "FOR DISC 1"]
                    val_col = COLOR_MENU_VALUE_MINT if is_disc_focused else TEXT_WHITE
                elif has_next_disc:
                    val_str = f"ON DISC {cur_idx + 1} / {len(discs)}"
                    if is_disc_focused:
                        val_str = [val_str + "  --", ("enter",), f"FOR DISC {cur_idx + 2}"]
                    val_col = COLOR_MENU_VALUE_MINT if is_disc_focused else TEXT_WHITE
                else:
                    val_str = f"FINAL DISC ({cur_idx + 1} / {len(discs)}) -- NO SWAP AVAILABLE"
                    val_col = COLOR_MENU_DISC_TEXT_OFF
                _val_parts = val_str if isinstance(val_str, list) else [val_str]
                _val_w = key_hint_size(_val_parts, f_ch_label, None)[0]
                draw_key_hint(menu_canvas, _val_parts, row_x + disc_row_w - _val_w - 16,
                              disc_row_y + cell_h // 2, f_ch_label, val_col)

            if console_key != "MENU":
                _draw_console_splash_panel(menu_canvas, db, console_key, cx, content_w, top_y,
                                            cell_h, y_gap, full_rows, sub_row, f_ch_label, _get_font,
                                            NEON_GREEN, TEXT_WHITE, OFF_GREEN)

            if console_key == "MENU":
                _flash_msg = app_state.get("_games_menu_flash_msg")
                _flash_at  = app_state.get("_games_menu_flash_at")
                if _flash_msg and _flash_at is not None and (pygame.time.get_ticks() - _flash_at) < 2000:
                    f_flash = _get_font("Courier New", 15, bold=True)
                    lbl_flash = f_flash.render(_flash_msg, True, COLOR_MENU_FLASH_TEXT)
                    menu_canvas.blit(lbl_flash, (cx + content_w // 2 - lbl_flash.get_width() // 2,
                                                  content_y + content_h - 52 - lbl_flash.get_height() - 10))

            close_row = full_rows.index("close")
            is_close_focused = (sub_row == close_row)
            _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_close_focused, f_ch_btn, NEON_GREEN)

    # ========================================================================
    # SECTION: Games tab: save profile
    # ========================================================================

    elif active_tab == "Games" and layer == "GAMES_SAVE_PROFILE":
        import os
        from game_deck import (CONSOLE_LABELS, HANDHELD_CONSOLES, BORDER_CONSOLES_HOME,
                                border_available_for_console, SCREEN_SIZE_OPTIONS)

        console_key = app_state.get("games_selected_console", "DVD")
        profile_num = int(app_state.get("games_profile_num", 1))
        profile_row = int(app_state.get("games_profile_row", 0))
        is_handheld = console_key in HANDHELD_CONSOLES
        is_border_console = border_available_for_console(console_key)

        right_specs = []
        if is_handheld:
            right_specs.append("screen_size")
        if is_border_console:
            right_specs.append("tv_bezel")
        SAVE_ROW      = 1
        LOAD_ROW      = 2
        AUTO_SAVE_ROW = 3
        AUTO_LOAD_ROW = 4
        TV_BEZEL_ROW    = 5 + right_specs.index("tv_bezel") if "tv_bezel" in right_specs else None

        # WARNING: must mirror retro_tv_emulator.py's GAMES_SAVE_PROFILE input handling exactly.
        import sys as _sys
        _main = _sys.modules.get("__main__")
        _gd   = getattr(_main, "game_deck", None) if _main else None
        _console_bezel_style_n = 1
        if TV_BEZEL_ROW is not None and console_key in BORDER_CONSOLES_HOME and _gd is not None:
            _console_bezel_style_n = max(_gd.console_bezel_style_count(console_key), 2)
        show_bezel_style = TV_BEZEL_ROW is not None and console_key in BORDER_CONSOLES_HOME
        TV_BEZEL_STYLE_ROW = (5 + len(right_specs)) if show_bezel_style else None
        back_row      = 5 + len(right_specs) + (1 if show_bezel_style else 0)

        _outer_bezel_blocks = bool(console_key in BORDER_CONSOLES_HOME
                                   and db and db.config.get("fake_43_border_enabled", False))

        all_profiles_cfg = db.config.get("console_profiles", {}) if db else {}
        con_prof_cfg     = all_profiles_cfg.get(console_key, {})
        active_profile   = int(con_prof_cfg.get("active", 1))
        profiles_map     = con_prof_cfg.get("profiles", {})
        this_prof        = profiles_map.get(profile_num, profiles_map.get(str(profile_num), {}))
        is_active        = (active_profile == profile_num)
        auto_save_on     = bool(this_prof.get("auto_save", False))
        auto_load_on     = bool(this_prof.get("auto_load", False))
        screen_size_key  = this_prof.get("screen_size", "full")
        screen_size_lbl  = next((lbl for k, lbl, _f in SCREEN_SIZE_OPTIONS if k == screen_size_key), "FULL")
        border_on        = bool(this_prof.get("border_enabled", this_prof.get("gb_border", False)))
        try:
            border_style_val = int(this_prof.get("border_style", 1))
        except (TypeError, ValueError) as _swallowed_error:
            _log_swallowed("_build_settings_overlay_menu: border_style_val = int(this_prof.get('border_style', 1))", _swallowed_error)
            border_style_val = 1
        border_style_val = max(1, min(border_style_val, _console_bezel_style_n))

        f_ptitle = f_ch_label
        title_col = NEON_GREEN if is_active else TEXT_WHITE
        prefix_str = f"PROFILE {profile_num}"
        suffix_str = f"  —  {CONSOLE_LABELS.get(console_key, console_key)}"
        lbl_prefix = f_ptitle.render(prefix_str, True, title_col)
        lbl_suffix = f_ptitle.render(suffix_str, True, title_col)

        title_sq_size = 14
        title_gap     = 8
        if is_active:
            lbl_active_word = f_ptitle.render("ACTIVE", True, title_col)
            total_w = (lbl_prefix.get_width() + title_gap + title_sq_size + title_gap
                       + lbl_active_word.get_width() + lbl_suffix.get_width())
        else:
            total_w = lbl_prefix.get_width() + lbl_suffix.get_width()

        title_y = content_y + 14
        cur_x   = cx + content_w // 2 - total_w // 2
        menu_canvas.blit(lbl_prefix, (cur_x, title_y))
        cur_x += lbl_prefix.get_width()
        if is_active:
            cur_x += title_gap
            sq_y = title_y + lbl_prefix.get_height() // 2 - title_sq_size // 2
            pygame.draw.rect(menu_canvas, COLOR_MENU_PLAYER_SQUARE, (cur_x, sq_y, title_sq_size, title_sq_size), border_radius=3)
            cur_x += title_sq_size + title_gap
            menu_canvas.blit(lbl_active_word, (cur_x, title_y))
            cur_x += lbl_active_word.get_width()
        menu_canvas.blit(lbl_suffix, (cur_x, title_y))

        cell_h2 = 48
        gap2    = 10

        col_gap  = 30
        left_x   = cx + 35
        col_w    = (content_w - 70 - col_gap) // 2
        right_x  = left_x + col_w + col_gap

        banner_w = content_w // 3
        banner_x = cx + content_w // 2 - banner_w // 2
        banner_h = cell_h2

        title_h   = lbl_prefix.get_height()
        grid_rows = 4
        grid_h    = grid_rows * cell_h2 + (grid_rows - 1) * gap2
        close_h   = 40
        top_margin    = 14
        bottom_margin = 12
        fixed_h = title_h + banner_h + grid_h + close_h
        equal_gap = max(10, (content_h - top_margin - bottom_margin - fixed_h) // 3)

        banner_y = title_y + title_h + equal_gap
        rows_top = banner_y + banner_h + equal_gap
        close_y  = rows_top + grid_h + equal_gap

        def _draw_prof_row(idx, label, x_pos, y_pos, width, toggle_val=None, greyed=False, value_text=None,
                            active_square=None, stepper_value=None, stepper_focused=False):
            is_foc = (profile_row == idx)
            bg   = COLOR_MENU_PROFILE_FOCUS if is_foc else COLOR_MENU_PROFILE
            bdr  = NEON_GREEN  if is_foc else COLOR_MENU_PROFILE_EDGE
            if greyed:
                bg  = COLOR_MENU_STEP_FOCUS if is_foc else COLOR_MENU_TOGGLE_GREYED
                bdr = COLOR_MENU_STEP_FOCUS_EDGE if is_foc else COLOR_MENU_EDGE_GREYED
            pygame.draw.rect(menu_canvas, bg,  (x_pos, y_pos, width, cell_h2), border_radius=5)
            pygame.draw.rect(menu_canvas, bdr, (x_pos, y_pos, width, cell_h2), 2, border_radius=5)
            txt_col = NEON_GREEN if is_foc and not greyed else (
                (COLOR_MENU_TEXT_GREYED_FOCUS if is_foc else COLOR_MENU_TEXT_GREYED_LILAC) if greyed else TEXT_WHITE)
            label_x = x_pos + 20
            if active_square is not None:
                sq_size = 14
                sq_y = y_pos + cell_h2 // 2 - sq_size // 2
                if active_square:
                    pygame.draw.rect(menu_canvas, COLOR_MENU_PLAYER_SQUARE, (label_x, sq_y, sq_size, sq_size), border_radius=3)
                else:
                    pygame.draw.rect(menu_canvas, COLOR_MENU_CARD_EDGE, (label_x, sq_y, sq_size, sq_size), 2, border_radius=3)
                label_x += sq_size + 10
            lbl_row = f_ch_label.render(label, True, txt_col)
            menu_canvas.blit(lbl_row, (label_x, y_pos + cell_h2 // 2 - lbl_row.get_height() // 2))
            if toggle_val is not None:
                _step_lbl = None
                _step_box_w = 0
                _step_reserve = 0
                if stepper_value is not None:
                    _step_lbl = f_ch_btn.render(str(stepper_value), True, TEXT_WHITE)
                    _step_box_w = max(32, _step_lbl.get_width() + 20)
                    _step_reserve = _step_box_w + 40

                sw_bg  = OFF_GREEN if toggle_val else COLOR_MENU_SWITCH_OFF
                if greyed:
                    sw_bg = COLOR_MENU_SWITCH_GREYED
                if stepper_value is not None:
                    sw_w, sw_h = 36, 20
                else:
                    sw_w, sw_h = 55, 24
                sw_x = x_pos + width - sw_w - 16 - _step_reserve
                sw_y = y_pos + cell_h2 // 2 - sw_h // 2
                pygame.draw.rect(menu_canvas, sw_bg, (sw_x, sw_y, sw_w, sw_h), border_radius=12)
                hx = sw_x + sw_w - (sw_h // 2 + 2) if toggle_val else sw_x + (sw_h // 2 + 2)
                pygame.draw.circle(menu_canvas, TEXT_WHITE if not greyed else COLOR_MENU_KNOB_GREYED,
                                   (hx, sw_y + sw_h // 2), sw_h // 2 - 3)
                on_lbl = _get_font("Courier New", 14, bold=True).render(
                    "ON" if toggle_val else "OFF", True,
                    COLOR_MENU_VALUE_MINT if toggle_val and not greyed else COLOR_MENU_HINT_TEXT)
                menu_canvas.blit(on_lbl, (sw_x - on_lbl.get_width() - 8, sw_y + sw_h // 2 - on_lbl.get_height() // 2))

                if _step_lbl is not None:
                    _step_box_x = x_pos + width - _step_box_w - 16
                    _step_box_y = y_pos + cell_h2 // 2 - (sw_h // 2 + 2)
                    _step_box_h = sw_h + 4
                    _step_bg = COLOR_MENU_VALUE_BOX
                    pygame.draw.rect(menu_canvas, _step_bg,
                                     (_step_box_x, _step_box_y, _step_box_w, _step_box_h), border_radius=5)
                    if stepper_focused and not greyed:
                        pygame.draw.rect(menu_canvas, NEON_GREEN,
                                         (_step_box_x, _step_box_y, _step_box_w, _step_box_h), SELECTION_BORDER_WIDTH, border_radius=5)
                    menu_canvas.blit(_step_lbl,
                                     (_step_box_x + (_step_box_w - _step_lbl.get_width()) // 2,
                                      _step_box_y + (_step_box_h - _step_lbl.get_height()) // 2))
            elif value_text is not None:
                val_col = COLOR_MENU_VALUE_MINT if (is_foc and not greyed) else (COLOR_MENU_HINT_TEXT if greyed else TEXT_WHITE)
                val_str = f"<  {value_text}  >" if (is_foc and not greyed) else value_text
                lbl_val = f_ch_btn.render(val_str, True, val_col)
                menu_canvas.blit(lbl_val, (x_pos + width - lbl_val.get_width() - 16,
                                           y_pos + cell_h2 // 2 - lbl_val.get_height() // 2))

        act_label = "ACTIVE PROFILE" if is_active else "SET AS ACTIVE PROFILE"
        _draw_prof_row(0, act_label, banner_x, banner_y, banner_w, active_square=(True if is_active else None))

        _draw_prof_row(SAVE_ROW,      "SAVE NOW",  left_x, rows_top + 0 * (cell_h2 + gap2), col_w,
                       greyed=not is_active)
        _draw_prof_row(LOAD_ROW,      "LOAD NOW",  left_x, rows_top + 1 * (cell_h2 + gap2), col_w,
                       greyed=not is_active)
        _draw_prof_row(AUTO_SAVE_ROW, "AUTO-SAVE", left_x, rows_top + 2 * (cell_h2 + gap2), col_w,
                       toggle_val=auto_save_on, greyed=not is_active)
        _draw_prof_row(AUTO_LOAD_ROW, "AUTO-LOAD", left_x, rows_top + 3 * (cell_h2 + gap2), col_w,
                       toggle_val=auto_load_on, greyed=not is_active)

        for i, key in enumerate(right_specs):
            idx = 5 + i
            y   = rows_top + i * (cell_h2 + gap2)
            if key == "screen_size":
                _draw_prof_row(idx, "SCREEN SIZE", right_x, y, col_w,
                               value_text=screen_size_lbl, greyed=not is_active)
            elif key == "tv_bezel":
                _draw_prof_row(idx, "TV BEZEL", right_x, y, col_w,
                               toggle_val=border_on, greyed=(not is_active) or _outer_bezel_blocks)

        if show_bezel_style:
            _style_y = rows_top + len(right_specs) * (cell_h2 + gap2)
            _draw_prof_row(TV_BEZEL_STYLE_ROW, "BEZEL STYLE", right_x, _style_y, col_w,
                           value_text=str(border_style_val),
                           greyed=(not is_active) or _outer_bezel_blocks)

        is_back_foc = (profile_row == back_row)
        _draw_menu_close(menu_canvas, cx, content_w, content_y, content_h, is_back_foc, f_ch_btn, NEON_GREEN)

        if not is_active:
            note = _get_font("Courier New", 11).render(
                "Set as active profile to enable auto-save / auto-load", True, COLOR_MENU_CARD_EDGE)
            menu_canvas.blit(note, (cx + content_w // 2 - note.get_width() // 2,
                                    close_y - note.get_height() - 8))
        elif _outer_bezel_blocks:
            note = _get_font("Courier New", 11).render(
                "Turn off the System Bezel (Channels tab) to use this console's own TV Bezel",
                True, COLOR_MENU_CARD_EDGE)
            menu_canvas.blit(note, (cx + content_w // 2 - note.get_width() // 2,
                                    close_y - note.get_height() - 8))

    # ========================================================================
    # SECTION: Fit to envelope & return placement
    # ========================================================================

    final_x, final_y, fit_w, fit_h = menu_panel_rect(real_w, real_h, db)

    if fit_w == w and fit_h == h:
        stamp = menu_canvas
    else:
        stamp = pygame.transform.smoothscale(menu_canvas, (fit_w, fit_h))

    return stamp, (final_x, final_y)


# ============================================================================
# SECTION: Popups: quit, pick layout, controller wait, clear confirm
# ============================================================================


def render_quit_prompt(surface, theme, db=None):
    import colorsys

    CANVAS_W, CANVAS_H = 650, 220
    real_w, real_h = surface.get_size()
    canvas = pygame.Surface((CANVAS_W, CANVAS_H), pygame.SRCALPHA)

    bg_hue = db.config.get("theme_bg_hue", 220) / 360.0 if db is not None else 220/360.0
    ui_hue = db.config.get("theme_ui_hue", 140) / 360.0 if db is not None else 140/360.0
    r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.9, 1.0)
    NEON_GREEN    = (int(r*255), int(g*255), int(b*255))
    r, g, b = colorsys.hsv_to_rgb(bg_hue, 0.6, 0.15)
    MIDNIGHT_NAVY = (int(r*255), int(g*255), int(b*255))
    ARCADE_GOLD   = COLOR_ARCADE_GOLD
    TEXT_WHITE    = COLOR_TEXT_WHITE
    ACTION_RED    = COLOR_ACTION_RED

    slider_val   = db.config.get("menu_opacity", 50) if db is not None else 50
    menu_opacity = int((slider_val / 100.0) * 255)

    bg_surf = pygame.Surface((CANVAS_W, CANVAS_H), pygame.SRCALPHA)
    bg_surf.fill(MIDNIGHT_NAVY + (menu_opacity,))
    canvas.blit(bg_surf, (0, 0))
    pygame.draw.rect(canvas, NEON_GREEN, (0, 0, CANVAS_W, CANVAS_H), 2)

    f_title = _get_font("Courier New", 22, bold=True)
    f_body  = _get_font("Courier New", 19, bold=True)

    lbl1 = f_title.render("MINIMIZE PROGRAM?", True, ARCADE_GOLD)
    canvas.blit(lbl1, (CANVAS_W//2 - lbl1.get_width()//2, 30))
    draw_key_hint(canvas, ["PRESS", ("esc",), "TO RESUME"], CANVAS_W // 2, 100 + f_body.get_height() // 2,
                  f_body, TEXT_WHITE, center=True)
    draw_key_hint(canvas, ["PRESS", ("enter",), "TO TOGGLE WINDOW"], CANVAS_W // 2, 144 + f_body.get_height() // 2,
                  f_body, ACTION_RED, center=True)

    dest_w, dest_h, dest_x, dest_y = fit_design_canvas(CANVAS_W, CANVAS_H, real_w, real_h)
    if dest_w == CANVAS_W and dest_h == CANVAS_H:
        surface.blit(canvas, (dest_x, dest_y))
    else:
        scaled = pygame.transform.smoothscale(canvas, (dest_w, dest_h))
        surface.blit(scaled, (dest_x, dest_y))


def render_pick_layout_popup(surface, theme, db, game_deck, player_num):
    import colorsys

    real_w, real_h = surface.get_size()
    box_w, box_h = 500, 200
    box_x = (real_w - box_w) // 2
    box_y = (real_h - box_h) // 2

    ui_color = _DEFAULT_THEME["ui"]
    bg_color = _DEFAULT_THEME["bg"]
    if isinstance(theme, dict):
        ui_color = tuple(theme.get("ui", ui_color))
        bg_color = tuple(theme.get("bg", bg_color))
    elif db is not None:
        try:
            bg_hue = db.config.get("theme_bg_hue", 220) / 360.0
            ui_hue = db.config.get("theme_ui_hue", 140) / 360.0
            r, g, b = colorsys.hsv_to_rgb(bg_hue, 0.85, 0.18)
            bg_color = (int(r * 255), int(g * 255), int(b * 255))
            r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.90, 1.00)
            ui_color = (int(r * 255), int(g * 255), int(b * 255))
        except (AttributeError, TypeError, ValueError) as _swallowed_error:
            _log_swallowed("render_pick_layout_popup: bg_hue = db.config.get('theme_bg_hue', 220) / 360.0", _swallowed_error)
            pass

    menu_opacity = db.config.get("menu_opacity", 50) if db is not None else 50
    try:
        menu_opacity = max(0, min(100, int(menu_opacity)))
    except (TypeError, ValueError) as _swallowed_error:
        _log_swallowed("render_pick_layout_popup: menu_opacity = max(0, min(100, int(menu_opacity)))", _swallowed_error)
        menu_opacity = 50
    alpha_val = int((menu_opacity / 100.0) * 255)

    overlay = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
    overlay.fill((int(bg_color[0]), int(bg_color[1]), int(bg_color[2]), alpha_val))
    surface.blit(overlay, (box_x, box_y))
    pygame.draw.rect(surface, ui_color, (box_x, box_y, box_w, box_h), 2, border_radius=4)

    f_title = _get_font("Courier New", 20, bold=True)
    f_sub   = _get_font("Courier New", 15, bold=True)
    f_btn   = _get_font("Courier New", 17, bold=True)
    f_hint  = _get_font("Courier New", 15, bold=True)

    title = f_title.render("DIDN'T REGISTER BUTTON LAYOUT", True, COLOR_ARCADE_GOLD)
    surface.blit(title, (real_w // 2 - title.get_width() // 2, box_y + 24))

    sub = f_sub.render(f"PLAYER {player_num}'S CONTROLLER NEEDS TO BE MAPPED MANUALLY", True, COLOR_MENU_POPUP_SUBTEXT)
    surface.blit(sub, (real_w // 2 - sub.get_width() // 2, box_y + 56))

    btn_w, btn_h = 220, 40
    btn_x = real_w // 2 - btn_w // 2
    btn_y = box_y + 100
    remap_rect = pygame.Rect(btn_x, btn_y, btn_w, btn_h)
    pygame.draw.rect(surface, COLOR_MENU_BUTTON_BLUE, remap_rect, border_radius=4)
    pygame.draw.rect(surface, ui_color, remap_rect, 2, border_radius=4)
    remap_lbl = f_btn.render("REMAP CONTROLLER", True, COLOR_TEXT_WHITE)
    surface.blit(remap_lbl, (remap_rect.centerx - remap_lbl.get_width() // 2,
                              remap_rect.centery - remap_lbl.get_height() // 2))

    draw_key_hint(surface, [("enter",), "REMAP NOW", ("esc",), "DO IT LATER"], real_w // 2,
                  box_y + box_h - 32 + f_hint.get_height() // 2, f_hint, COLOR_MENU_POPUP_HINT, center=True)


def render_controller_pick_wait_popup(surface, theme, db, game_deck, player_num):
    import colorsys

    real_w, real_h = surface.get_size()
    box_w, box_h = 500, 200
    box_x = (real_w - box_w) // 2
    box_y = (real_h - box_h) // 2

    ui_color = _DEFAULT_THEME["ui"]
    bg_color = _DEFAULT_THEME["bg"]
    if isinstance(theme, dict):
        ui_color = tuple(theme.get("ui", ui_color))
        bg_color = tuple(theme.get("bg", bg_color))
    elif db is not None:
        try:
            bg_hue = db.config.get("theme_bg_hue", 220) / 360.0
            ui_hue = db.config.get("theme_ui_hue", 140) / 360.0
            r, g, b = colorsys.hsv_to_rgb(bg_hue, 0.85, 0.18)
            bg_color = (int(r * 255), int(g * 255), int(b * 255))
            r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.90, 1.00)
            ui_color = (int(r * 255), int(g * 255), int(b * 255))
        except (AttributeError, TypeError, ValueError) as _swallowed_error:
            _log_swallowed("render_controller_pick_wait_popup: bg_hue = db.config.get('theme_bg_hue', 220) / 360.0", _swallowed_error)
            pass

    menu_opacity = db.config.get("menu_opacity", 50) if db is not None else 50
    try:
        menu_opacity = max(0, min(100, int(menu_opacity)))
    except (TypeError, ValueError) as _swallowed_error:
        _log_swallowed("render_controller_pick_wait_popup: menu_opacity = max(0, min(100, int(menu_opacity)))", _swallowed_error)
        menu_opacity = 50
    alpha_val = int((menu_opacity / 100.0) * 255)

    overlay = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
    overlay.fill((int(bg_color[0]), int(bg_color[1]), int(bg_color[2]), alpha_val))
    surface.blit(overlay, (box_x, box_y))
    pygame.draw.rect(surface, ui_color, (box_x, box_y, box_w, box_h), 2, border_radius=4)

    f_title = _get_font("Courier New", 20, bold=True)
    f_sub   = _get_font("Courier New", 15, bold=True)
    f_hint  = _get_font("Courier New", 15, bold=True)

    is_menu_pick = player_num is None
    title_txt = "PICK CONTROLLER \u2014 MENU NAVIGATION" if is_menu_pick else f"PICK CONTROLLER \u2014 PLAYER {player_num}"
    title = f_title.render(title_txt, True, COLOR_ARCADE_GOLD)
    surface.blit(title, (real_w // 2 - title.get_width() // 2, box_y + 30))

    sub = f_sub.render("PRESS ANY BUTTON ON THE CONTROLLER TO USE", True, COLOR_MENU_POPUP_SUBTEXT)
    surface.blit(sub, (real_w // 2 - sub.get_width() // 2, box_y + 66))

    sub2_txt = "IT WILL CONTROL MENU / LIST NAVIGATION" if is_menu_pick else f"IT WILL BECOME PLAYER {player_num}'S CONTROLLER"
    sub2 = f_sub.render(sub2_txt, True, COLOR_MENU_POPUP_SUBTEXT_DIM)
    surface.blit(sub2, (real_w // 2 - sub2.get_width() // 2, box_y + 92))

    draw_key_hint(surface, [("esc",), "CANCEL"], real_w // 2,
                  box_y + box_h - 32 + f_hint.get_height() // 2, f_hint, COLOR_MENU_POPUP_HINT, center=True)


def render_clear_confirm_popup(surface, theme, db=None, app_state=None):
    import colorsys

    real_w, real_h = surface.get_size()
    box_w, box_h = 500, 200

    state = app_state or {}
    target = state.get("clear_confirm_target")

    if target == "visualizer_style_change":
        _burnin_turning_on = True
    else:
        _burnin_turning_on = state.get("clear_confirm_burnin_new_value", True)
    _BURNIN_TITLES = {
        ("visualizer_style_change", True):    "SWITCH OFF RANDOM STYLE?",
        ("console_bezel_toggle", True):       "TURN ON BEZEL?",
        ("program_bezel_toggle", True):       "TURN ON BEZEL?",
        ("pixel_orbiter_toggle", False):      "TURN OFF PIXEL ORBITER?",
        ("ch03_screensaver_toggle", False):   "TURN OFF SCREEN SAVER?",
    }
    _BURNIN_WARNING_LINES_ON = [
        "Turning this on puts you at risk for burn in.",
        "Beware of burn in on older TVs.",
    ]
    _BURNIN_WARNING_LINES_OFF = [
        "Turning this off puts you at risk for burn in.",
        "Beware of burn in on older TVs.",
    ]
    _BURNIN_BODY = {
        ("visualizer_style_change", True): _BURNIN_WARNING_LINES_ON,
        ("console_bezel_toggle", True):    _BURNIN_WARNING_LINES_ON,
        ("program_bezel_toggle", True):    _BURNIN_WARNING_LINES_ON,
        ("pixel_orbiter_toggle", False):   _BURNIN_WARNING_LINES_OFF,
        ("ch03_screensaver_toggle", False): _BURNIN_WARNING_LINES_OFF,
    }
    _burnin_key = (target, _burnin_turning_on)
    _is_burnin_family = _burnin_key in _BURNIN_TITLES
    _has_checkbox = _is_burnin_family or target == "app_update"
    _update_version = str(state.get("update_prompt_version", "") or "").strip()
    _update_body = [
        f"Version {_update_version} is available." if _update_version else "A new version is available.",
        "The app restarts when it finishes.",
    ]
    _reset_body = [
        "Every setting and everything saved is forgotten.",
        "Your own files stay. The app restarts.",
    ]
    _has_body = _is_burnin_family or target in ("app_update", "reset_settings")

    if _has_checkbox:
        box_h = 290
    elif _has_body:
        box_h = 240
    box_x = (real_w - box_w) // 2
    box_y = (real_h - box_h) // 2

    ui_color = _DEFAULT_THEME["ui"]
    bg_color = _DEFAULT_THEME["bg"]
    TEXT_WHITE = COLOR_TEXT_WHITE
    if isinstance(theme, dict):
        ui_color = tuple(theme.get("ui", ui_color))
        bg_color = tuple(theme.get("bg", bg_color))

    alpha_val = 255
    if theme is None and db is not None:
        try:
            bg_hue = db.config.get("theme_bg_hue", 220) / 360.0
            ui_hue = db.config.get("theme_ui_hue", 140) / 360.0
            r, g, b = colorsys.hsv_to_rgb(bg_hue, 0.85, 0.18)
            bg_color = (int(r * 255), int(g * 255), int(b * 255))
            r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.90, 1.00)
            ui_color = (int(r * 255), int(g * 255), int(b * 255))
        except (AttributeError, TypeError, ValueError) as _swallowed_error:
            _log_swallowed("render_clear_confirm_popup: bg_hue = db.config.get('theme_bg_hue', 220) / 360.0", _swallowed_error)
            pass

    target_titles = {
        "channel_schedule": "CLEAR BROADCAST FILES?",
        "channel_music": "CLEAR MUSIC TRACKS?",
        "reset_settings": "RESET ALL SETTINGS?",
        "clear_roms": "CLEAR ALL ADDED ROMS?",
        "clear_cores": "CLEAR ALL CORES?",
        "clear_bios": "CLEAR ALL BIOS?",
        "poweroff": "TURN OFF COMPUTER?",
        "boot_on_start": "REPLACE WINDOWS SHELL?",
        "app_update": "UPDATE AND RESTART?",
    }
    if target == "clear_console_roms":
        from game_deck import CONSOLE_LABELS
        _cc_console_key = state.get("clear_confirm_console", "")
        _cc_console_lbl = CONSOLE_LABELS.get(_cc_console_key, _cc_console_key)
        title_text = f"CLEAR {_cc_console_lbl.upper()} ROMS?"
    elif _is_burnin_family:
        title_text = _BURNIN_TITLES[_burnin_key]
    else:
        title_text = target_titles.get(target, "ARE YOU SURE?")

    overlay = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
    overlay.fill((int(bg_color[0]), int(bg_color[1]), int(bg_color[2]), alpha_val))
    surface.blit(overlay, (box_x, box_y))
    pygame.draw.rect(surface, ui_color, (box_x, box_y, box_w, box_h), 2, border_radius=4)

    f_title = _get_font("Courier New", 22, bold=True)
    f_btn = _get_font("Courier New", 17, bold=True)
    f_hint = _get_font("Courier New", 17, bold=True)

    title = f_title.render(title_text, True, COLOR_ARCADE_GOLD)
    surface.blit(title, (real_w // 2 - title.get_width() // 2, box_y + 25))

    if _has_body:
        _body_lines = (_BURNIN_BODY[_burnin_key] if _is_burnin_family
                       else _update_body if target == "app_update" else _reset_body)
        _body_max_w = box_w - 40
        _body_size = 15
        while _body_size > 10:
            f_body = _get_font("Courier New", _body_size, bold=False)
            if max((f_body.render(_l, True, TEXT_WHITE).get_width() for _l in _body_lines), default=0) <= _body_max_w:
                break
            _body_size -= 1
        _body_y = box_y + 60
        for _line in _body_lines:
            _lbl = f_body.render(_line, True, TEXT_WHITE)
            surface.blit(_lbl, (real_w // 2 - _lbl.get_width() // 2, _body_y))
            _body_y += 19

    btn_y = box_y + (145 if _has_checkbox else 125 if _has_body else 85)
    selection = state.get("clear_confirm_selection", "No")
    _cc_focus = state.get("clear_confirm_focus", "buttons")
    _buttons_focused = (not _has_checkbox) or _cc_focus == "buttons"
    btn_h = POPUP_YES_NO_SIZE[1]
    draw_popup_yes_no(surface, real_w // 2, btn_y, selection, _buttons_focused, f_btn, ui_color)

    if _has_checkbox:
        _dont_show = state.get("clear_confirm_dont_show_again", False)
        _checkbox_focused = (_cc_focus == "checkbox")
        _cb_size = 16
        _cb_label = f_hint.render("DON'T SHOW THIS MESSAGE AGAIN", True, COLOR_MENU_CHECKBOX)
        _cb_row_w = _cb_size + 8 + _cb_label.get_width()
        _cb_x = real_w // 2 - _cb_row_w // 2
        _cb_y = btn_y + btn_h + 16
        pygame.draw.rect(surface, COLOR_MENU_CHECKBOX, (_cb_x, _cb_y, _cb_size, _cb_size), 2, border_radius=2)
        if _dont_show:
            pygame.draw.rect(surface, ui_color, (_cb_x + 3, _cb_y + 3, _cb_size - 6, _cb_size - 6), border_radius=1)
        if _checkbox_focused:
            pygame.draw.rect(surface, ui_color, (_cb_x - 6, _cb_y - 5, _cb_row_w + 12, _cb_size + 10), SELECTION_BORDER_WIDTH, border_radius=4)
        surface.blit(_cb_label, (_cb_x + _cb_size + 8, _cb_y + (_cb_size // 2) - (_cb_label.get_height() // 2)))

        _hint_parts = [("w", "a", "s", "d"), "NAVIGATE", ("enter",), "CONFIRM"]
    else:
        _hint_parts = [("a", "d"), "NAVIGATE", ("enter",), "CONFIRM"]

    _hint_font = _get_font("Courier New", 17, bold=True)
    draw_key_hint_fitted(surface, _hint_parts, real_w // 2, box_y + box_h - 35 + _hint_font.get_height() // 2,
                         "Courier New", 17, COLOR_MENU_POPUP_HINT, box_w - 20)


# ============================================================================
# SECTION: Exports
# ============================================================================
__all__ = [
    "draw_popup_yes_no", "popup_yes_no_rects",
    "CONTROLS_SPLASH_TITLES", "NUM_CONTROL_PAGES",
    "CAPTIONS_COLOR_BOX_SEL",
    "DEFAULT_REMOTE_BINDINGS",
    "DVD_MAP_ACTIONS",
    "DVD_MAP_DEFAULTS",
    "GPU_RENDER_MODE_ENABLED",
    "GUIDE_DESIGN_H",
    "GUIDE_DESIGN_H_4_3",
    "GUIDE_DESIGN_W",
    "GUIDE_DESIGN_W_4_3",
    "GUIDE_NUM_COLS_16_9",
    "GUIDE_NUM_COLS_4_3",
    "GUIDE_PAST_HALF_HOURS",
    "GUIDE_TOTAL_HALF_HOURS",
    "GUIDE_VISIBLE_ROWS_16_9",
    "GUIDE_VISIBLE_ROWS_4_3",
    "NUMBER_REMAP_ACTIONS",
    "REMOTE_REMAP_ACTIONS",
    "SELECTION_BORDER_WIDTH",
    "SERVER_MENU_BTN_SEL",
    "SERVER_TOGGLE_ENABLED",
    "SYSTEM_ROW_META",
    "UPDATER_AND_SERVER_FEATURES_ENABLED",
    "clamp_guide_time_idx",
    "compute_channel_name_char_limit",
    "fit_design_canvas",
    "fit_menu_envelope",
    "menu_panel_rect",
    "get_advanced_settings_menu_rows",
    "get_cached_row_stops",
    "get_channels_settings_menu_rows",
    "get_current_guide_time_idx",
    "get_dvd_binding",
    "get_games_sub_menu_rows",
    "get_guide_num_cols",
    "get_guide_visible_rows",
    "get_mock_discovered_servers",
    "get_server_picker_display_rows",
    "get_server_picker_entries",
    "get_server_picker_selectable_count",
    "get_system_menu_rows",
    "guide_preload_tick",
    "invalidate_guide_cache",
    "preload_games_tab_assets",
    "render_clear_confirm_popup",
    "render_controller_pick_wait_popup",
    "render_controls_splash",
    "render_dvd_button_mapping_popup",
    "render_loading_screen",
    "render_pick_layout_popup",
    "render_quit_prompt",
    "render_settings_overlay_menu",
    "render_tv_guide_async",
]
