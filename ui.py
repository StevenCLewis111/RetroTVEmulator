# ==============================================================================
# PART 1 OF 15: CORE IMPORTS, BASELINE LAYOUTS, & THEME CANVAS CLEANERS
# ==============================================================================

import pygame
import logging
import media

log = logging.getLogger(__name__)

# Active telemetry initialization tracker for UI Module entry verification passes
print("[TELEMETRY - UI PART 1] Core graphic layout libraries and OS dependency vectors successfully loaded.")

# --- REMOTE REMAPPING display data --------------------------------------
# Canonical source for REMOTE_REMAP_ACTIONS / NUMBER_REMAP_ACTIONS /
# CANONICAL_REMOTE_KEYS (imported here as DEFAULT_REMOTE_BINDINGS) /
# _REMOTE_KEY_NAME_OVERRIDES / the key-label helper / the binding lookup.
# retro_tv_emulator.py imports these from here rather than keeping its own
# copy — it already does `from ui import ...`, so there's no circular-import
# problem in this direction. (There used to be two separately-maintained
# copies of this whole block; this is now the only one.)
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
    ("esc",   "Back/Minimize"),
]

NUMBER_REMAP_ACTIONS = [(str(i), str(i)) for i in range(10)]

DEFAULT_REMOTE_BINDINGS = {
    "w": pygame.K_w, "a": pygame.K_a, "s": pygame.K_s, "d": pygame.K_d,
    "up": pygame.K_UP, "down": pygame.K_DOWN, "left": pygame.K_LEFT, "right": pygame.K_RIGHT,
    "enter": pygame.K_RETURN, "esc": pygame.K_ESCAPE,
    "n": pygame.K_n, "m": pygame.K_m,
}
for _i in range(10):
    DEFAULT_REMOTE_BINDINGS[str(_i)] = getattr(pygame, f"K_{_i}")
del _i

_SDLK_MASK = 0x40000000  # SDL2 scancode → keycode formula (scancode | mask)

_REMOTE_KEY_NAME_OVERRIDES = {
    pygame.K_RETURN:    "ENTER",   pygame.K_ESCAPE: "ESC",      pygame.K_SPACE:  "SPACE",
    pygame.K_UP:        "UP",      pygame.K_DOWN:   "DOWN",      pygame.K_LEFT:   "LEFT",
    pygame.K_RIGHT:     "RIGHT",   pygame.K_BACKSPACE: "BKSP",   pygame.K_TAB:    "TAB",
    pygame.K_LSHIFT:    "L-SHIFT", pygame.K_RSHIFT: "R-SHIFT",
    pygame.K_LCTRL:     "L-CTRL",  pygame.K_RCTRL:  "R-CTRL",
    pygame.K_LALT:      "L-ALT",   pygame.K_RALT:   "R-ALT",
    # Function keys (common on mini keyboards and PC remotes)
    pygame.K_F1:  "F1",  pygame.K_F2:  "F2",  pygame.K_F3:  "F3",  pygame.K_F4:  "F4",
    pygame.K_F5:  "F5",  pygame.K_F6:  "F6",  pygame.K_F7:  "F7",  pygame.K_F8:  "F8",
    pygame.K_F9:  "F9",  pygame.K_F10: "F10", pygame.K_F11: "F11", pygame.K_F12: "F12",
    # Navigation cluster
    pygame.K_INSERT:   "INS",   pygame.K_DELETE: "DEL",
    pygame.K_HOME:     "HOME",  pygame.K_END:    "END",
    pygame.K_PAGEUP:   "PGUP",  pygame.K_PAGEDOWN: "PGDN",
    # System keys
    pygame.K_NUMLOCK:  "NUMLK", pygame.K_CAPSLOCK: "CAPSLK",
    pygame.K_PRINT:    "PRINT", pygame.K_PAUSE:    "PAUSE",
    # Numpad — appear on full-size keyboards and some remotes
    pygame.K_KP_ENTER:    "KP-ENT", pygame.K_KP_PERIOD:   "KP-.",
    pygame.K_KP_PLUS:     "KP-+",   pygame.K_KP_MINUS:    "KP--",
    pygame.K_KP_MULTIPLY: "KP-*",   pygame.K_KP_DIVIDE:   "KP-/",
    pygame.K_KP0: "KP-0", pygame.K_KP1: "KP-1", pygame.K_KP2: "KP-2",
    pygame.K_KP3: "KP-3", pygame.K_KP4: "KP-4", pygame.K_KP5: "KP-5",
    pygame.K_KP6: "KP-6", pygame.K_KP7: "KP-7", pygame.K_KP8: "KP-8",
    pygame.K_KP9: "KP-9",
    # Media keys — arrive via VK_MEDIA_* (Windows hook) or APPCOMMAND.
    # pygame.key.name() returns nothing useful for these high-scancode keys,
    # so we give them explicit short labels for the remap UI.
    _SDLK_MASK | 258: "MED-NEXT",  # SDL_SCANCODE_AUDIONEXT  (VK_MEDIA_NEXT_TRACK)
    _SDLK_MASK | 259: "MED-PREV",  # SDL_SCANCODE_AUDIOPREV  (VK_MEDIA_PREV_TRACK)
    _SDLK_MASK | 260: "MED-STOP",  # SDL_SCANCODE_AUDIOSTOP  (VK_MEDIA_STOP)
    _SDLK_MASK | 261: "MED-PLAY",  # SDL_SCANCODE_AUDIOPLAY  (VK_MEDIA_PLAY_PAUSE)
    _SDLK_MASK | 262: "MED-MUTE",  # SDL_SCANCODE_AUDIOMUTE  (VK_VOLUME_MUTE)
    _SDLK_MASK | 285: "MED-REW",   # SDL_SCANCODE_AUDIOREWIND      (APPCOMMAND_MEDIA_REWIND)
    _SDLK_MASK | 286: "MED-FF",    # SDL_SCANCODE_AUDIOFASTFORWARD (APPCOMMAND_MEDIA_FASTFORWARD)
}


def _remote_key_label(key_code):
    """Short bordered-box display label for a pygame key code.
    Handles standard keys, override aliases, and unusual keys from PC
    remotes / mini keyboards that pygame has no name for — those get a
    unique numeric label (e.g. '#5765') instead of the useless '?'."""
    if key_code is None:
        return "---"
    if key_code in _REMOTE_KEY_NAME_OVERRIDES:
        return _REMOTE_KEY_NAME_OVERRIDES[key_code]
    try:
        name = pygame.key.name(key_code)
        if name and name not in ("", "unknown key"):
            return name.upper()[:8]
    except Exception:
        pass
    # Fallback: display a short numeric key ID so the user can still identify
    # what they pressed (e.g. a media key on a remote that pygame doesn't name).
    return f"#{key_code & 0xFFFF}"[:8]


def _get_remote_binding(db, action):
    bindings = db.config.get("remote_bindings", {}) if db is not None else {}
    return bindings.get(action, DEFAULT_REMOTE_BINDINGS.get(action))


# ── DVD PLAYER BUTTON MAPPING (DVD-ONLY) ─────────────────────────────────────
# Single source of truth for the DVD transport actions the "Button Mapping"
# popup exposes, in display order, plus their default (canonical) keys. These
# MUST match game_deck.ExternalGameDeck.DVD_ACTION_CANONICAL. Stored per-action
# under db.config["dvd_bindings"] — a namespace separate from remote_bindings
# and from menu/navigation WASD, so remapping here only affects DVD playback.
DVD_MAP_ACTIONS = [
    ("play_pause",     "Play / Pause"),
    ("rewind",         "Rewind  (hold = fast)"),
    ("fast_forward",   "Fast Forward  (hold = fast)"),
    ("captions",       "Captions"),
    ("return_to_menu", "Return to Main Menu"),
]
DVD_MAP_DEFAULTS = {
    # Default bindings use OS media keys so the physical transport buttons on
    # a PC remote or media keyboard work out-of-the-box without any remapping.
    # Internally the DVD handler still uses the canonical Z/X/C/V/Backspace
    # keys (see game_deck.DVD_ACTION_CANONICAL); _dvd_translate_key maps
    # the stored binding → canonical before _handle_dvd_input sees it.
    "play_pause":     _SDLK_MASK | 261,  # SDL_SCANCODE_AUDIOPLAY  (VK_MEDIA_PLAY_PAUSE)
    "rewind":         _SDLK_MASK | 285,  # SDL_SCANCODE_AUDIOREWIND      (APPCOMMAND_MEDIA_REWIND)
    "fast_forward":   _SDLK_MASK | 286,  # SDL_SCANCODE_AUDIOFASTFORWARD (APPCOMMAND_MEDIA_FASTFORWARD)
    "captions":       pygame.K_v,
    "return_to_menu": pygame.K_BACKSPACE,  # back to Backspace -- was SDL_SCANCODE_AUDIOSTOP
                                             # (VK_MEDIA_STOP), which left this unreachable on
                                             # hardware with no dedicated Stop button. Must match
                                             # game_deck.ExternalGameDeck._DVD_DEFAULTS.
}
# With fast_forward no longer defaulting to C, C is free again and has
# been returned to the global Controls splash hotkey
# (see retro_tv_emulator.py's FIXED INTERCEPT MATRIX).


def get_dvd_binding(db, action):
    """Current key bound to a DVD transport action (falls back to default)."""
    bindings = db.config.get("dvd_bindings", {}) if db is not None else {}
    return bindings.get(action, DVD_MAP_DEFAULTS.get(action))


def render_dvd_button_mapping_popup(surface, app_state, db):
    """Small centered popup listing the four DVD transport actions with their
    current key bindings, plus a 'Map Buttons' button and a bottom-right
    'Close' button — matching the settings-menu theme. Purely presentational;
    all navigation/capture state lives in app_state and is driven by the
    GAMES/DVD_BTN_MAP input handler in retro_tv_emulator.py.

    app_state keys read here:
      dvd_map_cursor     0 = Map Buttons, 1 = Close  (browse state)
      dvd_map_capturing  True while walking the actions waiting for a keypress
      dvd_map_capture_idx index into DVD_MAP_ACTIONS currently being captured
    """
    import colorsys

    cursor      = app_state.get("dvd_map_cursor", 0)
    capturing   = app_state.get("dvd_map_capturing", False)
    capture_idx = app_state.get("dvd_map_capture_idx", 0)

    # Theme colors (mirror the rest of the menu).
    ui_hue = db.config.get("theme_ui_hue", 140) / 360.0 if db is not None else 140 / 360.0
    r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.9, 1.0)
    NEON_GREEN = (int(r * 255), int(g * 255), int(b * 255))
    TEXT_WHITE = (255, 255, 255)
    OFF_RED    = (150, 45, 45)
    PANEL_BG   = (16, 22, 40)
    ROW_BG     = (25, 45, 80)
    KEY_BG     = (12, 16, 30)
    DIM        = (150, 160, 180)

    sw, sh = surface.get_size()
    win_w, win_h = 540, 420
    win_x = (sw - win_w) // 2
    win_y = (sh - win_h) // 2

    # Dim backdrop so the popup reads as a modal window over the sub-menu.
    veil = pygame.Surface((sw, sh), pygame.SRCALPHA)
    veil.fill((0, 0, 0, 150))
    surface.blit(veil, (0, 0))

    pygame.draw.rect(surface, PANEL_BG, (win_x, win_y, win_w, win_h), border_radius=10)
    pygame.draw.rect(surface, NEON_GREEN, (win_x, win_y, win_w, win_h), 2, border_radius=10)

    f_title = _get_font("Courier New", 20, bold=True)
    f_body  = _get_font("Courier New", 15, bold=True)
    f_key   = _get_font("Courier New", 14, bold=True)
    f_small = _get_font("Courier New", 12, bold=False)

    title = f_title.render("DVD BUTTON MAPPING", True, TEXT_WHITE)
    surface.blit(title, (win_x + win_w // 2 - title.get_width() // 2, win_y + 18))

    hint_txt = ("Press a button for the highlighted action  •  ESC to cancel"
                if capturing else
                "Select an action to see its key  •  ESC or Close to leave")
    hint = f_small.render(hint_txt, True, DIM)
    surface.blit(hint, (win_x + win_w // 2 - hint.get_width() // 2, win_y + 46))

    # ── Conflict-rejection flash (shown for 2 s when a reserved key is pressed) ──
    _reject_flash = app_state.get("_dvd_map_reject_flash")
    if _reject_flash is not None and (pygame.time.get_ticks() - _reject_flash) < 2000:
        _reject_key  = app_state.get("_dvd_map_reject_key")
        _reject_name = _remote_key_label(_reject_key) if _reject_key is not None else "KEY"
        _warn_txt = f"  {_reject_name} is already used — remap it first  "
        _warn_surf = f_small.render(_warn_txt, True, (255, 80, 80))
        _warn_x = win_x + win_w // 2 - _warn_surf.get_width() // 2
        surface.blit(_warn_surf, (_warn_x, win_y + 62))

    # ── Action rows (label + current key box) ───────────────────────────────
    list_x = win_x + 30
    list_w = win_w - 60
    row_h  = 44
    row_y  = win_y + 78
    for idx, (action, label) in enumerate(DVD_MAP_ACTIONS):
        ry = row_y + idx * (row_h + 8)
        is_cap_row = capturing and idx == capture_idx
        pygame.draw.rect(surface, ROW_BG, (list_x, ry, list_w, row_h), border_radius=6)
        if is_cap_row:
            pygame.draw.rect(surface, NEON_GREEN, (list_x, ry, list_w, row_h), 2, border_radius=6)
        lbl = f_body.render(label, True, NEON_GREEN if is_cap_row else TEXT_WHITE)
        surface.blit(lbl, (list_x + 14, ry + row_h // 2 - lbl.get_height() // 2))

        # Key box on the right.
        key_txt = "PRESS…" if is_cap_row else _remote_key_label(get_dvd_binding(db, action))
        kb_w, kb_h = 92, 28
        kb_x = list_x + list_w - kb_w - 12
        kb_y = ry + row_h // 2 - kb_h // 2
        pygame.draw.rect(surface, KEY_BG, (kb_x, kb_y, kb_w, kb_h), border_radius=4)
        pygame.draw.rect(surface, NEON_GREEN if is_cap_row else DIM, (kb_x, kb_y, kb_w, kb_h), 1, border_radius=4)
        ks = f_key.render(key_txt, True, NEON_GREEN if is_cap_row else TEXT_WHITE)
        surface.blit(ks, (kb_x + kb_w // 2 - ks.get_width() // 2, kb_y + kb_h // 2 - ks.get_height() // 2))

    # ── Map Buttons button (bottom-left) ────────────────────────────────────
    map_focused = (not capturing) and cursor == 0
    mb_w, mb_h = 150, 34
    mb_x = win_x + 30
    mb_y = win_y + win_h - 46
    pygame.draw.rect(surface, ROW_BG, (mb_x, mb_y, mb_w, mb_h), border_radius=5)
    if map_focused:
        pygame.draw.rect(surface, NEON_GREEN, (mb_x, mb_y, mb_w, mb_h), 2, border_radius=5)
    mb_lbl = f_body.render("Map Buttons", True, NEON_GREEN if map_focused else TEXT_WHITE)
    surface.blit(mb_lbl, (mb_x + mb_w // 2 - mb_lbl.get_width() // 2, mb_y + mb_h // 2 - mb_lbl.get_height() // 2))

    # ── Close button (bottom-right — same corner as every other sub-menu) ────
    close_focused = (not capturing) and cursor == 1
    cb_w, cb_h = 85, 34
    cb_x = win_x + win_w - cb_w - 25
    cb_y = win_y + win_h - 46
    pygame.draw.rect(surface, OFF_RED, (cb_x, cb_y, cb_w, cb_h), border_radius=5)
    if close_focused:
        pygame.draw.rect(surface, NEON_GREEN, (cb_x, cb_y, cb_w, cb_h), 2, border_radius=5)
    cb_lbl = f_body.render("Close", True, TEXT_WHITE)
    surface.blit(cb_lbl, (cb_x + cb_w // 2 - cb_lbl.get_width() // 2, cb_y + cb_h // 2 - cb_lbl.get_height() // 2))

# Module-level caches — font creation and image loading are expensive, never do per frame
_font_cache = {}
_menu_logo_cache_global = {}

# Per-channel timeline cache for the TV guide.
# Keyed by (ch_str, year, day_of_year, viewport_start_seconds). Stores the
# fully-merged timeline_entries BEFORE anchor reconciliation (which always
# runs fresh) so the expensive build_show_rotation + build_block_timeline
# pipeline only executes once per 30-min viewport window per channel per day,
# not on every twice-per-second frame-cache rebuild.
_guide_timeline_cache = {}

# --- Guide file-existence cache -------------------------------------------
# The guide filters missing media out of every row it lays out by calling
# os.path.exists() on each scheduled file. The frame/timeline caches reduced
# how OFTEN a rebuild happens, but each genuine rebuild (every time you scroll
# the viewport past its edge) still re-stat()'d every file across all visible
# rows -- real disk I/O, and on the spinning-disk machines this app targets
# that is the documented cause of the "guide is laggy while scrolling" symptom
# (see the note in render_tv_guide). Existence only changes when files are
# added/removed or a channel's active state flips -- all of which already bump
# app_state["guide_refresh_token"] -- so we memoize os.path.exists() results
# and clear the memo whenever that token changes. Within a rebuild the same
# path is often checked many times (multiple cycles/blocks); across rebuilds
# the result is stable until the token moves, so scrolling no longer pays the
# stat cost repeatedly.
_guide_exists_state = {"token": None, "cache": {}}

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

# Persistent offscreen canvas for the TV guide. The guide is authored at a
# single fixed design resolution (see GUIDE_DESIGN_W/H) and then uniformly
# smooth-scaled to fit the real window, so every element (preview, headers,
# cells, fonts, channel names) always keeps identical proportions and nothing
# overflows its box regardless of window size/shape. Cached here so we don't
# reallocate a full-size surface every frame.
#
# This is the 16:9 layout. It's LOCKED — do not change GUIDE_DESIGN_W/H,
# GUIDE_VISIBLE_ROWS_16_9, or the row/font math tied to them. It looks right
# as-is on 16:9 displays.
GUIDE_DESIGN_W, GUIDE_DESIGN_H = 1920, 1080
GUIDE_VISIBLE_ROWS_16_9 = 8
_guide_canvas = None

def _get_guide_canvas():
    global _guide_canvas
    if _guide_canvas is None or _guide_canvas.get_size() != (GUIDE_DESIGN_W, GUIDE_DESIGN_H):
        _guide_canvas = pygame.Surface((GUIDE_DESIGN_W, GUIDE_DESIGN_H))
    return _guide_canvas

# --- 4:3 GUIDE LAYOUT ------------------------------------------------------
# Separate design canvas, sized in true 4:3 proportions instead of reusing
# the 16:9 canvas. Two things fall out of that on a real 4:3/CRT window:
#   1. Independent X/Y "fill scale" (see the stretch-to-window blit at the
#      end of render_tv_guide) ends up with x_scale ~= y_scale instead of
#      x_scale << y_scale, so text stops getting squeezed skinny — a 1440
#      canvas stretching up to fill a narrower real window scales fonts UP
#      more than the 1920 canvas did.
#   2. Fewer visible rows (5 instead of 8) means each row gets a taller
#      row_height, and row text (dynamic_font_size, cell titles) already
#      scales off row_height, so it grows automatically — no per-row font
#      tuning needed here.
GUIDE_DESIGN_W_4_3, GUIDE_DESIGN_H_4_3 = 1440, 1080
GUIDE_VISIBLE_ROWS_4_3 = 5

# 4:3 shows 2 hours of timeline (4 half-hour columns) instead of the 16:9
# guide's 2.5 hours (5 columns). Fewer, wider columns means the same
# station-name text fits comfortably in each header cell on the narrower
# 4:3 canvas. 16:9 stays on GUIDE_NUM_COLS_16_9 = 5, unchanged.
GUIDE_NUM_COLS_16_9 = 5
GUIDE_NUM_COLS_4_3 = 4
_guide_canvas_43 = None

def _get_guide_canvas_43():
    global _guide_canvas_43
    if _guide_canvas_43 is None or _guide_canvas_43.get_size() != (GUIDE_DESIGN_W_4_3, GUIDE_DESIGN_H_4_3):
        _guide_canvas_43 = pygame.Surface((GUIDE_DESIGN_W_4_3, GUIDE_DESIGN_H_4_3))
    return _guide_canvas_43

def get_guide_visible_rows(db):
    """Single source of truth for how many guide rows are visible at once.
    Used both by render_tv_guide (to draw the right number of rows) and by
    the row-scroll/paging logic in retro_tv_emulator.py (so the highlighted
    row is kept inside whichever window size is actually on screen)."""
    aspect_mode = db.config.get("aspect_ratio", "16:9") if db is not None else "16:9"
    return GUIDE_VISIBLE_ROWS_4_3 if aspect_mode == "4:3" else GUIDE_VISIBLE_ROWS_16_9

def get_guide_num_cols(db):
    """Single source of truth for how many half-hour time columns the guide
    shows at once. Used both by render_tv_guide (to draw the right number of
    columns) and by the column-scroll/paging logic in retro_tv_emulator.py
    (so the highlighted column and left/right edge-pin scroll stay in sync
    with whichever column count is actually on screen)."""
    aspect_mode = db.config.get("aspect_ratio", "16:9") if db is not None else "16:9"
    return GUIDE_NUM_COLS_4_3 if aspect_mode == "4:3" else GUIDE_NUM_COLS_16_9

SYSTEM_ROW_META = {
    "game_ch":            {"kind": "toggle", "label": "Channel 03 (Games):"},
    "guide":              {"kind": "toggle", "label": "Channel 04 (TV Guide):"},
    "controls_on_start":  {"kind": "toggle", "label": "Controls Menu on Start:"},
    "boot_on_start":      {"kind": "toggle", "label": "Boot on Start:"},
    "kiosk_mode":         {"kind": "toggle", "label": "Kiosk Mode:"},
    "remote_remap":       {"kind": "button", "label": "Remote Remapping", "color": (45, 80, 130)},
    "reset_settings":     {"kind": "button", "label": "Reset All Settings"},
    "exit_program":       {"kind": "button", "label": "Exit Program"},
    "poweroff":           {"kind": "button", "label": "Turn Off Computer"},
}


def get_system_menu_rows(db, app_state):
    """Ordered list of row ids visible in the System tab right now.

    Kiosk Mode is meant to keep kids out of anything that could change
    settings or get at the underlying OS/files. When it's on, almost the
    entire System tab disappears -- what's left depends on whether "Boot on
    Start" is also on:
      - Boot on Start ON:  this session IS the shell (no desktop to fall
        back to), so the only way out is a direct power-off. Only the
        Kiosk Mode toggle, Turn Off Computer, and Close remain.
      - Boot on Start OFF: exiting just drops back to a normal desktop, so
        Exit Program is offered instead of a power-off. Only the Kiosk
        Mode toggle, Exit Program, and Close remain.

    "Close" is intentionally not included in this list -- it's always the
    last row, but it's drawn/handled separately since it's pinned to the
    bottom-right corner rather than flowing with the other rows.
    """
    kiosk_on = db.config.get("kiosk_mode_enabled", False) if db is not None else False
    boot_on_start = db.config.get("start_on_boot", False) if db is not None else False

    if kiosk_on:
        return ["kiosk_mode", "poweroff" if boot_on_start else "exit_program"]

    poweroff_visible = app_state.get("shell_takeover_active", False) if app_state is not None else False
    rows = ["game_ch", "guide", "controls_on_start", "boot_on_start", "kiosk_mode",
            "remote_remap", "reset_settings", "exit_program"]
    if poweroff_visible:
        rows.append("poweroff")
    return rows


def get_games_sub_menu_rows(db, app_state, console_key, is_on):
    """Ordered list of row ids visible below the Status toggle in a
    console's Games sub-menu right now. Mirrors get_system_menu_rows's job
    for the System tab: one shared list that both drawing and input
    handling walk, so a hidden row never leaves the two out of sync.

    Kiosk Mode hides "core_select" (SELECT LIBRETRO CORE) specifically --
    that's the one row that lets a kid browse the filesystem and swap in a
    different .dll, which is exactly the kind of file-system access Kiosk
    Mode exists to block. Everything else (controller mapping, save
    profiles, disc swap) stays available.

    "close" is intentionally not included -- same reasoning as
    get_system_menu_rows: it's pinned to the bottom-right corner.
    """
    if not is_on:
        # Console disabled.
        return []

    if console_key == "DVD":
        # DVD is a video player, not an emulator -- no core to select, no
        # gamepad controller mapping, no save-state profiles. It DOES get its
        # own "Button Mapping" row, though: a keyboard remap panel for the DVD
        # transport keys (Play/Pause, Rewind, Fast Forward, Captions) only.
        return ["dvd_button_mapping"]

    kiosk_on = db.config.get("kiosk_mode_enabled", False) if db is not None else False

    rows = []
    if not kiosk_on:
        rows.append("core_select")
    rows.append("controller_map")
    rows.extend(["profile1", "profile2", "profile3"])
    if console_key == "PSX":
        rows.append("disc")
    return rows


def preload_games_tab_assets(app_state):
    """Decode every console logo PNG (the slow part -- image.load off disk +
    convert_alpha) once, up front, the moment the settings menu opens --
    instead of on the first frame the Games tab is actually drawn, which is
    what caused the split-second hitch when navigating into it.

    This only warms the *raw* decoded logo cache, keyed by console_key.
    The Games tab render path still does its own per-cell smoothscale to
    fit the current cell size (cheap -- resizing an already-decoded surface
    is nowhere near as costly as the disk load + convert_alpha), so this
    function doesn't need to know the exact on-screen cell dimensions.

    Safe to call every time the menu opens -- it no-ops after the first
    successful run via the app_state flag below, and each console logo is
    only ever decoded once regardless.
    """
    if app_state.get("_games_tab_preloaded"):
        return
    app_state["_games_tab_preloaded"] = True

    import os
    from game_deck import CONSOLE_ORDER

    logos_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main", "images", "logos")
    _menu_logo_raw_cache = app_state.setdefault("_menu_logo_raw_cache", {})

    for console_key in CONSOLE_ORDER:
        if console_key in _menu_logo_raw_cache:
            continue
        logo_path = os.path.join(logos_dir, f"{console_key}.png")
        try:
            _menu_logo_raw_cache[console_key] = (
                pygame.image.load(logo_path).convert_alpha()
                if os.path.exists(logo_path) else None
            )
        except Exception:
            _menu_logo_raw_cache[console_key] = None


def _get_font(name, size, bold=False):
    """Return a cached SysFont. Creates it only on first call per (name,size,bold)."""
    key = (name, size, bold)
    if key not in _font_cache:
        _font_cache[key] = pygame.font.SysFont(name, size, bold=bold)
    return _font_cache[key]

def render_loading_screen(surface, progress, theme):
    """
    Paints a solid background canvas backdrop matching the active theme parameters.
    Monitors engine initialization steps before boot routing triggers execute.
    """
    # FIXED: Re-added the progress numbers safely inside parenthesis to stop the boot crash
    if progress in (1, 50, 99, 100):
        print(f"[TELEMETRY - UI PART 1] render_loading_screen invoked. Canvas size: {surface.get_size()} | Current Progress: {progress}%")
        
    surface.fill(theme.get("bg", (10, 15, 30)))

# ==============================================================================
# PART 2 OF 15: INTRO LAUNCH CONTROLS DOCUMENTATION SPLASH OVERLAY LAYOUTS
# ==============================================================================

def render_controls_splash(surface, theme, db=None, app_state=None):
    """
    2-page controls menu. Same 1036x680 canvas + smoothscale as the main menu.

    Page 0: TV Controls + TV Guide side by side (top row),
            Menu Navigation full-width below (bottom row).
    Page 1: Game Channel (top), DVD Player (bottom) — stacked.

    Footer: A/D navigate, Enter selects.
      Page 0 — highlighted right-arrow (→); Enter → page 1.
      Page 1 — left-arrow (←) and Close; Close highlighted by default;
                A focuses left-arrow (back); Enter activates focused item.
    """
    import colorsys

    CANVAS_W, CANVAS_H = 1036, 680
    real_w, real_h = surface.get_size()
    canvas = pygame.Surface((CANVAS_W, CANVAS_H), pygame.SRCALPHA)

    # ── Theme colours ──────────────────────────────────────────────────────
    bg_hue = db.config.get("theme_bg_hue", 220) / 360.0 if db is not None else 220/360.0
    ui_hue = db.config.get("theme_ui_hue", 140) / 360.0 if db is not None else 140/360.0
    r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.9, 1.0)
    NEON_GREEN    = (int(r*255), int(g*255), int(b*255))
    r, g, b = colorsys.hsv_to_rgb(bg_hue, 0.6, 0.15)
    MIDNIGHT_NAVY = (int(r*255), int(g*255), int(b*255))
    TEXT_WHITE    = (255, 255, 255)
    ARCADE_GOLD   = (255, 220, 0)
    DIM_GREY      = (60, 60, 80)

    slider_val   = db.config.get("menu_opacity", 50) if db is not None else 50
    menu_opacity = int((slider_val / 100.0) * 255)

    # ── Background + border ────────────────────────────────────────────────
    bg_surf = pygame.Surface((CANVAS_W, CANVAS_H), pygame.SRCALPHA)
    bg_surf.fill(MIDNIGHT_NAVY + (menu_opacity,))
    canvas.blit(bg_surf, (0, 0))
    pygame.draw.rect(canvas, NEON_GREEN, (0, 0, CANVAS_W, CANVAS_H), 3)

    # ── Fonts ──────────────────────────────────────────────────────────────
    f_title  = _get_font("Courier New", 44, bold=True)
    f_sect   = _get_font("Courier New", 22, bold=True)
    f_body   = _get_font("Courier New", 17, bold=True)
    f_footer = _get_font("Courier New", 17, bold=True)

    ROW_STEP = f_body.get_height() + 6
    SECT_H   = f_sect.get_height() + 11   # title height + underline gap + padding

    # ── State ─────────────────────────────────────────────────────────────
    if app_state is None:
        app_state = {}
    page  = app_state.get("controls_splash_page",  0)
    focus = app_state.get("controls_splash_focus", 1)  # page 1: 0=← 1=close

    # ── Control data ──────────────────────────────────────────────────────
    tv_controls = [
        ("UP / DOWN",    "CHANNEL UP / CHANNEL DOWN"),
        ("LEFT / RIGHT", "VOLUME DOWN / VOLUME UP"),
        ("N",            "MUTE / UNMUTE"),
        ("M",            "OPEN / CLOSE MAIN MENU"),
        ("B",            "OPEN / CLOSE CONTROL MENU"),
        ("ESCAPE",       "MINIMIZE WINDOW"),
    ]
    guide_controls = [
        ("W / S",  "SCROLL CHANNELS UP / DOWN"),
        ("A / D",  "SCROLL TIME SLOTS LEFT / RIGHT"),
        ("ENTER",  "TUNE TO SELECTED CHANNEL"),
    ]
    menu_controls = [
        ("W / S",  "NAVIGATE UP / DOWN"),
        ("A / D",  "NAVIGATE LEFT / RIGHT"),
        ("ENTER",  "SELECT / CONFIRM"),
        ("ESCAPE", "BACK / MINIMIZE"),
    ]
    game_controls_kb = [
        ("W / S",  "NAVIGATE MENUS"),
        ("ENTER",  "SELECT / CONFIRM"),
        ("ESCAPE", "BACK / QUIT GAME"),
    ]
    game_controls_pad = [
        ("D-PAD",           "NAVIGATE MENUS"),
        ("A BUTTON",        "SELECT / CONFIRM"),
        ("B BUTTON",        "BACK"),
        ("START / SELECT",  "QUIT GAME "),
    ]
    dvd_controls_kb = [
        ("V",          "CAPTIONS ON / OFF"),
        ("MED PLAY",   "PAUSE / UNPAUSE"),
        ("MED REW",    "REWIND  (HOLD FOR FAST REWIND)"),
        ("MED FF",     "FAST FORWARD  (HOLD FOR FASTER)"),
        ("MED STOP",   "RETURN TO MAIN MENU  (ISO DISC ONLY)"),
        ("ESCAPE",     "QUIT  —  YES / NO PROMPT"),
        ("ENTER",      "CONFIRM YES / NO SELECTION"),
    ]
    dvd_controls_pad = [
        ("UP",              "CAPTIONS ON / OFF"),
        ("LEFT",            "REWIND  (HOLD FOR FAST REWIND)"),
        ("DOWN",            "PAUSE / UNPAUSE"),
        ("RIGHT",           "FAST FORWARD  (HOLD FOR FASTER)"),
        ("A BUTTON",        "SELECT"),
        ("START / SELECT",  "QUIT GAME"),
    ]

    # ── Helpers ───────────────────────────────────────────────────────────
    def draw_sect_title(text, x, y, w):
        lbl = f_sect.render(text, True, TEXT_WHITE)
        canvas.blit(lbl, (x, y))
        uy = y + lbl.get_height() + 3
        pygame.draw.line(canvas, TEXT_WHITE, (x, uy), (x + w, uy), 1)
        return SECT_H

    def draw_rows(rows, x, y, col_w, key_offset=145):
        for key_lbl, desc_lbl in rows:
            canvas.blit(f_body.render(key_lbl,  True, ARCADE_GOLD), (x, y))
            canvas.blit(f_body.render(desc_lbl, True, TEXT_WHITE),  (x + key_offset, y))
            y += ROW_STEP
        return len(rows) * ROW_STEP

    def draw_subhead(text, x, y):
        """Small gold label (e.g. 'KEYBOARD' / 'CONTROLLER') marking a
        column of rows, so each row doesn't need to repeat '(CONTROLLER)'."""
        lbl = f_body.render(text, True, ARCADE_GOLD)
        canvas.blit(lbl, (x, y))
        return lbl.get_height() + 8

    def draw_footer(page_num):
        footer_y = CANVAS_H - 52
        pygame.draw.line(canvas, NEON_GREEN, (40, footer_y - 8), (CANVAS_W - 40, footer_y - 8), 1)
        hint = f_footer.render("A / D  TO NAVIGATE    ENTER  TO SELECT", True, TEXT_WHITE)
        canvas.blit(hint, (margin, footer_y + 6))

        if page_num == 0:
            # Right arrow — always highlighted (only nav item on page 0)
            lbl   = f_footer.render("  \u25ba  ", True, MIDNIGHT_NAVY)
            btn_w = lbl.get_width() + 8
            btn_h = lbl.get_height() + 4
            bx    = CANVAS_W - margin - btn_w
            by    = footer_y + 4
            pygame.draw.rect(canvas, NEON_GREEN, (bx - 4, by, btn_w + 4, btn_h), border_radius=4)
            canvas.blit(lbl, (bx, by + 2))
        else:
            left_focused = (focus == 0)
            # Close button
            cl  = f_footer.render("  CLOSE  ", True, MIDNIGHT_NAVY if not left_focused else TEXT_WHITE)
            cw  = cl.get_width() + 8
            ch  = cl.get_height() + 4
            cx_ = CANVAS_W - margin - cw
            cy_ = footer_y + 4
            pygame.draw.rect(canvas, NEON_GREEN if not left_focused else DIM_GREY,
                             (cx_ - 4, cy_, cw + 4, ch), border_radius=4)
            canvas.blit(cl, (cx_, cy_ + 2))
            # Left arrow
            ll  = f_footer.render("  \u25c4  ", True, MIDNIGHT_NAVY if left_focused else TEXT_WHITE)
            lw  = ll.get_width() + 8
            lh  = ll.get_height() + 4
            lx_ = cx_ - lw - 12
            ly_ = footer_y + 4
            pygame.draw.rect(canvas, NEON_GREEN if left_focused else DIM_GREY,
                             (lx_ - 4, ly_, lw + 4, lh), border_radius=4)
            canvas.blit(ll, (lx_, ly_ + 2))

    # ── Title (shared) ────────────────────────────────────────────────────
    title_lbl = f_title.render("CONTROL MENU", True, ARCADE_GOLD)
    canvas.blit(title_lbl, (CANVAS_W//2 - title_lbl.get_width()//2, 15))
    title_bottom = 15 + title_lbl.get_height() + 4
    pygame.draw.line(canvas, ARCADE_GOLD, (40, title_bottom), (CANVAS_W - 40, title_bottom), 1)

    content_y = title_bottom + 42
    margin    = 40
    gutter    = 28
    full_w    = CANVAS_W - margin * 2

    # ══════════════════════════════════════════════════════════════════════
    # PAGE 0 — TV Controls + TV Guide (side by side), Menu Navigation below
    # ══════════════════════════════════════════════════════════════════════
    if page == 0:
        col_w = (full_w - gutter) // 2

        # Top row: TV Controls (left) | TV Guide (right)
        cy = content_y
        th = draw_sect_title("TV CONTROLS", margin, cy, col_w)
        draw_rows(tv_controls, margin, cy + th, col_w)

        draw_sect_title("TV GUIDE", margin + col_w + gutter, cy, col_w)
        draw_rows(guide_controls, margin + col_w + gutter, cy + th, col_w)

        top_row_h = SECT_H + max(len(tv_controls), len(guide_controls)) * ROW_STEP

        # Divider
        div_y = content_y + top_row_h + 28
        pygame.draw.line(canvas, (80, 80, 110), (margin, div_y), (CANVAS_W - margin, div_y), 1)

        # Bottom row: Menu Navigation — full width
        cy2 = div_y + 22
        th2 = draw_sect_title("MENU NAVIGATION", margin, cy2, full_w)
        draw_rows(menu_controls, margin, cy2 + th2, full_w)

    # ══════════════════════════════════════════════════════════������═���════════
    # PAGE 1 — Game Channel (top), DVD Player (bottom) — stacked
    # ══════════════════════════════════════════════════════════════════════
    else:
        col_w = (full_w - gutter) // 2

        cy = content_y
        th = draw_sect_title("GAME CHANNEL", margin, cy, full_w)
        sub_y = cy + th
        sh = draw_subhead("KEYBOARD", margin, sub_y)
        draw_rows(game_controls_kb, margin, sub_y + sh, col_w)
        draw_subhead("CONTROLLER", margin + col_w + gutter, sub_y)
        draw_rows(game_controls_pad, margin + col_w + gutter, sub_y + sh, col_w)

        game_rows_h   = max(len(game_controls_kb), len(game_controls_pad)) * ROW_STEP
        game_block_h  = th + sh + game_rows_h

        # Divider
        div_y = content_y + game_block_h + 28
        pygame.draw.line(canvas, (80, 80, 110), (margin, div_y), (CANVAS_W - margin, div_y), 1)

        cy2 = div_y + 22
        th2 = draw_sect_title("DVD PLAYER", margin, cy2, full_w)
        sub_y2 = cy2 + th2
        sh2 = draw_subhead("KEYBOARD", margin, sub_y2)
        draw_rows(dvd_controls_kb, margin, sub_y2 + sh2, col_w)
        draw_subhead("CONTROLLER", margin + col_w + gutter, sub_y2)
        draw_rows(dvd_controls_pad, margin + col_w + gutter, sub_y2 + sh2, col_w)

    # ── Footer (shared) ───────────────────────────────────────────────────
    draw_footer(page)

    # ── Stamp canvas onto real screen (identical to main menu logic) ───────
    if real_w >= CANVAS_W and real_h >= CANVAS_H:
        fx = (real_w - CANVAS_W) // 2
        fy = (real_h - CANVAS_H) // 2
        surface.blit(canvas, (fx, fy))
    else:
        shrink_w = min(CANVAS_W, real_w - 20)
        shrink_h = int(shrink_w * (CANVAS_H / CANVAS_W))
        if shrink_h > real_h - 20:
            shrink_h = real_h - 20
            shrink_w = int(shrink_h * (CANVAS_W / CANVAS_H))
        scaled = pygame.transform.smoothscale(canvas, (shrink_w, shrink_h))
        surface.blit(scaled, ((real_w - shrink_w)//2, (real_h - shrink_h)//2))

# ==============================================================================
# PART 3 OF 15: FIXED-WIDTH TV GUIDE DASHBOARD HEADER INTERFACE
# ==============================================================================

_guide_frame_cache = {"key": None, "canvas": None, "preview_bounds": None}

def _clean_show_title(raw_title):
    """
    Turn a raw filename/path into a show-only title for the TV guide display:
    strips season/episode markers, years, and quality/rip tags, but
    deliberately leaves plain numbers alone otherwise.

    Cuts the title at the first clearly-a-marker match found ANYWHERE in
    the string (S01E02, "Season 1"/"Season.1", "1x02", "Episode 2"/
    "Episode.2", "Part 2"/"Part.2", a bare 19xx/20xx year, or a
    resolution/rip tag like 1080p/DVDRip/x264) -- these all use
    recognizable keywords or a 4-digit year, so they're safe to cut on
    regardless of where they appear or what separator (space, period,
    underscore, dash) sits between the keyword and its number.

    What this intentionally does NOT do: strip a bare, keyword-less
    episode number (e.g. "Show - 02" or a combined code like "Show.102").
    That convention is genuinely ambiguous with numbers that are actually
    part of a title -- "The 100", "101 Dalmatians", "007 Private Eye" all
    look identical in shape to "Show - 100"/"Show.101"/"Show.007". Rather
    than guess and risk chopping a real title down to "THE" or "007"
    turning into nothing, this only cuts on unambiguous keyword/year/tag
    matches and leaves bare numbers exactly as they are. The trade-off:
    a show that ONLY uses bare-number naming (no S/E, no "Season", no
    "Episode") will still show its trailing episode number in the guide
    (e.g. "SHOW NAME - 02") instead of being fully trimmed to "SHOW NAME".

    FOLDER-NAME FALLBACK: some libraries name the show's FOLDER properly
    (e.g. "Ben 10/") but leave the actual episode files anonymous/numeric
    (e.g. "001.mp4", "S01E04.mkv" with nothing else in the filename, or a
    ripper's generic "VTS_01_2.mkv"-style name). Cleaning a filename like
    that the normal way strips every marker/number and leaves nothing --
    an EMPTY title in the guide -- even though the show's real name was
    sitting right there in the parent folder the whole time. So if the
    filename-derived title cleans down to nothing, fall back to cleaning
    the immediate parent folder's name the same way and use that instead.
    """
    import re as _re
    import os

    def _strip_markers(text):
        """Shared cleanup pass (brackets/tags, cut-patterns, punctuation)
        used for both the filename-derived title and the folder-name
        fallback, so a messy folder name gets the exact same treatment a
        filename would."""
        t = text.replace("_", " ")
        # Bracketed/parenthesized chunks are always metadata, never title
        # content -- release-group tags, "(1999)", "[1080p]", etc.
        t = _re.sub(r'[\[\(][^\]\)]*[\]\)]', ' ', t)

        _CUT_PATTERNS = [
            r's\d{1,2}[\.\s_-]*[ex]\d{1,3}\b',     # S01E02 / S01.E13 / s01e02
            r'\bseason[\.\s_-]*\d+\b',              # Season 1 / Season.1 / Season_1
            r'\b\d{1,2}x\d{2,3}\b',                 # 1x02
            r'\bepisode[\.\s_-]*\d+\b',              # Episode 2 / Episode.2
            r'\bep\d+\b',                            # ep2
            r'\bpart[\.\s_-]*\d+\b',                 # Part 2 / Part.2
            r'\bpt\.?[\.\s_-]*\d+\b',                 # Pt.2 / Pt 2
            r'\b(?:19|20)\d{2}\b',                  # a bare year: 1999 / 2020
            r'\b(?:1080p|720p|480p|2160p|4k)\b',
            r'\b(?:dvdrip|webrip|web-?dl|blu-?ray|hdtv|xvid|divx|x264|x265|h26[45]|hevc|aac|ac3|dts|remux|repack|proper|extended|unrated)\b',
        ]
        cut_at = len(t)
        for pat in _CUT_PATTERNS:
            m = _re.search(pat, t, _re.IGNORECASE)
            if m and m.start() < cut_at:
                cut_at = m.start()

        t = t[:cut_at]
        t = t.replace(".", " ").replace("-", " ")
        t = _re.sub(r'\s+', ' ', t).strip()
        return t

    s = raw_title if isinstance(raw_title, str) else str(raw_title)
    _folder_fallback = ""
    if "/" in s or "\\" in s:
        _parent = os.path.dirname(s)
        if _parent:
            _folder_fallback = os.path.basename(_parent)
        s = os.path.basename(s)
    if "." in s:
        s = os.path.splitext(s)[0]

    cleaned = _strip_markers(s)
    if cleaned:
        return cleaned

    # Filename cleaned down to nothing -- try the folder name instead.
    if _folder_fallback:
        cleaned_folder = _strip_markers(_folder_fallback)
        if cleaned_folder:
            return cleaned_folder

    return cleaned


def render_tv_guide(surface, app_state, db, theme, target_rect=None):
    """
    Cached entry point for the TV guide.

    target_rect (optional pygame.Rect): the on-screen region the guide should
    fill. Defaults to the whole surface. This is what lets 4:3 Test Mode on a
    16:9 machine confine the guide to the pillarboxed 4:3 content region (with
    black bars / TV border around it) instead of stretching the guide across
    the full 16:9 window. On a real 4:3 display target_rect is simply the whole
    window, so behavior there is unchanged.

    _render_tv_guide_impl() below rebuilds the ENTIRE guide from scratch on
    every call: for every visible row it walks the channel's schedule,
    os.path.exists()-checks every media file in the relevant block(s),
    rebuilds the show rotation, and re-pairs short episodes. That's real
    disk I/O and non-trivial CPU work, and it was happening once per
    RENDERED FRAME (i.e. up to 60x/sec) while the guide was on screen --
    across up to 8 visible rows and multiple day-parts each. On the slower,
    older machines this app targets (recycled Windows boxes with spinning
    disks), that's almost certainly the source of "the TV guide is laggy":
    the guide was redoing a full schedule rebuild far more often than its
    content could possibly change.

    The guide's actual content (what's airing when) only changes a few
    times a second at most -- on user navigation, or when the live show
    rolls over. There's no need to redo the heavy rebuild more than a
    couple of times a second; the on-screen clock's seconds display doesn't
    need finer than that either. So: rebuild at most twice a second (or
    immediately on navigation/theme/window changes), and on every other
    frame just re-scale + re-blit the cached canvas -- which is orders of
    magnitude cheaper than the rebuild it replaces.
    """
    import time as _time
    real_w, real_h = surface.get_size()
    # Region the guide fills. Defaults to the whole surface (unchanged behavior);
    # a target_rect confines it to e.g. the 4:3 pillarboxed content region.
    if target_rect is not None:
        tx, ty, tw, th = target_rect.x, target_rect.y, target_rect.width, target_rect.height
    else:
        tx, ty, tw, th = 0, 0, real_w, real_h
    aspect_mode = db.config.get("aspect_ratio", "16:9") if db is not None else "16:9"

    cache_key = (
        aspect_mode, real_w, real_h, tx, ty, tw, th,
        app_state.get("selected_guide_channel"),
        app_state.get("guide_col_pos"),
        app_state.get("guide_row_offset"),
        app_state.get("selected_guide_time_idx"),
        db.config.get("theme_bg_hue") if db is not None else None,
        db.config.get("theme_ui_hue") if db is not None else None,
        # Refresh twice a second -- keeps the clock and any schedule
        # rollover looking live without paying the full rebuild cost on
        # every one of up to 60 frames/sec.
        int(_time.time() * 2),
    )

    cached = _guide_frame_cache
    if cached["key"] == cache_key and cached["canvas"] is not None:
        surface.blit(pygame.transform.smoothscale(cached["canvas"], (tw, th)), (tx, ty))
        if cached["preview_bounds"] is not None:
            app_state["guide_preview_bounds"] = cached["preview_bounds"]
        return

    _render_tv_guide_impl(surface, app_state, db, theme, target_rect=target_rect)

    design_canvas = _get_guide_canvas_43() if aspect_mode == "4:3" else _get_guide_canvas()
    cached["key"] = cache_key
    cached["canvas"] = design_canvas.copy()
    cached["preview_bounds"] = app_state.get("guide_preview_bounds")


def _render_tv_guide_impl(surface, app_state, db, theme, target_rect=None):
    """
    EPG Grid System with Anchored Center Preview & 5-column Screen Boundary Lock.
    Fixed: Synchronized row fonts to scale with cells to prevent layout text leaks.
    Row count is 8 on the locked 16:9 layout, 5 on the dedicated 4:3 layout
    (see GUIDE_VISIBLE_ROWS_16_9 / GUIDE_VISIBLE_ROWS_4_3).
    """
    import os
    import datetime
    import math

    # Draw the whole guide onto a fixed-size design canvas, then uniformly
    # smooth-scale it to fit the real window at the very end. This is what makes
    # everything scale together as one unit — the internal layout below always
    # runs against the same logical dimensions, so a short/wide (or any-shaped)
    # window can never collapse the rows while leaving the preview huge.
    #
    # 16:9 vs 4:3 pick a different design canvas + row count (see the 4:3
    # comment block above _get_guide_canvas_43). The 16:9 canvas/behavior is
    # unchanged — this only branches for the 4:3 case.
    real_surface = surface
    real_w, real_h = real_surface.get_size()
    aspect_mode = db.config.get("aspect_ratio", "16:9") if db is not None else "16:9"
    is_4_3 = (aspect_mode == "4:3")
    num_rows = GUIDE_VISIBLE_ROWS_4_3 if is_4_3 else GUIDE_VISIBLE_ROWS_16_9
    num_cols = GUIDE_NUM_COLS_4_3 if is_4_3 else GUIDE_NUM_COLS_16_9
    last_col = num_cols - 1
    viewport_seconds = num_cols * 1800  # 2hrs (4:3) / 2.5hrs (16:9) of visible timeline
    surface = _get_guide_canvas_43() if is_4_3 else _get_guide_canvas()
    w, h = (GUIDE_DESIGN_W_4_3, GUIDE_DESIGN_H_4_3) if is_4_3 else (GUIDE_DESIGN_W, GUIDE_DESIGN_H)
    surface.fill((1, 1, 1))  # not pure black: (0,0,0) is the game-overlay transparency key
    
    import colorsys
    bg_hue = db.config.get("theme_bg_hue", 220) / 360.0 if db is not None else 220/360.0
    ui_hue = db.config.get("theme_ui_hue", 140) / 360.0 if db is not None else 140/360.0
    r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.9, 1.0)
    MINT_GREEN = (int(r * 255), int(g * 255), int(b * 255))
    r, g, b = colorsys.hsv_to_rgb(bg_hue, 0.6, 0.15)
    UNSELECTED_BLUE = (int(r * 255), int(g * 255), int(b * 255))
    r, g, b = colorsys.hsv_to_rgb(bg_hue, 0.7, 0.08)
    DARKER_TIME_BLUE = (int(r * 255), int(g * 255), int(b * 255))
    CYAN_GRID = (255, 255, 255)
    TEXT_WHITE = (255, 255, 255)
    
    # Time-slot header font ("08:00 AM" labels) is sized independently of the
    # row/clock fonts: 4:3 gets 22pt, 16:9 stays at the original 17pt. Only
    # this label reads _HDR_FONT_PX_4_3 below -- font_row_lg/font_clock are
    # untouched.
    _HDR_FONT_PX_4_3 = 22
    _HDR_FONT_PX_16_9 = 17
    font_hdr = _get_font("Courier New", _HDR_FONT_PX_4_3 if is_4_3 else _HDR_FONT_PX_16_9, bold=True)
    font_row_lg = _get_font("Courier New", 17, bold=True)
    font_clock = _get_font("Courier New", 17, bold=True)

    # --- MINIMUM READABLE TEXT SIZE (4:3 mode) --------------------------------
    # The whole design canvas gets smoothscaled to the real window/screen size
    # at the very end of this function (see "Fill scale" below). A 17pt font
    # drawn in canvas space can still end up much smaller once that scale is
    # applied, if the real window/display is smaller than the design canvas
    # (h=1080) — which is exactly what made the guide unreadable on a shrunk
    # or low-res 4:3 display. Boost the canvas-space point size by the inverse
    # of that shrink factor so the text drawn on screen is never smaller than
    # _MIN_GUIDE_FONT_PX actual pixels, no matter how small the real window
    # gets. Only kicks in when the canvas is being shrunk (scale < 1) — a
    # canvas being enlarged already renders text bigger than the minimum.
    _MIN_GUIDE_FONT_PX = 17
    _guide_scale_y = (real_h / h) if h else 1.0
    if is_4_3 and 0 < _guide_scale_y < 1.0:
        _min_font_pt = max(_MIN_GUIDE_FONT_PX, int(math.ceil(_MIN_GUIDE_FONT_PX / _guide_scale_y)))
        _min_font_pt_hdr = max(_HDR_FONT_PX_4_3, int(math.ceil(_HDR_FONT_PX_4_3 / _guide_scale_y)))
        font_hdr = _get_font("Courier New", _min_font_pt_hdr, bold=True)
        font_row_lg = _get_font("Courier New", _min_font_pt, bold=True)
        font_clock = _get_font("Courier New", _min_font_pt, bold=True)
    else:
        _min_font_pt = _MIN_GUIDE_FONT_PX

    # --- 4:3 ONLY: double the CH ##, OFF-AIR/PLAYING MUSIC, and show-title
    # text. font_row_lg is used EXCLUSIVELY for those three things (nothing
    # else in the guide reads it), so doubling this one font doubles exactly
    # those three and nothing else. Built off _min_font_pt -- the size
    # font_row_lg was just set to above, whether that's the boosted
    # minimum-readable size or the plain 17px floor -- so this still respects
    # the existing minimum-readability logic instead of overriding it.
    # font_clock (live clock/date) and font_hdr (time-slot column headers)
    # are left exactly as computed above, untouched. Every label using this
    # font is still centered/anchored inside its existing fixed-size cell
    # box, so only the glyph size changes -- no box, row, or column moves or
    # resizes.
    if is_4_3:
        font_row_lg = _get_font("Courier New", _min_font_pt * 2, bold=True)
        # Separate, slightly smaller font ONLY for the "CH ##" number label in
        # the channel-name column. Show titles, OFF-AIR, and PLAYING MUSIC
        # (all also rendered with font_row_lg) are untouched — only the big
        # channel number shrinks, which lets each row be a few pixels shorter
        # without any other text changing size.
        font_ch_num = _get_font("Courier New", max(13, int(_min_font_pt * 1.35)), bold=True)
    else:
        font_ch_num = font_row_lg   # 16:9: CH ## shares the same size as show titles

    # 4:3 gets a wider preview window (900/1920 vs 800/1920 before) — the
    # ~51px of extra preview height is funded by each of the 5 visible rows
    # being proportionally shorter. 16:9 is completely unchanged (800/1920).
    _pw_ratio = (900.0 / 1920.0) if is_4_3 else (800.0 / 1920.0)
    pw = int(w * _pw_ratio)
    ph = int(pw * (9.0 / 16.0))
    px = (w - pw) // 2
    py = 15
    app_state["guide_preview_bounds"] = (px, py, pw, ph)
    
    grid_top_y = py + ph + 10 
    header_height = 32
    # 1 station-name column + num_cols time columns, spread evenly across w.
    # 16:9 keeps its locked 1/6th split (5 time cols); 4:3 splits into fifths
    # (4 time cols) so each header cell is wider and the station name/time
    # text fits without crowding.
    col_width = w // (num_cols + 1)
    
    pygame.draw.rect(surface, UNSELECTED_BLUE, (0, grid_top_y, w, h - grid_top_y))
    
    now_dt = datetime.datetime.now()
    clock_txt = now_dt.strftime("%I:%M:%S %p")
    date_txt = now_dt.strftime("%m/%d/%Y")
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

    system_live_c_idx = (now_dt.hour * 2) + (1 if now_dt.minute >= 30 else 0)
    user_shift = app_state.get("selected_guide_time_idx", 0)
    time_idx = (system_live_c_idx + user_shift) % 48
    viewport_start_seconds = time_idx * 1800

    for col in range(num_cols):
        c_idx = (time_idx + col) % 48
        hr = c_idx // 2
        mn = "00" if (c_idx % 2 == 0) else "30"
        ampm = "AM" if hr < 12 else "PM"
        hr_12 = hr if (0 < hr <= 12) else (12 if hr == 0 or hr == 12 else hr - 12)
        
        hdr_txt = f"{hr_12:02d}:{mn} {ampm}"
        h_lbl = font_hdr.render(hdr_txt, True, MINT_GREEN)
        cell_x = col_width + (col * col_width)
        
        current_block_w = (w - cell_x) if col == last_col else col_width
            
        pygame.draw.rect(surface, DARKER_TIME_BLUE, (cell_x, grid_top_y, current_block_w, header_height))
        pygame.draw.rect(surface, CYAN_GRID, (cell_x, grid_top_y, current_block_w, header_height), 1)
        
        hdr_lbl_x = cell_x + (current_block_w // 2 - h_lbl.get_width() // 2)
        hdr_lbl_y = grid_top_y + (header_height // 2 - h_lbl.get_height() // 2)
        surface.blit(h_lbl, (hdr_lbl_x, hdr_lbl_y))
        
        if c_idx == system_live_c_idx:
            pygame.draw.rect(surface, MINT_GREEN, (cell_x, grid_top_y, current_block_w, header_height), 2)
        
    pygame.draw.rect(surface, CYAN_GRID, (0, grid_top_y, col_width, header_height), 1)
    
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
    viewport_end_seconds = viewport_start_seconds + viewport_seconds

# ==============================================================================
# PART 4 OF 15: STATION NAME BLITTERS & METADATA OVERFLOW POOL HANDLERS
# ==============================================================================

    # ── PRIORITY SCAN ORDER ───────────────────────────────────────────────────
    # Pre-compute each channel's Y position so processing them in
    # highlighted-first order still draws every row at the correct screen Y.
    # Sort: highlighted (currently-watched) channel first, others in natural
    # top-to-bottom visual order. On a cold timeline cache the highlighted
    # row's build_show_rotation + build_block_timeline run before any other
    # row; on a warm cache every row is an instant hit regardless of order.
    _row_start_y_map = {
        str(visible_channels[_ri]).zfill(2): start_y + _ri * row_height
        for _ri in range(len(visible_channels))
    }
    _sel_guide_ch = str(app_state.get("selected_guide_channel", "05")).zfill(2)
    _guide_proc_order = sorted(
        range(len(visible_channels)),
        key=lambda _i: (0 if str(visible_channels[_i]).zfill(2) == _sel_guide_ch else 1, _i)
    )
    for _pi in _guide_proc_order:
        r_idx = _pi
        ch_num = visible_channels[_pi]
        start_y = _row_start_y_map.get(str(ch_num).zfill(2), grid_top_y + header_height)
        if start_y + row_height > h + 5:
            continue

        ch_str = str(ch_num).zfill(2)
        ch_info = db.channels_db.get(ch_str, {})
        is_highlighted_row = (ch_str == str(app_state["selected_guide_channel"]).zfill(2))
        
        pygame.draw.rect(surface, UNSELECTED_BLUE, (0, start_y, col_width, row_height))
        pygame.draw.rect(surface, CYAN_GRID, (0, start_y, col_width, row_height), 1)
        
        display_station_name = str(ch_info.get("name", f"STATION {ch_str}")).upper()
        
        # FIXED TYPOGRAPHY TRACKING: Font dimensions dynamically adapt to your locked row height!
        # This keeps text from scaling weirdly or clipping out of its borders on smaller windows.
        # In 4:3 mode, floored to the same _min_font_pt as the rest of the guide
        # (see _MIN_GUIDE_FONT_PX above) so the station-name line doesn't stay
        # tiny/unreadable while everything else around it gets boosted. 16:9
        # behavior is untouched — still just the plain 13px floor.
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
        
        if ch_info.get("is_visualizer", False):
            s_list = ch_info.get("schedules", {}).get("Music Tracks", [])
            for col in range(num_cols):
                current_row_blocks_list.append(col)
                is_selected_cell = (is_highlighted_row and col == app_state.get("guide_col_pos", 0))
                cell_bg = MINT_GREEN if is_selected_cell else UNSELECTED_BLUE
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
                                # The char-count guess above isn't checked against the
                                # actual rendered pixel width, so it can still come out
                                # wider than max_txt_w (e.g. "PLAYING MUSIC"). Centering
                                # that oversized label with current_cell_w // 2 - width // 2
                                # goes negative and bleeds left into the previous cell
                                # (the station-name column for col 0). Keep shrinking
                                # until it actually fits, the same guarantee the regular-
                                # channel path gets for free by left-aligning instead.
                                while lbl_cell.get_width() > max_txt_w and len(truncated_label) > 1:
                                    truncated_label = truncated_label[:-1]
                                    lbl_cell = font_row_lg.render(truncated_label, True, cell_txt_col)
                                cell_text_x = cell_x + (current_cell_w // 2 - lbl_cell.get_width() // 2)
                                cell_text_x = max(cell_x + 2, cell_text_x)
                                surface.blit(lbl_cell, (cell_text_x, start_y + (row_height // 2 - lbl_cell.get_height() // 2)))
                
            if is_highlighted_row:
                app_state["guide_current_row_blocks"] = current_row_blocks_list
        else:
            last_drawn_pixel_x = col_width
            has_rendered_any_video = False
            
            time_blocks = [
                ("Morning", 5, 13),   
                ("Evening", 13, 21),  
                ("Night", 21, 29),    
            ]
            
            viewport_adjusted_start = viewport_start_seconds
            viewport_adjusted_end = viewport_adjusted_start + viewport_seconds
            last_drawn_abs_time = viewport_adjusted_start
            
            _is_full_day_guide = (not ch_info.get("is_visualizer", False)
                                  and ch_info.get("scheduling_mode", "random_slots") != "marathon"
                                  and ch_info.get("block_mode", "full_day") == "full_day")

            def get_block_for_hour(hour):
                if _is_full_day_guide:
                    return "Full Day", hour * 3600
                if 5 <= hour < 13:
                    return "Morning", (hour - 5) * 3600
                elif 13 <= hour < 21:
                    return "Evening", (hour - 13) * 3600
                else:  
                    if hour >= 21:
                        return "Night", (hour - 21) * 3600
                    else:  
                        return "Night", (hour + 3) * 3600  
                        
            def _build_commercial_tracks():
                """Mirrors calculate_slotted_playback_state's commercial-track
                normalization exactly (unprobed/-1 durations clamped to a 30s
                default) so the guide feeds media.build_block_timeline() the
                same commercial pool the engine would build."""
                COMMERCIAL_DEFAULT_DUR = 30
                commercials_enabled = ch_info.get("commercials_enabled", False)
                commercial_items = ch_info.get("schedules", {}).get("Commercials", [])
                tracks = []
                if commercials_enabled and commercial_items:
                    for asset in commercial_items:
                        path = asset.get("path", asset.get("file", "")) if isinstance(asset, dict) else str(asset)
                        if path and _guide_path_exists(path, app_state):
                            duration = asset.get("duration", COMMERCIAL_DEFAULT_DUR) if isinstance(asset, dict) else COMMERCIAL_DEFAULT_DUR
                            if not duration or duration <= 0:
                                duration = COMMERCIAL_DEFAULT_DUR
                            tracks.append({"path": path, "duration": int(duration)})
                return tracks

            def get_block_content(block_name, _override_sp=None):
                """Build the ordered list of video tracks for one block.

                _override_sp: when provided (a show_pos dict), skips the live-block
                cache and calls build_show_rotation with this show_pos directly —
                used by the guide's within-block cycle advancement to get the NEXT
                set of episodes after the current cycle's playlist has run through.

                Returns (video_tracks, advanced_show_pos).  advanced_show_pos is the
                show_pos dict AFTER picking this cycle's episodes; pass it back in as
                _override_sp on the next call to get the cycle after that.
                """
                is_vis_chan = ch_info.get("is_visualizer", False)
                schedules = ch_info.get("schedules", {})
                folder_items = schedules.get("Music Tracks" if is_vis_chan else block_name, [])
                if not folder_items:
                    return [], {}

                raw_tracks = []
                _media_probe_cache = (db.config.get("media_probe_cache", {}) if db is not None else {})
                for item in folder_items:
                    p = item.get("path", item.get("file", "")) if isinstance(item, dict) else str(item)
                    if p and _guide_path_exists(p, app_state):
                        # SINGLE SOURCE OF TRUTH (media.resolve_track_duration):
                        # always prefer a real probed length over the stored
                        # value for laying out not-yet-aired slots. The
                        # background prober writes true file lengths into
                        # media_probe_cache (global path-keyed store); the
                        # stored schedule `duration` may still be the 1800s
                        # placeholder estimate, and laying out future/other-
                        # channel rows at an estimate is exactly what made the
                        # guide drift onto the WRONG EPISODE/movie the further
                        # a real runtime diverged from 1800s -- a real 2h film
                        # would fall under is_movie_track's 4800s threshold
                        # and be mis-treated as a series episode. The
                        # live-block parity path below still re-freezes to
                        # sched_cached_durations, so this only affects
                        # not-yet-aired layout and never disturbs the
                        # currently-airing split.
                        stored = item.get("duration", 0) if isinstance(item, dict) else 0
                        d = media.resolve_track_duration(_media_probe_cache, p, stored)
                        raw_tracks.append({"path": p, "duration": d})
                if not raw_tracks:
                    return [], {}

                block_offset = {"Morning": 100, "Evening": 200, "Night": 300, "Full Day": 500}.get(block_name, 400)
                day_seed = now_dt.year * 1000 + now_dt.timetuple().tm_yday + int(ch_str) + block_offset
                sched_mode = ch_info.get("scheduling_mode", "random_slots")

                # SAME SOURCE OF TRUTH as actual playback (media.build_show_rotation),
                # seeded from the channel's real persisted per-show position, so the
                # guide always matches what's actually airing instead of drifting out
                # of sync with a separate reimplementation.
                playback_log = ch_info.get("playback_log", {})
                _show_pos_copy = dict(_override_sp if _override_sp is not None
                                      else playback_log.get("show_pos", {}))
                _pair_thresh_sec = max(0, ch_info.get("pair_threshold_minutes", 15)) * 60

                # CURRENT-BLOCK PARITY: when the scheduler builds the live block it
                # ADVANCES and persists each show's position, then caches the exact
                # chosen rotation in sched_cached_tracks. If the guide re-ran
                # build_show_rotation with that already-advanced show_pos it would
                # compute the NEXT block's lineup, so the currently-airing cell would
                # show the wrong episode. For the live block, replay the scheduler's
                # cache verbatim (reorder raw_tracks by cached paths + re-apply pairing)
                # exactly like calculate_slotted_playback_state's re-tune branch does.
                # Only for real video channels — visualizer channels have their own
                # half-hour "playing music" display and are intentionally left alone.
                # Skip the cache when _override_sp is provided — caller wants a fresh
                # cycle pick from a specific show_pos, not the cached current-cycle lineup.
                if not is_vis_chan and _override_sp is None:
                    if _is_full_day_guide:
                        _live_block = "Full Day"
                    else:
                        _gh = now_dt.hour
                        if 5 <= _gh < 13:
                            _live_block = "Morning"
                        elif 13 <= _gh < 21:
                            _live_block = "Evening"
                        else:
                            _live_block = "Night"
                    _live_block_key = f"{now_dt.year}_{now_dt.timetuple().tm_yday}_{_live_block}"
                    if block_name == _live_block and playback_log.get("sched_block_key") == _live_block_key:
                        _cached = playback_log.get("sched_cached_tracks", [])
                        _by_path = {t["path"]: t for t in raw_tracks}
                        _raw_ordered = [_by_path[p] for p in _cached if p in _by_path]
                        # Freeze durations to what the engine actually committed
                        # for this airing (see retro_tv_emulator._snapshot_track_
                        # durations) -- otherwise a probe correction landing
                        # after the block started could make the guide show a
                        # different split/timing than what's actually airing.
                        _cached_durs = playback_log.get("sched_cached_durations", {})
                        if _cached_durs:
                            _raw_ordered = [
                                {**t, "duration": _cached_durs[t["path"]]}
                                if t["path"] in _cached_durs and _cached_durs[t["path"]] > 0
                                else t
                                for t in _raw_ordered
                            ]
                        if _raw_ordered:
                            # The engine already advanced show_pos through the current
                            # cycle; the persisted show_pos IS the starting point for
                            # cycle N+1 — hand it back so the while loop can build
                            # the next cycle correctly without re-calling build_show_rotation.
                            return (media.pair_short_episodes(_raw_ordered, threshold_seconds=_pair_thresh_sec),
                                    dict(playback_log.get("show_pos", {})))

                # Same day-to-day POSITION template the scheduler persists
                # (see playback_log["show_order"] in calculate_slotted_
                # playback_state) — peeked, not written, so previewing a
                # future block can never accidentally establish/overwrite the
                # real template before it actually airs.
                _fixed_order = playback_log.get("show_order", {}).get(block_name)
                _block_dur_sec = {
                    "Morning": 8 * 3600,
                    "Evening": 8 * 3600,
                    "Night":   8 * 3600,
                    "Full Day": 86400,
                }.get(block_name, 8 * 3600)
                video_tracks, _show_pos_copy, _ = media.build_show_rotation(
                    raw_tracks, sched_mode, _show_pos_copy, day_seed,
                    pair_threshold_seconds=_pair_thresh_sec,
                    episode_order_mode=ch_info.get("episode_order_mode", "sequential"),
                    fixed_show_order=_fixed_order,
                    block_duration_seconds=_block_dur_sec
                )
                return video_tracks, _show_pos_copy

            def get_block_timeline(block_name, _override_sp=None):
                """The REAL per-second timeline for a block — shows AND
                commercial breaks, each with their exact start/stop time.

                SINGLE SOURCE OF TRUTH: calls the exact same
                media.build_block_timeline() the engine calls in
                calculate_slotted_playback_state, fed the same commercial
                pool/placement/day_seed the engine would use, so a show's
                displayed start/stop time here can never diverge from when
                it actually starts/stops on air — no more approximating that
                every show snaps cleanly to the next :30/:00 mark.

                _override_sp: forwarded to get_block_content for cycle advancement
                (see get_block_content's docstring).

                Returns (timeline, total_block_content, advanced_show_pos);
                timeline is [] if the block has no content.
                """
                block_content, _new_sp = get_block_content(block_name, _override_sp)
                if not block_content:
                    return [], 0, _new_sp
                block_offset = {"Morning": 100, "Evening": 200, "Night": 300, "Full Day": 500}.get(block_name, 400)
                day_seed = now_dt.year * 1000 + now_dt.timetuple().tm_yday + int(ch_str) + block_offset
                commercial_placement = ch_info.get("commercial_placement", "interrupt_half_hour")
                _pair_thresh_min = ch_info.get("pair_threshold_minutes", 15)
                interrupt_floor_seconds = (_pair_thresh_min if _pair_thresh_min > 0 else 15) * 60
                _tl, _tc = media.build_block_timeline(
                    block_content, _build_commercial_tracks(), commercial_placement, day_seed,
                    interrupt_floor_seconds=interrupt_floor_seconds
                )
                return _tl, _tc, _new_sp

            def get_marathon_content():
                """Mirror the scheduler's marathon playlist: the ENTIRE Marathon
                folder as one fixed, fully-ordered list (folder-by-folder, natural
                episode order within each), so the guide's continuous 24-hour
                lineup matches what actually airs nonstop across day resets.

                SINGLE SOURCE OF TRUTH: calls the exact same media.build_ordered_tracks()
                the scheduler uses for Marathon (see calculate_slotted_playback_state's
                Marathon branch in retro_tv_emulator.py) — no separate reimplementation
                to drift out of sync with."""
                folder_items = ch_info.get("schedules", {}).get("Marathon", [])
                ordered = media.build_ordered_tracks(folder_items, "natural", 0)
                if not ordered:
                    return []
                _pt = max(0, ch_info.get("pair_threshold_minutes", 15)) * 60
                return media.pair_short_episodes(ordered, threshold_seconds=_pt)

# ==============================================================================
# PART 5 OF 15: VIEWPORT SHOW SEGMENT BUILDING
# ==============================================================================

            _is_marathon = (not ch_info.get("is_visualizer", False)
                            and ch_info.get("scheduling_mode", "random_slots") == "marathon")

            # BUG FIX: the guide never checked holiday_schedules, so during a
            # holiday window (see calculate_slotted_playback_state's "1b.
            # HOLIDAY DATE OVERRIDE CHECK") actual playback switches a channel
            # over to its holiday playlist while the guide kept showing that
            # channel's normal-day lineup -- a total title/content mismatch
            # for as long as the holiday window lasts. This mirrors the
            # engine's _get_holiday_key + holiday_schedules lookup exactly so
            # the guide always reflects what's actually airing, holiday or not.
            def _get_holiday_key_guide(d):
                m, day = d.month, d.day
                if m == 10 and 25 <= day <= 31:
                    return "halloween"
                if m == 12 and 20 <= day <= 26:
                    return "christmas"
                if m == 2 and day == 14:
                    return "valentine"
                if (m == 12 and day == 31) or (m == 1 and day == 1):
                    return "new_year"
                if m == 11:
                    thursdays = [
                        datetime.date(d.year, 11, x)
                        for x in range(1, 31)
                        if datetime.date(d.year, 11, x).weekday() == 3
                    ]
                    if len(thursdays) >= 4 and d == thursdays[3]:
                        return "thanksgiving"
                return None

            _holiday_key_guide = (None if ch_info.get("is_visualizer", False)
                                   else _get_holiday_key_guide(now_dt.date()))
            _holiday_entries_guide = (
                ch_info.get("holiday_schedules", {}).get(_holiday_key_guide, [])
                if _holiday_key_guide else []
            )
            _is_holiday_guide = bool(_holiday_entries_guide)

            viewport_start_hour = viewport_adjusted_start // 3600
            viewport_end_hour = (viewport_adjusted_end - 1) // 3600
            
            # ── TIMELINE CACHE fast path ───────────────────────────────────────────
            # Key encodes channel + calendar day + viewport window. The viewport
            # advances every 30 min automatically, so the cache self-invalidates
            # on both user scroll and the natural half-hour tick — no explicit TTL
            # needed. Anchor reconciliation (Part 5a3) always runs fresh after a
            # hit so the live channel's currently-playing slot stays correct.
            # guide_refresh_token is incremented by the main engine whenever files
            # are added or channel active-state changes, so the timeline cache
            # invalidates immediately rather than waiting for a viewport shift.
            _tl_key = (ch_str, now_dt.year, now_dt.timetuple().tm_yday, viewport_start_seconds,
                       app_state.get("guide_refresh_token", 0))
            _tl_hit = _guide_timeline_cache.get(_tl_key)
            timeline_entries = _tl_hit if _tl_hit is not None else []
            # Tracks where each block's last show actually ends (in abs seconds),
            # so the following block can start its content after any overflow.
            _block_overflow_end = {}
            _next_block_map = {"Morning": "Evening", "Evening": "Night", "Night": "Morning", "Full Day": "Full Day"}

            if _tl_hit is None and _is_holiday_guide:
                # HOLIDAY: mirrors calculate_slotted_playback_state's holiday
                # branch exactly -- entries play back-to-back in their
                # original (unshuffled) order, looping every time the total
                # playlist duration elapses since midnight (current_seconds %
                # total), with no commercial insertion. Lay that same looping
                # sequence across the guide's viewport window.
                _h_tracks = []
                _media_probe_cache_h = (db.config.get("media_probe_cache", {}) if db is not None else {})
                for _asset in _holiday_entries_guide:
                    _p = _asset.get("path", _asset.get("file", "")) if isinstance(_asset, dict) else str(_asset)
                    if _p and _guide_path_exists(_p, app_state):
                        _stored = _asset.get("duration", 0) if isinstance(_asset, dict) else 0
                        _d = media.resolve_track_duration(_media_probe_cache_h, _p, _stored)
                        _h_tracks.append({"path": _p, "duration": _d})
                _h_total = sum(t["duration"] for t in _h_tracks)
                if _h_total > 0:
                    _win_lo = viewport_adjusted_start
                    _win_hi = viewport_adjusted_end + 3600
                    _cycle_start = (_win_lo // _h_total) * _h_total
                    _guard = 0
                    while _cycle_start < _win_hi and _guard < 10000:
                        _pos = 0
                        for _t in _h_tracks:
                            _s_start = _cycle_start + _pos
                            _s_end = _s_start + _t["duration"]
                            if _s_end > _win_lo and _s_start < _win_hi:
                                timeline_entries.append((
                                    {"path": _t["path"], "duration": _t["duration"], "is_commercial": False},
                                    _s_start, _s_end, "Holiday"))
                            _pos += _t["duration"]
                            _guard += 1
                        _cycle_start += _h_total
            elif _tl_hit is None and _is_marathon:
                # MARATHON: one continuous playlist on an epoch clock (mirrors
                # calculate_slotted_playback_state, which also runs Marathon's
                # video_tracks through the same commercial-timeline builder).
                # Map the guide viewport window onto that epoch, then lay the
                # looping REAL timeline (shows + actual commercial breaks,
                # exact durations — no half-hour snap guess) across it so the
                # lineup flows unbroken across day boundaries.
                marathon_content = get_marathon_content()
                commercial_placement = ch_info.get("commercial_placement", "interrupt_half_hour")
                _pair_thresh_min = ch_info.get("pair_threshold_minutes", 15)
                _interrupt_floor = (_pair_thresh_min if _pair_thresh_min > 0 else 15) * 60
                m_timeline, m_total = media.build_block_timeline(
                    marathon_content, _build_commercial_tracks(), commercial_placement,
                    day_seed=0, interrupt_floor_seconds=_interrupt_floor
                ) if marathon_content else ([], 0)
                if m_total > 0:
                    # CONTINUOUS epoch of the first visible column. Unlike the
                    # column LABELS (which fold mod 48), the content clock must NOT
                    # wrap at midnight — otherwise navigating past 12:00 AM would
                    # jump back to today's early hours and show the wrong episodes.
                    # user_shift is applied unfolded so the stream flows unbroken
                    # into the next day.
                    view_start_epoch = (now_dt.toordinal() * 86400
                                        + (system_live_c_idx + user_shift) * 1800)
                    # Same anchor the scheduler uses: episode 1 begins at the
                    # half-hour mark when marathon was enabled.
                    anchor = ch_info.get("marathon_anchor_epoch")
                    if not anchor:
                        _cs0 = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second
                        anchor = now_dt.toordinal() * 86400 + (_cs0 // 1800) * 1800
                    win_lo = view_start_epoch
                    win_hi = view_start_epoch + viewport_seconds + 3600   # visible columns + margin
                    # Offset into the looping REAL timeline at the window start, then
                    # step back to the top of the loop so we lay whole entries.
                    off = (win_lo - anchor) % m_total
                    cycle_start_epoch = win_lo - off
                    _guard = 0
                    while cycle_start_epoch < win_hi and _guard < 10000:
                        for entry in m_timeline:
                            sd = entry["duration"]
                            s_start = cycle_start_epoch + entry["start_time"]
                            s_end = s_start + sd
                            if s_end > win_lo and s_start < win_hi:
                                # Renderer space: offset from the first column plus
                                # viewport_adjusted_start (what clipping expects).
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
              # ── DETERMINISTIC OVERFLOW PRECOMPUTE ────────────────────────────
              # Chain every block's overflow into the next BEFORE any hour gets
              # scanned below, so _block_overflow_end is a pure function of the
              # day's schedule -- never of the viewport/scroll position.
              #
              # Previously this dict was only populated as a SIDE EFFECT of the
              # hour-loop happening to touch the predecessor block, which only
              # happened via the "-1 hour" back-scan landing on it (i.e. only
              # when the viewport sat right at a block boundary). Scroll deep
              # into the middle of a block -- away from its start -- and the
              # predecessor was never scanned, so this block silently
              # re-anchored to its nominal start (block_abs_start) instead of
              # the true overflow-shifted start, and every show in it visibly
              # jumped by the overflow amount the instant you scrolled past
              # the point where the predecessor fell out of range.
              #
              # Walking the whole day's chain up front — Night(0-5) ->
              # Morning(5-13) -> Evening(13-21) -> Night(21-29) — fixes that:
              # each block's start is resolved from its predecessor's real
              # overflow regardless of what's currently on screen, so the
              # lineup is identical no matter where you scroll to.
              _NIGHT_SECS_BEFORE_MIDNIGHT = 86400 - 21 * 3600  # 10800

              def _walk_block_overflow(block_name, block_abs_start, block_abs_end):
                  """Simulate `block_name`'s looping timeline from its (possibly
                  overflow-shifted) start up to block_abs_end and report where
                  the entry straddling the boundary actually ends — WITHOUT
                  writing anything to timeline_entries. Mirrors the exact
                  cycle-stepping the real render loop below uses, so the two
                  can never disagree with each other."""
                  _tl, _total, _ = get_block_timeline(block_name)
                  if not _tl or _total <= 0:
                      return None
                  if block_name == "Night" and block_abs_start == 0:
                      abs_pos = -_NIGHT_SECS_BEFORE_MIDNIGHT
                  else:
                      abs_pos = max(block_abs_start,
                                     _block_overflow_end.get(block_name, block_abs_start))
                  _guard = 0
                  while abs_pos < block_abs_end and _guard < 10000:
                      cycle_start = abs_pos
                      for entry in _tl:
                          e_start = cycle_start + entry["start_time"]
                          e_end = e_start + entry["duration"]
                          if e_start >= block_abs_end:
                              return None
                          if e_end > block_abs_end:
                              return e_end
                          _guard += 1
                      abs_pos = cycle_start + _total
                  return None

              if not _is_full_day_guide:
                  # (block_name, block_abs_start, block_abs_end, next_block)
                  for _bname, _bstart, _bend, _nb in (
                      ("Night",   0,           5 * 3600,            "Morning"),
                      ("Morning", 5 * 3600,    13 * 3600,           "Evening"),
                      ("Evening", 13 * 3600,   21 * 3600,           "Night"),
                      ("Night",   21 * 3600,   24 * 3600 + 5 * 3600, "Morning"),
                  ):
                      _overflow_end = _walk_block_overflow(_bname, _bstart, _bend)
                      if _overflow_end is not None:
                          _block_overflow_end[_nb] = max(
                              _block_overflow_end.get(_nb, 0), _overflow_end)

              # Always include the hour immediately before the viewport too.
              # Block boundaries (Morning/Evening/Night) fall on fixed hours
              # (5, 13, 21); if the viewport's left edge happens to land
              # exactly ON one of those boundaries (e.g. cursor parked right
              # at 09:00 PM), viewport_start_hour's block is the NEW block
              # only — the block that just ended is never touched by this
              # loop at all, so any show of its that overflowed past the
              # boundary never gets computed and its trailing minutes render
              # as plain OFF-AIR instead of the still-airing show. Scanning
              # one hour earlier guarantees the previous block gets triggered
              # at least once; once triggered it always rebuilds its FULL
              # timeline from its own true start, so this is safe (no
              # duplicate entries — block_already_rendered still guards that)
              # and correct regardless of how long the overflow runs. The
              # overflow precompute above means this back-scan is now purely
              # a rendering nicety (making sure the overflowing show's cell
              # itself gets drawn) rather than the only source of correct
              # timing.
              for hour in range(viewport_start_hour - 1, viewport_end_hour + 1):
                hour_mod = hour % 24
                block_name, _ = get_block_for_hour(hour_mod)
                
                if block_name == "Morning":
                    block_abs_start = 5 * 3600
                    block_abs_end = 13 * 3600
                elif block_name == "Evening":
                    block_abs_start = 13 * 3600
                    block_abs_end = 21 * 3600
                elif block_name == "Full Day":
                    block_abs_start = 0
                    block_abs_end = 24 * 3600
                else:  
                    if hour_mod >= 21:
                        block_abs_start = 21 * 3600
                        block_abs_end = 24 * 3600 + 5 * 3600  
                    else:  
                        block_abs_start = 0
                        block_abs_end = 5 * 3600
                
                # Changed from name-only check to TIME-RANGE OVERLAP check.
                # The Night block legitimately needs two rendering passes:
                # one for the 21h-24h portion (block_abs_start=75600) and
                # one for the 0h-5h portion (block_abs_start=0).  Those two
                # ranges do NOT overlap, so the overlap check correctly allows
                # both; the old name-only check blocked the second pass and
                # left midnight→5 AM entirely off-air.
                block_already_rendered = any(
                    e[3] == block_name and e[0] is not None
                    and e[2] > block_abs_start and e[1] < block_abs_end
                    for e in timeline_entries
                )
                if block_already_rendered:
                    continue

                # REAL per-second timeline for this block — shows AND commercial
                # breaks with their exact, unsnapped durations (see Task 6 /
                # get_block_timeline's docstring). Replaces the old approach of
                # summing raw show durations and blindly rounding each one up
                # to the next :30/:00 mark whenever commercials were on.
                block_timeline, total_content, _guide_cycle_sp = get_block_timeline(block_name)
                if not block_timeline or total_content <= 0:
                    continue
                
                # If the previous block's last entry overflowed into this block,
                # start placing content from after that overflow ends.
                #
                # NIGHT BLOCK SPECIAL CASE (early-morning pass, block_abs_start=0):
                # The Night block starts at 21:00 (75600s).  By midnight (0h of
                # the NEXT calendar day) the Night timeline is already 10800s in.
                # Starting abs_pos at 0 would restart the playlist from show 1
                # (the 9 PM show) misplaced at midnight.  Instead seed abs_pos at
                # −10800 so cycle_start = −10800 and
                #   entry_abs_start = cycle_start + entry["start_time"]
                # maps the 10800s-mark entry to abs_time 0 (midnight),
                # the 12600s-mark entry to abs_time 1800 (12:30 AM), etc.
                # Pre-midnight entries land at negative abs_times and are silently
                # skipped by Part 6's  "show_abs_end <= viewport_start"  guard.
                if block_name == "Night" and block_abs_start == 0:
                    _night_secs_before_midnight = 86400 - 21 * 3600  # 10800
                    abs_pos = -_night_secs_before_midnight
                else:
                    abs_pos = max(block_abs_start,
                                  _block_overflow_end.get(block_name, block_abs_start))
                
                while abs_pos < block_abs_end and abs_pos < viewport_adjusted_end + 3600:
                    loop_start_pos = abs_pos  # FREEZE GUARD: track progress each pass
                    cycle_start = abs_pos
                    hit_block_boundary = False
                    for entry in block_timeline:
                        entry_abs_start = cycle_start + entry["start_time"]
                        entry_abs_end = entry_abs_start + entry["duration"]

                        # Once an entry would START at/after the block boundary, this
                        # block is done — that slot belongs to the NEXT block (or to
                        # off-air, if the next block has nothing scheduled). Without
                        # this check the loop only stopped at the viewport's right
                        # edge, so it kept dumping further Evening/Morning/Night
                        # entries past their block's real end — the more of the guide
                        # you scrolled into view, the more "extra" content it added,
                        # which is why the schedule looked different every time you
                        # scrolled and why an unscheduled next block never showed
                        # off-air.
                        if entry_abs_start >= block_abs_end:
                            hit_block_boundary = True
                            break

                        # If this entry overflows past the block boundary, let it run
                        # to completion and tell the next block to start after it ends.
                        if entry_abs_end > block_abs_end:
                            _nb = _next_block_map.get(block_name)
                            if _nb is not None:
                                _block_overflow_end[_nb] = max(
                                    _block_overflow_end.get(_nb, 0), entry_abs_end)

                        timeline_entries.append((entry, entry_abs_start, entry_abs_end, block_name))

                        if entry_abs_end >= viewport_adjusted_end + 3600:
                            abs_pos = entry_abs_end
                            break

                        if entry_abs_end > block_abs_end:
                            # This entry already carried us past the block boundary
                            # (and isn't the last entry in block_timeline) — nothing
                            # after it belongs to this block either, so stop here
                            # rather than looping into the next cycle.
                            hit_block_boundary = True
                            abs_pos = entry_abs_end
                            break
                    else:
                        # Full cycle completed without hitting the block boundary or
                        # viewport edge — advance show_pos and rebuild the timeline
                        # with the NEXT set of episodes so the guide shows different
                        # movies/episodes each cycle instead of replaying the same
                        # rotation over and over (the old bug: all cycles reused the
                        # same block_timeline picked at the top of the outer for-loop).
                        abs_pos = cycle_start + total_content
                        if abs_pos < block_abs_end:
                            _next_tl, _next_tc, _guide_cycle_sp = get_block_timeline(
                                block_name, _override_sp=_guide_cycle_sp)
                            if _next_tl and _next_tc > 0:
                                block_timeline = _next_tl
                                total_content  = _next_tc

                    if hit_block_boundary:
                        break
                    
                    # FREEZE GUARD: if a full pass made no forward progress, bail out
                    # instead of spinning forever.
                    if abs_pos <= loop_start_pos:
                        break
                    
                    if abs_pos < block_abs_end:
                        continue
                    else:
                        break

              # ── MIDNIGHT CONTINUATION for 24-HOUR (Full Day) channels ──────────
              # The hour loop above lays a Full Day channel's looping day-long
              # schedule across [0, 86400) and then stops at midnight. A 24-hour
              # block has block_abs_end == 86400, and once that first pass has
              # filled the day the block_already_rendered overlap check skips
              # every later hour, so NOTHING is ever laid past 86400s. When the
              # viewport straddles midnight (its right edge lands in the next
              # day — e.g. the cursor parked around 11:00–11:30 PM) those
              # post-midnight columns had no entry covering them and rendered as
              # OFF-AIR, right up until you scrolled far enough that time_idx
              # (folded % 48 up top) wrapped and the window sat wholly inside a
              # single day again. That's exactly the "off-air toward midnight
              # that disappears if I scroll far enough" glitch. (The 8-hour Night
              # block never shows this: its 9 PM pass already runs to 05:00 =
              # 104400s, so it inherently covers past midnight.)
              #
              # A Full Day channel repeats the SAME lineup every day, so the
              # correct content for the post-midnight columns is simply the day
              # we just built, replayed starting at the moment the day's content
              # actually finishes — so the schedule no longer changes as you
              # scroll across midnight.
              #
              # NOTE: the hour-loop above already lets a show that crosses
              # midnight run to its real, unclipped end (same overflow logic
              # used for the 8-hour Morning/Evening/Night blocks — see "If
              # this entry overflows past the block boundary, let it run to
              # completion" a bit further up), and it already records that
              # real end time in _block_overflow_end["Full Day"] (Full Day
              # maps to itself in _next_block_map). This section used to
              # THEN clip that already-correct entry back down to exactly
              # 86400 and always shift day-2 by a flat +86400 regardless —
              # which re-cut the overflowing show a second time (the "cuts
              # off the rest of the show at midnight" bug) and, whenever the
              # clip left a sliver of a few seconds, produced a near-zero-width
              # leftover cell at the seam that was too thin to select (the
              # "blank gaps I can't highlight" bug). Fixed by NOT re-clipping
              # anything here, and starting day-2 at the real overflow end
              # (falling back to a flat 86400 when nothing overflowed) —
              # matching the same "let it overflow, next cycle schedules
              # around it" rule used for the 8-hour blocks.
              if _tl_hit is None and _is_full_day_guide and viewport_adjusted_end > 86400:
                  _DAY_SPAN = 86400
                  _day2_start = _block_overflow_end.get("Full Day", _DAY_SPAN)
                  _shifted = []
                  for _e, _es, _ee, _eb in timeline_entries:
                      if _e is None:
                          continue
                      _ns, _ne = _es + _day2_start, _ee + _day2_start
                      # Only keep the shifted slice the viewport actually needs.
                      if _ne > viewport_adjusted_start and _ns < viewport_adjusted_end + 3600:
                          _shifted.append((_e, _ns, _ne, _eb))
                  timeline_entries = timeline_entries + _shifted

# ==============================================================================
# PART 5a2 OF 15: MERGE COMMERCIAL BREAKS INTO THEIR SHOW'S SLOT
# ==============================================================================
            # Merge only runs on a cache miss (_tl_hit is None). A cache hit
            # means timeline_entries already holds the fully-merged result from
            # a prior rebuild this viewport/day window, so skip straight to
            # anchor reconciliation (Part 5a3) which always runs fresh.
            if _tl_hit is None:
                # build_block_timeline (the real per-second engine timeline) splits
                # a long show into multiple segments — same file, later segments
                # carrying a seek_offset — with commercial-break entries dropped in
                # between and after. That's correct for actual playback, but a real
                # TV guide doesn't list each commercial as its own row entry: one
                # program gets one slot, and airing commercials just makes that
                # slot run longer. Collapse each run of "same show's segments +
                # any commercials in/around them" into a single display entry
                # before anything downstream (column ownership, cell drawing)
                # treats a raw segment as its own selectable/visible slot.
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
                timeline_entries = _merged_entries
                # Store the merged result in the per-channel timeline cache.
                # Cap at 80 entries (~8 channels × several viewport positions ×
                # 1 day). A full clear when the limit is hit is safe — the next
                # rebuild repopulates only the currently-visible rows.
                if len(_guide_timeline_cache) > 80:
                    _guide_timeline_cache.clear()
                _guide_timeline_cache[_tl_key] = timeline_entries

# ==============================================================================
# PART 5a3 OF 15: LIVE PLAYBACK ANCHOR RECONCILIATION
# ==============================================================================
            # ROOT CAUSE OF "guide says X but Y is actually playing": the real
            # engine (calculate_slotted_playback_state in retro_tv_emulator.py)
            # treats a channel's playback_anchor as ground truth for "what's on
            # right now" whenever it's still within its remaining duration --
            # that's what keeps a file airing untouched across a mode change, a
            # library rescan, etc. Everything above this point (timeline_entries)
            # is pure schedule math and never looks at that anchor, so a channel
            # currently honoring one can show a completely different title than
            # what's actually on screen. This is worst on the default boot
            # channel (05): it's been anchored continuously since the app
            # launched, so it's the row most likely to have drifted from the
            # schedule's idea of "what's airing" by the time anyone checks the
            # guide -- but any channel with a live anchor can show this same
            # mismatch, not just 05.
            #
            # Fix: reconcile the ONE timeline entry that covers "now" with the
            # anchor, using the exact same elapsed/remaining math the engine
            # uses, so the guide always names the real file and real start/end
            # for whatever is genuinely airing. This only reproduces the
            # engine's PRIMARY anchor branch (anchor still within its stored
            # duration) -- the engine's secondary "still verifiably playing
            # past its estimated duration" fallback requires live VLC state the
            # guide has no access to, so a live show that's overrunning its
            # estimate will re-sync with the schedule once that estimate
            # expires, same as before this fix.
            _is_live_ch_row = (str(app_state.get("current_channel", "")).zfill(2) == ch_str)
            _anchor = {} if (ch_info.get("is_visualizer", False) or not _is_live_ch_row) else ch_info.get("playback_anchor", {})
            _anchor_file = _anchor.get("file") if isinstance(_anchor, dict) else None
            if _anchor_file and os.path.exists(str(_anchor_file)):
                _now_secs = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second
                _wall_start = _anchor.get("wall_start", 0)
                _seek_off = _anchor.get("seek_offset", 0)
                _a_dur = _anchor.get("duration", 1800)
                # DEFENSIVE DURATION CHECK: the anchor's stored duration is
                # frozen at whatever was known when the file started playing.
                # If the background prober has since learned the REAL
                # duration (e.g. a movie that started before it was probed),
                # prefer that -- otherwise a stale 1800s placeholder makes
                # the guide think a 2-hour film ended after 30 minutes and
                # reverts to showing the wrong (schedule-computed) title for
                # the rest of the movie's actual runtime. See
                # _patch_probed_gain in retro_tv_emulator.py, which patches
                # the anchor itself the instant a probe completes; this is
                # a second line of defense for anchors that predate that fix
                # or were set by any path that doesn't go through it.
                if db is not None:
                    _probed_real_dur = (db.config.get("media_probe_cache", {})
                                         .get(os.path.normpath(str(_anchor_file)), {})
                                         .get("duration"))
                    if _probed_real_dur and _probed_real_dur > _a_dur:
                        _a_dur = _probed_real_dur
                _elapsed = _now_secs - _wall_start
                if _elapsed < 0:
                    _elapsed += 86400  # midnight rollover, same as the engine
                # slot_start_seek: the file offset where the CURRENT SLOT begins.
                # For ordinary (non-split) episodes this is 0.  For the second
                # half of a split show it equals the halfway point in the file
                # (e.g. 1800 for a 60-minute episode split at 30 min).
                # seek_offset: where VLC actually started reading in the file —
                # equals slot_start_seek when the viewer tunes in at the slot
                # boundary, or slot_start_seek + X when they tune in X seconds
                # late.  The viewer's position *within* the slot is the diff.
                #
                # BUG (old code): used _seek_off (= slot_start_seek) directly in
                # the remaining formula.  For a split-show second half both values
                # are 1800, so remaining = duration − 1800 − elapsed → always < 0,
                # making anchor reconciliation a complete no-op for those slots.
                # The same error made _a_start_abs point 1800 s before the segment
                # actually started, so even if the guard ever passed the start/end
                # times shown in the guide were wrong.
                _slot_start_seek_a = _anchor.get("slot_start_seek", 0)
                _seek_into_slot_a  = _seek_off - _slot_start_seek_a   # viewer position within slot
                if (_a_dur - _seek_into_slot_a - _elapsed) > 0:
                    # Wall-clock start of the CURRENT SEGMENT (not necessarily the
                    # merged show's first segment for split shows).
                    _a_start_abs_seg = _wall_start - _seek_into_slot_a
                    # End of the current (last) segment = end of the full show slot.
                    _a_end_abs = _a_start_abs_seg + _a_dur
                    # Normalize into the same neighborhood as "now" so it lines
                    # up with this row's other (possibly midnight-spanning) abs
                    # times instead of accidentally landing a day off.
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
                        # --- Pin the currently-airing slot to the anchor's REAL
                        #     window. Even when the schedule guessed the right
                        #     title, its start/end here can be wrong because
                        #     upstream durations drifted; the anchor is ground
                        #     truth for both what's on screen AND when it started,
                        #     so always stamp the real start/end (not just when the
                        #     title mismatched, as the old code did).
                        _now_is_wrong_title = (
                            os.path.normpath(str(_e.get("path", "")))
                            != os.path.normpath(str(_anchor_file)))
                        if _now_is_wrong_title:
                            _now_entry = {
                                "path": _anchor_file, "duration": _a_dur,
                                "is_commercial": (_anchor.get("mode") == "Commercial"),
                            }
                            _a_start_abs = _a_start_abs_seg
                        else:
                            # Keep the schedule's richer entry dict (title/metadata)
                            # but correct its length to the anchor's real duration.
                            _now_entry = dict(_e)
                            _now_entry["duration"] = _a_dur
                            # For split shows the merged entry's existing start (_es)
                            # is the FIRST segment's wall-clock start — earlier than
                            # _a_start_abs_seg which only covers the current (second)
                            # segment.  Preserve the earlier boundary so the guide
                            # cell doesn't shrink to just the remaining half-show.
                            _a_start_abs = min(_es, _a_start_abs_seg)
                        timeline_entries[_idx] = (
                            _now_entry, _a_start_abs, _a_end_abs,
                            _anchor.get("block", _eb))
                        # --- FULL-ROW RE-BASE: every later entry must flow from
                        #     the anchored show's REAL end, not the schedule's
                        #     drifted boundary. Re-chain the tail contiguously,
                        #     preserving each entry's own length (which now reflects
                        #     real probed durations thanks to the engine-side fix),
                        #     so "what's next and when" matches reality. The old
                        #     code only trimmed the single overlapping neighbor, so
                        #     every upcoming title/time past that stayed shifted.
                        _cursor = _a_end_abs
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

# ==============================================================================
# PART 5b OF 15: PER-COLUMN OWNERSHIP (fixes double-highlight / unreachable slots)
# ==============================================================================
            # The guide cursor only ever sits on one of 5 fixed half-hour
            # columns, but a column's contents aren't always one clean segment
            # — a show can overflow a few minutes past its block boundary,
            # leaving a short off-air remainder sharing that same column. Two
            # different selection rules used to disagree about who "owned"
            # that column (one said the overflowing show, the other said the
            # off-air remainder), so both lit up at once. And a rule based
            # purely on "who's on exactly at the boundary instant" made a
            # short remainder — one that doesn't itself start ON a boundary —
            # completely unreachable by the cursor.
            #
            # Fix: for the highlighted row only, figure out up front which
            # single segment occupies the MOST time within each column, and
            # use that as the one and only source of truth for which cell
            # highlights when the cursor sits on that column. A show is
            # identified by its unique abs start time; an off-air run by the
            # abs time it begins.
            _column_owner = {}
            if is_highlighted_row:
                _seg_list = []
                _cursor_abs = viewport_adjusted_start
                for _show, _s_start, _s_end, _bn in timeline_entries:
                    if _show is None:
                        continue
                    if _s_end <= viewport_adjusted_start:
                        continue
                    if _s_start >= viewport_adjusted_end:
                        break
                    _c_start = max(viewport_adjusted_start, _s_start)
                    _c_end = min(viewport_adjusted_end, _s_end)
                    if _c_start > _cursor_abs:
                        _seg_list.append((_cursor_abs, _c_start, ("offair", _cursor_abs)))
                    _seg_list.append((_c_start, _c_end, ("show", _s_start)))
                    _cursor_abs = _c_end
                if _cursor_abs < viewport_adjusted_end:
                    _seg_list.append((_cursor_abs, viewport_adjusted_end, ("offair", _cursor_abs)))

                for _c in range(num_cols):
                    _col_lo = viewport_adjusted_start + _c * 1800
                    _col_hi = _col_lo + 1800
                    _best_key, _best_overlap = None, -1
                    for _s_start, _s_end, _key in _seg_list:
                        _ov = min(_s_end, _col_hi) - max(_s_start, _col_lo)
                        if _ov > _best_overlap:
                            _best_overlap, _best_key = _ov, _key
                    _column_owner[_c] = _best_key

# ==============================================================================
# PART 6 OF 15: FIXED GRID RENDERING & SCISSOR CLIP TEXT FLOWS
# ==============================================================================

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
                
                rounded_start_block_col = int(col_start_float)
                if rounded_start_block_col not in current_row_blocks_list:
                    current_row_blocks_list.append(rounded_start_block_col)
                
                # FIXED MATH POSITIONS: Uses locked width dimensions
                cell_x = col_width + int(col_start_float * col_width)
                pixel_cell_width = int((col_end_float - col_start_float) * col_width)
                pixel_cell_width = max(pixel_cell_width, 2)
                
                # Stop rendering this item entirely if it starts completely past the screen's right boundary
                if cell_x >= w:
                    continue
                
                # Scissor crop grid boxes that extend off-screen
                current_draw_w = min(pixel_cell_width, w - cell_x)
                
                if cell_x > last_drawn_pixel_x:
                    # Render one cell per half-hour column so each 30-min off-air
                    # slot is individually navigable (fixes the one-big-bar bug).
                    _gap_abs_start = last_drawn_abs_time
                    _oa_x = last_drawn_pixel_x
                    while _oa_x < cell_x and _oa_x < w:
                        _oa_col  = max(0, int(float(_oa_x - col_width) / col_width))
                        _oa_next = col_width + (_oa_col + 1) * col_width
                        _oa_end  = min(cell_x, _oa_next, w)
                        _oa_bw   = _oa_end - _oa_x
                        if _oa_bw <= 0:
                            break
                        if _oa_col not in current_row_blocks_list:
                            current_row_blocks_list.append(_oa_col)
                        # Selected only if BOTH: this is the exact fixed column
                        # the cursor is on right now (_oa_col), AND the
                        # ownership pre-pass gave that column to THIS off-air
                        # run. Checking only the second half (as before) meant
                        # every pixel-column this same run happens to be drawn
                        # across would all match at once, since _gap_abs_start
                        # never changes across this run's draw loop — that's
                        # what lit up several off-air columns together instead
                        # of just the one under the cursor. See PART 5b above
                        # for why a bare column-index match isn't enough
                        # on its own either.
                        _oa_sel = (is_highlighted_row and
                                   _oa_col == app_state.get("guide_col_pos", 0) and
                                   _column_owner.get(_oa_col) == ("offair", _gap_abs_start))
                        _oa_bg  = MINT_GREEN if _oa_sel else UNSELECTED_BLUE
                        _oa_tc  = (0, 0, 0) if _oa_sel else TEXT_WHITE
                        pygame.draw.rect(surface, _oa_bg, (_oa_x, start_y, _oa_bw, row_height))
                        pygame.draw.rect(surface, CYAN_GRID, (_oa_x, start_y, _oa_bw, row_height), 1)
                        if _oa_x + 8 < w and _oa_bw > 30:
                            _lbl_oa = font_row_lg.render("OFF-AIR", True, _oa_tc)
                            surface.blit(_lbl_oa, (_oa_x + 8, start_y + (row_height // 2 - _lbl_oa.get_height() // 2)))
                        _oa_x = _oa_end
                
                is_selected_cell = (is_highlighted_row and
                                     _column_owner.get(app_state.get("guide_col_pos", 0)) == ("show", show_abs_start))
                cell_bg = MINT_GREEN if is_selected_cell else UNSELECTED_BLUE
                cell_txt_col = (0, 0, 0) if is_selected_cell else TEXT_WHITE
                
                pygame.draw.rect(surface, cell_bg, (cell_x, start_y, current_draw_w, row_height))
                pygame.draw.rect(surface, CYAN_GRID, (cell_x, start_y, current_draw_w, row_height), 1)
                
                # Build the display title — paired short episodes show as "Ep1 + Ep2";
                # a real commercial-break entry (from media.build_block_timeline,
                # see Task 6) gets its own fixed label rather than being run
                # through filename cleanup like a show would.
                if isinstance(show, dict) and show.get("is_commercial"):
                    cleaned_title = "COMMERCIAL BREAK"
                elif isinstance(show, dict) and show.get("is_pair"):
                    _p1 = show.get("pair_ep1_path", show.get("path", ""))
                    _p2 = show.get("pair_ep2_path", "")
                    _n1 = _clean_show_title(_p1) if _p1 else ""
                    _n2 = _clean_show_title(_p2) if _p2 else ""
                    # A paired slot is usually two episodes of the SAME show
                    # (that's why they're paired), so once markers are
                    # stripped _n1 and _n2 are normally identical -- only
                    # join them with "+" when they differ (e.g. a same-show
                    # double feature that legitimately has two names, like a
                    # movie + its sequel sharing one slot).
                    cleaned_title = ((_n1 + " + " + _n2) if (_n2 and _n2 != _n1) else _n1).upper()
                else:
                    raw_title = ""
                    if isinstance(show, dict):
                        raw_title = show.get("title", show.get("path", ""))
                    else:
                        raw_title = str(show)
                    cleaned_title = _clean_show_title(raw_title).upper()
                
                # FIXED SCISSOR TEXT ALIGNMENT: Tracks cell_x directly so text cannot float away from its box
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
                            # Per-character width estimate recalibrated for the larger
                            # font (was tuned to the old 14px size) so titles truncate
                            # before they'd actually overflow the cell, not after.
                            max_chars = max(3, max_txt_w // 10)
                            if max_txt_w >= 15:
                                truncated_title = cleaned_title[:max_chars]
                                if len(truncated_title) < len(cleaned_title) and max_chars > 3:
                                    truncated_title = truncated_title[:-1] + "."
                                lbl_cell = font_row_lg.render(truncated_title, True, cell_txt_col)
                                surface.blit(lbl_cell, (cell_x + 8, start_y + (row_height // 2 - lbl_cell.get_height() // 2)))
                
                last_drawn_pixel_x = cell_x + pixel_cell_width
                last_drawn_abs_time = clip_end
                
            viewport_absolute_max_pixel_x = col_width + (num_cols * col_width)
            if last_drawn_pixel_x < viewport_absolute_max_pixel_x and has_rendered_any_video:
                # Trailing off-air: one cell per half-hour column so each slot is
                # individually selectable. The first cell may be narrower when a
                # show overflows a boundary — it fills to the next :00/:30 line.
                _gap_abs_start = last_drawn_abs_time
                _tr_x = last_drawn_pixel_x
                while _tr_x < viewport_absolute_max_pixel_x and _tr_x < w:
                    _tr_col  = max(0, int(float(_tr_x - col_width) / col_width))
                    _tr_next = col_width + (_tr_col + 1) * col_width
                    _tr_end  = min(viewport_absolute_max_pixel_x, _tr_next, w)
                    _tr_bw   = _tr_end - _tr_x
                    if _tr_bw <= 0:
                        break
                    if _tr_col not in current_row_blocks_list:
                        current_row_blocks_list.append(_tr_col)
                    # See the matching comment on the leading off-air fill above —
                    # same per-column + ownership-map check, same fix.
                    _tr_sel = (is_highlighted_row and
                               _tr_col == app_state.get("guide_col_pos", 0) and
                               _column_owner.get(_tr_col) == ("offair", _gap_abs_start))
                    _tr_bg  = MINT_GREEN if _tr_sel else UNSELECTED_BLUE
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
                    is_selected_cell = (is_highlighted_row and fallback_col == app_state.get("guide_col_pos", 0))
                    cell_bg = MINT_GREEN if is_selected_cell else UNSELECTED_BLUE
                    cell_txt_col = (0, 0, 0) if is_selected_cell else TEXT_WHITE
                    cell_x = col_width + (fallback_col * col_width)
                    
                    if cell_x < w:
                        current_cell_w = min(col_width, w - cell_x)
                        pygame.draw.rect(surface, cell_bg, (cell_x, start_y, current_cell_w, row_height))
                        pygame.draw.rect(surface, CYAN_GRID, (cell_x, start_y, current_cell_w, row_height), 1)
                        if cell_x + 15 < w:
                            lbl_cell = font_row_lg.render("OFF-AIR", True, cell_txt_col)
                            surface.blit(lbl_cell, (cell_x + 15, start_y + (row_height // 2 - lbl_cell.get_height() // 2)))
            
            if is_highlighted_row:
                current_row_blocks_list.sort()
                app_state["guide_current_row_blocks"] = current_row_blocks_list
                # start_y is pre-computed per channel; no increment needed here

    # ── Fill scale: stretch the design canvas to exactly match the real ──────
    # window/screen shape. This is UI, not video — on a true 4:3 display we
    # want the guide to use the whole 4:3 area edge-to-edge, not preserve the
    # 16:9 design canvas's shape and letterbox it (that min()-based uniform
    # scale is correct for a movie playing on a 4:3 TV, but was wrong here:
    # it was adding the same top/bottom bars to the guide itself). Scaling X
    # and Y independently means the grid/fonts stretch slightly off-square on
    # non-16:9 screens, but the guide fills the actual screen with no bars,
    # which is what a real 4:3 EPG looks like.
    #
    # When a target_rect is supplied (4:3 Test Mode on a 16:9 machine) the guide
    # fills THAT region instead of the whole window, so it sits inside the
    # pillarboxed 4:3 content area / TV border. The preview-bounds remap below
    # already adds off_x/off_y, so the separately-blitted live preview follows
    # the guide into the region automatically.
    if target_rect is not None:
        disp_w = max(1, int(target_rect.width))
        disp_h = max(1, int(target_rect.height))
        off_x = int(target_rect.x)
        off_y = int(target_rect.y)
    else:
        disp_w = max(1, int(real_w))
        disp_h = max(1, int(real_h))
        off_x = 0
        off_y = 0

    real_surface.blit(pygame.transform.smoothscale(surface, (disp_w, disp_h)), (off_x, off_y))

    # Remap the preview box from canvas space to real-window space so the live
    # VLC/visualizer preview (blitted separately by the main loop) lands exactly
    # on the guide's preview rectangle after scaling. Independent X/Y factors
    # (not a single uniform scale) since the canvas is now stretched to fill,
    # not letterbox-fit.
    x_scale = disp_w / float(w)
    y_scale = disp_h / float(h)
    app_state["guide_preview_bounds"] = (
        int(off_x + px * x_scale),
        int(off_y + py * y_scale),
        max(1, int(pw * x_scale)),
        max(1, int(ph * y_scale)),
    )

# ==============================================================================
# PART 7 OF 15: MAIN SETTINGS OVERLAY LAYOUT SECTIONS & TAB BUTTONS
# ==============================================================================

# GLOBAL CACHE MODULES: Stores our color gradients as permanent images so they never lag your processor
_cached_bg_gradient_surf = None
_cached_ui_gradient_surf = None
_cached_gradient_width = -1

def render_settings_overlay_menu(surface, theme, app_state, db):
    """
    Renders the menu inside a 1036x680 hidden sheet.
    Optimized: Cleared out console prints to stop 60 FPS terminal bottlenecks.
    """
    global _cached_bg_gradient_surf, _cached_ui_gradient_surf, _cached_gradient_width
    real_w, real_h = surface.get_size()
    
    w = 1036
    h = 680
    scale_factor = 1.0  
    
    menu_canvas = pygame.Surface((w, h), pygame.SRCALPHA)
    
    import colorsys
    bg_hue = db.config.get("theme_bg_hue", 220) / 360.0 if db is not None else 220/360.0
    ui_hue = db.config.get("theme_ui_hue", 140) / 360.0 if db is not None else 140/360.0
    r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.9, 1.0)
    NEON_GREEN = (int(r * 255), int(g * 255), int(b * 255))
    r, g, b = colorsys.hsv_to_rgb(bg_hue, 0.6, 0.15)
    MIDNIGHT_NAVY = (int(r * 255), int(g * 255), int(b * 255))
    DARK_BLUE = tuple(min(255, c + 5) for c in MIDNIGHT_NAVY)
    TEXT_WHITE = (255, 255, 255)
    OFF_RED = (220, 53, 69)
    OFF_GREEN = (40, 167, 69)
    
    box_w, box_h = w, h
    box_x, box_y = 0, 0
    
    slider_val = db.config.get("menu_opacity", 50) if db is not None else 50
    menu_opacity = int((slider_val / 100.0) * 255) 
    
    menu_overlay = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
    menu_overlay.fill(MIDNIGHT_NAVY + (menu_opacity,))
    menu_canvas.blit(menu_overlay, (box_x, box_y))
    
    pygame.draw.rect(menu_canvas, NEON_GREEN, (box_x, box_y, box_w, box_h), 3)
    
    f_header = _get_font("Courier New", 44, bold=True)
    lbl_title = f_header.render("MENU", True, (255, 220, 0))
    menu_canvas.blit(lbl_title, (box_x + (box_w // 2) - (lbl_title.get_width() // 2), box_y + 15))
    
    game_ch_on = db.config.get("game_channel_enabled", False) if db is not None else False
    kiosk_on = db.config.get("kiosk_mode_enabled", False) if db is not None else False
    tabs_list = ["System", "Channels", "Games", "Video", "Theme"] if game_ch_on else ["System", "Channels", "Video", "Theme"]
    if kiosk_on:
        # Kiosk Mode locks kids out of re-scheduling/editing channels, so
        # the Channels tab itself disappears from the bar entirely.
        tabs_list = [t for t in tabs_list if t != "Channels"]
    tab_w = 110 if (game_ch_on and not kiosk_on) else 135

    tab_h = 44
    tab_gap = 8 
    
    start_tab_x = box_x + (box_w // 2) - (((tab_w * len(tabs_list)) + (tab_gap * (len(tabs_list) - 1))) // 2)
    tab_y = box_y + 75
    
    global f_tab, f_body
    f_tab = _get_font("Courier New", 17, bold=True)
    f_body = _get_font("Courier New", 17, bold=True)
    
    active_tab = app_state.get("active_menu_tab", "System")
    layer = app_state.get("menu_layer", "TAB_SELECTION")
    cur_sel = app_state.get("menu_selection_index", 0)
    
    for i, tab_name in enumerate(tabs_list):
        tx = start_tab_x + i * (tab_w + tab_gap)
        is_focused_tab = (tab_name == active_tab)
        
        t_bg = DARK_BLUE if is_focused_tab else (25, 45, 80)
        
        tab_surface = pygame.Surface((tab_w, tab_h), pygame.SRCALPHA)
        tab_surface.fill(t_bg + (menu_opacity,))
        menu_canvas.blit(tab_surface, (tx, tab_y))
        
        if is_focused_tab and layer == "TAB_SELECTION":
            pygame.draw.rect(menu_canvas, NEON_GREEN, (tx, tab_y, tab_w, tab_h), 2, border_radius=4)
            
        lbl_t = f_tab.render(tab_name, True, TEXT_WHITE)
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

# ==============================================================================
# PART 9 OF 15: SPECIFIC TABS CONTENT INJECTIONS - SYSTEM OVERLAY PANEL
# ==============================================================================

    # --------------------------------------------------------------------------
    # VIEWPORT PANEL DRAW ENGINE TAB 1: SYSTEM SETTINGS (CANVAS SHEET PASS)
    # --------------------------------------------------------------------------
    if active_tab == "System" and layer not in ("REMOTE_REMAP_LIST", "REMOTE_REMAP_CAPTURE", "NUMBER_REMAP_CAPTURE"):
        # FULL CORES ENGINE LOCK: Positions use native constants inside the hidden sheet layout
        scale_factor = 1.0

        toggle_w = 55
        toggle_h = 24
        toggle_x = cx + 300
        pill_radius = 12
        handle_rad = 9
        focus_thickness = 2

        label_padding_x = 35

        # sys_rows is the single source of truth (see get_system_menu_rows in
        # this module) for which rows exist right now and in what order --
        # Kiosk Mode (and, within Kiosk Mode, whether Boot on Start is also
        # on) collapses this down to just 2-3 rows instead of the full list.
        # "close" is handled separately below since it's pinned to the
        # bottom-right corner rather than flowing with the other rows.
        sys_rows = get_system_menu_rows(db, app_state)
        toggle_rows = [r for r in sys_rows if SYSTEM_ROW_META[r]["kind"] == "toggle"]
        button_rows = [r for r in sys_rows if SYSTEM_ROW_META[r]["kind"] == "button"]

        current_values = {
            "game_ch":           db.config.get("game_channel_enabled", False) if db is not None else False,
            "guide":             db.config.get("tv_guide_enabled", True) if db is not None else True,
            "controls_on_start": db.config.get("show_controls_on_launch", True) if db is not None else True,
            "boot_on_start":     db.config.get("start_on_boot", False) if db is not None else False,
            "kiosk_mode":        db.config.get("kiosk_mode_enabled", False) if db is not None else False,
        }

        # Explanatory blurb shown to the right of each toggle -- 17pt white
        # text, wrapped to fit the remaining panel width so long descriptions
        # (e.g. Boot on Start's) don't run off the edge of the 1036px sheet.
        TOGGLE_HELP_TEXT = {
            "boot_on_start":     "Hides Windows and Boots in Fullscreen (Quit Program to see windows)",
            "kiosk_mode":        "Hides File Explorer Options and Boots in Fullscreen",
            "controls_on_start": "Turns on/off Controls Menu Visible on Boot",
            "game_ch":           "Turns on/off Ability to Channel Surf to Channel 03",
            "guide":             "Turns on/off Ability to Channel Surf to Channel 04",
        }
        f_help = _get_font("Courier New", 17, bold=True)
        help_text_x = toggle_x + toggle_w + 16
        help_text_max_w = (cx + content_w) - help_text_x - 12

        def _wrap_help_text(text, font, max_w):
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

        # --- Toggle rows (top block) ---
        # Evenly distribute whichever toggle rows are visible right now
        # across the space between the top of the panel and wherever the
        # button rows need to start, instead of a fixed 45px gap. This fills
        # the panel better when Kiosk Mode's short 1-2 row layout would
        # otherwise leave a lot of empty space, and gives the longer
        # two-line "Boot on Start" help text comfortable room to breathe
        # instead of a cramped fixed gap. Clamped to a sane max so a
        # 1-row Kiosk layout doesn't stretch to something silly.
        toggle_start_y = 40
        bottom_reserved = (len(button_rows) * 50) + 60
        available_h = max(45, content_h - toggle_start_y - bottom_reserved)
        toggle_gap = min(70, available_h / max(1, len(toggle_rows)))
        for t_idx, row_id in enumerate(toggle_rows):
            row_y_offset = int(toggle_start_y + (t_idx * toggle_gap))
            row_idx = sys_rows.index(row_id)
            is_focused = (layer == "SYSTEM_ROWS" and cur_sel == row_idx)
            row_enabled = current_values[row_id]

            row_label = f_body.render(SYSTEM_ROW_META[row_id]["label"], True, TEXT_WHITE)
            menu_canvas.blit(row_label, (cx + label_padding_x, content_y + row_y_offset))

            toggle_y = content_y + row_y_offset - 3
            sw_bg = OFF_GREEN if row_enabled else (45, 65, 100)
            pygame.draw.rect(menu_canvas, sw_bg, (toggle_x, toggle_y, toggle_w, toggle_h), border_radius=pill_radius)
            if is_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (toggle_x, toggle_y, toggle_w, toggle_h), focus_thickness, border_radius=pill_radius)
            handle_x = toggle_x + toggle_w - 13 if row_enabled else toggle_x + 12
            pygame.draw.circle(menu_canvas, TEXT_WHITE, (handle_x, toggle_y + toggle_h // 2), handle_rad)
            if is_focused:
                pygame.draw.circle(menu_canvas, NEON_GREEN, (handle_x, toggle_y + toggle_h // 2), handle_rad + 2, 2)

            help_text = TOGGLE_HELP_TEXT.get(row_id)
            if help_text and help_text_max_w > 20:
                help_lines = _wrap_help_text(help_text, f_help, help_text_max_w)
                line_h = f_help.get_height()
                # Vertically center the (possibly 2-line) blurb on the toggle pill
                block_h = line_h * len(help_lines)
                help_y = toggle_y + (toggle_h // 2) - (block_h // 2)
                for line in help_lines:
                    lbl_help = f_help.render(line, True, TEXT_WHITE)
                    menu_canvas.blit(lbl_help, (help_text_x, help_y))
                    help_y += line_h

# ==============================================================================
# PART 10 OF 15: SPECIFIC TABS CONTENT INJECTIONS - SYSTEM ACTIONS & MAIN CHANNELS GRID
# ==============================================================================

        # --- Action buttons (LEFT column, below the toggles) ---
        box_radius = 4
        focus_thickness = 2
        label_padding_x = 35

        btn_w, btn_h = 180, 32
        bx_left = cx + label_padding_x
        btn_text_y_offset = (btn_h // 2) - (f_tab.get_height() // 2)

        # Same formula as the toggle block above -- this reproduces the
        # original fixed 220px offset exactly when all 5 toggle rows are
        # showing, and shrinks proportionally for Kiosk Mode's 1-toggle
        # layout so the button block sits right below whatever toggles
        # are actually visible instead of leaving a big empty gap.
        button_start_y = int(toggle_start_y + (len(toggle_rows) * toggle_gap) + 5)

        for b_idx, row_id in enumerate(button_rows):
            by = content_y + button_start_y + (b_idx * 50)
            row_idx = sys_rows.index(row_id)
            is_btn_highlighted = (layer == "SYSTEM_ROWS" and cur_sel == row_idx)
            b_color = SYSTEM_ROW_META[row_id].get("color", OFF_RED)
            btn_txt = SYSTEM_ROW_META[row_id]["label"]

            pygame.draw.rect(menu_canvas, b_color, (bx_left, by, btn_w, btn_h), border_radius=box_radius)
            if is_btn_highlighted:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (bx_left, by, btn_w, btn_h), focus_thickness, border_radius=box_radius)

            lbl_b = f_tab.render(btn_txt, True, TEXT_WHITE)
            menu_canvas.blit(lbl_b, (bx_left + (btn_w // 2) - (lbl_b.get_width() // 2), by + btn_text_y_offset))

        # --- Close (always the last row, pinned to the bottom-right corner) ---
        sys_rows_close_idx = len(sys_rows)
        is_cl_focused = (layer == "SYSTEM_ROWS" and cur_sel == sys_rows_close_idx)
        close_btn_w = 85
        close_btn_h = 32
        close_btn_x = cx + content_w - 110
        close_btn_y = content_y + content_h - 45
        close_txt_y_offset = (close_btn_h // 2) - (f_tab.get_height() // 2)

        pygame.draw.rect(menu_canvas, OFF_RED, (close_btn_x, close_btn_y, close_btn_w, close_btn_h), border_radius=box_radius)
        if is_cl_focused: 
            pygame.draw.rect(menu_canvas, NEON_GREEN, (close_btn_x, close_btn_y, close_btn_w, close_btn_h), focus_thickness, border_radius=box_radius)
        lbl_c = f_tab.render("Close", True, TEXT_WHITE)
        menu_canvas.blit(lbl_c, (close_btn_x + (close_btn_w // 2) - (lbl_c.get_width() // 2), close_btn_y + close_txt_y_offset))


    # --------------------------------------------------------------------------
    # VIEWPORT PANEL DRAW ENGINE TAB 1b: REMOTE + NUMBER REMAPPING (combined)
    # --------------------------------------------------------------------------
    elif active_tab == "System" and layer in ("REMOTE_REMAP_LIST", "REMOTE_REMAP_CAPTURE", "NUMBER_REMAP_CAPTURE"):
        box_radius = 4
        focus_thickness = 2
        label_padding_x = 35

        is_capturing = layer in ("REMOTE_REMAP_CAPTURE", "NUMBER_REMAP_CAPTURE")
        is_number_capture = (layer == "NUMBER_REMAP_CAPTURE")
        capture_idx = app_state.get("remote_remap_index", 0)
        rr_sel = cur_sel  # 0 = Remap Remote, 1 = Remap Numbers, 2 = Close

        # --- Column geometry: Remote list (left), Number list (right) ---
        col1_label_x = cx + label_padding_x
        col1_key_x   = cx + 295
        col2_label_x = cx + 520
        col2_key_x   = cx + content_w - 110

        key_box_w, key_box_h = 90, 26
        row_top = content_y + 44
        row_h = 30

        f_sub_header = _get_font("Courier New", 19, bold=True)
        hdr1 = f_sub_header.render("Remote Remapping", True, (255, 220, 0))
        menu_canvas.blit(hdr1, (col1_label_x, content_y + 14))
        hdr2 = f_sub_header.render("Number Remapping", True, (255, 220, 0))
        menu_canvas.blit(hdr2, (col2_label_x, content_y + 14))

        # Reject flash state — shared by both columns.
        _remap_reject_flash = app_state.get("_remap_reject_flash")
        _remap_reject_active = (
            _remap_reject_flash is not None
            and (pygame.time.get_ticks() - _remap_reject_flash) < 2000
        )

        def _draw_remap_column(action_set, label_x, key_x, col_is_capturing):
            for i, (action_id, action_label) in enumerate(action_set):
                ry = row_top + i * row_h
                row_is_capturing = col_is_capturing and capture_idx == i
                row_is_rejected  = row_is_capturing and _remap_reject_active

                lbl_row = f_body.render(f"{action_label}:", True, TEXT_WHITE)
                menu_canvas.blit(lbl_row, (label_x, ry + 2))

                if row_is_rejected:
                    box_bg     = (120, 20, 20)
                    box_border = (255, 80, 80)
                elif row_is_capturing:
                    box_bg     = (90, 30, 30)
                    box_border = NEON_GREEN
                else:
                    box_bg     = (60, 60, 80)
                    box_border = (120, 120, 140)
                pygame.draw.rect(menu_canvas, box_bg, (key_x, ry, key_box_w, key_box_h), border_radius=box_radius)
                pygame.draw.rect(menu_canvas, box_border, (key_x, ry, key_box_w, key_box_h), 2, border_radius=box_radius)

                if row_is_rejected:
                    key_text = "!DUP"
                    key_color = (255, 100, 100)
                elif row_is_capturing:
                    key_text  = "..."
                    key_color = TEXT_WHITE
                else:
                    key_text  = _remote_key_label(_get_remote_binding(db, action_id))
                    key_color = TEXT_WHITE
                lbl_key = f_body.render(key_text, True, key_color)
                menu_canvas.blit(lbl_key, (key_x + (key_box_w // 2) - (lbl_key.get_width() // 2),
                                            ry + (key_box_h // 2) - (lbl_key.get_height() // 2)))

        _draw_remap_column(REMOTE_REMAP_ACTIONS, col1_label_x, col1_key_x, is_capturing and not is_number_capture)
        _draw_remap_column(NUMBER_REMAP_ACTIONS, col2_label_x, col2_key_x, is_capturing and is_number_capture)

        # --- Bottom row: Remap Remote | Remap Numbers | Close, all aligned ---
        btn_w, btn_h = 180, 32
        close_btn_w, close_btn_h = 85, 32
        btn_y = content_y + content_h - 45
        btn_text_y_offset = (btn_h // 2) - (f_tab.get_height() // 2)

        # Fixed-position prompt line directly above the button row (reserved
        # space whether or not a capture is in progress, so buttons never move).
        prompt_y = btn_y - 26
        if is_capturing:
            active_set = NUMBER_REMAP_ACTIONS if is_number_capture else REMOTE_REMAP_ACTIONS
            cur_action_label = active_set[capture_idx][1]
            f_prompt = _get_font("Courier New", 15, bold=True)
            # Some remotes' OK/Back buttons send a literal hardware Escape,
            # indistinguishable from a real keyboard Escape press -- see the
            # _ESC_CAPTURE_RESOLVE_EVENT / _esc_capture_pending handling in
            # retro_tv_emulator.py. A single ESC-like press now BINDS like any
            # other key (after a short pause to make sure a 2nd press isn't
            # coming); only a quick 2nd ESC-like press cancels. Surface that
            # pending/confirming state here so the screen doesn't just look
            # "frozen" for the ~500ms window -- and so the label doesn't still
            # claim a single press cancels, which was the old (now removed)
            # behavior.
            _esc_pending = app_state.get("_esc_capture_pending")
            _esc_pending_here = (
                _esc_pending is not None
                and _esc_pending.get("capture_layer") == layer
                and _esc_pending.get("remap_idx") == capture_idx
            )
            # Duplicate-binding reject flash — shown for 2s when the user
            # presses a key that's already bound to another action.
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
                    True, (255, 80, 80))
            elif _esc_pending_here:
                prompt_txt = f_prompt.render(
                    "Confirming... press ESC/Back again fast to cancel, or wait to bind it", True, (255, 140, 0))
            else:
                prompt_txt = f_prompt.render(
                    f"Press a key for: {cur_action_label}   "
                    f"(WASD/arrows not allowed - double-tap ESC/Back fast to cancel)", True, (255, 220, 0))
            menu_canvas.blit(prompt_txt, (col1_label_x, prompt_y))

        # Remap Remote button
        bx_remote = col1_label_x
        is_remote_focused = (not is_capturing) and rr_sel == 0
        remote_bg = (45, 80, 130) if layer != "NUMBER_REMAP_CAPTURE" else (35, 60, 95)
        pygame.draw.rect(menu_canvas, remote_bg, (bx_remote, btn_y, btn_w, btn_h), border_radius=box_radius)
        if is_remote_focused or layer == "REMOTE_REMAP_CAPTURE":
            pygame.draw.rect(menu_canvas, NEON_GREEN, (bx_remote, btn_y, btn_w, btn_h), focus_thickness, border_radius=box_radius)
        remote_lbl = f_tab.render("Remapping..." if layer == "REMOTE_REMAP_CAPTURE" else "Remap Remote", True, TEXT_WHITE)
        menu_canvas.blit(remote_lbl, (bx_remote + (btn_w // 2) - (remote_lbl.get_width() // 2), btn_y + btn_text_y_offset))

        # Remap Numbers button
        bx_numbers = col2_label_x
        is_numbers_focused = (not is_capturing) and rr_sel == 1
        numbers_bg = (45, 80, 130) if layer != "REMOTE_REMAP_CAPTURE" else (35, 60, 95)
        pygame.draw.rect(menu_canvas, numbers_bg, (bx_numbers, btn_y, btn_w, btn_h), border_radius=box_radius)
        if is_numbers_focused or layer == "NUMBER_REMAP_CAPTURE":
            pygame.draw.rect(menu_canvas, NEON_GREEN, (bx_numbers, btn_y, btn_w, btn_h), focus_thickness, border_radius=box_radius)
        numbers_lbl = f_tab.render("Remapping..." if layer == "NUMBER_REMAP_CAPTURE" else "Remap Numbers", True, TEXT_WHITE)
        menu_canvas.blit(numbers_lbl, (bx_numbers + (btn_w // 2) - (numbers_lbl.get_width() // 2), btn_y + btn_text_y_offset))

        # Close button (bottom-right)
        is_cl_focused = (not is_capturing) and rr_sel == 2
        close_btn_x = cx + content_w - 110
        close_txt_y_offset = (close_btn_h // 2) - (f_tab.get_height() // 2)

        pygame.draw.rect(menu_canvas, OFF_RED, (close_btn_x, btn_y, close_btn_w, close_btn_h), border_radius=box_radius)
        if is_cl_focused:
            pygame.draw.rect(menu_canvas, NEON_GREEN, (close_btn_x, btn_y, close_btn_w, close_btn_h), focus_thickness, border_radius=box_radius)
        lbl_c2 = f_tab.render("Close", True, TEXT_WHITE)
        menu_canvas.blit(lbl_c2, (close_btn_x + (close_btn_w // 2) - (lbl_c2.get_width() // 2), btn_y + close_txt_y_offset))

    # --------------------------------------------------------------------------
    # VIEWPORT PANEL DRAW ENGINE TAB 2: CHANNELS MATRIX GRID BROWSER
    # --------------------------------------------------------------------------
    elif active_tab == "Channels" and layer in ("CHANNEL_LIST", "TAB_SELECTION"):
        box_radius = 4
        focus_thickness = 2
        
        cols = 5
        cell_w, cell_h = 154, 44  
        x_pad, y_pad = 12, 11
        cell_radius = 6
        dot_radius = 5
        
        grid_start_x = cx + (content_w // 2) - (((cell_w * cols) + (x_pad * (cols - 1))) // 2)
        grid_start_y = content_y + 22
        
        filtered_channels_list = [i for i in range(5, 45)]
        cell_text_y_offset = (cell_h // 2) - (f_body.get_height() // 2)
        cell_text_x_offset = 14
        cell_dot_x_offset = 20
        
        for idx, ch_num in enumerate(filtered_channels_list):
            r_pos = idx // cols
            c_pos = idx % cols
            
            cell_x = grid_start_x + c_pos * (cell_w + x_pad)
            cell_y = grid_start_y + r_pos * (cell_h + y_pad)
            
            is_ch_highlighted = (layer == "CHANNEL_LIST" and cur_sel == idx)
            
            current_lookup_key = str(ch_num).zfill(2)
            if db is not None and current_lookup_key in db.channels_db:
                ch_active = db.channels_db[current_lookup_key].get("active", True)
            else:
                ch_active = False
                
            dot_color = OFF_GREEN if ch_active else OFF_RED
            
            pygame.draw.rect(menu_canvas, (25, 45, 80), (cell_x, cell_y, cell_w, cell_h), border_radius=cell_radius)
            if is_ch_highlighted: 
                pygame.draw.rect(menu_canvas, NEON_GREEN, (cell_x, cell_y, cell_w, cell_h), focus_thickness, border_radius=cell_radius)
                
            lbl_ch_name = f_body.render(f"Channel {str(ch_num).zfill(2)}", True, TEXT_WHITE)
            menu_canvas.blit(lbl_ch_name, (cell_x + cell_text_x_offset, cell_y + cell_text_y_offset))
            
            pygame.draw.circle(menu_canvas, dot_color, (cell_x + cell_w - cell_dot_x_offset, cell_y + (cell_h // 2)), dot_radius)
            
        is_close_focused = (layer == "CHANNEL_LIST" and cur_sel == 99)
        close_btn_w = 85
        close_btn_h = 32
        close_btn_x = cx + content_w - 110
        close_btn_y = content_y + content_h - 45
        close_txt_y_offset = (close_btn_h // 2) - (f_tab.get_height() // 2)

        pygame.draw.rect(menu_canvas, OFF_RED, (close_btn_x, close_btn_y, close_btn_w, close_btn_h), border_radius=box_radius)
        if is_close_focused: 
            pygame.draw.rect(menu_canvas, NEON_GREEN, (close_btn_x, close_btn_y, close_btn_w, close_btn_h), focus_thickness, border_radius=box_radius)
        lbl_cl = f_tab.render("Close", True, TEXT_WHITE)
        menu_canvas.blit(lbl_cl, (close_btn_x + (close_btn_w // 2) - (lbl_cl.get_width() // 2), close_btn_y + close_txt_y_offset))

        # --- 4:3 MODE + BORDER footer toggles (bottom-left) ---
        # Visible counterpart to the input-handler footer rows in
        # retro_tv_emulator.py (cur_sel 40 = "4:3 Mode", cur_sel 41 = "Border").
        # Both are Enter-only toggles rendered as the same boxed-label + sliding
        # pill switch used by Screen Saver / Menu Music / All Consoles: a lit
        # (OFF_GREEN) pill with the knob slid right means ON, a dark pill with
        # the knob left means OFF -- no separate status dot. The "Border" switch
        # only renders while 4:3 Mode is ON, matching the navigation guard (D
        # from 40 only reaches 41 when 4:3 Mode is enabled) so the cursor can
        # never land on a hidden switch.
        #
        # These are a 16:9-only convenience: on a display Windows already
        # reports as 4:3 there's nothing to fake or bezel, so the whole footer
        # row is hidden (and the input handler blocks navigating into it). The
        # gate is the REAL boot-detected ratio (db._detected_aspect_ratio), not
        # the live "aspect_ratio" value -- turning the toggle on sets the live
        # value to 4:3 while detection stays 16:9, so the row correctly stays
        # visible to be turned back off.
        _true_43_display = (getattr(db, "_detected_aspect_ratio", None) == "4:3") if db is not None else False
        if not _true_43_display:
            _test_on = bool(db.config.get("fake_43_test_mode_enabled", False)) if db is not None else False
            _border_on = bool(db.config.get("fake_43_border_enabled", True)) if db is not None else True

            box_h_43 = 28
            footer_y = content_y + content_h - 45   # same baseline as the Close button
            pill_w, pill_h = 55, 24

            def _draw_footer_switch(x, label, is_on, focused):
                """Boxed label + sliding pill switch (matches Screen Saver style).
                Returns the right edge x so the next switch can sit after it."""
                lbl_surf = f_body.render(label, True, TEXT_WHITE)
                lbox_w = lbl_surf.get_width() + 24
                lbox_col = (60, 140, 220) if focused else (40, 100, 180)
                pygame.draw.rect(menu_canvas, lbox_col, (x, footer_y, lbox_w, box_h_43), border_radius=5)
                if focused:
                    pygame.draw.rect(menu_canvas, NEON_GREEN, (x, footer_y, lbox_w, box_h_43), 2, border_radius=5)
                menu_canvas.blit(lbl_surf, (x + (lbox_w - lbl_surf.get_width()) // 2,
                                            footer_y + (box_h_43 - lbl_surf.get_height()) // 2))
                pill_x = x + lbox_w + 10
                pill_y = footer_y + (box_h_43 - pill_h) // 2
                pygame.draw.rect(menu_canvas, OFF_GREEN if is_on else (45, 65, 100),
                                 (pill_x, pill_y, pill_w, pill_h), border_radius=12)
                if focused:
                    pygame.draw.rect(menu_canvas, NEON_GREEN, (pill_x, pill_y, pill_w, pill_h), 2, border_radius=12)
                handle_x = pill_x + pill_w - 12 if is_on else pill_x + 12
                pygame.draw.circle(menu_canvas, TEXT_WHITE, (handle_x, pill_y + pill_h // 2), 9)
                if focused:
                    pygame.draw.circle(menu_canvas, NEON_GREEN, (handle_x, pill_y + pill_h // 2), 11, 2)
                return pill_x + pill_w

            # "4:3 Mode" switch, left-aligned to grid column 0.
            t43_end_x = _draw_footer_switch(grid_start_x, "4:3 Mode",
                                            _test_on, layer == "CHANNEL_LIST" and cur_sel == 40)

            # "Border" switch -- only shown/reachable while 4:3 Mode is ON.
            if _test_on:
                _draw_footer_switch(t43_end_x + 30, "Border",
                                    _border_on, layer == "CHANNEL_LIST" and cur_sel == 41)

# ==============================================================================
# PART 11 OF 15: SPECIFIC TABS CONTENT INJECTIONS - CHANNEL SUB-MENU SCHEDULER
# ==============================================================================

    elif active_tab == "Channels" and layer == "CHANNEL_SUB_MENU":
        scale_factor = 1.0
        
        target_ch_str = str(app_state.get("selected_channel_row", 5)).zfill(2)
        ch_info = db.channels_db.get(target_ch_str, {"active": True, "is_visualizer": False, "name": f"STATION {target_ch_str}", "visualizer_style": "Random"})
        
        f_sub_title = _get_font("Courier New", 20, bold=True)
        lbl_sub = f_sub_title.render(f"Channel {target_ch_str} Scheduling Settings", True, TEXT_WHITE)
        title_x = cx + (content_w // 2) - (lbl_sub.get_width() // 2)
        menu_canvas.blit(lbl_sub, (title_x, content_y + 20))
        
        sub_r = app_state.get("sub_menu_row_index", 0) 
        sub_c = app_state.get("sub_menu_col_index", 0)
        
        top_y = content_y + 55
        
        is_toggle_focused = (sub_r == 0 and sub_c == 0)
        t_active = ch_info.get("active", True)

        # Count active content channels to know if this is the last one
        _active_ch_count = 0
        if db is not None:
            for _n in range(5, 45):
                _cs = str(_n).zfill(2)
                if db.channels_db.get(_cs, {}).get("active", _cs == "05"):
                    _active_ch_count += 1
        _is_last_active = t_active and (_active_ch_count <= 1)

        # When this is the last active channel the Status toggle is locked — cursor
        # should land on the Vis toggle (col 1) instead of Status (col 0).
        # Clamp sub_c so col 0 is unreachable while locked.
        if _is_last_active and sub_c == 0:
            _effective_col = 1   # display-only remap; don't write back to app_state here
        else:
            _effective_col = sub_c

        # is_toggle_focused is False when locked (greyed item gets NO highlight)
        is_toggle_focused = (not _is_last_active) and (sub_r == 0 and _effective_col == 0)

        # Grey out the label and toggle when it's the last active channel
        _toggle_label_color = (90, 90, 90) if _is_last_active else (NEON_GREEN if is_toggle_focused else TEXT_WHITE)
        lbl_toggle_txt = f_body.render("Status:", True, _toggle_label_color)
        menu_canvas.blit(lbl_toggle_txt, (cx + 25, top_y + 4))

        # Greyed out pill when locked; normal colors otherwise
        if _is_last_active:
            sw_bg = (55, 90, 55)   # muted green - on but locked
            sw_handle_col = (130, 130, 130)
            # Draw lock icon (small padlock shape using rect+circle)
            pygame.draw.rect(menu_canvas, (80, 80, 80), (cx + 158, top_y + 4, 10, 8), border_radius=2)
            pygame.draw.circle(menu_canvas, (80, 80, 80), (cx + 163, top_y + 4), 4, 2)
        else:
            sw_bg = OFF_GREEN if t_active else (45, 65, 100)
            sw_handle_col = TEXT_WHITE

        pygame.draw.rect(menu_canvas, sw_bg, (cx + 95, top_y, 55, 24), border_radius=12)
        # Only draw focus rings when NOT locked
        if is_toggle_focused:
            pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 95, top_y, 55, 24), 2, border_radius=12)
        sw_handle_x = cx + 137 if t_active else cx + 107
        pygame.draw.circle(menu_canvas, sw_handle_col, (sw_handle_x, top_y + 12), 9)
        if is_toggle_focused:
            pygame.draw.circle(menu_canvas, NEON_GREEN, (sw_handle_x, top_y + 12), 11, 2)

        # Tooltip when locked — show it while vis toggle is focused (row 0, any col)
        if _is_last_active and sub_r == 0:
            _f_tip = _get_font("Courier New", 11, bold=True)
            _lbl_tip = _f_tip.render("Last active channel — enable another channel first", True, (140, 100, 60))
            menu_canvas.blit(_lbl_tip, (cx + 25, top_y + 28))

        # Only show vis mode toggle and name field when channel is ON
        t_vis = ch_info.get("is_visualizer", False)
        if t_active:
            is_vis_mode_focused = (sub_r == 0 and _effective_col == 1)
            lbl_vis_txt = f_body.render("Mode: MUSIC", True, NEON_GREEN if is_vis_mode_focused else TEXT_WHITE)
            menu_canvas.blit(lbl_vis_txt, (cx + 175, top_y + 4))
            
            v_sw_bg = OFF_GREEN if t_vis else (45, 65, 100)
            pygame.draw.rect(menu_canvas, v_sw_bg, (cx + 285, top_y, 55, 24), border_radius=12)
            if is_vis_mode_focused: pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 285, top_y, 55, 24), 2, border_radius=12)
            v_sw_handle_x = cx + 327 if t_vis else cx + 297
            pygame.draw.circle(menu_canvas, TEXT_WHITE, (v_sw_handle_x, top_y + 12), 9)
            if is_vis_mode_focused: pygame.draw.circle(menu_canvas, NEON_GREEN, (v_sw_handle_x, top_y + 12), 11, 2)

            is_name_focused = (sub_r == 0 and _effective_col == 2)
            is_editing_name = app_state.get("editing_channel_name", False) and is_name_focused
            all_selected    = is_editing_name and app_state.get("channel_name_all_selected", False)
            pygame.draw.rect(menu_canvas, (25, 45, 80), (cx + 365, top_y - 2, 235, 28), border_radius=4)
            if is_name_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 365, top_y - 2, 235, 28), 2, border_radius=4)
            ch_name_str = ch_info.get("name", f"STATION {target_ch_str}").upper()
            if all_selected:
                # Draw selection highlight behind the text
                sel_lbl = f_body.render(f"NAME: {ch_name_str[:12]}", True, (0, 0, 0))
                sel_w = sel_lbl.get_width() + 6
                pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 372, top_y + 1, sel_w, 20), border_radius=2)
                menu_canvas.blit(sel_lbl, (cx + 375, top_y + 4))
            else:
                # Normal text, plus a blinking cursor while editing
                display_str = f"NAME: {ch_name_str[:12]}"
                if is_editing_name and (pygame.time.get_ticks() // 500) % 2 == 0:
                    display_str += "_"
                lbl_name_txt = f_body.render(display_str, True, NEON_GREEN if is_name_focused else TEXT_WHITE)
                menu_canvas.blit(lbl_name_txt, (cx + 375, top_y + 4))

        cell_w, cell_h = 340, 48
        grid_y_start = content_y + 115
        y_gap = 20
        x_gap = 30

        if not t_active:
            msg_y = grid_y_start + 60
            f_msg = _get_font("Courier New", 18, bold=True)
            lbl_msg1 = f_msg.render("CHANNEL IS CURRENTLY OFF", True, OFF_RED)
            lbl_msg2 = f_msg.render("Turn ON the channel to access scheduling options", True, TEXT_WHITE)
            menu_canvas.blit(lbl_msg1, (cx + (content_w // 2) - (lbl_msg1.get_width() // 2), msg_y))
            menu_canvas.blit(lbl_msg2, (cx + (content_w // 2) - (lbl_msg2.get_width() // 2), msg_y + 30))
            
            close_row_idx = 1
            is_close_focused = (sub_r == close_row_idx)
            pygame.draw.rect(menu_canvas, OFF_RED, (cx + content_w - 110, content_y + content_h - 42, 85, 30), border_radius=4)
            if is_close_focused: pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + content_w - 110, content_y + content_h - 42, 85, 30), 2, border_radius=4)
            lbl_cl = f_tab.render("Close", True, TEXT_WHITE)
            menu_canvas.blit(lbl_cl, (cx + content_w - 85, content_y + content_h - 35))
        elif t_vis:
            is_style_focused = (sub_r == 1)
            pygame.draw.rect(menu_canvas, (25, 45, 80), (cx + 35, grid_y_start, content_w - 70, cell_h), border_radius=6)
            if is_style_focused: pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 35, grid_y_start, content_w - 70, cell_h), 2, border_radius=6)
            style_str = ch_info.get("visualizer_style", "Random").upper()
            lbl_style = f_body.render(f"VISUALIZER ENGINE STYLE: < {style_str} >", True, NEON_GREEN if is_style_focused else TEXT_WHITE)
            menu_canvas.blit(lbl_style, (cx + 55, grid_y_start + 15))

# ==============================================================================
# PART 12 OF 15: SPECIFIC TABS CONTENT INJECTIONS - CHANNEL SUB-MENU SCHEDULER CONT
# ==============================================================================

            is_audio_dir_focused = (sub_r == 2)
            pygame.draw.rect(menu_canvas, (25, 45, 80), (cx + 35, grid_y_start + cell_h + y_gap, content_w - 70, cell_h), border_radius=6)
            if is_audio_dir_focused: pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 35, grid_y_start + cell_h + y_gap, content_w - 70, cell_h), 2, border_radius=6)
            lbl_audio = f_body.render("AUDIO FILES STORAGE PLATFORM: Select Audio Tracks Folder", True, NEON_GREEN if is_audio_dir_focused else TEXT_WHITE)
            menu_canvas.blit(lbl_audio, (cx + 55, grid_y_start + cell_h + y_gap + 15))

            is_clear_focused = (sub_r == 3)
            pygame.draw.rect(menu_canvas, OFF_RED if is_clear_focused else (45, 20, 25), (cx + 35, grid_y_start + (cell_h * 2) + (y_gap * 2), content_w - 70, cell_h), border_radius=6)
            if is_clear_focused: pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 35, grid_y_start + (cell_h * 2) + (y_gap * 2), content_w - 70, cell_h), 2, border_radius=6)
            lbl_clr = f_body.render("CLEAR MUSIC TRACKS PROFILE MEMORY SELECTIONS", True, NEON_GREEN if is_clear_focused else TEXT_WHITE)
            menu_canvas.blit(lbl_clr, (cx + 55, grid_y_start + (cell_h * 2) + (y_gap * 2) + 15))
            
            close_row_idx = 4
            is_close_focused = (sub_r == close_row_idx)
            pygame.draw.rect(menu_canvas, OFF_RED, (cx + content_w - 110, content_y + content_h - 42, 85, 30), border_radius=4)
            if is_close_focused: pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + content_w - 110, content_y + content_h - 42, 85, 30), 2, border_radius=4)
            lbl_cl = f_tab.render("Close", True, TEXT_WHITE)
            menu_canvas.blit(lbl_cl, (cx + content_w - 85, content_y + content_h - 35))
        else:
            SCHED_MODES = ["random_slots", "one_slot", "two_slots", "marathon"]
            SCHED_LABELS = {
                "random_slots": "RANDOM SLOTS",
                "one_slot":     "ONE SLOT",
                "two_slots":    "TWO SLOTS",
                "marathon":     "MARATHON",
            }
            cur_sched = ch_info.get("scheduling_mode", "random_slots")
            _sched_is_marathon = (cur_sched == "marathon")

            # Row layout depends on whether Marathon collapses things:
            #   marathon:    0 header,1 sched,2 pair,3 commercials,4 folder(24h),
            #                5 holiday,6 clear,7 audio track,8 close
            #   block modes: 0 header,1 sched,2 pair,3 commercials,4 EPISODE ORDER,
            #                5 BLOCK LENGTH,6 folder(3-wide or 24h),7 holiday,8 clear,
            #                9 audio track,10 close
            # Marathon has no per-episode "next up" selection (it just plays the
            # whole folder straight through) and is already inherently 24-hour,
            # so neither the episode-order row nor the block-length row exist
            # there at all — row numbers stay exactly as they were before these
            # features existed for marathon channels, and only shift for the
            # three block modes that actually use them.
            _block_mode  = ch_info.get("block_mode", "full_day")
            _is_full_day = (not _sched_is_marathon) and (_block_mode == "full_day")
            _folder_row  = 4 if _sched_is_marathon else 6
            _holiday_row = 5 if _sched_is_marathon else 7
            # Audio Track now sits ABOVE Clear All in both nav order and layout.
            _audio_track_row = _holiday_row + 1
            _clear_row   = _audio_track_row + 1
            _close_row   = _clear_row + 1

            # ── ROW 1: Scheduling mode selector ──────────────────────────────────
            sched_r_h = 36
            sched_y   = grid_y_start
            sched_w   = int((content_w - 70) * 0.78)
            is_sched_focused = (sub_r == 1)
            pygame.draw.rect(menu_canvas, (25, 45, 80), (cx + 35, sched_y, sched_w, sched_r_h), border_radius=6)
            if is_sched_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 35, sched_y, sched_w, sched_r_h), 2, border_radius=6)
            lbl_sched = f_body.render(
                f"SCHEDULING MODE:   \u25c4  {SCHED_LABELS.get(cur_sched, cur_sched.upper())}  \u25ba",
                True, NEON_GREEN if is_sched_focused else TEXT_WHITE
            )
            menu_canvas.blit(lbl_sched, (cx + 55, sched_y + sched_r_h // 2 - lbl_sched.get_height() // 2))

            # ── ROW 2: Pair short episodes threshold slider ───────────────────────
            pair_thresh_y = sched_y + sched_r_h + 8
            pair_r_h      = 36
            pair_w        = int((content_w - 70) * 0.66)
            is_pair_focused = (sub_r == 2)
            _pt_min = ch_info.get("pair_threshold_minutes", 15)
            _pt_label = "OFF" if _pt_min <= 0 else f"{_pt_min} MIN"
            pygame.draw.rect(menu_canvas, (25, 45, 80), (cx + 35, pair_thresh_y, pair_w, pair_r_h), border_radius=6)
            if is_pair_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 35, pair_thresh_y, pair_w, pair_r_h), 2, border_radius=6)
            lbl_pair = f_body.render(
                f"PAIR SHORTS UNDER:   \u25c4  {_pt_label}  \u25ba   (0 = OFF, max 30 MIN)",
                True, NEON_GREEN if is_pair_focused else TEXT_WHITE
            )
            menu_canvas.blit(lbl_pair, (cx + 55, pair_thresh_y + pair_r_h // 2 - lbl_pair.get_height() // 2))

            # ── ROW 3: Commercials ON/OFF toggle (matches Status pill style) ────
            com_row_y = pair_thresh_y + pair_r_h + 8
            com_on    = ch_info.get("commercials_enabled", False)

            is_com_toggle_focused = (sub_r == 3 and sub_c == 0)
            lbl_com_txt = f_body.render("Commercials:", True, NEON_GREEN if is_com_toggle_focused else TEXT_WHITE)
            menu_canvas.blit(lbl_com_txt, (cx + 25, com_row_y + 4))

            com_sw_bg = OFF_GREEN if com_on else (45, 65, 100)
            pygame.draw.rect(menu_canvas, com_sw_bg, (cx + 165, com_row_y, 55, 24), border_radius=12)
            if is_com_toggle_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 165, com_row_y, 55, 24), 2, border_radius=12)
            com_sw_handle_x = cx + 207 if com_on else cx + 177
            pygame.draw.circle(menu_canvas, TEXT_WHITE, (com_sw_handle_x, com_row_y + 12), 9)
            if is_com_toggle_focused:
                pygame.draw.circle(menu_canvas, NEON_GREEN, (com_sw_handle_x, com_row_y + 12), 11, 2)

            # Button to ADD commercial files — only visible when ON. This used
            # to open the "Commercials & Extras" sub-menu (with Intro/Outro
            # pickers + placement radios); that sub-menu has been removed, so the
            # button now opens the file explorer directly to add commercials (see
            # the CHANNEL_SUB_MENU row-3/col-1 K_RETURN handler). The "\u25ba"
            # arrow was dropped because it signified "opens a sub-menu", which is
            # no longer what this button does.
            if com_on:
                btn_x2 = cx + 235
                btn_w2 = 200
                is_com_btn_focused = (sub_r == 3 and sub_c == 1)
                _com_file_ct = len(ch_info.get("schedules", {}).get("Commercials", []))
                pygame.draw.rect(menu_canvas,
                                 (25, 45, 80),
                                 (btn_x2, com_row_y, btn_w2, 24), border_radius=12)
                if is_com_btn_focused:
                    pygame.draw.rect(menu_canvas, NEON_GREEN, (btn_x2, com_row_y, btn_w2, 24), 2, border_radius=12)
                btn_txt2 = f_body.render(
                    f"COMMERCIALS ({_com_file_ct})",
                    True, NEON_GREEN if is_com_btn_focused else TEXT_WHITE)
                menu_canvas.blit(btn_txt2, (
                    btn_x2 + btn_w2 // 2 - btn_txt2.get_width() // 2,
                    com_row_y + 12 - btn_txt2.get_height() // 2
                ))

                # Placement-mode toggle button, to the RIGHT of the Commercials
                # button. Shows the current placement mode's name; ENTER cycles it
                # (Interrupt show <-> End of show only). Mirrors the "CHANNEL
                # TRANSITION: BLACK/STATIC" button pattern in the Theme tab: a
                # plain button whose label is the current value, changed only by
                # ENTER (no A/D slider). Replaces the placement radios that lived
                # in the removed sub-menu.
                _placement = ch_info.get("commercial_placement", "interrupt_half_hour")
                _placement_label = "END OF SHOW" if _placement == "end_of_show" else "INTERRUPT SHOW"
                is_com_mode_focused = (sub_r == 3 and sub_c == 2)
                mode_btn_x = btn_x2 + btn_w2 + 12
                _mode_lbl_surf = f_body.render(f"MODE: {_placement_label}", True,
                                               NEON_GREEN if is_com_mode_focused else TEXT_WHITE)
                mode_btn_w = _mode_lbl_surf.get_width() + 28
                pygame.draw.rect(menu_canvas, (25, 45, 80),
                                 (mode_btn_x, com_row_y, mode_btn_w, 24), border_radius=12)
                if is_com_mode_focused:
                    pygame.draw.rect(menu_canvas, NEON_GREEN, (mode_btn_x, com_row_y, mode_btn_w, 24), 2, border_radius=12)
                menu_canvas.blit(_mode_lbl_surf, (
                    mode_btn_x + mode_btn_w // 2 - _mode_lbl_surf.get_width() // 2,
                    com_row_y + 12 - _mode_lbl_surf.get_height() // 2
                ))

            # ── ROW 4: Episode order — Sequential vs Random (hidden in Marathon) ──
            # Marathon has no per-episode selection at all, so this row simply
            # doesn't exist there — see the row-layout comment above.
            order_row_bottom = com_row_y + 24 + 8
            if not _sched_is_marathon:
                order_y = com_row_y + 24 + 8
                order_h = 28
                is_order_focused = (sub_r == 4)
                _order_mode = ch_info.get("episode_order_mode", "sequential")
                lbl_order = f_body.render(
                    f"EPISODE ORDER:   \u25c4  {_order_mode.upper()}  \u25ba   (per-show pick order)",
                    True, NEON_GREEN if is_order_focused else TEXT_WHITE
                )
                # Size the box to the text (with padding) instead of a fixed
                # fraction of the content width — "SEQUENTIAL"/"RANDOM" plus
                # the trailing hint text can be longer than a flat 0.6 ratio
                # allows, which was causing the label to spill past the box.
                order_w = max(int((content_w - 70) * 0.6), lbl_order.get_width() + 40)
                order_w = min(order_w, content_w - 70)
                pygame.draw.rect(menu_canvas, (25, 45, 80), (cx + 35, order_y, order_w, order_h), border_radius=6)
                if is_order_focused:
                    pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 35, order_y, order_w, order_h), 2, border_radius=6)
                menu_canvas.blit(lbl_order, (cx + 55, order_y + order_h // 2 - lbl_order.get_height() // 2))
                order_row_bottom = order_y + order_h + 8

            # ── ROW 5: Block length — 8-hour segmented blocks vs one 24-hour pool ──
            # Hidden in Marathon: it's already a single continuous 24-hour
            # playlist by definition, so this toggle would be meaningless there.
            block_len_row_bottom = order_row_bottom
            if not _sched_is_marathon:
                bl_y = order_row_bottom
                bl_h = 28
                is_bl_focused = (sub_r == 5)
                _bl_label = "24-HOUR (FULL DAY)" if _is_full_day else "8-HOUR BLOCKS"
                lbl_bl = f_body.render(
                    f"BLOCK LENGTH:   \u25c4  {_bl_label}  \u25ba",
                    True, NEON_GREEN if is_bl_focused else TEXT_WHITE
                )
                bl_w = max(int((content_w - 70) * 0.6), lbl_bl.get_width() + 40)
                bl_w = min(bl_w, content_w - 70)
                pygame.draw.rect(menu_canvas, (25, 45, 80), (cx + 35, bl_y, bl_w, bl_h), border_radius=6)
                if is_bl_focused:
                    pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 35, bl_y, bl_w, bl_h), 2, border_radius=6)
                menu_canvas.blit(lbl_bl, (cx + 55, bl_y + bl_h // 2 - lbl_bl.get_height() // 2))
                block_len_row_bottom = bl_y + bl_h + 8

            # ── ROW 6: Broadcast folder slots — laid out SIDE BY SIDE ────────────
            # Morning / Evening / Night now sit next to each other on ONE row so
            # A/D navigates between them (instead of stacking vertically). When
            # MARATHON scheduling is active, or a block-mode channel's BLOCK
            # LENGTH is set to 24-HOUR (FULL DAY), the three collapse into a
            # single wide button spanning the same width, because either way
            # the channel plays one continuous folder around the clock.
            folder_y_start = block_len_row_bottom
            f_cell_h = 44
            slots_total_w = content_w - 70
            if cur_sched == "marathon":
                is_slot_focused = (sub_r == _folder_row)
                file_count = len(ch_info.get("schedules", {}).get("Marathon", []))
                pygame.draw.rect(menu_canvas, (25, 45, 80), (cx + 35, folder_y_start, slots_total_w, f_cell_h), border_radius=6)
                if is_slot_focused:
                    pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 35, folder_y_start, slots_total_w, f_cell_h), 2, border_radius=6)
                lbl_m = f_body.render(
                    f"24-HOUR MARATHON  ({file_count} file{'s' if file_count != 1 else ''})",
                    True, NEON_GREEN if is_slot_focused else TEXT_WHITE
                )
                menu_canvas.blit(lbl_m, (cx + 35 + slots_total_w // 2 - lbl_m.get_width() // 2,
                                         folder_y_start + f_cell_h // 2 - lbl_m.get_height() // 2))
            elif _is_full_day:
                is_slot_focused = (sub_r == _folder_row)
                file_count = len(ch_info.get("schedules", {}).get("Full Day", []))
                pygame.draw.rect(menu_canvas, (25, 45, 80), (cx + 35, folder_y_start, slots_total_w, f_cell_h), border_radius=6)
                if is_slot_focused:
                    pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 35, folder_y_start, slots_total_w, f_cell_h), 2, border_radius=6)
                lbl_fd = f_body.render(
                    f"24-HOUR BLOCK  ({file_count} file{'s' if file_count != 1 else ''})",
                    True, NEON_GREEN if is_slot_focused else TEXT_WHITE
                )
                menu_canvas.blit(lbl_fd, (cx + 35 + slots_total_w // 2 - lbl_fd.get_width() // 2,
                                          folder_y_start + f_cell_h // 2 - lbl_fd.get_height() // 2))
            else:
                f_gap = 12
                f_cell_w = (slots_total_w - 2 * f_gap) // 3
                for i, name in enumerate(("Morning", "Evening", "Night")):
                    item_x = cx + 35 + i * (f_cell_w + f_gap)
                    is_slot_focused = (sub_r == _folder_row and sub_c == i)
                    file_count = len(ch_info.get("schedules", {}).get(name, []))
                    pygame.draw.rect(menu_canvas, (25, 45, 80), (item_x, folder_y_start, f_cell_w, f_cell_h), border_radius=6)
                    if is_slot_focused:
                        pygame.draw.rect(menu_canvas, NEON_GREEN, (item_x, folder_y_start, f_cell_w, f_cell_h), 2, border_radius=6)
                    lbl_slot = f_body.render(
                        f"{name.upper()} ({file_count})",
                        True, NEON_GREEN if is_slot_focused else TEXT_WHITE
                    )
                    menu_canvas.blit(lbl_slot, (item_x + f_cell_w // 2 - lbl_slot.get_width() // 2,
                                                folder_y_start + f_cell_h // 2 - lbl_slot.get_height() // 2))

            # ── ROW: Holiday overrides ────────────────────────────────────────────
            hol_y  = folder_y_start + f_cell_h + 12
            hol_h  = 28
            hol_w  = int((content_w - 70) * 0.36)
            is_hol_focused = (sub_r == _holiday_row)
            pygame.draw.rect(menu_canvas, (30, 50, 90), (cx + 35, hol_y, hol_w, hol_h), border_radius=6)
            if is_hol_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 35, hol_y, hol_w, hol_h), 2, border_radius=6)
            lbl_hol = f_body.render("HOLIDAY SCHEDULE OVERRIDES  \u2192", True, NEON_GREEN if is_hol_focused else TEXT_WHITE)
            menu_canvas.blit(lbl_hol, (cx + 55, hol_y + hol_h // 2 - lbl_hol.get_height() // 2))

            # ── ROW: Change Current Playing Audio Track ──────────────────��──────────
            # Fixes shows that default to the wrong embedded audio track (e.g. a
            # foreign dub) by letting the viewer pick the right one for what's
            # airing right now; only usable while this channel is the one live
            # on screen, since that's the only time there's a VLC media loaded
            # with tracks to list. Sits directly under Holiday (above Clear All).
            _is_live_ch_for_audio = (str(app_state.get("current_channel", "")).zfill(2) == target_ch_str)
            atr_y = hol_y + hol_h + 12
            atr_h = 28
            is_atr_focused = (sub_r == _audio_track_row)
            # Same blue as the other action buttons (matches Holiday's (30,50,90));
            # greyed out when this isn't the live channel.
            _atr_bg = (30, 50, 90) if _is_live_ch_for_audio else (35, 35, 35)
            _atr_txt_color = TEXT_WHITE if _is_live_ch_for_audio else (110, 110, 110)
            lbl_atr = f_body.render(
                "CHANGE CURRENT PLAYING AUDIO TRACK  \u25ba",
                True, NEON_GREEN if (is_atr_focused and _is_live_ch_for_audio) else _atr_txt_color
            )
            # Size the box to the text (20px padding each side) instead of a fixed
            # fraction, so it doesn't run long past the label.
            atr_w = lbl_atr.get_width() + 40
            pygame.draw.rect(menu_canvas, _atr_bg, (cx + 35, atr_y, atr_w, atr_h), border_radius=6)
            if is_atr_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 35, atr_y, atr_w, atr_h), 2, border_radius=6)
            menu_canvas.blit(lbl_atr, (cx + 55, atr_y + atr_h // 2 - lbl_atr.get_height() // 2))
            if is_atr_focused and not _is_live_ch_for_audio:
                _f_tip2 = _get_font("Courier New", 11, bold=True)
                _lbl_tip2 = _f_tip2.render("Tune to this channel first to change its audio track", True, (140, 100, 60))
                menu_canvas.blit(_lbl_tip2, (cx + 55, atr_y + atr_h + 2))

            # ── ROW: Clear All ──────────────────────────────────────────────────────
            clear_y = atr_y + atr_h + 8
            clear_h = 28
            clear_w = int((content_w - 70) * 0.67)
            clear_x = cx + 35 + ((content_w - 70) - clear_w) // 2
            is_clear_focused = (sub_r == _clear_row)
            pygame.draw.rect(menu_canvas, OFF_RED if is_clear_focused else (45, 20, 25), (clear_x, clear_y, clear_w, clear_h), border_radius=6)
            if is_clear_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (clear_x, clear_y, clear_w, clear_h), 2, border_radius=6)
            lbl_clr = f_body.render("CLEAR ALL BROADCAST TIME-SLOT SCHEDULE FILES", True, NEON_GREEN if is_clear_focused else TEXT_WHITE)
            menu_canvas.blit(lbl_clr, (clear_x + clear_w // 2 - lbl_clr.get_width() // 2, clear_y + clear_h // 2 - lbl_clr.get_height() // 2))

            # ── ROW: Close ──────────────────────────────────────────────────────────
            is_close_focused = (sub_r == _close_row)
            pygame.draw.rect(menu_canvas, OFF_RED, (cx + content_w - 110, content_y + content_h - 42, 85, 30), border_radius=4)
            if is_close_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + content_w - 110, content_y + content_h - 42, 85, 30), 2, border_radius=4)
            lbl_cl = f_tab.render("Close", True, TEXT_WHITE)
            menu_canvas.blit(lbl_cl, (cx + content_w - 85, content_y + content_h - 35))

    elif active_tab == "Channels" and layer == "AUDIO_TRACK_PICKER":
        # Small popup window, deliberately simpler than the other sub-menus:
        # just the track list, a highlighted cursor (W/S to move), a checkbox
        # showing which track is actually playing right now, and a Close
        # button in the same spot every other sub-menu puts one.
        scale_factor = 1.0
        target_ch_str = str(app_state.get("audio_track_source_channel", app_state.get("selected_channel_row", 5))).zfill(2)
        ch_info = db.channels_db.get(target_ch_str, {"name": f"STATION {target_ch_str}"})

        f_sub_title = _get_font("Courier New", 20, bold=True)
        _ch_name = ch_info.get("name", f"STATION {target_ch_str}").upper()
        lbl_hdr = f_sub_title.render(
            f"Channel {target_ch_str} — {_ch_name}: Audio Track", True, TEXT_WHITE
        )
        menu_canvas.blit(lbl_hdr, (cx + content_w // 2 - lbl_hdr.get_width() // 2, content_y + 20))

        f_hint = _get_font("Courier New", 13, bold=True)
        lbl_hint = f_hint.render("W / S to move   \u2022   ENTER to select and close", True, (150, 150, 150))
        menu_canvas.blit(lbl_hint, (cx + content_w // 2 - lbl_hint.get_width() // 2, content_y + 48))

        sub_r  = app_state.get("sub_menu_row_index", 0)
        tracks = app_state.get("audio_track_list", [])
        active_id = app_state.get("audio_track_active_id")
        close_row = len(tracks)

        row_h  = 40
        row_gap = 8
        # Popup is a fixed-height panel that just fits the track list (up to a
        # reasonable cap), centered in the available content area — this is
        # what keeps it feeling like a small focused window rather than a
        # full-size sub-menu panel.
        list_top = content_y + 85

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
                row_w = content_w - 70
                pygame.draw.rect(menu_canvas, (25, 45, 80), (cx + 35, ty, row_w, row_h), border_radius=6)
                if is_focused:
                    pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 35, ty, row_w, row_h), 2, border_radius=6)

                # Checkbox: filled square = the track actually playing right
                # now; empty (outline only) square = every other track.
                box_x = cx + 50
                box_y = ty + row_h // 2 - box_size // 2
                box_color = NEON_GREEN if is_focused else TEXT_WHITE
                if is_active:
                    pygame.draw.rect(menu_canvas, box_color, (box_x, box_y, box_size, box_size), border_radius=3)
                else:
                    pygame.draw.rect(menu_canvas, box_color, (box_x, box_y, box_size, box_size), 2, border_radius=3)

                lbl_track = f_body.render(
                    track_label,
                    True, NEON_GREEN if is_focused else TEXT_WHITE
                )
                menu_canvas.blit(lbl_track, (box_x + box_size + 14, ty + row_h // 2 - lbl_track.get_height() // 2))

        # ── ROW: Close ─────────────���────────────────────────────────────────────
        is_close_focused = (sub_r == close_row)
        pygame.draw.rect(menu_canvas, OFF_RED, (cx + content_w - 110, content_y + content_h - 42, 85, 30), border_radius=4)
        if is_close_focused:
            pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + content_w - 110, content_y + content_h - 42, 85, 30), 2, border_radius=4)
        lbl_cl = f_tab.render("Close", True, TEXT_WHITE)
        menu_canvas.blit(lbl_cl, (cx + content_w - 85, content_y + content_h - 35))

    elif active_tab == "Channels" and layer == "HOLIDAY_SUB_MENU":
        scale_factor = 1.0
        target_ch_str = str(app_state.get("selected_channel_row", 5)).zfill(2)
        ch_info = db.channels_db.get(
            target_ch_str,
            {"active": True, "is_visualizer": False, "name": f"STATION {target_ch_str}"}
        )

        f_sub_title = _get_font("Courier New", 20, bold=True)
        lbl_hdr = f_sub_title.render(
            f"Channel {target_ch_str}  —  Holiday Schedule Overrides", True, TEXT_WHITE
        )
        menu_canvas.blit(lbl_hdr, (cx + content_w // 2 - lbl_hdr.get_width() // 2, content_y + 20))

        sub_r  = app_state.get("sub_menu_row_index", 0)
        HKEYS  = ["halloween", "christmas", "valentine", "thanksgiving", "new_year"]
        HNAMES = ["Halloween", "Christmas", "Valentine's Day", "Thanksgiving", "New Year's Eve"]

        cell_h_hol = 44
        y_gap_hol  = 10
        top_hol_y  = content_y + 70

        for h_idx, (h_key, h_name) in enumerate(zip(HKEYS, HNAMES)):
            hy = top_hol_y + h_idx * (cell_h_hol + y_gap_hol)
            is_focused = (sub_r == h_idx)
            entries    = ch_info.get("holiday_schedules", {}).get(h_key, [])
            fc = len(entries)
            pygame.draw.rect(menu_canvas, (25, 45, 80), (cx + 35, hy, content_w - 70, cell_h_hol), border_radius=6)
            if is_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 35, hy, content_w - 70, cell_h_hol), 2, border_radius=6)
            lbl_h = f_body.render(
                f"{h_name.upper()}:  {fc} file{'s' if fc != 1 else ''} selected — ENTER to pick files",
                True, NEON_GREEN if is_focused else TEXT_WHITE
            )
            menu_canvas.blit(lbl_h, (cx + 55, hy + cell_h_hol // 2 - lbl_h.get_height() // 2))

        # Row 5: Back button
        is_back_focused = (sub_r == 5)
        pygame.draw.rect(menu_canvas, OFF_RED, (cx + content_w - 110, content_y + content_h - 42, 85, 30), border_radius=4)
        if is_back_focused:
            pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + content_w - 110, content_y + content_h - 42, 85, 30), 2, border_radius=4)
        lbl_bk = f_tab.render("Back", True, TEXT_WHITE)
        menu_canvas.blit(lbl_bk, (cx + content_w - 85, content_y + content_h - 35))

  # ==============================================================================
# PART 13 OF 15: SPECIFIC TABS CONTENT INJECTIONS - VIDEO PANELS (SLIMMED)
# ==============================================================================

    if active_tab == "Video" or active_tab == "video" or active_tab.upper() == "VIDEO":
        scale_factor = 1.0
        
        top_buttons_y = content_y + 20
        close_button_y = content_y + content_h - 50
        
        slider_area_top = top_buttons_y + 15
        slider_area_bottom = close_button_y - 20
        slider_area_height = slider_area_bottom - slider_area_top
        
        slider_spacing = slider_area_height // 5  
        
        box_radius = 4
        inner_radius = 3
        focus_thickness = 2
        
        sliders = [
            (0, "Brightness:", db.config.get("brightness", 50), slider_area_top + slider_spacing * 0),
            (1, "Contrast:", db.config.get("contrast", 50), slider_area_top + slider_spacing * 1),
            (2, "Color:", db.config.get("color", 50), slider_area_top + slider_spacing * 2),
            (3, "Sharpness:", db.config.get("sharpness", 50), slider_area_top + slider_spacing * 3),
            (4, "Tint:", db.config.get("tint", 50), slider_area_top + slider_spacing * 4)
        ]
        
        slider_padding_x = 35
        slider_w_total = content_w - (slider_padding_x * 2)
        slider_bar_h = 6
        handle_rad = 8
        focus_handle_rad = 10
        
        for s_idx, s_lbl, s_val, sy in sliders:
            is_sl_focused = (layer == "VIDEO_ROWS" and cur_sel == s_idx)
            t_color = NEON_GREEN if is_sl_focused else TEXT_WHITE
            
            lbl_s = f_body.render(f"{s_lbl} {s_val}", True, t_color)
            menu_canvas.blit(lbl_s, (cx + slider_padding_x, sy))
            
            pygame.draw.rect(menu_canvas, (25, 45, 80), (cx + slider_padding_x, sy + 22, slider_w_total, slider_bar_h), border_radius=inner_radius)
            handle_x = cx + slider_padding_x + int((slider_w_total * (s_val / 100.0)))
            
            pygame.draw.circle(menu_canvas, TEXT_WHITE, (handle_x, sy + 25), handle_rad)
            if is_sl_focused: pygame.draw.circle(menu_canvas, NEON_GREEN, (handle_x, sy + 25), focus_handle_rad, 2)
            
        is_cl_focused = (layer == "VIDEO_ROWS" and cur_sel == 5)
        close_btn_w = 85
        close_btn_h = 32
        close_btn_x = cx + content_w - 110
        close_txt_y_offset = (close_btn_h // 2) - (f_tab.get_height() // 2)

        pygame.draw.rect(menu_canvas, OFF_RED, (close_btn_x, close_button_y, close_btn_w, close_btn_h), border_radius=box_radius)
        if is_cl_focused: pygame.draw.rect(menu_canvas, NEON_GREEN, (close_btn_x, close_button_y, close_btn_w, close_btn_h), focus_thickness, border_radius=box_radius)
        lbl_c = f_tab.render("Close", True, TEXT_WHITE)
        menu_canvas.blit(lbl_c, (close_btn_x + (close_btn_w // 2) - (lbl_c.get_width() // 2), close_button_y + close_txt_y_offset))

# ==============================================================================
# PART 14 OF 15: SPECIFIC TABS CONTENT INJECTIONS - THEME COLOR PICKERS & MASTER SURFACE STAMP RENDERING
# ==============================================================================

    # --------------------------------------------------------------------------
    # VIEWPORT PANEL DRAW ENGINE TAB 4: THEME CUSTOMIZATION COLOR SLIDERS
    # --------------------------------------------------------------------------
    elif active_tab == "Theme" or active_tab == "theme" or active_tab.upper() == "THEME":
        scale_factor = 1.0
        import colorsys
        
        bg_hue = db.config.get("theme_bg_hue", 220) if db is not None else 220  
        ui_hue = db.config.get("theme_ui_hue", 140) if db is not None else 140  
        
        slider_width = content_w - 100
        slider_x = cx + 50
        
        trans_title_y = content_y + 45
        trans_slider_y = content_y + 75
        
        bg_title_y = content_y + 155
        bg_slider_y = content_y + 185
        
        ui_title_y = content_y + 265
        ui_slider_y = content_y + 295
        
        gradient_height = 18
        knob_rad = 10
        focus_knob_rad = 12
        box_radius = 4
        inner_radius = 3
        focus_thickness = 2
        
        slider_padding_x = 50
        slider_w_total = content_w - (slider_padding_x * 2)
        slider_bar_h = 6
        handle_rad = 8
        focus_handle_rad = 10

        # --- DRAW ROW 0: MENU TRANSPARENCY SLIDER ---
        is_trans_focused = (layer == "THEME_ROWS" and cur_sel == 0)
        title_color = NEON_GREEN if is_trans_focused else TEXT_WHITE
        
        trans_val = db.config.get("menu_opacity", 50)
        lbl_trans = f_tab.render(f"MENU TRANSPARENCY: {trans_val}%", True, title_color)
        trans_title_x = cx + (content_w // 2) - (lbl_trans.get_width() // 2)
        menu_canvas.blit(lbl_trans, (trans_title_x, trans_title_y))
        
        pygame.draw.rect(menu_canvas, (25, 45, 80), (cx + slider_padding_x, trans_slider_y + 5, slider_w_total, slider_bar_h), border_radius=inner_radius)
        handle_x = cx + slider_padding_x + int((slider_w_total * (trans_val / 100.0)))
        
        pygame.draw.circle(menu_canvas, TEXT_WHITE, (handle_x, trans_slider_y + 8), handle_rad)
        if is_trans_focused:
            pygame.draw.circle(menu_canvas, NEON_GREEN, (handle_x, trans_slider_y + 8), focus_handle_rad, 2)

        # ----------------------------------------------------------------------
        # OPTIMIZED PASSTHROUGH: RE-USE OR GENERATE PERMANENT GRADIENT TRACK SURFACES
        # ----------------------------------------------------------------------
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

        # --- DRAW ROW 1: BACKGROUND COLOR GRADIENT ---
        is_bg_focused = (layer == "THEME_ROWS" and cur_sel == 1)
        title_color = NEON_GREEN if is_bg_focused else TEXT_WHITE
        
        lbl_bg = f_tab.render("BACKGROUND COLOR", True, title_color)
        bg_title_x = cx + (content_w // 2) - (lbl_bg.get_width() // 2)
        menu_canvas.blit(lbl_bg, (bg_title_x, bg_title_y))
        
        # Super-fast instant texture stamp replace pass
        menu_canvas.blit(_cached_bg_gradient_surf, (slider_x, bg_slider_y))
        
        bg_knob_x = slider_x + int((bg_hue / 360.0) * slider_width)
        pygame.draw.circle(menu_canvas, TEXT_WHITE, (bg_knob_x, bg_slider_y - 5), knob_rad)
        if is_bg_focused:
            pygame.draw.circle(menu_canvas, NEON_GREEN, (bg_knob_x, bg_slider_y - 5), focus_knob_rad, 2)
        
        # --- DRAW ROW 2: BORDER/TEXT COLOR GRADIENT ---
        is_ui_focused = (layer == "THEME_ROWS" and cur_sel == 2)
        title_color = NEON_GREEN if is_ui_focused else TEXT_WHITE
        
        lbl_ui = f_tab.render("BORDER/TEXT COLOR", True, title_color)
        ui_title_x = cx + (content_w // 2) - (lbl_ui.get_width() // 2)
        menu_canvas.blit(lbl_ui, (ui_title_x, ui_title_y))
        
        # Super-fast instant texture stamp replace pass
        menu_canvas.blit(_cached_ui_gradient_surf, (slider_x, ui_slider_y))
        
        ui_knob_x = slider_x + int((ui_hue / 360.0) * slider_width)
        pygame.draw.circle(menu_canvas, TEXT_WHITE, (ui_knob_x, ui_slider_y - 5), knob_rad)
        if is_ui_focused:
            pygame.draw.circle(menu_canvas, NEON_GREEN, (ui_knob_x, ui_slider_y - 5), focus_knob_rad, 2)

        # --- DRAW ROW 3: CHANNEL TRANSITION TYPE (BUTTON, Enter to change) ---
        # Styled like every other button row (e.g. Remote Remapping in the
        # System tab) instead of a left/right slider -- same blue fill,
        # same focus outline, and only Enter changes the value now (see the
        # THEME_ROWS K_RETURN handler).
        BUTTON_BLUE = (45, 80, 130)
        trans_type_button_y = content_y + 355
        trans_btn_h = 34

        is_tt_focused = (layer == "THEME_ROWS" and cur_sel == 3)

        transition_type_val = db.config.get("transition_type", "black")
        tt_value_text = "STATIC" if transition_type_val == "static" else "BLACK"

        # Size the button off the WIDER of the two possible labels ("BLACK" vs
        # "STATIC") instead of a fixed 260px -- at 260 the "STATIC" text was
        # getting clipped/crowded against the button edges. 40px of side
        # padding (20 each side) keeps the label clear of the rounded corners.
        _trans_label_w = max(
            f_tab.size("CHANNEL TRANSITION: BLACK")[0],
            f_tab.size("CHANNEL TRANSITION: STATIC")[0],
        )
        trans_btn_w = _trans_label_w + 40

        trans_btn_x = cx + (content_w // 2) - (trans_btn_w // 2)
        pygame.draw.rect(menu_canvas, BUTTON_BLUE, (trans_btn_x, trans_type_button_y, trans_btn_w, trans_btn_h), border_radius=box_radius)
        if is_tt_focused:
            pygame.draw.rect(menu_canvas, NEON_GREEN, (trans_btn_x, trans_type_button_y, trans_btn_w, trans_btn_h), focus_thickness, border_radius=box_radius)

        lbl_tt = f_tab.render(f"CHANNEL TRANSITION: {tt_value_text}", True, TEXT_WHITE)
        lbl_tt_y = trans_type_button_y + (trans_btn_h // 2) - (f_tab.get_height() // 2)
        menu_canvas.blit(lbl_tt, (cx + (content_w // 2) - (lbl_tt.get_width() // 2), lbl_tt_y))

        is_cl_focused = (layer == "THEME_ROWS" and cur_sel == 4)
        close_btn_w = 85
        close_btn_h = 32
        close_btn_x = cx + content_w - 110
        close_btn_y = content_y + content_h - 45
        close_txt_y_offset = (close_btn_h // 2) - (f_tab.get_height() // 2)

        pygame.draw.rect(menu_canvas, OFF_RED, (close_btn_x, close_btn_y, close_btn_w, close_btn_h), border_radius=box_radius)
        if is_cl_focused: 
            pygame.draw.rect(menu_canvas, NEON_GREEN, (close_btn_x, close_btn_y, close_btn_w, close_btn_h), focus_thickness, border_radius=box_radius)
        lbl_c = f_tab.render("Close", True, TEXT_WHITE)
        menu_canvas.blit(lbl_c, (close_btn_x + (close_btn_w // 2) - (lbl_c.get_width() // 2), close_btn_y + close_txt_y_offset))


    # --------------------------------------------------------------------------
    # VIEWPORT PANEL DRAW ENGINE TAB 3: GAMES CONSOLE MANAGER
    # --------------------------------------------------------------------------
    elif active_tab == "Games" and layer in ("GAMES_LIST", "TAB_SELECTION"):
        import os
        from game_deck import CONSOLE_ORDER, CONSOLE_LABELS

        cols = 4
        console_list = list(CONSOLE_ORDER)
        total = len(console_list)
        cur_sel_g  = app_state.get("menu_selection_index", 0)
        games_page = app_state.get("games_tab_page", 0)

        # Scale cell size from content_w so the grid never overflows on smaller windows
        x_pad, y_pad = 8, 8
        cell_w = (content_w - (x_pad * (cols - 1)) - 20) // cols
        cell_h = max(52, int(content_h * 0.16))
        grid_start_x = cx + 10
        grid_start_y = content_y + 14

        logos_dir   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main", "images", "logos")
        enabled_map = db.config.get("consoles_enabled", {}) if db else {}

        # ── Page 0: console grid ──────────────────────────────────────────────
        if games_page == 0:
            for idx, console_key in enumerate(console_list):
                r_pos  = idx // cols
                c_pos  = idx % cols
                cell_x = grid_start_x + c_pos * (cell_w + x_pad)
                cell_y = grid_start_y + r_pos * (cell_h + y_pad)

                is_focused = (layer == "GAMES_LIST" and cur_sel_g == idx)
                default_on = (console_key == "DVD")
                is_on      = enabled_map.get(console_key, default_on)
                dot_color  = OFF_GREEN if is_on else OFF_RED

                pygame.draw.rect(menu_canvas, (25, 45, 80), (cell_x, cell_y, cell_w, cell_h), border_radius=6)
                if is_focused:
                    pygame.draw.rect(menu_canvas, NEON_GREEN, (cell_x, cell_y, cell_w, cell_h), 2, border_radius=6)

                # Black inset background box — sits inside the blue button, makes logos pop
                inset_pad = 6
                inset_x   = cell_x + inset_pad
                inset_y   = cell_y + inset_pad
                inset_w   = cell_w - inset_pad * 2
                inset_h   = cell_h - inset_pad * 2
                pygame.draw.rect(menu_canvas, (0, 0, 0), (inset_x, inset_y, inset_w, inset_h), border_radius=4)

                # Logo — decode cached per-console, smoothscale cached per cell size
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
                        except Exception:
                            logo_raw = None
                        _menu_logo_raw_cache[console_key] = logo_raw
                    if logo_raw is not None:
                        try:
                            max_lh = inset_h - 6
                            max_lw = inset_w - 12
                            ratio  = min(max_lw / logo_raw.get_width(), max_lh / logo_raw.get_height())
                            lw     = max(1, int(logo_raw.get_width()  * ratio))
                            lh     = max(1, int(logo_raw.get_height() * ratio))
                            _menu_logo_cache[_cache_key] = pygame.transform.smoothscale(logo_raw, (lw, lh))
                        except Exception:
                            _menu_logo_cache[_cache_key] = None
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

                # Enabled indicator dot — top-right corner of the blue button (outside inset)
                pygame.draw.circle(menu_canvas, dot_color, (cell_x + cell_w - 10, cell_y + 10), 5)

        # ── Page 1: empty placeholder ─────────────────────────────────────────
        else:
            grid_h        = 3 * (cell_h + y_pad) - y_pad
            empty_rect_w  = content_w - 20
            pygame.draw.rect(menu_canvas, (18, 28, 48),
                             (grid_start_x, grid_start_y, empty_rect_w, grid_h), border_radius=8)
            pygame.draw.rect(menu_canvas, (40, 60, 95),
                             (grid_start_x, grid_start_y, empty_rect_w, grid_h), 1, border_radius=8)
            ph_lbl = f_body.render("Page 2 \u2014 coming soon", True, (90, 110, 150))
            menu_canvas.blit(ph_lbl, (grid_start_x + (empty_rect_w - ph_lbl.get_width()) // 2,
                                       grid_start_y + grid_h // 2 - ph_lbl.get_height() // 2))

        # ── Arrow button (► page 0 | ◄ page 1) ───────────────────────────────
        # Sits just below the console grid, right-aligned.  sel=102.
        arrow_btn_y = grid_start_y + 3 * (cell_h + y_pad) - y_pad + 10
        arrow_btn_w = 44
        arrow_btn_h = 30
        arrow_btn_x = cx + content_w - arrow_btn_w - 14
        is_arrow_focused = (layer == "GAMES_LIST" and cur_sel_g == 102)
        arrow_bg = (60, 140, 220) if is_arrow_focused else (40, 100, 180)
        pygame.draw.rect(menu_canvas, arrow_bg,
                         (arrow_btn_x, arrow_btn_y, arrow_btn_w, arrow_btn_h), border_radius=6)
        if is_arrow_focused:
            pygame.draw.rect(menu_canvas, NEON_GREEN,
                             (arrow_btn_x, arrow_btn_y, arrow_btn_w, arrow_btn_h), 2, border_radius=6)
        arrow_char = "\u25ba" if games_page == 0 else "\u25c4"
        arrow_lbl  = f_body.render(arrow_char, True, TEXT_WHITE)
        menu_canvas.blit(arrow_lbl, (arrow_btn_x + (arrow_btn_w - arrow_lbl.get_width())  // 2,
                                      arrow_btn_y + (arrow_btn_h - arrow_lbl.get_height()) // 2))

        # ── 3-column bottom controls ──────────────────────────────────────────
        # Column layout (left → right):
        #   Col 1: All Consoles (sel=95) / All Profiles (sel=101)
        #   Col 2: Screen Saver (sel=98) / Screen Saver Timer (sel=100)
        #   Col 3: Menu Music  (sel=97) / Set Music (sel=96)
        # Row 1 y = content_h - 114 | Row 2 y = content_h - 78 | Close y = content_h - 42
        kiosk_on_gl       = db.config.get("kiosk_mode_enabled",          False) if db else False
        mm_enabled        = db.config.get("ch03_menu_music_enabled",     True)  if db else True
        mm_playlist       = db.config.get("ch03_menu_music",             [])    if db else []
        mm_count          = len(mm_playlist)
        ss_on             = db.config.get("ch03_screensaver_enabled",    True)  if db else True
        all_profiles_sync = db.config.get("all_profiles_sync",           False) if db else False

        ctrl_col_w = content_w // 3
        col1_x = cx + 12
        col2_x = cx + 12 + ctrl_col_w
        col3_x = cx + 12 + 2 * ctrl_col_w + 40
        # Anchor rows to the bottom of the console grid / arrow button (not the
        # bottom of the box) so they sit right under the grid instead of
        # floating far below it with a big empty gap when content_h is tall.
        grid_bottom_y = grid_start_y + 3 * (cell_h + y_pad) - y_pad
        row1_y = grid_bottom_y + 64
        row2_y = row1_y + 36
        row_h  = 28
        pill_w, pill_h = 55, 24

        def _draw_toggle(surf, x, y, label, is_on, focused):
            """Label in a blue box + sliding pill knob."""
            lbl_s = f_body.render(label, True, TEXT_WHITE)
            bw    = lbl_s.get_width() + 24
            bcol  = (60, 140, 220) if focused else (40, 100, 180)
            pygame.draw.rect(surf, bcol, (x, y, bw, row_h), border_radius=5)
            if focused:
                pygame.draw.rect(surf, NEON_GREEN, (x, y, bw, row_h), 2, border_radius=5)
            surf.blit(lbl_s, (x + (bw - lbl_s.get_width()) // 2, y + (row_h - lbl_s.get_height()) // 2))
            px  = x + bw + 10
            py  = y + (row_h - pill_h) // 2
            pbg = OFF_GREEN if is_on else (45, 65, 100)
            pygame.draw.rect(surf, pbg, (px, py, pill_w, pill_h), border_radius=12)
            if focused:
                pygame.draw.rect(surf, NEON_GREEN, (px, py, pill_w, pill_h), 2, border_radius=12)
            hx = px + pill_w - 12 if is_on else px + 12
            pygame.draw.circle(surf, TEXT_WHITE, (hx, py + pill_h // 2), 9)
            if focused:
                pygame.draw.circle(surf, NEON_GREEN, (hx, py + pill_h // 2), 11, 2)

        # Col 1 row 1 — All Consoles (sel=95)
        allcon_others = [c for c in console_list if c != "DVD"]
        allcon_all_on = bool(allcon_others) and all(enabled_map.get(c, False) for c in allcon_others)
        _draw_toggle(menu_canvas, col1_x, row1_y, "All Consoles", allcon_all_on, cur_sel_g == 95)

        # Col 1 row 2 — All Profiles (sel=101)
        _draw_toggle(menu_canvas, col1_x, row2_y, "All Profiles", all_profiles_sync, cur_sel_g == 101)

        # Col 2 row 1 — Screen Saver (sel=98)
        _draw_toggle(menu_canvas, col2_x, row1_y, "Screen Saver:", ss_on, cur_sel_g == 98)

        # Col 2 row 2 — Screen Saver Timer (sel=100, only when ss_on)
        if ss_on:
            is_timer_focused = (cur_sel_g == 100)
            ss_timeout_min   = db.config.get("ch03_screensaver_timeout_min", 5) if db else 5
            timer_lbl_surf   = f_body.render("Screen Saver Timer", True, TEXT_WHITE)
            timer_btn_w      = timer_lbl_surf.get_width() + 24
            timer_btn_col    = (60, 140, 220) if is_timer_focused else (40, 100, 180)
            pygame.draw.rect(menu_canvas, timer_btn_col,
                             (col2_x, row2_y, timer_btn_w, row_h), border_radius=5)
            if is_timer_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN,
                                 (col2_x, row2_y, timer_btn_w, row_h), 2, border_radius=5)
            menu_canvas.blit(timer_lbl_surf,
                             (col2_x + (timer_btn_w - timer_lbl_surf.get_width()) // 2, row2_y + 6))
            val_str     = f"{ss_timeout_min} min"
            if is_timer_focused:
                val_str = f"[ {val_str} ]"
            val_col     = (200, 255, 200) if is_timer_focused else TEXT_WHITE
            lbl_tv      = f_body.render(val_str, True, val_col)
            val_badge_x = col2_x + timer_btn_w + 10
            val_badge_w = lbl_tv.get_width() + 18
            pygame.draw.rect(menu_canvas, (0, 0, 0),
                             (val_badge_x, row2_y + 3, val_badge_w, 24), border_radius=4)
            menu_canvas.blit(lbl_tv, (val_badge_x + 9, row2_y + 6))

        # Col 3 row 1 — Menu Music (sel=97)
        _draw_toggle(menu_canvas, col3_x, row1_y, "Menu Music:", mm_enabled, cur_sel_g == 97)

        # Col 3 row 2 — Set Music (sel=96, only when mm_enabled and not kiosk)
        if mm_enabled and not kiosk_on_gl:
            is_set_focused = (cur_sel_g == 96)
            btn_col        = (60, 140, 220) if is_set_focused else (40, 100, 180)
            pygame.draw.rect(menu_canvas, btn_col,    (col3_x, row2_y, 110, 28), border_radius=5)
            if is_set_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (col3_x, row2_y, 110, 28), 2, border_radius=5)
            btn_lbl = f_body.render("Set Music", True, TEXT_WHITE)
            menu_canvas.blit(btn_lbl, (col3_x + (110 - btn_lbl.get_width()) // 2, row2_y + 6))
            # Track count badge
            if mm_count == 1:
                badge_txt = "1 track added"
            elif mm_count > 1:
                badge_txt = f"{mm_count} tracks added"
            else:
                badge_txt = "default"
            badge_col  = (0, 0, 0) if mm_count else (60, 100, 60)
            badge_surf = f_body.render(badge_txt, True, TEXT_WHITE)
            badge_x    = col3_x + 110 + 10
            badge_w    = badge_surf.get_width() + 18
            pygame.draw.rect(menu_canvas, badge_col,
                             (badge_x, row2_y + 3, badge_w, 24), border_radius=4)
            menu_canvas.blit(badge_surf, (badge_x + 9, row2_y + 6))

        # Close button
        is_close_g = (cur_sel_g == 99)
        pygame.draw.rect(menu_canvas, OFF_RED,
                         (cx + content_w - 110, content_y + content_h - 42, 85, 30), border_radius=4)
        if is_close_g:
            pygame.draw.rect(menu_canvas, NEON_GREEN,
                             (cx + content_w - 110, content_y + content_h - 42, 85, 30), 2, border_radius=4)
        lbl_cl = f_tab.render("Close", True, TEXT_WHITE)
        menu_canvas.blit(lbl_cl, (cx + content_w - 85, content_y + content_h - 35))

    elif active_tab == "Games" and layer == "GAMES_SUB_MENU":
        import os
        from game_deck import CONSOLE_LABELS

        console_key = app_state.get("games_selected_console", "DVD")
        sub_row     = app_state.get("games_sub_row", 0)
        enabled_map = db.config.get("consoles_enabled", {}) if db else {}
        default_on  = (console_key == "DVD")
        is_on       = enabled_map.get(console_key, default_on)

        logos_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main", "images", "logos")

        # ── Header: centred black box (matches the red bracket lines in the UI) ──
        # Width is the centre third of the content area — same proportion as the
        # red lines drawn in the screenshot. Height matches the grid cell height.
        header_pad   = 8
        header_box_h = max(52, int(content_h * 0.18))
        header_box_w = content_w // 3           # centre third only
        header_box_x = cx + (content_w // 2) - (header_box_w // 2)  # centred
        header_box_y = content_y + 10

        logo_path  = os.path.join(logos_dir, f"{console_key}.png")
        logo_drawn = False
        # Cache key includes size so it re-renders if the window is resized
        _sub_logo_cache = app_state.setdefault("_sub_logo_cache", {})
        _sub_key = (console_key, header_box_w, header_box_h)
        if _sub_key not in _sub_logo_cache and os.path.exists(logo_path):
            try:
                logo_raw = pygame.image.load(logo_path).convert_alpha()
                # Crop transparent padding once at load time
                try:
                    import pygame.surfarray as sa
                    alpha = sa.array_alpha(logo_raw)
                    cols = alpha.max(axis=1)
                    rows = alpha.max(axis=0)
                    col_indices = [i for i, v in enumerate(cols) if v > 10]
                    row_indices = [i for i, v in enumerate(rows) if v > 10]
                    if col_indices and row_indices:
                        x1, x2 = col_indices[0], col_indices[-1] + 1
                        y1, y2 = row_indices[0], row_indices[-1] + 1
                        if x2 - x1 > 0 and y2 - y1 > 0:
                            logo_raw = logo_raw.subsurface((x1, y1, x2 - x1, y2 - y1))
                except Exception as e:
                    log.warning("Logo transparent-padding crop failed for %s (using uncropped image): %s", logo_path, e)
                target_w = header_box_w - 8
                target_h = header_box_h - 8
                ratio = min(target_w / logo_raw.get_width(), target_h / logo_raw.get_height())
                lw = max(1, int(logo_raw.get_width() * ratio))
                lh = max(1, int(logo_raw.get_height() * ratio))
                _sub_logo_cache[_sub_key] = pygame.transform.smoothscale(logo_raw, (lw, lh))
            except Exception:
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

        # Everything below starts after the header box with a gap
        top_y  = header_box_y + header_box_h + 14
        row_x  = cx + 35
        row_w  = content_w - 70
        cell_h = 48
        y_gap  = 14

        # SELECT CORE / CONTROLLER MAPPING / PROFILE 1-3 don't need to span
        # the full content width — a third of it is plenty for their label
        # text and reads cleaner (matches the length marked in the reference
        # screenshot). The full-width row_w above is still used for the
        # status toggle line, the CHANGE DISC row, and other full-width rows.
        short_row_w = content_w // 3

        # Count active consoles to know if this is the last one — mirrors the
        # Channels sub-menu "last active channel" lock (05-44). DVD is the
        # console-neutral, non-BIOS default, same role channel 05 plays there.
        _active_console_count = 0
        if db is not None:
            from game_deck import CONSOLE_ORDER as _CON_ORDER
            _con_enabled_map = db.config.get("consoles_enabled", {})
            for _c in _CON_ORDER:
                if _con_enabled_map.get(_c, _c == "DVD"):
                    _active_console_count += 1
        _is_last_console = is_on and (_active_console_count <= 1)

        # ── Row 0: Status toggle ──────────────────────────────────────────────
        # is_toggle_focused is False when locked (greyed item gets NO highlight)
        is_toggle_focused = (not _is_last_console) and (sub_row == 0)
        _toggle_label_color = (90, 90, 90) if _is_last_console else (NEON_GREEN if is_toggle_focused else TEXT_WHITE)
        lbl_toggle_txt = f_body.render("Status:", True, _toggle_label_color)
        menu_canvas.blit(lbl_toggle_txt, (cx + 25, top_y + 4))

        # Greyed out pill when locked; normal colors otherwise
        if _is_last_console:
            sw_bg = (55, 90, 55)   # muted green - on but locked
            sw_handle_col = (130, 130, 130)
            # Draw lock icon (small padlock shape using rect+circle)
            pygame.draw.rect(menu_canvas, (80, 80, 80), (cx + 158, top_y + 4, 10, 8), border_radius=2)
            pygame.draw.circle(menu_canvas, (80, 80, 80), (cx + 163, top_y + 4), 4, 2)
        else:
            sw_bg = OFF_GREEN if is_on else (45, 65, 100)
            sw_handle_col = TEXT_WHITE

        pygame.draw.rect(menu_canvas, sw_bg, (cx + 95, top_y, 55, 24), border_radius=12)
        if is_toggle_focused:
            pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + 95, top_y, 55, 24), 2, border_radius=12)
        sw_handle_x = cx + 137 if is_on else cx + 107
        pygame.draw.circle(menu_canvas, sw_handle_col, (sw_handle_x, top_y + 12), 9)
        if is_toggle_focused:
            pygame.draw.circle(menu_canvas, NEON_GREEN, (sw_handle_x, top_y + 12), 11, 2)

        # Tooltip when locked
        if _is_last_console:
            _f_tip = _get_font("Courier New", 11, bold=True)
            _lbl_tip = _f_tip.render("Last active console — enable another console first", True, (140, 100, 60))
            menu_canvas.blit(_lbl_tip, (cx + 25, top_y + 28))

        if not is_on:
            # Console is OFF — show message and only Close button, same as channel sub-menu
            f_msg = _get_font("Courier New", 18, bold=True)
            lbl_msg1 = f_msg.render("CONSOLE IS CURRENTLY DISABLED", True, OFF_RED)
            lbl_msg2 = f_msg.render("Enable the console to access settings", True, TEXT_WHITE)
            msg_y = top_y + cell_h + 30
            menu_canvas.blit(lbl_msg1, (cx + content_w // 2 - lbl_msg1.get_width() // 2, msg_y))
            menu_canvas.blit(lbl_msg2, (cx + content_w // 2 - lbl_msg2.get_width() // 2, msg_y + 34))

            is_close_focused = (sub_row == 1)
            pygame.draw.rect(menu_canvas, OFF_RED, (cx + content_w - 110, content_y + content_h - 42, 85, 30), border_radius=4)
            if is_close_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + content_w - 110, content_y + content_h - 42, 85, 30), 2, border_radius=4)
            lbl_cl = f_tab.render("Close", True, TEXT_WHITE)
            menu_canvas.blit(lbl_cl, (cx + content_w - 85, content_y + content_h - 35))
        elif console_key == "DVD":
            # DVD is a video player, not an emulator — no core to select, no
            # controller mapping, no save-state profiles. It DOES get one blue
            # action button matching the emulator sub-menu buttons: "Button
            # Mapping", which opens the DVD transport-key remap popup. Rows here
            # are: 0=Status (above), 1=Button Mapping, 2=Close.
            btn_y = top_y + cell_h + y_gap
            is_map_focused = (sub_row == 1)
            pygame.draw.rect(menu_canvas, (25, 45, 80), (row_x, btn_y, short_row_w, cell_h), border_radius=6)
            if is_map_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (row_x, btn_y, short_row_w, cell_h), 2, border_radius=6)
            lbl_map = f_body.render("BUTTON MAPPING", True, NEON_GREEN if is_map_focused else TEXT_WHITE)
            menu_canvas.blit(lbl_map, (row_x + 20, btn_y + cell_h // 2 - lbl_map.get_height() // 2))

            is_close_focused = (sub_row == 2)
            pygame.draw.rect(menu_canvas, OFF_RED, (cx + content_w - 110, content_y + content_h - 42, 85, 30), border_radius=4)
            if is_close_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + content_w - 110, content_y + content_h - 42, 85, 30), 2, border_radius=4)
            lbl_cl = f_tab.render("Close", True, TEXT_WHITE)
            menu_canvas.blit(lbl_cl, (cx + content_w - 85, content_y + content_h - 35))
        else:
            # Console is ON — show full options
            # sub_rows is the single source of truth (see
            # get_games_sub_menu_rows in this module) for which rows exist
            # below the Status toggle right now — Kiosk Mode hides
            # "core_select" specifically, since that's the row that lets a
            # kid browse the filesystem and swap in a different core .dll.
            sub_rows = get_games_sub_menu_rows(db, app_state, console_key, is_on)
            full_rows = ["status"] + sub_rows + ["close"]

            grid_row_ids = [r for r in sub_rows if r in ("core_select", "controller_map")]
            has_disc = "disc" in sub_rows

            grid_y = top_y + cell_h + y_gap

            row_labels = {
                "core_select":    "SELECT LIBRETRO CORE (.DLL)",
                "controller_map": "CONTROLLER MAPPING",
            }
            for g_idx, row_id in enumerate(grid_row_ids):
                ry = grid_y + g_idx * (cell_h + y_gap)
                row_idx = full_rows.index(row_id)
                is_focused = (sub_row == row_idx)
                pygame.draw.rect(menu_canvas, (25, 45, 80), (row_x, ry, short_row_w, cell_h), border_radius=6)
                if is_focused:
                    pygame.draw.rect(menu_canvas, NEON_GREEN, (row_x, ry, short_row_w, cell_h), 2, border_radius=6)
                lbl_r = f_body.render(row_labels[row_id], True, NEON_GREEN if is_focused else TEXT_WHITE)
                menu_canvas.blit(lbl_r, (row_x + 20, ry + cell_h // 2 - lbl_r.get_height() // 2))

            # ── Save profiles ────────────────────────────────────────────────
            all_profiles_cfg = db.config.get("console_profiles", {}) if db else {}
            con_prof_cfg     = all_profiles_cfg.get(console_key, {})
            active_profile   = int(con_prof_cfg.get("active", 1))

            prof_section_y = grid_y + len(grid_row_ids) * (cell_h + y_gap) + 8
            f_prof_label = _get_font("Courier New", 13, bold=True)
            sec_lbl = f_prof_label.render("SAVE PROFILES", True, (120, 120, 120))
            menu_canvas.blit(sec_lbl, (row_x + 20, prof_section_y))
            prof_section_y += sec_lbl.get_height() + 4

            prof_cell_h = 38
            prof_gap    = 6
            for p_num in (1, 2, 3):
                row_idx = full_rows.index(f"profile{p_num}")
                ry      = prof_section_y + (p_num - 1) * (prof_cell_h + prof_gap)
                is_foc  = (sub_row == row_idx)
                is_act  = (active_profile == p_num)

                # All three rows share the same base style now — the row
                # itself no longer tints green for the active profile, only
                # the little status square does (see below). Focus is still
                # shown the same way every other row in this menu shows it.
                bg_col  = (25, 35, 55)
                bdr_col = NEON_GREEN if is_foc else (40, 60, 90)
                pygame.draw.rect(menu_canvas, bg_col,  (row_x, ry, short_row_w, prof_cell_h), border_radius=5)
                pygame.draw.rect(menu_canvas, bdr_col, (row_x, ry, short_row_w, prof_cell_h), 2, border_radius=5)

                txt_col = NEON_GREEN if is_foc else TEXT_WHITE
                lbl_p   = f_body.render(f"PROFILE  {p_num}", True, txt_col)
                lbl_x   = row_x + 20
                lbl_y   = ry + prof_cell_h // 2 - lbl_p.get_height() // 2
                menu_canvas.blit(lbl_p, (lbl_x, lbl_y))

                # Status square — filled green for the active profile, a
                # hollow grey outline for the others. This is now the only
                # thing that changes when the active profile switches; the
                # word "ACTIVE" moves along with it.
                sq_size = 14
                sq_x = lbl_x + lbl_p.get_width() + 14
                sq_y = ry + prof_cell_h // 2 - sq_size // 2
                if is_act:
                    pygame.draw.rect(menu_canvas, (80, 220, 120), (sq_x, sq_y, sq_size, sq_size), border_radius=3)
                else:
                    pygame.draw.rect(menu_canvas, (90, 90, 110), (sq_x, sq_y, sq_size, sq_size), 2, border_radius=3)

                if is_act:
                    lbl_active = f_body.render("ACTIVE", True, txt_col)
                    menu_canvas.blit(lbl_active, (sq_x + sq_size + 8, lbl_y))

            # ── PSX only: live multi-disc swap ──────────────────────────────
            # Only meaningful for PSX — other consoles have no optical media —
            # and only actionable once a multi-disc game is actually detected
            # around whatever PSX game is currently loaded/live. When no set is
            # detected the row still shows (so the option is discoverable) but
            # is greyed out and won't respond to A/D/Enter.
            disc_row_y = prof_section_y + 3 * (prof_cell_h + prof_gap) + 10
            if has_disc:
                import sys as _sys
                _main = _sys.modules.get("__main__")
                _gd   = getattr(_main, "game_deck", None) if _main else None
                discs, cur_idx = _gd.get_psx_disc_info() if (_gd is not None and hasattr(_gd, "get_psx_disc_info")) else ([], -1)

                disc_row_idx = full_rows.index("disc")
                is_disc_focused = (sub_row == disc_row_idx)
                disc_available  = bool(discs)
                bg_col  = (25, 45, 80) if disc_available else (20, 20, 28)
                bdr_col = NEON_GREEN if is_disc_focused else ((40, 60, 90) if disc_available else (45, 45, 55))
                pygame.draw.rect(menu_canvas, bg_col, (row_x, disc_row_y, row_w, cell_h), border_radius=6)
                pygame.draw.rect(menu_canvas, bdr_col, (row_x, disc_row_y, row_w, cell_h), 2, border_radius=6)
                txt_col = NEON_GREEN if (is_disc_focused and disc_available) else (TEXT_WHITE if disc_available else (120, 120, 130))
                lbl_disc = f_body.render("CHANGE DISC", True, txt_col)
                menu_canvas.blit(lbl_disc, (row_x + 20, disc_row_y + cell_h // 2 - lbl_disc.get_height() // 2))

                if disc_available:
                    val_str = f"DISC {cur_idx + 1} / {len(discs)}" if cur_idx >= 0 else f"{len(discs)} DISCS FOUND"
                    if is_disc_focused:
                        val_str = f"<  {val_str}  >"
                    val_col = (200, 255, 200) if is_disc_focused else TEXT_WHITE
                else:
                    val_str = "NO MULTI-DISC GAME LOADED"
                    val_col = (120, 120, 130)
                lbl_val = f_body.render(val_str, True, val_col)
                menu_canvas.blit(lbl_val, (row_x + row_w - lbl_val.get_width() - 16,
                                            disc_row_y + cell_h // 2 - lbl_val.get_height() // 2))

            # ── Close button ─────────��────────────────────────────────────────
            close_row = full_rows.index("close")
            is_close_focused = (sub_row == close_row)
            pygame.draw.rect(menu_canvas, OFF_RED, (cx + content_w - 110, content_y + content_h - 42, 85, 30), border_radius=4)
            if is_close_focused:
                pygame.draw.rect(menu_canvas, NEON_GREEN, (cx + content_w - 110, content_y + content_h - 42, 85, 30), 2, border_radius=4)
            lbl_cl = f_tab.render("Close", True, TEXT_WHITE)
            menu_canvas.blit(lbl_cl, (cx + content_w - 85, content_y + content_h - 35))



    # ==========================================================================
    # GAMES_SAVE_PROFILE: per-console profile sub-menu
    # ==========================================================================
    elif active_tab == "Games" and layer == "GAMES_SAVE_PROFILE":
        import os
        from game_deck import CONSOLE_LABELS, HANDHELD_CONSOLES, BORDER_CONSOLES, border_available_for_console, SCREEN_SIZE_OPTIONS

        console_key = app_state.get("games_selected_console", "DVD")
        profile_num = int(app_state.get("games_profile_num", 1))
        profile_row = int(app_state.get("games_profile_row", 0))
        is_handheld = console_key in HANDHELD_CONSOLES
        aspect_ratio_now = db.config.get("aspect_ratio", "16:9") if db else "16:9"
        is_border_console = border_available_for_console(console_key, aspect_ratio_now)
        back_row    = 5 + (1 if is_handheld else 0) + (1 if is_border_console else 0)

        # Read active profile + this profile's settings from db
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
        # "gb_border" is the old GB-only config key; still read as a fallback
        # so an existing GB setting doesn't appear to reset. Default is ON
        # for a profile that's never explicitly set this.
        border_on        = bool(this_prof.get("border_enabled", this_prof.get("gb_border", True)))

        # ── Title ─────────────────────────────────────────────────────────────
        # Built from separate pieces (instead of one string with a "★" glyph
        # that Courier New doesn't render) so a real drawn square can sit
        # inline between "PROFILE N" and "ACTIVE" — matches the ACTIVE
        # PROFILE row below and the profile list this page was opened from.
        f_ptitle = _get_font("Courier New", 18, bold=True)
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
            pygame.draw.rect(menu_canvas, (80, 220, 120), (cur_x, sq_y, title_sq_size, title_sq_size), border_radius=3)
            cur_x += title_sq_size + title_gap
            menu_canvas.blit(lbl_active_word, (cur_x, title_y))
            cur_x += lbl_active_word.get_width()
        menu_canvas.blit(lbl_suffix, (cur_x, title_y))

        top_y  = content_y + 14 + lbl_prefix.get_height() + 18
        row_x2 = cx + 35
        row_w2 = content_w // 3
        cell_h2 = 46
        gap2    = 10

        def _draw_prof_row(idx, label, y_pos, toggle_val=None, greyed=False, value_text=None, active_square=None):
            is_foc = (profile_row == idx)
            bg   = (30, 50, 30) if is_foc else (20, 30, 50)
            bdr  = NEON_GREEN  if is_foc else (50, 70, 100)
            if greyed:
                bg  = (22, 22, 30)
                bdr = (45, 45, 55)
            pygame.draw.rect(menu_canvas, bg,  (row_x2, y_pos, row_w2, cell_h2), border_radius=5)
            pygame.draw.rect(menu_canvas, bdr, (row_x2, y_pos, row_w2, cell_h2), 2, border_radius=5)
            txt_col = NEON_GREEN if is_foc and not greyed else ((130, 130, 150) if greyed else TEXT_WHITE)
            label_x = row_x2 + 20
            if active_square is not None:
                # Status square instead of the old unrendered "★" glyph —
                # filled green when this profile is the active one.
                sq_size = 14
                sq_y = y_pos + cell_h2 // 2 - sq_size // 2
                if active_square:
                    pygame.draw.rect(menu_canvas, (80, 220, 120), (label_x, sq_y, sq_size, sq_size), border_radius=3)
                else:
                    pygame.draw.rect(menu_canvas, (90, 90, 110), (label_x, sq_y, sq_size, sq_size), 2, border_radius=3)
                label_x += sq_size + 10
            lbl_row = f_body.render(label, True, txt_col)
            menu_canvas.blit(lbl_row, (label_x, y_pos + cell_h2 // 2 - lbl_row.get_height() // 2))
            if toggle_val is not None:
                # Draw pill toggle on the right side
                sw_bg  = OFF_GREEN if toggle_val else (45, 65, 100)
                if greyed:
                    sw_bg = (35, 35, 45)
                sw_w, sw_h = 50, 22
                sw_x = row_x2 + row_w2 - sw_w - 16
                sw_y = y_pos + cell_h2 // 2 - sw_h // 2
                pygame.draw.rect(menu_canvas, sw_bg, (sw_x, sw_y, sw_w, sw_h), border_radius=11)
                hx = sw_x + sw_w - 14 if toggle_val else sw_x + 4
                pygame.draw.circle(menu_canvas, TEXT_WHITE if not greyed else (80, 80, 90),
                                   (hx + 7, sw_y + sw_h // 2), 9)
                on_lbl = _get_font("Courier New", 10, bold=True).render(
                    "ON" if toggle_val else "OFF", True,
                    (200, 255, 200) if toggle_val and not greyed else (120, 120, 140))
                menu_canvas.blit(on_lbl, (sw_x - on_lbl.get_width() - 6, sw_y + sw_h // 2 - on_lbl.get_height() // 2))
            elif value_text is not None:
                # Draw "<  VALUE  >" cycle indicator on the right side
                val_col = (200, 255, 200) if (is_foc and not greyed) else ((120, 120, 140) if greyed else TEXT_WHITE)
                val_str = f"<  {value_text}  >" if (is_foc and not greyed) else value_text
                lbl_val = _get_font("Courier New", 14, bold=True).render(val_str, True, val_col)
                menu_canvas.blit(lbl_val, (row_x2 + row_w2 - lbl_val.get_width() - 16,
                                           y_pos + cell_h2 // 2 - lbl_val.get_height() // 2))

        # Row 0: Set as Active Profile — status square only shows once this
        # profile actually is the active one; otherwise it's just the plain
        # "SET AS ACTIVE PROFILE" action button, same as before.
        act_label = "ACTIVE PROFILE" if is_active else "SET AS ACTIVE PROFILE"
        _draw_prof_row(0, act_label, top_y, active_square=(True if is_active else None))

        # Row 1: Save Now
        _draw_prof_row(1, "SAVE NOW", top_y + (cell_h2 + gap2))

        # Row 2: Load Now
        _draw_prof_row(2, "LOAD NOW", top_y + 2 * (cell_h2 + gap2))

        # Row 3: Auto-Save toggle (greyed when not active profile)
        _draw_prof_row(3, "AUTO-SAVE", top_y + 3 * (cell_h2 + gap2),
                       toggle_val=auto_save_on, greyed=not is_active)

        # Row 4: Auto-Load toggle (greyed when not active profile)
        _draw_prof_row(4, "AUTO-LOAD", top_y + 4 * (cell_h2 + gap2),
                       toggle_val=auto_load_on, greyed=not is_active)

        # Row 5: Screen Size (see HANDHELD_CONSOLES — now every emulated
        # console except DVD; greyed when not active profile)
        next_idx = 5
        if is_handheld:
            _draw_prof_row(next_idx, "SCREEN SIZE", top_y + next_idx * (cell_h2 + gap2),
                           value_text=screen_size_lbl, greyed=not is_active)
            next_idx += 1

        # TV Border toggle — hidden entirely (not shown as a row) for CRT-bezel
        # home/arcade consoles on a detected 4:3 display; see border_available_for_console()
        if is_border_console:
            _draw_prof_row(next_idx, "TV BORDER", top_y + next_idx * (cell_h2 + gap2),
                           toggle_val=border_on, greyed=not is_active)
            next_idx += 1

        # Back
        is_back_foc = (profile_row == back_row)
        pygame.draw.rect(menu_canvas, OFF_RED,
                         (cx + content_w - 110, content_y + content_h - 42, 85, 30), border_radius=4)
        if is_back_foc:
            pygame.draw.rect(menu_canvas, NEON_GREEN,
                             (cx + content_w - 110, content_y + content_h - 42, 85, 30), 2, border_radius=4)
        lbl_bk = f_tab.render("Back", True, TEXT_WHITE)
        menu_canvas.blit(lbl_bk, (cx + content_w - 88, content_y + content_h - 35))

        # Note if toggles are greyed
        if not is_active:
            note = _get_font("Courier New", 11).render(
                "Set as active profile to enable auto-save / auto-load", True, (90, 90, 110))
            menu_canvas.blit(note, (cx + content_w // 2 - note.get_width() // 2,
                                    content_y + content_h - 58))

    # ==========================================================================
    # MASTER VIEWPORT ANCHOR LAYER STAMP OPERATIONS
    # ==========================================================================
    if real_w >= w and real_h >= h:
        final_x = (real_w - w) // 2
        final_y = (real_h - h) // 2
        surface.blit(menu_canvas, (final_x, final_y))
    else:
        shrink_w = min(w, real_w - 20)
        shrink_h = int(shrink_w * (680.0 / 1036.0))
        if shrink_h > real_h - 20:
            shrink_h = real_h - 20
            shrink_w = int(shrink_h * (1036.0 / 680.0))
            
        scaled_stamp = pygame.transform.smoothscale(menu_canvas, (shrink_w, shrink_h))
        final_x = (real_w - shrink_w) // 2
        final_y = (real_h - shrink_h) // 2
        surface.blit(scaled_stamp, (final_x, final_y))

# ==============================================================================
# PART 15 OF 15: RETRO TERMINAL PROMPT CLOSURE & QUIT VERIFICATION OVERLAYS
# ==============================================================================

def render_quit_prompt(surface, theme, db=None):
    """
    Minimize confirmation popup. Fixed 650x220 canvas, smoothscaled if needed.
    Text matches the controls menu floor: 17pt bold Courier New minimum.
    ENTER toggles fullscreen<->windowed (see show_quit_confirm handler in
    retro_tv_emulator.py).
    """
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
    ARCADE_GOLD   = (255, 220, 0)
    TEXT_WHITE    = (255, 255, 255)
    ACTION_RED    = (255, 60, 60)

    slider_val   = db.config.get("menu_opacity", 50) if db is not None else 50
    menu_opacity = int((slider_val / 100.0) * 255)

    bg_surf = pygame.Surface((CANVAS_W, CANVAS_H), pygame.SRCALPHA)
    bg_surf.fill(MIDNIGHT_NAVY + (menu_opacity,))
    canvas.blit(bg_surf, (0, 0))
    pygame.draw.rect(canvas, NEON_GREEN, (0, 0, CANVAS_W, CANVAS_H), 2)

    f_title = _get_font("Courier New", 22, bold=True)
    f_body  = _get_font("Courier New", 17, bold=True)

    lbl1 = f_title.render("MINIMIZE PROGRAM?", True, ARCADE_GOLD)
    lbl2 = f_body.render("PRESS  [ ESCAPE ]  TO RESUME", True, TEXT_WHITE)
    lbl3 = f_body.render("PRESS  [ ENTER ]  TO TOGGLE WINDOW", True, ACTION_RED)

    canvas.blit(lbl1, (CANVAS_W//2 - lbl1.get_width()//2, 30))
    canvas.blit(lbl2, (CANVAS_W//2 - lbl2.get_width()//2, 100))
    canvas.blit(lbl3, (CANVAS_W//2 - lbl3.get_width()//2, 140))

    # Stamp onto screen — same logic as main menu
    if real_w >= CANVAS_W and real_h >= CANVAS_H:
        surface.blit(canvas, ((real_w - CANVAS_W)//2, (real_h - CANVAS_H)//2))
    else:
        shrink_w = min(CANVAS_W, real_w - 20)
        shrink_h = int(shrink_w * (CANVAS_H / CANVAS_W))
        if shrink_h > real_h - 20:
            shrink_h = real_h - 20
            shrink_w = int(shrink_h * (CANVAS_W / CANVAS_H))
        scaled = pygame.transform.smoothscale(canvas, (shrink_w, shrink_h))
        surface.blit(scaled, ((real_w - shrink_w)//2, (real_h - shrink_h)//2))
