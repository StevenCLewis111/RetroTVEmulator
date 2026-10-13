"""
Menu navigation core: one table per menu layer says where each highlight cell leads for up, down, left and right, and one function moves the highlight by it.

Owns: the direction names, key_to_direction(), key_to_action(), action_keys(), action_key() and pressed_actions() (the one place a raw key becomes a direction, accept or back, and back again), is_accept(), is_back(), the slider rules (SLIDER_SPECS, slider_adjust), NavTable and Remember (the table and the
"go back to where you came from" rule), navigate() (the one move function), and the tables and cell/index converters for the
System tab (system_table, system_cell, system_index), Advanced Settings (advanced_table, advanced_cell, advanced_index), the Channels tab
(channels_table, channels_cell, channels_index, channels_page_of), Channel settings (channel_settings_table, channel_settings_cell,
channel_settings_index), the Video tab (video_table, video_cell, video_index) and the Theme tab (theme_table, theme_cell, theme_index,
theme_row2_column), the Games tab list (games_table, games_cell, games_index, games_page_of), the Games sub-menu (games_sub_table, games_sub_cell,
games_sub_index), the DVD button map (dvd_map_table, dvd_map_cell, dvd_map_index), Save Profile (save_profile_rows, save_profile_table, save_profile_cell,
save_profile_index) and the controller map (ctrl_map_rows, ctrl_map_table, ctrl_map_cell, ctrl_map_index, ctrl_map_squares_step),
the confirm popups and the controls splash (confirm_cell, confirm_answer, confirm_focus, confirm_table, splash_cell, splash_focus, splash_table), the Channels per-channel sub-menu (channel_sub_layout, channel_sub_close_row, channel_sub_cell, channel_sub_index, channel_sub_table), the tab bar (tabs_cell, tabs_table, TAB_PANEL_LAYERS), the single-column lists of the Server menu, server picker, server slot actions, audio track picker and Holiday sub-menu (column_move), the wrapping Remote Remap list (ring_move), the Server Slots grid (server_slots_cell, server_slots_index, server_slots_table), the TV guide (guide_move, guide_row_step) the game channel (deck_browse_step, deck_list_step, deck_arrows_step, deck_confirm_choice), held-key repeat (HOLD_RULES, HOLD_STUCK_MS, HoldState, hold_rule, hold_due, hold_stuck, hold_step, hold_pick, hold_released, hold_conflict, repeat_allowed, press_is_duplicate), the joystick (JOY_*, joystick_action), channel number entry (DIGIT_ENTRY_*, key_to_digit, digit_entry_step, digit_entry_due, digit_entry_channel), name entry for the channel name and the server slot name (NAME_*, name_entry_step), the DVD controls (DVD_* ids, canonical codes, default keys and skip numbers, dvd_canonical, dvd_default_binding, dvd_action, dvd_translate, dvd_return_menu_step, dvd_swallows_transport, dvd_skip_blocked, dvd_tap_seek, dvd_hold_seek, dvd_hold_direction, dvd_hold_due), and the file explorer's mouse and keyboard rules (PARENT, the EXPLORER_* numbers, explorer_wheel, explorer_wheel_button, explorer_thumb, explorer_scroll_from_thumb, explorer_scrollbar_press, explorer_scrollbar_drag, explorer_select, explorer_double_click, explorer_drag_moved, explorer_drag_step, explorer_button_at and the row and hit helpers).
Does not own: any drawing, any pygame event, any selection number stored in app_state (the callers convert with the cell/index functions), what an explorer button or row does once it is picked.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ============================================================================
# SECTION: Directions and the table
# ============================================================================

UP = "up"
DOWN = "down"
LEFT = "left"
RIGHT = "right"
DIRECTIONS = (UP, DOWN, LEFT, RIGHT)
ACCEPT = "accept"
BACK = "back"
PARENT = "parent"

_OPPOSITE = {UP: DOWN, DOWN: UP, LEFT: RIGHT, RIGHT: LEFT}

TAB_BAR = "@tab_bar"
PANEL = "@panel"
CLOSE = "close"


@dataclass(frozen=True)
class Remember:
    """A target that leads back to the cell the highlight last came from (when it came from a listed cell), else to the default."""
    default: str


@dataclass
class NavTable:
    """Where each cell leads: cells maps a cell name to {direction: target}; remember maps a cell to the cells worth remembering as its way back; alias maps a side button to the main cell it is remembered as."""
    cells: dict = field(default_factory=dict)
    remember: dict = field(default_factory=dict)
    alias: dict = field(default_factory=dict)

    def link(self, cell, direction, target):
        """Sets where `direction` leads from `cell`; a None target leaves the highlight where it is."""
        if target is None:
            return
        self.cells.setdefault(cell, {})[direction] = target

    def known(self, *cells):
        """Registers cells so they are valid destinations even when nothing leads out of them."""
        for cell in cells:
            self.cells.setdefault(cell, {})

    def _slot(self, cell, back):
        """Returns (memory key, allowed cells) for the way back out of `cell` in direction `back`; a remember entry that is a dict keeps one way back per direction."""
        allowed = self.remember.get(cell, ())
        if isinstance(allowed, dict):
            return (cell, back), allowed.get(back, ())
        return cell, allowed

    def step(self, cell, direction, memory):
        """Returns the cell the highlight moves to, TAB_BAR, PANEL, or None when it stays; records the way back in `memory`."""
        entry = self.cells.get(cell)
        if entry is None:
            return None
        target = entry.get(direction)
        if isinstance(target, Remember):
            key, allowed = self._slot(cell, direction)
            wanted = memory.get(key)
            target = wanted if wanted in allowed and wanted in self.cells else target.default
        if target is None or target == cell:
            return None
        if target not in (TAB_BAR, PANEL) and target not in self.cells:
            return None
        if target not in (TAB_BAR, PANEL):
            recorded = self.alias.get(cell, cell)
            key, allowed = self._slot(target, _OPPOSITE[direction])
            if recorded in allowed:
                memory[key] = recorded
        return target


def _key_actions(arrows=False, parent=False):
    """Returns the table of raw keys and the action each means: W, S, A, D (and the four arrow keys when arrows is True), Enter and Escape (and Backspace as PARENT when parent is True)."""
    import pygame
    keys = {pygame.K_w: UP, pygame.K_s: DOWN, pygame.K_a: LEFT, pygame.K_d: RIGHT}
    if arrows:
        keys.update({pygame.K_UP: UP, pygame.K_DOWN: DOWN, pygame.K_LEFT: LEFT, pygame.K_RIGHT: RIGHT})
    keys.update({pygame.K_RETURN: ACCEPT, pygame.K_ESCAPE: BACK})
    if parent:
        keys[pygame.K_BACKSPACE] = PARENT
    return keys


def key_to_direction(key, arrows=False):
    """Returns the direction for a (remote-translated) key: W, S, A or D, else None; with arrows=True the four arrow keys count too."""
    action = _key_actions(arrows).get(key)
    return action if action in DIRECTIONS else None


def key_to_action(key, arrows=False, parent=False):
    """Returns the action for a (remote-translated) key: a direction, ACCEPT for Enter, BACK for Escape, else None; with parent=True Backspace is PARENT."""
    return _key_actions(arrows, parent).get(key)


def action_keys(action, arrows=False):
    """Returns the keys that mean an action, W, S, A, D, Enter or Escape first and then the arrow key when arrows is True."""
    return tuple(key for key, meant in _key_actions(arrows).items() if meant == action)


def action_key(action):
    """Returns the one key that stands for an action when the app has to post it (W, S, A, D, Enter, Escape)."""
    return action_keys(action)[0]


def pressed_actions(keys, arrows=False):
    """Returns the set of actions whose key is down in a pygame key-state matrix (pygame.key.get_pressed())."""
    return {action for key, action in _key_actions(arrows).items() if keys[key]}


def is_accept(key):
    """Returns True when the key is the accept key."""
    return key_to_action(key) == ACCEPT


def is_back(key):
    """Returns True when the key is the back key."""
    return key_to_action(key) == BACK


# WARNING: layers where a held key must never repeat: their handlers treat every key as input to capture or map.
NO_REPEAT_LAYERS = ("GAMES_CTRL_MAP", "DVD_BTN_MAP", "REMOTE_REMAP_CAPTURE", "NUMBER_REMAP_CAPTURE")

# VALUES CHANGE HERE (milliseconds: a second press of one key with no release between counts as the same press inside the first window; the same press arriving by a different key code counts as one inside the second)
NAV_DUP_HELD_MS = 250
NAV_DUP_PATH_MS = 50


def repeat_allowed(layer, key, suppressed=False):
    """Returns True when a held W/A/S/D may auto-repeat in a menu layer; suppressed is True while a popup or text entry owns the keys."""
    import pygame
    if suppressed or layer in NO_REPEAT_LAYERS:
        return False
    return key in (pygame.K_w, pygame.K_a, pygame.K_s, pygame.K_d)


def press_is_duplicate(since_ms, same_key_code, still_down):
    """Returns True when a key press arriving since_ms after the last accepted press of the same action is a second delivery of it: the key never came up (still_down) inside NAV_DUP_HELD_MS, or it came by a different key code inside NAV_DUP_PATH_MS."""
    if still_down and since_ms < NAV_DUP_HELD_MS:
        return True
    return bool(not same_key_code and since_ms < NAV_DUP_PATH_MS)


def navigate(app_state, layer, table, cell, direction):
    """Returns the cell the highlight moves to, TAB_BAR, PANEL, or None; the way back is kept in app_state per layer."""
    memory = app_state.setdefault("_nav_memory", {}).setdefault(layer, {})
    return table.step(cell, direction, memory)


# ============================================================================
# SECTION: System tab
# ============================================================================

# WARNING: these two groups must match LEFT_TOGGLE_IDS and RIGHT_TOGGLE_IDS in the System tab drawing in ui.py, or the table and the picture disagree about which column a row is in.
SYSTEM_LEFT_TOGGLES = ("game_ch", "guide", "controls_on_start", "kiosk_mode")
SYSTEM_RIGHT_TOGGLES = ("loudness_scan", "pixel_orbiter", "captions_toggle", "mono_stereo_toggle")

SYSTEM_SERVER_BUTTON_SELECTION = 500
SYSTEM_CAPTIONS_COLOR_SELECTION = 502
SYSTEM_CAPTIONS_COLOR_CELL = "captions_color"

_system_tables = {}


def system_cell(rows, selection):
    """Returns the cell name for a System tab selection number: a row name, CLOSE, the captions colour box, or None for anything else."""
    if selection == SYSTEM_CAPTIONS_COLOR_SELECTION:
        return SYSTEM_CAPTIONS_COLOR_CELL
    if selection == SYSTEM_SERVER_BUTTON_SELECTION:
        return None
    if isinstance(selection, int) and 0 <= selection < len(rows):
        return rows[selection]
    return CLOSE


def system_index(rows, cell):
    """Returns the System tab selection number for a cell name."""
    if cell == SYSTEM_CAPTIONS_COLOR_CELL:
        return SYSTEM_CAPTIONS_COLOR_SELECTION
    if cell == CLOSE:
        return len(rows)
    return rows.index(cell)


def system_table(rows):
    """Returns the cached NavTable for the System tab rows."""
    key = tuple(rows)
    table = _system_tables.get(key)
    if table is None:
        table = _build_system_table(list(rows))
        _system_tables[key] = table
    return table


def _build_system_table(rows):
    left = [r for r in rows if r in SYSTEM_LEFT_TOGGLES]
    right = [r for r in rows if r in SYSTEM_RIGHT_TOGGLES]
    has_advanced = "advanced_settings" in rows
    poweroff_standalone = "poweroff" in rows and has_advanced
    bottom = [r for r in rows
              if r not in left and r not in right and r != "advanced_settings"
              and not (r == "poweroff" and poweroff_standalone)]
    t = NavTable()
    t.known(CLOSE, *rows)
    if "captions_toggle" in right:
        t.known(SYSTEM_CAPTIONS_COLOR_CELL)

    below_columns = "advanced_settings" if has_advanced else (bottom[0] if bottom else CLOSE)
    above_bottom = "advanced_settings" if has_advanced else (left[-1] if left else TAB_BAR)

    for i, row in enumerate(left):
        t.link(row, UP, TAB_BAR if i == 0 else left[i - 1])
        t.link(row, DOWN, left[i + 1] if i + 1 < len(left) else below_columns)
        if right:
            t.link(row, RIGHT, right[min(i, len(right) - 1)])

    for j, row in enumerate(right):
        t.link(row, UP, TAB_BAR if j == 0 else right[j - 1])
        if j + 1 < len(right):
            t.link(row, DOWN, right[j + 1])
        else:
            t.link(row, DOWN, "poweroff" if poweroff_standalone else below_columns)
        if left:
            t.link(row, LEFT, left[min(j, len(left) - 1)])
        if row == "captions_toggle":
            t.link(row, RIGHT, SYSTEM_CAPTIONS_COLOR_CELL)

    if "captions_toggle" in right:
        c = right.index("captions_toggle")
        t.link(SYSTEM_CAPTIONS_COLOR_CELL, UP, TAB_BAR if c == 0 else right[c - 1])
        t.link(SYSTEM_CAPTIONS_COLOR_CELL, DOWN, "mono_stereo_toggle" if "mono_stereo_toggle" in rows else CLOSE)
        t.link(SYSTEM_CAPTIONS_COLOR_CELL, LEFT, "captions_toggle")

    if has_advanced:
        if "kiosk_mode" in rows:
            t.link("advanced_settings", UP, Remember("kiosk_mode"))
            sources = tuple(s for s in ("kiosk_mode", "mono_stereo_toggle") if s in rows)
            t.remember["advanced_settings"] = sources
        t.link("advanced_settings", DOWN, bottom[0] if bottom else CLOSE)

    for k, row in enumerate(bottom):
        t.link(row, UP, above_bottom if k == 0 else bottom[k - 1])
        t.link(row, DOWN, bottom[k + 1] if k + 1 < len(bottom) else CLOSE)
        t.link(row, LEFT, bottom[k - 1] if k > 0 else None)
        t.link(row, RIGHT, bottom[k + 1] if k + 1 < len(bottom) else CLOSE)

    if poweroff_standalone:
        t.link("poweroff", UP, right[-1] if right else None)
        t.link("poweroff", DOWN, CLOSE)

    if "mono_stereo_toggle" in rows:
        close_up = "mono_stereo_toggle"
    elif "captions_toggle" in rows:
        close_up = "captions_toggle"
    else:
        close_up = rows[-1] if rows else None
    t.link(CLOSE, UP, close_up)
    t.link(CLOSE, LEFT, bottom[-1] if bottom else None)
    return t


# ============================================================================
# SECTION: Advanced Settings
# ============================================================================

_advanced_tables = {}


def advanced_cell(rows, selection):
    """Returns the cell name for an Advanced Settings selection number: a row name or CLOSE."""
    if isinstance(selection, int) and 0 <= selection < len(rows):
        return rows[selection]
    return CLOSE


def advanced_index(rows, cell):
    """Returns the Advanced Settings selection number for a cell name."""
    if cell == CLOSE:
        return len(rows)
    return rows.index(cell)


def advanced_table(rows):
    """Returns the cached NavTable for the Advanced Settings rows."""
    key = tuple(rows)
    table = _advanced_tables.get(key)
    if table is None:
        table = _build_advanced_table(list(rows))
        _advanced_tables[key] = table
    return table


def _build_advanced_table(rows):
    visible = [r for r in rows if r != "server_menu_open"]
    has_menu_button = "server_menu_open" in rows and "server_option" in rows
    t = NavTable()
    t.known(CLOSE, *rows)
    for i, row in enumerate(visible):
        t.link(row, UP, visible[i - 1] if i > 0 else None)
        t.link(row, DOWN, visible[i + 1] if i + 1 < len(visible) else CLOSE)
    if has_menu_button:
        i = visible.index("server_option")
        t.link("server_option", RIGHT, "server_menu_open")
        t.link("server_menu_open", LEFT, "server_option")
        t.link("server_menu_open", UP, visible[i - 1] if i > 0 else None)
        t.link("server_menu_open", DOWN, visible[i + 1] if i + 1 < len(visible) else CLOSE)
    t.link(CLOSE, UP, visible[-1] if visible else None)
    return t


# ============================================================================
# SECTION: Channels tab
# ============================================================================

# WARNING: these four numbers must match cols, rows_per_page and range(5, 45) in the Channels tab drawing in ui.py, or the table and the picture disagree about which channel is where.
CHANNELS_COLS = 4
CHANNELS_ROWS_PER_PAGE = 5
CHANNELS_TOTAL = 40
CHANNELS_PAGE_SIZE = CHANNELS_COLS * CHANNELS_ROWS_PER_PAGE
CHANNELS_NUM_PAGES = max(1, (CHANNELS_TOTAL + CHANNELS_PAGE_SIZE - 1) // CHANNELS_PAGE_SIZE)

CHANNELS_FIRST_NUMBER = 5
CHANNELS_PAGE_BUTTON_SELECTION = 42
CHANNELS_SETTINGS_SELECTION = 43
CHANNELS_CLOSE_SELECTION = 99
CHANNELS_PAGE_BUTTON = "page_button"
CHANNELS_SETTINGS = "settings"

_channels_tables = {}


def channels_page_of(selection, stored_page=0):
    """Returns the page the highlight is on: the page of a channel cell, else the stored page, kept inside the page count."""
    if isinstance(selection, int) and 0 <= selection < CHANNELS_TOTAL:
        return selection // CHANNELS_PAGE_SIZE
    return max(0, min(int(stored_page or 0), CHANNELS_NUM_PAGES - 1))


def _channel_cell_name(index):
    return "ch%02d" % (index + CHANNELS_FIRST_NUMBER)


def channels_cell(selection):
    """Returns the cell name for a Channels tab selection number: a channel cell, the page button, Settings, CLOSE, or None for anything else."""
    if selection == CHANNELS_PAGE_BUTTON_SELECTION:
        return CHANNELS_PAGE_BUTTON
    if selection == CHANNELS_SETTINGS_SELECTION:
        return CHANNELS_SETTINGS
    if selection == CHANNELS_CLOSE_SELECTION:
        return CLOSE
    if isinstance(selection, int) and 0 <= selection < CHANNELS_TOTAL:
        return _channel_cell_name(selection)
    return None


def channels_index(cell):
    """Returns the Channels tab selection number for a cell name."""
    if cell == CHANNELS_PAGE_BUTTON:
        return CHANNELS_PAGE_BUTTON_SELECTION
    if cell == CHANNELS_SETTINGS:
        return CHANNELS_SETTINGS_SELECTION
    if cell == CLOSE:
        return CHANNELS_CLOSE_SELECTION
    return int(cell[2:]) - CHANNELS_FIRST_NUMBER


def channels_table(page):
    """Returns the cached NavTable for one page of the Channels tab."""
    page = max(0, min(int(page), CHANNELS_NUM_PAGES - 1))
    table = _channels_tables.get(page)
    if table is None:
        table = _build_channels_table(page)
        _channels_tables[page] = table
    return table


def _build_channels_table(page):
    start = page * CHANNELS_PAGE_SIZE
    end = min(start + CHANNELS_PAGE_SIZE, CHANNELS_TOTAL)
    cells = [_channel_cell_name(i) for i in range(start, end)]
    t = NavTable()
    t.known(CLOSE, CHANNELS_SETTINGS, CHANNELS_PAGE_BUTTON, *cells)
    left_bottom = []
    right_bottom = []
    for i in range(start, end):
        col = (i - start) % CHANNELS_COLS
        name = _channel_cell_name(i)
        t.link(name, UP, TAB_BAR if i - start < CHANNELS_COLS else _channel_cell_name(i - CHANNELS_COLS))
        if i + CHANNELS_COLS < end:
            t.link(name, DOWN, _channel_cell_name(i + CHANNELS_COLS))
        elif col < CHANNELS_COLS // 2:
            t.link(name, DOWN, CHANNELS_SETTINGS)
            left_bottom.append(name)
        else:
            t.link(name, DOWN, CHANNELS_PAGE_BUTTON)
            right_bottom.append(name)
        if col > 0:
            t.link(name, LEFT, _channel_cell_name(i - 1))
        if col < CHANNELS_COLS - 1 and i + 1 < end:
            t.link(name, RIGHT, _channel_cell_name(i + 1))
    first_of_last_row = start + ((end - 1 - start) // CHANNELS_COLS) * CHANNELS_COLS
    t.link(CHANNELS_SETTINGS, UP, Remember(_channel_cell_name(first_of_last_row)))
    t.remember[CHANNELS_SETTINGS] = tuple(left_bottom)
    t.link(CHANNELS_SETTINGS, DOWN, CLOSE)
    t.link(CHANNELS_SETTINGS, RIGHT, CHANNELS_PAGE_BUTTON)
    t.link(CHANNELS_PAGE_BUTTON, UP, Remember(_channel_cell_name(end - 1)))
    t.remember[CHANNELS_PAGE_BUTTON] = tuple(right_bottom)
    t.link(CHANNELS_PAGE_BUTTON, DOWN, CLOSE)
    t.link(CHANNELS_PAGE_BUTTON, LEFT, CHANNELS_SETTINGS)
    t.link(CLOSE, UP, Remember(CHANNELS_SETTINGS))
    t.remember[CLOSE] = (CHANNELS_SETTINGS, CHANNELS_PAGE_BUTTON)
    return t


# ============================================================================
# SECTION: Channel settings
# ============================================================================

# WARNING: these groups must match the grid in the Channel settings drawing in ui.py: the left column gets (n+1)//2 of the toggles, the bezel style box hangs off Bezel and Add hangs off Water Mark, and Reception Lines sits alone under both columns.
CHANNEL_SETTINGS_SECONDARY = {"bezel_style_select": "bezel_toggle", "watermark_select": "watermark"}
CHANNEL_SETTINGS_BOTTOM = "reception_lines"

_channel_settings_tables = {}


def channel_settings_cell(rows, selection):
    """Returns the cell name for a Channel settings selection number: a row name or CLOSE."""
    if isinstance(selection, int) and 0 <= selection < len(rows):
        return rows[selection]
    return CLOSE


def channel_settings_index(rows, cell):
    """Returns the Channel settings selection number for a cell name."""
    if cell == CLOSE:
        return len(rows)
    return rows.index(cell)


def channel_settings_table(rows):
    """Returns the cached NavTable for the Channel settings rows."""
    key = tuple(rows)
    table = _channel_settings_tables.get(key)
    if table is None:
        table = _build_channel_settings_table(list(rows))
        _channel_settings_tables[key] = table
    return table


def _build_channel_settings_table(rows):
    secondary_of = {s: p for s, p in CHANNEL_SETTINGS_SECONDARY.items() if s in rows and p in rows}
    attached = {p: s for s, p in secondary_of.items()}
    bottom = CHANNEL_SETTINGS_BOTTOM if CHANNEL_SETTINGS_BOTTOM in rows else None
    grid = [r for r in rows if r not in secondary_of and r != bottom]
    left_count = (len(grid) + 1) // 2
    columns = [grid[:left_count], grid[left_count:]]
    t = NavTable()
    t.known(CLOSE, *rows)

    def below_grid():
        return bottom if bottom else CLOSE

    for c, column in enumerate(columns):
        other = columns[1 - c]
        for k, row in enumerate(column):
            side = attached.get(row)
            up = column[k - 1] if k > 0 else None
            down = column[k + 1] if k + 1 < len(column) else below_grid()
            across = other[min(k, len(other) - 1)] if other else None
            for cell in (row, side):
                if cell is not None:
                    t.link(cell, UP, up)
                    t.link(cell, DOWN, down)
            if side is not None:
                t.link(row, RIGHT, side)
                t.link(side, LEFT, row)
            if c == 1:
                t.link(row, LEFT, across)
            else:
                t.link(side or row, RIGHT, across)

    ends = tuple(column[-1] for column in columns if column)
    if bottom:
        t.link(bottom, UP, Remember(ends[0] if ends else CLOSE))
        t.remember[bottom] = ends
        t.link(bottom, DOWN, CLOSE)
        t.link(CLOSE, UP, bottom)
    elif ends:
        t.link(CLOSE, UP, ends[0])
    return t


# ============================================================================
# SECTION: Video tab
# ============================================================================

# WARNING: this order must match the selection numbers in the Video tab drawing and key handler: 0 to 3 the four screen-mode radios, 4 to 8 the five sliders, then Close.
VIDEO_CELLS = ("mode_normal", "mode_stretch", "mode_crop", "mode_panscan",
               "brightness", "contrast", "color", "sharpness", "tint")
VIDEO_RADIOS = VIDEO_CELLS[0:4]
VIDEO_SLIDERS = VIDEO_CELLS[4:9]

_video_table = None


def video_cell(selection):
    """Returns the cell name for a Video tab selection number: a row name or CLOSE."""
    if isinstance(selection, int) and 0 <= selection < len(VIDEO_CELLS):
        return VIDEO_CELLS[selection]
    return CLOSE


def video_index(cell):
    """Returns the Video tab selection number for a cell name."""
    if cell == CLOSE:
        return len(VIDEO_CELLS)
    return VIDEO_CELLS.index(cell)


def video_table():
    """Returns the cached NavTable for the Video tab."""
    global _video_table
    if _video_table is None:
        _video_table = _build_video_table()
    return _video_table


def _build_video_table():
    t = NavTable()
    t.known(CLOSE, *VIDEO_CELLS)
    first_slider = VIDEO_SLIDERS[0]
    last_slider = VIDEO_SLIDERS[-1]
    for k, radio in enumerate(VIDEO_RADIOS):
        t.link(radio, UP, TAB_BAR)
        t.link(radio, DOWN, first_slider)
        t.link(radio, LEFT, VIDEO_RADIOS[k - 1] if k > 0 else None)
        t.link(radio, RIGHT, VIDEO_RADIOS[k + 1] if k + 1 < len(VIDEO_RADIOS) else None)
    t.link(first_slider, UP, Remember(VIDEO_RADIOS[0]))
    t.remember[first_slider] = VIDEO_RADIOS
    for k, slider in enumerate(VIDEO_SLIDERS):
        if k > 0:
            t.link(slider, UP, VIDEO_SLIDERS[k - 1])
        t.link(slider, DOWN, VIDEO_SLIDERS[k + 1] if k + 1 < len(VIDEO_SLIDERS) else CLOSE)
    t.link(CLOSE, UP, last_slider)
    return t


# ============================================================================
# SECTION: Theme tab
# ============================================================================

# WARNING: this order must match the selection numbers in the Theme tab drawing and key handler: 0 Volume Bar with Channel Static beside it, 1 Channel Transition with Letterboxing beside it, 2 Menu Opacity, 3 Background Color, 4 Border/Text Color, then Close. Each of the first two selections is a row of two cells; the tuple holds the left cell and THEME_RIGHT_CELLS holds the right one.
THEME_SELECTION_CELLS = ("volume_bar", "transition", "opacity", "bg_hue", "ui_hue")
THEME_STATIC = "static_color"
THEME_LETTERBOX = "letterbox"
THEME_RIGHT_CELLS = {0: THEME_STATIC, 1: THEME_LETTERBOX}
THEME_SLIDERS = ("opacity", "bg_hue", "ui_hue")

# VALUES CHANGE HERE: each slider is (config key, value when unset, lowest, highest); one left or right press moves SLIDER_STEP.
SLIDER_STEP = 5
SLIDER_SPECS = {
    "brightness": ("brightness", 50, 0, 100), "contrast": ("contrast", 50, 0, 100), "color": ("color", 50, 0, 100),
    "sharpness": ("sharpness", 50, 0, 100), "tint": ("tint", 50, 0, 100),
    "opacity": ("menu_opacity", 50, 0, 100), "bg_hue": ("theme_bg_hue", 220, 0, 360), "ui_hue": ("theme_ui_hue", 140, 0, 360),
}


def slider_adjust(config, cell, direction):
    """Moves a slider one step for a left or right press and returns True when the cell is a slider (the value is clamped and snapped to the step); False for any other cell or direction."""
    spec = SLIDER_SPECS.get(cell)
    if spec is None or direction not in (LEFT, RIGHT):
        return False
    key, default, low, high = spec
    moved = config.get(key, default) + (SLIDER_STEP if direction == RIGHT else -SLIDER_STEP)
    config[key] = max(low, min(high, (moved // SLIDER_STEP) * SLIDER_STEP))
    return True

_theme_table = None


def theme_cell(selection, row2_column=0):
    """Returns the cell name for a Theme tab selection number (row2_column 1 picks the right-hand cell on the two button rows) or CLOSE."""
    if row2_column == 1 and selection in THEME_RIGHT_CELLS:
        return THEME_RIGHT_CELLS[selection]
    if isinstance(selection, int) and 0 <= selection < len(THEME_SELECTION_CELLS):
        return THEME_SELECTION_CELLS[selection]
    return CLOSE


def theme_index(cell):
    """Returns the Theme tab selection number for a cell name."""
    if cell == CLOSE:
        return len(THEME_SELECTION_CELLS)
    for number, right_cell in THEME_RIGHT_CELLS.items():
        if cell == right_cell:
            return number
    return THEME_SELECTION_CELLS.index(cell)


def theme_row2_column(cell):
    """Returns 1 for the right-hand cells (Channel Static, Letterboxing) and 0 for every other cell."""
    return 1 if cell in THEME_RIGHT_CELLS.values() else 0


def theme_table():
    """Returns the cached NavTable for the Theme tab."""
    global _theme_table
    if _theme_table is None:
        _theme_table = _build_theme_table()
    return _theme_table


def _build_theme_table():
    t = NavTable()
    t.known(CLOSE, THEME_STATIC, THEME_LETTERBOX, *THEME_SELECTION_CELLS)
    cells = list(THEME_SELECTION_CELLS)
    for k, cell in enumerate(cells):
        t.link(cell, UP, TAB_BAR if k == 0 else cells[k - 1])
        t.link(cell, DOWN, cells[k + 1] if k + 1 < len(cells) else CLOSE)
    t.link("volume_bar", RIGHT, THEME_STATIC)
    t.link(THEME_STATIC, LEFT, "volume_bar")
    t.link(THEME_STATIC, UP, TAB_BAR)
    t.link(THEME_STATIC, DOWN, THEME_LETTERBOX)
    t.link("transition", RIGHT, THEME_LETTERBOX)
    t.link(THEME_LETTERBOX, LEFT, "transition")
    t.link(THEME_LETTERBOX, UP, THEME_STATIC)
    t.link(THEME_LETTERBOX, DOWN, "opacity")
    t.link(CLOSE, UP, cells[-1])
    return t


# ============================================================================
# SECTION: Games tab list
# ============================================================================

# WARNING: these numbers must match cols and the three grid rows in the Games tab drawing in ui.py, and the selection numbers 95 to 103 there, or the table and the picture disagree about which cell is where.
GAMES_COLS = 4
GAMES_ROWS_PER_PAGE = 3
GAMES_PAGE_SIZE = GAMES_COLS * GAMES_ROWS_PER_PAGE

GAMES_ALL_CONSOLES = "all_consoles"
GAMES_SET_MUSIC = "set_music"
GAMES_MENU_MUSIC = "menu_music"
GAMES_SCREENSAVER = "screensaver"
GAMES_SS_TIMER = "ss_timer"
GAMES_ALL_SETTINGS = "all_settings"
GAMES_PAGE_BUTTON = "page_button"
GAMES_SYNC_PROFILE = "sync_profile"

_GAMES_SELECTIONS = {
    95: GAMES_ALL_CONSOLES, 96: GAMES_SET_MUSIC, 97: GAMES_MENU_MUSIC, 98: GAMES_SCREENSAVER,
    99: CLOSE, 100: GAMES_SS_TIMER, 101: GAMES_ALL_SETTINGS, 102: GAMES_PAGE_BUTTON, 103: GAMES_SYNC_PROFILE,
}
_GAMES_CELL_SELECTIONS = {cell: number for number, cell in _GAMES_SELECTIONS.items()}

_games_tables = {}


def games_page_count(total):
    """Returns how many pages the console grid has."""
    return max(1, (total + GAMES_PAGE_SIZE - 1) // GAMES_PAGE_SIZE)


def games_page_of(selection, total, stored_page=0):
    """Returns the page the highlight is on: the page of a console cell, else the stored page, kept inside the page count."""
    if isinstance(selection, int) and 0 <= selection < total:
        return selection // GAMES_PAGE_SIZE
    return max(0, min(int(stored_page or 0), games_page_count(total) - 1))


def games_cell(selection, total):
    """Returns the cell name for a Games tab selection number: a console cell, one of the buttons, CLOSE, or None for anything else."""
    if isinstance(selection, int) and 0 <= selection < total:
        return "console%02d" % selection
    return _GAMES_SELECTIONS.get(selection)


def games_index(cell):
    """Returns the Games tab selection number for a cell name."""
    if cell in _GAMES_CELL_SELECTIONS:
        return _GAMES_CELL_SELECTIONS[cell]
    return int(cell[len("console"):])


def games_table(page, total, kiosk):
    """Returns the cached NavTable for one page of the Games tab; kiosk mode hides the Set Music button."""
    page = max(0, min(int(page), games_page_count(total) - 1))
    key = (page, int(total), bool(kiosk))
    table = _games_tables.get(key)
    if table is None:
        table = _build_games_table(page, int(total), bool(kiosk))
        _games_tables[key] = table
    return table


def _build_games_table(page, total, kiosk):
    start = page * GAMES_PAGE_SIZE
    end = min(start + GAMES_PAGE_SIZE, total)
    name = lambda i: "console%02d" % i
    t = NavTable()
    t.known(CLOSE, GAMES_ALL_CONSOLES, GAMES_MENU_MUSIC, GAMES_SCREENSAVER, GAMES_SS_TIMER, GAMES_ALL_SETTINGS,
            GAMES_PAGE_BUTTON, GAMES_SYNC_PROFILE, *[name(i) for i in range(start, end)])
    if not kiosk:
        t.known(GAMES_SET_MUSIC)
    bottoms = []
    for i in range(start, end):
        col = (i - start) % GAMES_COLS
        t.link(name(i), UP, TAB_BAR if i - start < GAMES_COLS else name(i - GAMES_COLS))
        if i + GAMES_COLS < end:
            t.link(name(i), DOWN, name(i + GAMES_COLS))
        else:
            t.link(name(i), DOWN, GAMES_PAGE_BUTTON)
            bottoms.append(name(i))
        if col > 0:
            t.link(name(i), LEFT, name(i - 1))
        if col < GAMES_COLS - 1 and i + 1 < end:
            t.link(name(i), RIGHT, name(i + 1))
    t.link(GAMES_PAGE_BUTTON, UP, Remember(name(end - 1)))
    t.link(GAMES_PAGE_BUTTON, DOWN, Remember(GAMES_ALL_CONSOLES))
    t.remember[GAMES_PAGE_BUTTON] = {UP: tuple(bottoms), DOWN: (GAMES_ALL_CONSOLES, GAMES_ALL_SETTINGS)}

    t.link(GAMES_ALL_CONSOLES, UP, GAMES_PAGE_BUTTON)
    t.link(GAMES_ALL_CONSOLES, DOWN, GAMES_SCREENSAVER)
    t.link(GAMES_ALL_CONSOLES, RIGHT, GAMES_ALL_SETTINGS)
    t.link(GAMES_SCREENSAVER, UP, GAMES_ALL_CONSOLES)
    t.link(GAMES_SCREENSAVER, DOWN, GAMES_MENU_MUSIC)
    t.link(GAMES_SCREENSAVER, RIGHT, GAMES_SS_TIMER)
    t.link(GAMES_MENU_MUSIC, UP, GAMES_SCREENSAVER)
    t.link(GAMES_MENU_MUSIC, DOWN, CLOSE)
    last_right = CLOSE if kiosk else GAMES_SET_MUSIC
    if not kiosk:
        t.link(GAMES_MENU_MUSIC, RIGHT, GAMES_SET_MUSIC)
        t.link(GAMES_SET_MUSIC, UP, GAMES_SS_TIMER)
        t.link(GAMES_SET_MUSIC, DOWN, CLOSE)
        t.link(GAMES_SET_MUSIC, LEFT, GAMES_MENU_MUSIC)
    for cell in (GAMES_ALL_SETTINGS, GAMES_SYNC_PROFILE):
        t.link(cell, UP, GAMES_PAGE_BUTTON)
        t.link(cell, DOWN, GAMES_SS_TIMER)
    t.link(GAMES_ALL_SETTINGS, LEFT, GAMES_ALL_CONSOLES)
    t.link(GAMES_ALL_SETTINGS, RIGHT, GAMES_SYNC_PROFILE)
    t.link(GAMES_SYNC_PROFILE, LEFT, GAMES_ALL_SETTINGS)
    t.alias[GAMES_SYNC_PROFILE] = GAMES_ALL_SETTINGS
    t.link(GAMES_SS_TIMER, UP, GAMES_ALL_SETTINGS)
    t.link(GAMES_SS_TIMER, DOWN, last_right)
    t.link(GAMES_SS_TIMER, LEFT, GAMES_SCREENSAVER)
    t.link(CLOSE, UP, Remember(last_right if not kiosk else GAMES_MENU_MUSIC))
    t.remember[CLOSE] = (GAMES_MENU_MUSIC,) if kiosk else (GAMES_SET_MUSIC, GAMES_MENU_MUSIC)
    return t


# ============================================================================
# SECTION: Games sub-menu
# ============================================================================

# WARNING: these lists must match the column split in the Games sub-menu drawing in ui.py (_menu_left_order, _menu_right_order, and the splash panel that fills the right column), or the table and the picture disagree about which cell is where.
GAMES_SUB_MENU_LEFT = ("controller_map", "stick_deadzone", "game_screen_mode", "scan_for_cores", "clear_cores")
GAMES_SUB_MENU_RIGHT = ("menu_bg_select_image", "multitap_toggle", "scroll_style", "scan_for_roms", "clear_roms")
GAMES_SUB_RIGHT_COLUMN = ("dvd_captions_toggle", "dvd_resume_toggle", "splash_toggle", "splash_select_image", "splash_select_video",
                          "add_bios", "relocate_roms_folder", "clear_console_roms")
GAMES_SUB_CONSOLE_LEFT = ("core_select", "controller_map")
GAMES_SUB_DVD_LEFT = ("dvd_button_mapping", "dvd_screen_mode", "dvd_audio_track")
GAMES_SUB_PROFILES = ("profile1", "profile2", "profile3")

GAMES_SUB_STATUS = "status"
GAMES_SUB_TEXT_COLOR = "text_color"
GAMES_SUB_SCROLL_STYLE = "scroll_style"
GAMES_SUB_SPLASH_TOGGLE = "splash_toggle"
GAMES_SUB_SPLASH_IMAGE = "splash_select_image"
GAMES_SUB_SPLASH_VIDEO = "splash_select_video"
GAMES_SUB_DISC = "disc"

_games_sub_tables = {}


def games_sub_cell(rows, selection):
    """Returns the cell name (the row id) for a Games sub-menu selection; a number outside the rows is Close."""
    if isinstance(selection, int) and 0 <= selection < len(rows):
        return rows[selection]
    return CLOSE


def games_sub_index(rows, cell):
    """Returns the Games sub-menu selection number for a cell name."""
    return list(rows).index(cell)


def games_sub_table(rows, last_console):
    """Returns the cached NavTable for one Games sub-menu layout; rows is the full row list the handler builds (status first except on the MENU page, Close last), last_console is True when this is the only console left on."""
    key = (tuple(rows), bool(last_console))
    table = _games_sub_tables.get(key)
    if table is None:
        table = _build_games_sub_table(list(rows), bool(last_console))
        _games_sub_tables[key] = table
    return table


def _build_games_sub_table(rows, last_console):
    t = NavTable()
    t.known(*rows, CLOSE)
    if GAMES_SUB_TEXT_COLOR in rows or GAMES_SUB_STATUS not in rows:
        _build_games_menu_page(t, rows)
    elif GAMES_SUB_DVD_LEFT[0] in rows:
        _build_games_dvd(t, rows, last_console)
    elif len(rows) <= 2:
        t.link(GAMES_SUB_STATUS, DOWN, CLOSE)
        t.link(CLOSE, UP, GAMES_SUB_STATUS)
    else:
        _build_games_console(t, rows, last_console)
    return t


def _chain(t, cells, top, bottom):
    """Links a column of cells top to bottom; top is the target above the first (None for none), bottom the target below the last."""
    for i, cell in enumerate(cells):
        t.link(cell, UP, cells[i - 1] if i else top)
        t.link(cell, DOWN, cells[i + 1] if i + 1 < len(cells) else bottom)


def _build_games_menu_page(t, rows):
    left = [r for r in GAMES_SUB_MENU_LEFT if r in rows]
    right = [r for r in GAMES_SUB_MENU_RIGHT if r in rows]
    _chain(t, left, None, CLOSE)
    _chain(t, right, None, CLOSE)
    for i, cell in enumerate(left):
        if right:
            t.link(cell, RIGHT, right[min(i, len(right) - 1)])
    for i, cell in enumerate(right):
        if left:
            t.link(cell, LEFT, left[min(i, len(left) - 1)])
    if GAMES_SUB_TEXT_COLOR in rows and GAMES_SUB_SCROLL_STYLE in rows:
        swatch, main = GAMES_SUB_TEXT_COLOR, GAMES_SUB_SCROLL_STYLE
        t.link(main, RIGHT, swatch)
        t.link(swatch, LEFT, main)
        t.link(swatch, UP, t.cells[main].get(UP))
        t.link(swatch, DOWN, t.cells[main].get(DOWN))
    column = left or right
    if column:
        ends = tuple(c[-1] for c in (left, right) if c)
        t.link(CLOSE, UP, Remember(column[-1]))
        t.remember[CLOSE] = ends
        if left:
            t.link(CLOSE, LEFT, left[-1])


def _splash_pair(t, toggle_up, below):
    """Links the splash toggle, the Video and Image buttons that share one row under it, and the first row below them."""
    toggle, image, video = GAMES_SUB_SPLASH_TOGGLE, GAMES_SUB_SPLASH_IMAGE, GAMES_SUB_SPLASH_VIDEO
    pair = (video, image)
    t.link(toggle, UP, toggle_up)
    t.link(toggle, DOWN, Remember(image))
    for cell in pair:
        t.link(cell, UP, toggle)
        t.link(cell, DOWN, below)
    t.link(video, RIGHT, image)
    t.link(image, LEFT, video)
    if below != CLOSE:
        t.link(below, UP, Remember(video))
        t.remember[below] = pair
    return pair


def _build_games_dvd(t, rows, last_console):
    status, toggle, video = GAMES_SUB_STATUS, GAMES_SUB_SPLASH_TOGGLE, GAMES_SUB_SPLASH_VIDEO
    mapping, mode, audio = GAMES_SUB_DVD_LEFT
    captions, resume = "dvd_captions_toggle", "dvd_resume_toggle"
    above = None if last_console else status
    t.link(status, DOWN, Remember(mapping))
    t.remember[status] = {DOWN: (mapping, toggle)}
    t.link(status, RIGHT, toggle)
    pair = _splash_pair(t, above, captions)
    t.remember[toggle] = pair
    t.link(toggle, LEFT, above)
    t.link(captions, DOWN, resume)
    t.link(resume, UP, captions)
    t.link(resume, DOWN, CLOSE)
    t.link(captions, LEFT, mode)
    t.link(resume, LEFT, audio)
    _chain(t, [mapping, mode, audio], above, CLOSE)
    t.link(mapping, RIGHT, video)
    t.link(video, LEFT, mapping)
    t.link(mode, RIGHT, captions)
    t.link(audio, RIGHT, resume)
    t.link(CLOSE, UP, Remember(resume))
    t.remember[CLOSE] = (resume, audio)


def _build_games_console(t, rows, last_console):
    status, toggle, image, video = GAMES_SUB_STATUS, GAMES_SUB_SPLASH_TOGGLE, GAMES_SUB_SPLASH_IMAGE, GAMES_SUB_SPLASH_VIDEO
    above = None if last_console else status
    lefts = [r for r in GAMES_SUB_CONSOLE_LEFT if r in rows]
    profiles = [r for r in GAMES_SUB_PROFILES if r in rows]
    disc = [GAMES_SUB_DISC] if GAMES_SUB_DISC in rows else []
    column = lefts + profiles + disc
    first_left = lefts[0] if lefts else (column[0] if column else None)
    kiosk_first = bool(lefts) and lefts[0] == "controller_map"
    right = [r for r in rows if r in GAMES_SUB_RIGHT_COLUMN]
    below_pair = [r for r in right if r not in (toggle, image, video)]
    clear = right[-1] if right else None

    t.link(status, DOWN, Remember(first_left) if first_left else CLOSE)
    t.remember[status] = {DOWN: (first_left, toggle)}
    t.link(status, RIGHT, toggle)

    _chain(t, column, above, CLOSE)
    for cell in profiles:
        t.link(cell, RIGHT, CLOSE if cell == profiles[-1] else clear)
    for cell in disc:
        t.link(cell, RIGHT, CLOSE)
    for cell in lefts:
        t.link(cell, RIGHT, toggle if (cell == lefts[0] and kiosk_first) else video)

    pair = _splash_pair(t, above, below_pair[0] if below_pair else CLOSE)
    _chain(t, below_pair, None, CLOSE)
    if first_left:
        reach_toggle = ((lefts[0],) if kiosk_first else ()) + (() if last_console else (status,))
        t.link(video, LEFT, Remember(first_left))
        t.remember[video] = tuple(c for c in lefts if not (c == lefts[0] and kiosk_first))
        t.link(toggle, LEFT, Remember(first_left))
        t.remember[toggle] = {DOWN: pair, LEFT: reach_toggle}
        for cell in below_pair:
            t.link(cell, LEFT, first_left)
    else:
        t.remember[toggle] = {DOWN: pair}
    last_left = column[-1] if column else None
    t.link(CLOSE, UP, Remember(clear if clear else last_left))
    ways = {UP: tuple(c for c in (clear, last_left) if c)}
    if last_left:
        t.link(CLOSE, LEFT, Remember(last_left))
        ways[LEFT] = tuple(c for c in (profiles[-1] if profiles else None, disc[0] if disc else None) if c)
    t.remember[CLOSE] = ways


# ============================================================================
# SECTION: DVD button map
# ============================================================================

# WARNING: these two buttons must match the Map Buttons and Close buttons in render_dvd_button_mapping_popup in ui.py, and dvd_map_cursor 0 and 1.
DVD_MAP_BUTTONS = "map_buttons"
DVD_MAP_CELLS = (DVD_MAP_BUTTONS, CLOSE)

_dvd_map_tables = {}


def dvd_map_cell(selection):
    """Returns the cell name for a dvd_map_cursor value: Map Buttons for 0, Close for anything else."""
    return DVD_MAP_BUTTONS if selection == 0 else CLOSE


def dvd_map_index(cell):
    """Returns the dvd_map_cursor value for a cell name."""
    return DVD_MAP_CELLS.index(cell)


def dvd_map_table():
    """Returns the cached NavTable of the DVD button map: two buttons side by side, and every key moves between them."""
    table = _dvd_map_tables.get(0)
    if table is None:
        table = NavTable()
        table.known(*DVD_MAP_CELLS)
        table.link(DVD_MAP_BUTTONS, DOWN, CLOSE)
        table.link(DVD_MAP_BUTTONS, RIGHT, CLOSE)
        table.link(CLOSE, UP, DVD_MAP_BUTTONS)
        table.link(CLOSE, LEFT, DVD_MAP_BUTTONS)
        _dvd_map_tables[0] = table
    return table


# ============================================================================
# SECTION: Save Profile
# ============================================================================

# WARNING: these lists and the row order in save_profile_rows must match the Save Profile drawing in ui.py (the banner, the SAVE NOW to AUTO-LOAD column on the left, SCREEN SIZE, TV BEZEL and BEZEL STYLE down the right, then Close), or the table and the picture disagree about which cell is where.
SAVE_PROFILE_ACTIVE = "active"
SAVE_PROFILE_LEFT = ("save", "load", "auto_save", "auto_load")
SAVE_PROFILE_RIGHT = ("screen_size", "tv_bezel", "bezel_style")

_save_profile_tables = {}


def save_profile_rows(handheld, border, show_style):
    """Returns the cell names of one Save Profile layout in selection-number order: the banner, the four left rows, the right rows that console has, then Close."""
    rows = [SAVE_PROFILE_ACTIVE] + list(SAVE_PROFILE_LEFT)
    if handheld:
        rows.append("screen_size")
    if border:
        rows.append("tv_bezel")
        if show_style:
            rows.append("bezel_style")
    rows.append(CLOSE)
    return rows


def save_profile_cell(rows, selection):
    """Returns the cell name for a Save Profile selection number; a number outside the rows is Close."""
    if isinstance(selection, int) and 0 <= selection < len(rows):
        return rows[selection]
    return CLOSE


def save_profile_index(rows, cell):
    """Returns the Save Profile selection number for a cell name."""
    return list(rows).index(cell)


def save_profile_table(rows):
    """Returns the cached NavTable for one Save Profile layout."""
    key = tuple(rows)
    table = _save_profile_tables.get(key)
    if table is None:
        table = _build_save_profile_table(list(rows))
        _save_profile_tables[key] = table
    return table


def _build_save_profile_table(rows):
    left = [r for r in SAVE_PROFILE_LEFT if r in rows]
    right = [r for r in SAVE_PROFILE_RIGHT if r in rows]
    active = SAVE_PROFILE_ACTIVE
    t = NavTable()
    t.known(*rows)
    _chain(t, left, active, CLOSE)
    _chain(t, right, active, CLOSE)
    for i, cell in enumerate(left):
        if i < len(right):
            t.link(cell, RIGHT, right[i])
    for i, cell in enumerate(right):
        if i < len(left):
            t.link(cell, LEFT, left[i])
    entries = tuple(column[0] for column in (left, right) if column)
    t.link(active, DOWN, Remember(left[0]))
    t.remember[active] = {DOWN: entries}
    exits = tuple(column[-1] for column in (left, right) if column)
    t.link(CLOSE, UP, Remember("bezel_style" if "bezel_style" in rows else left[-1]))
    t.remember[CLOSE] = {UP: exits}
    return t


# ============================================================================
# SECTION: Stick deadzone screen
# ============================================================================

# WARNING: these cells and their order must match the cursor numbers of render_stick_deadzone_ui in game_deck.py (the switch, then the three setting rows and Reset to Defaults only while the player's switch is on, then the player squares, then Close).
STICK_DZ_SWITCH = "switch"
STICK_DZ_DEADZONE = "deadzone"
STICK_DZ_SENSITIVITY = "sensitivity"
STICK_DZ_AXIS = "axis_threshold"
STICK_DZ_RESET = "reset"
STICK_DZ_PLAYERS = "players"
STICK_DZ_PLAYER_COUNT = 4
STICK_DZ_SETTINGS = (STICK_DZ_DEADZONE, STICK_DZ_SENSITIVITY, STICK_DZ_AXIS)

_stick_dz_tables = {}


def stick_dz_rows(enabled):
    """Returns the cell names of the Stick Deadzone screen in cursor-number order."""
    rows = [STICK_DZ_SWITCH]
    if enabled:
        rows.extend(STICK_DZ_SETTINGS)
        rows.append(STICK_DZ_RESET)
    rows.append(STICK_DZ_PLAYERS)
    rows.append(CLOSE)
    return rows


def stick_dz_cell(rows, selection):
    """Returns the cell name for a Stick Deadzone cursor value; a number outside the rows is Close."""
    if isinstance(selection, int) and 0 <= selection < len(rows):
        return rows[selection]
    return CLOSE


def stick_dz_index(rows, cell):
    """Returns the Stick Deadzone cursor value for a cell name."""
    return list(rows).index(cell)


def stick_dz_player_step(player, direction):
    """Returns the player square a Left or Right press moves to on the Stick Deadzone screen, or None when Right is pressed on the last square and the cursor leaves toward Close."""
    if direction == LEFT:
        return max(1, player - 1)
    if direction == RIGHT:
        return player + 1 if player < STICK_DZ_PLAYER_COUNT else None
    return player


def stick_dz_table(enabled):
    """Returns the cached NavTable of the Stick Deadzone screen."""
    key = bool(enabled)
    table = _stick_dz_tables.get(key)
    if table is None:
        rows = stick_dz_rows(key)
        table = NavTable()
        table.known(*rows)
        _chain(table, rows, None, None)
        table.link(STICK_DZ_PLAYERS, RIGHT, CLOSE)
        table.link(CLOSE, LEFT, STICK_DZ_PLAYERS)
        _stick_dz_tables[key] = table
    return table


# ============================================================================
# SECTION: Controller map
# ============================================================================

# WARNING: these cells and their order must match the cursor numbers of render_controller_mapping_ui in game_deck.py (0 the player squares and Pick Controller row, 1 Map Buttons, 2 Stick as D-pad, then Close; N64 has no stick, so its Close is 2).
CTRL_MAP_SQUARES = "squares"
CTRL_MAP_BUTTONS = "map_buttons"
CTRL_MAP_STICK = "stick_as_dpad"

_ctrl_map_tables = {}


def ctrl_map_rows(has_stick):
    """Returns the cell names of the controller map in cursor-number order; has_stick is False for N64."""
    rows = [CTRL_MAP_SQUARES, CTRL_MAP_BUTTONS]
    if has_stick:
        rows.append(CTRL_MAP_STICK)
    rows.append(CLOSE)
    return rows


def ctrl_map_cell(rows, selection):
    """Returns the cell name for a controller map cursor value; a number outside the rows is Close."""
    if isinstance(selection, int) and 0 <= selection < len(rows):
        return rows[selection]
    return CLOSE


def ctrl_map_index(rows, cell):
    """Returns the controller map cursor value for a cell name."""
    return list(rows).index(cell)


def ctrl_map_table(has_stick):
    """Returns the cached NavTable of the controller map; the player squares row is one cell here, and ctrl_map_squares_step walks inside it."""
    key = bool(has_stick)
    table = _ctrl_map_tables.get(key)
    if table is None:
        table = _build_ctrl_map_table(key)
        _ctrl_map_tables[key] = table
    return table


def _build_ctrl_map_table(has_stick):
    squares, buttons, stick = CTRL_MAP_SQUARES, CTRL_MAP_BUTTONS, CTRL_MAP_STICK
    below_buttons = stick if has_stick else CLOSE
    above_close = stick if has_stick else buttons
    t = NavTable()
    t.known(*ctrl_map_rows(has_stick))
    for direction in (LEFT, RIGHT):
        t.link(squares, direction, Remember(buttons))
    t.remember[squares] = (buttons, stick) if has_stick else (buttons,)
    t.link(buttons, LEFT, squares)
    t.link(buttons, DOWN, below_buttons)
    t.link(buttons, RIGHT, below_buttons)
    if has_stick:
        t.link(stick, UP, buttons)
        t.link(stick, LEFT, squares)
        t.link(stick, DOWN, CLOSE)
        t.link(stick, RIGHT, CLOSE)
    t.link(CLOSE, UP, above_close)
    t.link(CLOSE, LEFT, above_close)
    return t


def ctrl_map_squares_step(pickable, selected, on_pick, direction, is_menu):
    """Walks the player squares row; returns (selected player, on the Pick Controller button, leave the row). The row is the pickable players then the Pick button; left from the first square wraps to the Pick button, right from the Pick button leaves, and on the MENU console both keys leave."""
    if direction not in (LEFT, RIGHT):
        return selected, on_pick, False
    if is_menu:
        return selected, on_pick, True
    step = -1 if direction == LEFT else 1
    length = len(pickable) + 1
    index = length - 1 if on_pick else (pickable.index(selected) if selected in pickable else 0)
    if step == 1 and index == length - 1:
        return selected, on_pick, True
    index = (index + step) % length
    if index == length - 1:
        return selected, True, False
    return pickable[index], False, False


# ============================================================================
# SECTION: Tab bar
# ============================================================================

# WARNING: this mapping must match the tab names and panel layers the tab bar drawing and the panel handlers use in ui.py and the main script.
TAB_PANEL_LAYERS = {"System": "SYSTEM_ROWS", "Channels": "CHANNEL_LIST", "Games": "GAMES_LIST", "Video": "VIDEO_ROWS", "Theme": "THEME_ROWS"}

_tab_tables = {}


def tabs_cell(tabs, active):
    """Returns the cell name of the active tab; a tab that is not in the bar counts as the first one."""
    return active if active in tabs else tabs[0]


def tabs_table(tabs):
    """Returns the cached NavTable of the tab bar: left and right walk the tabs and wrap, down leaves into the panel."""
    key = tuple(tabs)
    table = _tab_tables.get(key)
    if table is None:
        table = NavTable()
        table.known(*key)
        for i, tab in enumerate(key):
            table.link(tab, LEFT, key[i - 1])
            table.link(tab, RIGHT, key[(i + 1) % len(key)])
            table.link(tab, DOWN, PANEL)
        _tab_tables[key] = table
    return table


# ============================================================================
# SECTION: Single-column lists and the three-by-three Server Slots grid
# ============================================================================

SERVER_SLOT_CELLS = ("slot0", "slot1", "slot2", "slot3", "slot4", "slot5", "create", "manage", CLOSE)
SERVER_SLOTS_COLUMNS = 3
SERVER_SLOTS_ROWS = 3

_column_tables = {}
_ring_tables = {}
_server_slots_table = None


def _column_table(count):
    """Returns the cached NavTable of a list of `count` rows where up and down stop at the ends."""
    table = _column_tables.get(count)
    if table is None:
        table = NavTable()
        names = ["row%d" % i for i in range(count)]
        table.known(*names)
        for i, name in enumerate(names):
            if i > 0:
                table.link(name, UP, names[i - 1])
            if i + 1 < count:
                table.link(name, DOWN, names[i + 1])
        _column_tables[count] = table
    return table


def column_move(app_state, layer, selection, count, direction):
    """Returns the new selection number after one key in a single-column list of `count` rows; up and down stop at the ends and pull a stale number back to the nearest row, left and right do nothing."""
    if count <= 0 or direction not in (UP, DOWN):
        return selection
    snapped = max(0, min(count - 1, selection))
    if snapped != selection:
        return snapped
    target = navigate(app_state, layer, _column_table(count), "row%d" % selection, direction)
    return selection if target is None else int(target[3:])


def _ring_table(count):
    """Returns the cached NavTable of a list of `count` rows where up and left go to the row before, down and right to the row after, and both wrap."""
    table = _ring_tables.get(count)
    if table is None:
        table = NavTable()
        names = ["row%d" % i for i in range(count)]
        table.known(*names)
        for i, name in enumerate(names):
            for direction in (UP, LEFT):
                table.link(name, direction, names[i - 1])
            for direction in (DOWN, RIGHT):
                table.link(name, direction, names[(i + 1) % count])
        _ring_tables[count] = table
    return table


def ring_move(app_state, layer, selection, count, direction):
    """Returns the new selection number after one key in a wrapping list of `count` rows."""
    if count <= 0:
        return selection
    selection %= count
    target = navigate(app_state, layer, _ring_table(count), "row%d" % selection, direction)
    return selection if target is None else int(target[3:])


def server_slots_cell(selection):
    """Returns the cell name for a Server Slots selection number: slot0 to slot5, create, manage or CLOSE."""
    return SERVER_SLOT_CELLS[selection] if isinstance(selection, int) and 0 <= selection < len(SERVER_SLOT_CELLS) else CLOSE


def server_slots_index(cell):
    """Returns the Server Slots selection number for a cell name."""
    return SERVER_SLOT_CELLS.index(cell)


def server_slots_table():
    """Returns the cached NavTable of the Server Slots menu: three columns of three, numbered down each column, and every key stops at the edge."""
    global _server_slots_table
    if _server_slots_table is None:
        table = NavTable()
        table.known(*SERVER_SLOT_CELLS)
        for i, cell in enumerate(SERVER_SLOT_CELLS):
            column, row = divmod(i, SERVER_SLOTS_ROWS)
            if row > 0:
                table.link(cell, UP, SERVER_SLOT_CELLS[i - 1])
            if row < SERVER_SLOTS_ROWS - 1:
                table.link(cell, DOWN, SERVER_SLOT_CELLS[i + 1])
            if column > 0:
                table.link(cell, LEFT, SERVER_SLOT_CELLS[i - SERVER_SLOTS_ROWS])
            if column < SERVER_SLOTS_COLUMNS - 1:
                table.link(cell, RIGHT, SERVER_SLOT_CELLS[i + SERVER_SLOTS_ROWS])
        _server_slots_table = table
    return _server_slots_table


# ============================================================================
# SECTION: Channels per-channel sub-menu
# ============================================================================

# WARNING: the row numbers and column counts here must match the Channel sub-menu drawing in ui.py and the Enter code in the main script: row 0 is Active, Mode and Name, then Close is row 1 for an off channel, row 4 for a music channel and row 6 (marathon) or 7 otherwise, with Clear and Close sharing the last row for a content channel that is on.
CHANNEL_SUB_MAX_COL = 5

_channel_sub_tables = {}


def channel_sub_layout(ch_info, active_count):
    """Returns the hashable layout of one channel's sub-menu: (on, music, marathon, full day, six block, commercials, only active channel); active_count is how many channels are on."""
    on = bool(ch_info.get("active", True))
    music = bool(ch_info.get("is_visualizer", False))
    marathon = (not music) and ch_info.get("scheduling_mode", "random_slots") == "marathon"
    full_day = (not marathon) and ch_info.get("block_mode", "full_day") == "full_day"
    six_block = (not marathon) and ch_info.get("block_mode", "full_day") == "six_block"
    return (on, music, marathon, full_day, six_block, bool(ch_info.get("commercials_enabled", False)), on and active_count <= 1)


def channel_sub_close_row(layout):
    """Returns the row number of the Close row for a layout."""
    on, music, marathon = layout[0], layout[1], layout[2]
    if not on:
        return 1
    if music:
        return 4
    return 6 if marathon else 7


def channel_sub_cell(row, col):
    """Returns the cell name for a sub-menu row and column."""
    return "r%dc%d" % (row, col)


def channel_sub_index(cell):
    """Returns (row, column) for a cell name."""
    row, col = cell[1:].split("c")
    return int(row), int(col)


def channel_sub_table(layout):
    """Returns the cached NavTable for one channel sub-menu layout; every column up to CHANNEL_SUB_MAX_COL is a cell so a column left over from a changed setting still moves."""
    table = _channel_sub_tables.get(layout)
    if table is None:
        table = NavTable()
        close = channel_sub_close_row(layout)
        table.known(*[channel_sub_cell(r, c) for r in range(close + 1) for c in range(CHANNEL_SUB_MAX_COL + 1)])
        for row in range(close + 1):
            for col in range(CHANNEL_SUB_MAX_COL + 1):
                for direction in DIRECTIONS:
                    target = _channel_sub_target(layout, close, row, col, direction)
                    if target is not None:
                        table.link(channel_sub_cell(row, col), direction, channel_sub_cell(*target))
        _channel_sub_tables[layout] = table
    return table


def _channel_sub_target(layout, close, row, col, direction):
    """Returns the (row, column) one key leads to from a cell, or None when it stays."""
    on, music, marathon, full_day, six_block, commercials, single = layout
    folder_row = 4 if marathon else 5
    holiday_row = 5 if marathon else 6
    has_audio_row = on and not music
    only_off_at_row0 = 1 if single else 0
    if direction == UP:
        if row == 0:
            return None
        if row == close:
            if (not music) and on and col == 1:
                return row, 0
            return close - 1, 0
        return row - 1, (only_off_at_row0 if row - 1 == 0 else 0)
    if direction == DOWN:
        if row == close - 1:
            return close, 0
        if row == close:
            if (not music) and on and col == 0:
                return row, 1
            return None
        return row + 1, 0
    if direction == RIGHT:
        if row == close:
            limit = 1 if (not music) and on else None
        elif row == 0:
            limit = 2 if on else None
        elif not music and row == 1:
            limit = None if marathon else 1
        elif not music and row == 3:
            limit = 2 if commercials else None
        elif not music and row == folder_row:
            limit = (5 if six_block else 2) if (not marathon and not full_day) else None
        elif has_audio_row and row == holiday_row and not marathon:
            limit = 1
        else:
            limit = None
        return None if limit is None else (row, min(limit, col + 1))
    if row == close:
        floor = 0 if (not music) and on else None
    elif row == 0:
        floor = only_off_at_row0 if on else None
    elif not music and row == 3:
        floor = 0
    elif has_audio_row and row == holiday_row and not marathon:
        floor = 0
    else:
        floor = 0 if on else None
    return None if floor is None else (row, max(floor, col - 1))


# ============================================================================
# SECTION: Popups (clear and burn-in confirm, controls splash)
# ============================================================================

CONFIRM_YES = "yes"
CONFIRM_NO = "no"
CONFIRM_CHECK_YES = "check_yes"
CONFIRM_CHECK_NO = "check_no"
SPLASH_BACK = "back"
SPLASH_NEXT = "next"

_confirm_tables = {}
_splash_table = None


def confirm_cell(selection, focus):
    """Returns the cell of a confirm popup: Yes or No, or the same answer with the Don't show again box focused."""
    answer = CONFIRM_YES if selection == "Yes" else CONFIRM_NO
    return "check_" + answer if focus == "checkbox" else answer


def confirm_answer(cell):
    """Returns "Yes" or "No" for a confirm popup cell."""
    return "Yes" if cell in (CONFIRM_YES, CONFIRM_CHECK_YES) else "No"


def confirm_focus(cell):
    """Returns "checkbox" for the cells with the Don't show again box focused, else "buttons"."""
    return "checkbox" if cell in (CONFIRM_CHECK_YES, CONFIRM_CHECK_NO) else "buttons"


def confirm_table(has_checkbox):
    """Returns the cached NavTable of a confirm popup: left is Yes, right is No, and when the popup has a Don't show again box, down reaches it and up returns to the answer."""
    key = bool(has_checkbox)
    table = _confirm_tables.get(key)
    if table is None:
        table = NavTable()
        table.known(CONFIRM_YES, CONFIRM_NO)
        for cell in (CONFIRM_YES, CONFIRM_NO):
            table.link(cell, LEFT, CONFIRM_YES)
            table.link(cell, RIGHT, CONFIRM_NO)
        if key:
            table.known(CONFIRM_CHECK_YES, CONFIRM_CHECK_NO)
            table.link(CONFIRM_YES, DOWN, CONFIRM_CHECK_YES)
            table.link(CONFIRM_NO, DOWN, CONFIRM_CHECK_NO)
            table.link(CONFIRM_CHECK_YES, UP, CONFIRM_YES)
            table.link(CONFIRM_CHECK_NO, UP, CONFIRM_NO)
        _confirm_tables[key] = table
    return table


def splash_cell(focus):
    """Returns the cell of the controls splash for its focus number: Back for 0, Next for anything else."""
    return SPLASH_BACK if focus == 0 else SPLASH_NEXT


def splash_focus(cell):
    """Returns the controls splash focus number for a cell: 0 for Back, 1 for Next."""
    return 0 if cell == SPLASH_BACK else 1


def splash_table():
    """Returns the cached NavTable of the controls splash buttons: left is Back, right is Next."""
    global _splash_table
    if _splash_table is None:
        table = NavTable()
        table.known(SPLASH_BACK, SPLASH_NEXT)
        table.link(SPLASH_BACK, RIGHT, SPLASH_NEXT)
        table.link(SPLASH_NEXT, LEFT, SPLASH_BACK)
        _splash_table = table
    return _splash_table


# ============================================================================
# SECTION: TV guide (ch04)
# ============================================================================

GUIDE_ROW = "row"
GUIDE_TIME = "time"


def guide_move(direction):
    """Returns (axis, step) for a guide key: up and down walk the channel rows (-1, +1), left and right walk the time stops (-1, +1); None for anything else."""
    return {UP: (GUIDE_ROW, -1), DOWN: (GUIDE_ROW, 1), LEFT: (GUIDE_TIME, -1), RIGHT: (GUIDE_TIME, 1)}.get(direction)


def guide_row_step(allowed, selected, row_offset, visible_rows, step):
    """Returns (channel, new row offset) for one step through the guide rows, wrapping at both ends and scrolling the window to keep the row visible; None when there are no rows."""
    if not allowed:
        return None
    selected = str(selected).zfill(2)
    position = allowed.index(selected) if selected in allowed else 0
    target = (position + step) % len(allowed)
    offset = row_offset
    if target < row_offset:
        offset = target
    elif target >= row_offset + visible_rows:
        offset = max(0, target - (visible_rows - 1))
    return allowed[target], offset


# ============================================================================
# SECTION: Game channel (ch03)
# ============================================================================

DECK_PREV = "prev"
DECK_NEXT = "next"
DECK_TO_ARROWS = "to_arrows"
DECK_TO_LIST = "to_list"
DECK_ACTIVATE = "activate"
DECK_SELECT_UP = "select_up"
DECK_SELECT_DOWN = "select_down"


def deck_browse_step(scroll_style, direction):
    """Returns (step, flashed arrow direction) for the console carousel: left and right when the style is side, up and down otherwise; None for the other two directions."""
    previous, following = (LEFT, RIGHT) if scroll_style == "side" else (UP, DOWN)
    if direction == previous:
        return -1, previous
    if direction == following:
        return 1, following
    return None


def deck_list_step(direction):
    """Returns what a direction does on the ROM list: DECK_PREV, DECK_NEXT, DECK_TO_ARROWS (right), or None."""
    return {UP: DECK_PREV, DOWN: DECK_NEXT, RIGHT: DECK_TO_ARROWS}.get(direction)


def deck_arrows_step(selected, direction):
    """Returns what a direction does on the ROM list's console arrows: DECK_ACTIVATE when it points at the arrow already selected, DECK_SELECT_UP or DECK_SELECT_DOWN to move the selection, DECK_TO_LIST (left), or None."""
    if direction == UP:
        return DECK_ACTIVATE if selected == "up" else DECK_SELECT_UP
    if direction == DOWN:
        return DECK_ACTIVATE if selected == "down" else DECK_SELECT_DOWN
    if direction == LEFT:
        return DECK_TO_LIST
    return None


def deck_confirm_choice(direction):
    """Returns the answer a direction picks on a DVD Yes / No confirm popup: left is Yes, right is No, else None."""
    return {LEFT: "Yes", RIGHT: "No"}.get(direction)


# ============================================================================
# SECTION: Held-key repeat
# ============================================================================

HOLD_STARTED = "started"
HOLD_WAIT = "wait"
HOLD_REPEAT = "repeat"
HOLD_STUCK = "stuck"

DROP_FIRST = "drop_first"
DROP_SECOND = "drop_second"
DROP_BOTH = "drop_both"
DROP_OLDER = "drop_older"


@dataclass(frozen=True)
class HoldRule:
    """How a held key repeats: delay_ms before the first repeat (None means the caller's menu delay), interval_ms between repeats, and inclusive when a hold of exactly delay_ms already counts."""
    delay_ms: int | None
    interval_ms: int
    inclusive: bool = False


# VALUES CHANGE HERE
HOLD_RULES = {
    "vertical": HoldRule(None, 100),
    "horizontal": HoldRule(None, 110),
    "rom_list": HoldRule(380, 70, True),
    "gamepad": HoldRule(400, 180, True),
    "volume": HoldRule(200, 180),
}
# VALUES CHANGE HERE
HOLD_STUCK_MS = {"menu": 1500, "guide": 3000, "rom_list": 1500, "volume": 1500}
# VALUES CHANGE HERE: percent one volume press or one repeat moves.
VOLUME_STEP = 5


@dataclass
class HoldState:
    """What one repeating hold remembers: the key (or direction) being held, when it went down and when it last repeated."""
    key: object = None
    start: int = 0
    last: int = 0

    def clear(self):
        """Forgets the hold."""
        self.key = None
        self.start = 0
        self.last = 0


def volume_direction(key):
    """Returns +1 for the Right arrow, -1 for the Left arrow (the volume keys), else 0."""
    import pygame
    if key == pygame.K_RIGHT:
        return 1
    if key == pygame.K_LEFT:
        return -1
    return 0


def volume_next(volume, direction):
    """Returns the volume after one step in a direction (+1 or -1), kept between 0 and 100."""
    return max(0, min(100, int(volume) + direction * VOLUME_STEP))


def hold_rule(key):
    """Returns the HoldRule for a held W, S, A or D (W and S are vertical, A and D horizontal) or a held Left or Right arrow (volume); None for any other key."""
    import pygame
    if key in (pygame.K_LEFT, pygame.K_RIGHT):
        return HOLD_RULES["volume"]
    if key in (pygame.K_w, pygame.K_s):
        return HOLD_RULES["vertical"]
    if key in (pygame.K_a, pygame.K_d):
        return HOLD_RULES["horizontal"]
    return None


def hold_due(rule, held_ms, since_ms, menu_delay_ms=None):
    """Returns True when a hold of held_ms, last repeated since_ms ago, is due its next repeat under the rule."""
    delay = rule.delay_ms if rule.delay_ms is not None else menu_delay_ms
    long_enough = held_ms >= delay if rule.inclusive else held_ms > delay
    return bool(long_enough and since_ms >= rule.interval_ms)


def hold_stuck(pressed, held_ms, released_now, limit_ms):
    """Returns True when a key the app counts as held is not down on the keyboard, was not released in this frame's events and has been held longer than limit_ms."""
    return bool(not pressed and not released_now and held_ms > limit_ms)


def hold_step(rule, state, key, now, pressed, released_now, stuck_ms, menu_delay_ms=None):
    """Advances one hold and returns HOLD_STARTED (a new key began), HOLD_STUCK (the key is stuck, the hold is cleared), HOLD_REPEAT (a repeat is due, the repeat time is set to now) or HOLD_WAIT."""
    if state.key != key:
        state.key = key
        state.start = now
        state.last = now
        return HOLD_STARTED
    held_ms = now - state.start
    if hold_stuck(pressed, held_ms, released_now, stuck_ms):
        state.clear()
        return HOLD_STUCK
    if hold_due(rule, held_ms, now - state.last, menu_delay_ms):
        state.last = now
        return HOLD_REPEAT
    return HOLD_WAIT


def hold_pick(held, order):
    """Returns the first key of `order` that is in the held set, else None."""
    for key in order:
        if key in held:
            return key
    return None


def hold_released(events, key, source=None):
    """Returns True when this frame's events hold a key-up for `key`, directly or through the remote-remap source map."""
    import pygame
    source = source or {}
    return any(event.type == pygame.KEYUP and (event.key == key or source.get(event.key) == key) for event in events)


def hold_conflict(first_pressed, second_pressed, first_down=0, second_down=0, both_pressed=DROP_OLDER):
    """Returns which of two opposite keys that are both counted as held is dropped: the one not down on the keyboard, and when neither is down both; when both are down DROP_OLDER drops the one pressed first (the first when tied) and DROP_BOTH drops both."""
    if first_pressed and not second_pressed:
        return DROP_SECOND
    if second_pressed and not first_pressed:
        return DROP_FIRST
    if first_pressed and second_pressed:
        if both_pressed == DROP_BOTH:
            return DROP_BOTH
        return DROP_FIRST if first_down <= second_down else DROP_SECOND
    return DROP_BOTH


# ============================================================================
# SECTION: File explorer
# ============================================================================

# VALUES CHANGE HERE
EXPLORER_WHEEL_ROWS = 3
EXPLORER_DOUBLE_CLICK_MS = 500
EXPLORER_DRAG_START_PX = 4
EXPLORER_DRAG_EDGE_ZONE_PX = 50
EXPLORER_DRAG_SCROLL_SLOW_MS = 120
EXPLORER_DRAG_SCROLL_FAST_MS = 60
EXPLORER_THUMB_MIN_H = 20
EXPLORER_SB_WIDTH = 8
EXPLORER_SB_PAD = 4


def explorer_hit(rect, x, y):
    """Returns True when the point is inside the rectangle."""
    return bool(rect.collidepoint(x, y))


def explorer_in_band(y, top, height):
    """Returns True when y is inside the band that starts at top and is `height` tall."""
    return top <= y < top + height


def explorer_visible_rows(height, row_h):
    """Returns how many whole rows of row_h fit in `height`, at least one."""
    return max(1, height // row_h)


def explorer_row(y, top, row_h, scroll=0):
    """Returns the index of the row under y in a list whose first row starts at top and whose first shown row is `scroll`."""
    return int(scroll + (y - top) // row_h)


def explorer_row_at(y, top, bottom, row_h, scroll=0):
    """Returns the row index under y inside the list area from top to bottom, or -1 when y is outside it."""
    if y < top or y >= bottom:
        return -1
    return explorer_row(y, top, row_h, scroll)


def explorer_list_pick(y, top, rows, row_h, count, scroll):
    """Returns None when y is outside the `rows` shown rows of a side list that starts at top, -1 when it is inside but no item is there, else the item index (the scroll is first held inside its range)."""
    if not explorer_in_band(y, top, rows * row_h):
        return None
    scroll = max(0, min(max(0, count - rows), scroll))
    index = scroll + explorer_row(y, top, row_h)
    return index if 0 <= index < count else -1


def explorer_in_remove_zone(x, right, zone_w):
    """Returns True when x is inside the remove zone on the right edge of a row that ends at `right`."""
    return x >= right - zone_w


def explorer_wheel(scroll, count, visible, notches):
    """Returns the scroll after `notches` wheel notches (positive scrolls up) over a list of count items with `visible` shown, kept inside the list."""
    target = scroll - notches * EXPLORER_WHEEL_ROWS
    return max(0, min(max(0, count - visible), target))


def explorer_wheel_button(button, scroll, count, visible):
    """Returns the scroll after a legacy wheel button press (button 4 scrolls up, 5 down), or None when the button is not a wheel button."""
    if button == 4:
        return explorer_wheel(scroll, count, visible, 1)
    if button == 5:
        return explorer_wheel(scroll, count, visible, -1)
    return None


def explorer_thumb(top, height, count, visible, scroll):
    """Returns (y, height) of the scrollbar thumb on a track that starts at top and is `height` tall."""
    if count > visible:
        thumb_h = max(EXPLORER_THUMB_MIN_H, height * visible // max(1, count))
        thumb_y = top + (height - thumb_h) * scroll // max(1, count - visible)
        return thumb_y, thumb_h
    return top, height


def explorer_scroll_from_thumb(thumb_y, top, height, count, visible):
    """Returns the scroll that puts the scrollbar thumb's top edge at thumb_y."""
    max_scroll = max(0, count - visible)
    if max_scroll <= 0:
        return 0
    thumb_h = max(EXPLORER_THUMB_MIN_H, height * visible // max(1, count))
    travel = max(1, height - thumb_h)
    return max(0, min(max_scroll, round((thumb_y - top) / float(travel) * max_scroll)))


def explorer_scrollbar_track(bar_x, top, height):
    """Returns the rectangle a press counts as on the scrollbar: the thumb's column widened by the pad on both sides."""
    import pygame
    return pygame.Rect(bar_x - EXPLORER_SB_PAD, top, EXPLORER_SB_WIDTH + EXPLORER_SB_PAD * 2, height)


def explorer_scrollbar_press(x, y, bar_x, top, height, count, visible, scroll):
    """Returns (new scroll or None, grab offset) for a press on the scrollbar track: on the thumb the scroll stays and the grab is where the thumb was held, off it the scroll jumps to centre the thumb on y."""
    import pygame
    thumb_y, thumb_h = explorer_thumb(top, height, count, visible, scroll)
    if pygame.Rect(bar_x, thumb_y, EXPLORER_SB_WIDTH, thumb_h).collidepoint(x, y):
        return None, y - thumb_y
    grab = thumb_h // 2
    new_y = max(top, min(top + height - thumb_h, y - grab))
    return explorer_scroll_from_thumb(new_y, top, height, count, visible), grab


def explorer_scrollbar_drag(y, grab, top, height, count, visible):
    """Returns the scroll for a scrollbar thumb dragged to y while held at offset `grab`."""
    thumb_h = explorer_thumb(top, height, count, visible, 0)[1]
    new_y = max(top, min(top + height - thumb_h, y - grab))
    return explorer_scroll_from_thumb(new_y, top, height, count, visible)


def explorer_modifiers(mods):
    """Returns (shift, ctrl) for a pygame key-modifier mask."""
    import pygame
    return bool(mods & pygame.KMOD_SHIFT), bool(mods & pygame.KMOD_CTRL)


def explorer_select(index, count, selection, anchor, shift, ctrl):
    """Returns (selection, anchor) after a press on row `index` (-1 or past count is no row): Shift selects from the anchor, Ctrl toggles the row, a plain press selects only it, a press on no row clears unless Shift or Ctrl is held."""
    if 0 <= index < count:
        if shift:
            return set(range(min(anchor, index), max(anchor, index) + 1)), anchor
        if ctrl:
            chosen = set(selection)
            if index in chosen:
                chosen.discard(index)
            else:
                chosen.add(index)
            return chosen, index
        return {index}, index
    if not (shift or ctrl):
        return set(), anchor
    return selection, anchor


def explorer_double_click(now, last_ms, last_index, index):
    """Returns True when a press on row `index` at `now` is the second press of a double click."""
    return last_index == index and (now - last_ms) < EXPLORER_DOUBLE_CLICK_MS


def explorer_drag_moved(start, x, y):
    """Returns True when the pointer has moved far enough from where the press began to count as a drag."""
    return abs(x - start[0]) > EXPLORER_DRAG_START_PX or abs(y - start[1]) > EXPLORER_DRAG_START_PX


def explorer_drag_step(y, top, bottom, row_h, count, visible, scroll, anchor, scroll_t, now):
    """Returns (scroll, selection, scroll_t) for a drag select with the pointer at y: the list scrolls while the pointer is above top or below bottom (faster the further out), and the selection runs from the anchor to the row under the pointer."""
    max_scroll = max(0, count - visible)
    delta = 0
    speed = EXPLORER_DRAG_SCROLL_SLOW_MS
    span = EXPLORER_DRAG_SCROLL_SLOW_MS - EXPLORER_DRAG_SCROLL_FAST_MS
    if y < top:
        speed = max(EXPLORER_DRAG_SCROLL_FAST_MS, EXPLORER_DRAG_SCROLL_SLOW_MS - int(span * min(top - y, EXPLORER_DRAG_EDGE_ZONE_PX) / EXPLORER_DRAG_EDGE_ZONE_PX))
        delta = -1
    elif y > bottom:
        speed = max(EXPLORER_DRAG_SCROLL_FAST_MS, EXPLORER_DRAG_SCROLL_SLOW_MS - int(span * min(y - bottom, EXPLORER_DRAG_EDGE_ZONE_PX) / EXPLORER_DRAG_EDGE_ZONE_PX))
        delta = 1
    if delta and (now - scroll_t) >= speed:
        scroll = max(0, min(max_scroll, scroll + delta))
        scroll_t = now
    if y <= top:
        index = scroll
    elif y >= bottom:
        index = scroll + visible - 1
    else:
        index = scroll + int((y - top) // row_h)
    index = max(0, min(count - 1, index))
    return scroll, set(range(min(anchor, index), max(anchor, index) + 1)), scroll_t


def explorer_button_at(buttons, x, y):
    """Returns the key of the first (rectangle, key) button that holds the point, else None."""
    for rect, key in buttons:
        if rect.collidepoint(x, y):
            return key
    return None


# ============================================================================
# SECTION: Joystick events
# ============================================================================


# WARNING: the fallback number must match STICK_MENU_AXIS_THRESHOLD in performance_governor.py; it is used only when that module fails to import.
try:
    import performance_governor as _governor
    JOY_AXIS_THRESHOLD = float(_governor.STICK_MENU_AXIS_THRESHOLD)
except Exception:
    JOY_AXIS_THRESHOLD = 0.6
# VALUES CHANGE HERE
JOY_VERTICAL_AXIS = 1
JOY_HORIZONTAL_AXIS = 0
JOY_ACCEPT_BUTTONS = (0, 7)
JOY_BACK_BUTTONS = (1, 6)


def joystick_action(event):
    """Returns the action a pygame joystick event means (a direction, ACCEPT or BACK), or None."""
    import pygame
    if event.type == pygame.JOYHATMOTION:
        x, y = event.value if isinstance(event.value, tuple) else (0, event.value)
        if y == 1:
            return UP
        if y == -1:
            return DOWN
        if x == -1:
            return LEFT
        if x == 1:
            return RIGHT
    elif event.type == pygame.JOYAXISMOTION:
        if event.axis == JOY_VERTICAL_AXIS:
            if event.value < -JOY_AXIS_THRESHOLD:
                return UP
            if event.value > JOY_AXIS_THRESHOLD:
                return DOWN
        elif event.axis == JOY_HORIZONTAL_AXIS:
            if event.value < -JOY_AXIS_THRESHOLD:
                return LEFT
            if event.value > JOY_AXIS_THRESHOLD:
                return RIGHT
    elif event.type == pygame.JOYBUTTONDOWN:
        if event.button in JOY_ACCEPT_BUTTONS:
            return ACCEPT
        if event.button in JOY_BACK_BUTTONS:
            return BACK
    return None


# ============================================================================
# SECTION: Channel number entry
# ============================================================================


# VALUES CHANGE HERE
DIGIT_ENTRY_MAX = 2
DIGIT_ENTRY_SINGLE_MS = 2500
DIGIT_ENTRY_CONFIRM_MS = 350


def key_to_digit(key):
    """Returns "0" to "9" for a number-row or keypad digit key, else None."""
    import pygame
    if pygame.K_0 <= key <= pygame.K_9:
        return chr(key)
    if pygame.K_KP1 <= key <= pygame.K_KP9:
        return str(key - pygame.K_KP1 + 1)
    if key == pygame.K_KP0:
        return "0"
    return None


def digit_entry_step(buffer, digit, now):
    """Returns (buffer, deadline) after a digit is typed: the digit is added while the buffer has room, and the deadline is when the typed number is tuned (short once the buffer is full, long while it waits for a second digit)."""
    if len(buffer) < DIGIT_ENTRY_MAX:
        buffer = buffer + digit
    wait = DIGIT_ENTRY_CONFIRM_MS if len(buffer) >= DIGIT_ENTRY_MAX else DIGIT_ENTRY_SINGLE_MS
    return buffer, now + wait


def digit_entry_due(buffer, deadline, now):
    """Returns True when a typed number is waiting and its deadline has passed."""
    return bool(buffer) and now >= deadline


def digit_entry_channel(buffer):
    """Returns the two-character channel number a typed buffer stands for."""
    return buffer.zfill(DIGIT_ENTRY_MAX)


# ============================================================================
# SECTION: Name entry (channel name and server slot name)
# ============================================================================

NAME_SAVE = "save"
NAME_CANCEL = "cancel"
NAME_EDIT = "edit"

# VALUES CHANGE HERE
NAME_ENTRY_CHANNEL_MAX_DEFAULT = 18
NAME_ENTRY_SLOT_MAX = 24


def name_entry_step(key, char, text, all_selected, max_len, cancel_on_back=False):
    """Returns (result, text, all_selected) for one key in a name box: NAME_SAVE for Enter, NAME_CANCEL for Escape when cancel_on_back is True, NAME_EDIT with the new text when Backspace deletes (everything when all_selected, else the last character) or a letter, digit or space is typed (replacing everything when all_selected, else added while there is room), and None with nothing changed for any other key."""
    import pygame
    action = key_to_action(key, parent=True)
    if cancel_on_back and action == BACK:
        return NAME_CANCEL, text, all_selected
    if action == ACCEPT:
        return NAME_SAVE, text, all_selected
    if action == PARENT:
        return NAME_EDIT, "" if all_selected else text[:-1], False
    typed = " " if key == pygame.K_SPACE else (char if char.isalnum() else None)
    if typed is None:
        return None, text, all_selected
    if all_selected:
        return NAME_EDIT, typed, False
    if len(text) < max_len:
        return NAME_EDIT, text + typed, all_selected
    return None, text, all_selected


# ============================================================================
# SECTION: DVD controls
# ============================================================================

DVD_PLAY_PAUSE = "play_pause"
DVD_REWIND = "rewind"
DVD_FAST_FORWARD = "fast_forward"
DVD_RETURN_MENU = "return_to_menu"
DVD_ACTIONS = (DVD_PLAY_PAUSE, DVD_REWIND, DVD_FAST_FORWARD, DVD_RETURN_MENU)

DVD_RETURN_IGNORE = "ignore"
DVD_RETURN_REISSUE = "reissue"
DVD_RETURN_CONFIRM = "confirm"

# WARNING: the three canonical codes are negative so they can never equal a real pygame key; game_deck translates a remapped key to its code and ui.py stores the bindings.
DVD_CANON_PLAY_PAUSE = -9001
DVD_CANON_REWIND = -9002
DVD_CANON_FAST_FORWARD = -9003

# WARNING: a skip never lands within DVD_SKIP_END_GUARD_MS of the real end, or playback runs past the length libVLC knows.
# VALUES CHANGE HERE
_SDLK_SCANCODE_MASK = 0x40000000
DVD_REMOTE_PLAY_PAUSE = _SDLK_SCANCODE_MASK | 261
DVD_REMOTE_REWIND = _SDLK_SCANCODE_MASK | 285
DVD_REMOTE_FAST_FORWARD = _SDLK_SCANCODE_MASK | 286
DVD_SKIP_TAP_MS = 30000
DVD_SKIP_HOLD_MS = 5000
DVD_SKIP_END_GUARD_MS = 10000
DVD_SKIP_COOLDOWN_MS = 400
DVD_SKIP_HOLD_START_MS = 500


def dvd_canonical(action):
    """Returns the key code the DVD input works with for an action: a negative code for play, rewind and fast forward, Backspace for return to menu."""
    import pygame
    return {DVD_PLAY_PAUSE: DVD_CANON_PLAY_PAUSE, DVD_REWIND: DVD_CANON_REWIND, DVD_FAST_FORWARD: DVD_CANON_FAST_FORWARD, DVD_RETURN_MENU: pygame.K_BACKSPACE}[action]


def dvd_default_binding(action):
    """Returns the key a DVD action is bound to until the person remaps it."""
    import pygame
    return {DVD_PLAY_PAUSE: DVD_REMOTE_PLAY_PAUSE, DVD_REWIND: DVD_REMOTE_REWIND, DVD_FAST_FORWARD: DVD_REMOTE_FAST_FORWARD, DVD_RETURN_MENU: pygame.K_BACKSPACE}[action]


def dvd_action(key, remote=False):
    """Returns the DVD action a canonical key means, or None; with remote=True the remote's own rewind and fast-forward keys count for those two."""
    for action in DVD_ACTIONS:
        if key == dvd_canonical(action):
            return action
    if remote:
        if key == DVD_REMOTE_FAST_FORWARD:
            return DVD_FAST_FORWARD
        if key == DVD_REMOTE_REWIND:
            return DVD_REWIND
    return None


def dvd_translate(bindings, key):
    """Returns the canonical key for a pressed key under the person's bindings (a bound key becomes its action's code), or the key itself when none is bound to it."""
    if not bindings:
        return key
    for action in DVD_ACTIONS:
        mapped = bindings.get(action)
        if mapped is not None and key == mapped:
            return dvd_canonical(action)
    return key


def dvd_return_menu_step(is_disc_image, menu_up):
    """Returns what the return-to-menu key does: DVD_RETURN_IGNORE for a plain video file, DVD_RETURN_REISSUE when a disc menu is already up, else DVD_RETURN_CONFIRM."""
    if not is_disc_image:
        return DVD_RETURN_IGNORE
    return DVD_RETURN_REISSUE if menu_up else DVD_RETURN_CONFIRM


def dvd_swallows_transport(key, is_disc_image, menu_up):
    """Returns True when a canonical play, rewind or fast-forward key does nothing because a disc menu is up."""
    return bool(is_disc_image and menu_up and dvd_action(key) in (DVD_PLAY_PAUSE, DVD_REWIND, DVD_FAST_FORWARD))


def dvd_skip_blocked(now, cooldown):
    """Returns True while a skip is still inside its cooldown."""
    return now <= cooldown


def dvd_tap_seek(direction, position, length):
    """Returns the new position after a skip tap (+1 forward, -1 back); forward never goes within the end guard of the end."""
    target = position + direction * DVD_SKIP_TAP_MS
    if direction > 0 and length and length > 0:
        target = min(target, max(0, length - DVD_SKIP_END_GUARD_MS))
    return max(0, target)


def dvd_hold_seek(direction, position, length):
    """Returns the new position after one step of a held skip; forward never goes within the end guard of the end."""
    target = position + direction * DVD_SKIP_HOLD_MS
    if direction > 0 and length and length > 0:
        target = min(target, max(0, length - DVD_SKIP_END_GUARD_MS))
    return max(0, target)


def dvd_hold_direction(forward_down, rewind_down):
    """Returns +1 while fast forward is held, else -1 while rewind is held, else 0."""
    if forward_down:
        return 1
    if rewind_down:
        return -1
    return 0


def dvd_hold_due(held_ms, now, cooldown):
    """Returns True when a held skip key has been down long enough and its cooldown is over, so it seeks again."""
    return held_ms > DVD_SKIP_HOLD_START_MS and now > cooldown


# ============================================================================
# SECTION: Exports
# ============================================================================

__all__ = [
    "UP", "DOWN", "LEFT", "RIGHT", "DIRECTIONS", "TAB_BAR", "PANEL", "CLOSE",
    "ACCEPT", "BACK", "Remember", "NavTable", "key_to_direction", "key_to_action", "action_keys", "action_key", "pressed_actions", "is_accept", "is_back", "navigate",
    "SLIDER_STEP", "SLIDER_SPECS", "slider_adjust",
    "system_cell", "system_index", "system_table",
    "advanced_cell", "advanced_index", "advanced_table",
    "GAMES_SUB_MENU_LEFT", "GAMES_SUB_MENU_RIGHT", "GAMES_SUB_RIGHT_COLUMN", "games_sub_cell", "games_sub_index", "games_sub_table",
    "DVD_MAP_BUTTONS", "dvd_map_cell", "dvd_map_index", "dvd_map_table",
    "SAVE_PROFILE_ACTIVE", "SAVE_PROFILE_LEFT", "SAVE_PROFILE_RIGHT", "save_profile_rows", "save_profile_cell", "save_profile_index", "save_profile_table",
    "STICK_DZ_SWITCH", "STICK_DZ_DEADZONE", "STICK_DZ_SENSITIVITY", "STICK_DZ_AXIS", "STICK_DZ_RESET", "STICK_DZ_PLAYERS", "STICK_DZ_PLAYER_COUNT", "stick_dz_player_step", "STICK_DZ_SETTINGS", "stick_dz_rows", "stick_dz_cell", "stick_dz_index", "stick_dz_table",
    "CTRL_MAP_SQUARES", "CTRL_MAP_BUTTONS", "CTRL_MAP_STICK", "ctrl_map_rows", "ctrl_map_cell", "ctrl_map_index", "ctrl_map_table", "ctrl_map_squares_step",
    "GAMES_COLS", "GAMES_ROWS_PER_PAGE", "GAMES_PAGE_SIZE", "games_cell", "games_index", "games_table", "games_page_of", "games_page_count",
    "CHANNELS_COLS", "CHANNELS_ROWS_PER_PAGE", "CHANNELS_TOTAL", "CHANNELS_PAGE_SIZE", "CHANNELS_NUM_PAGES",
    "channels_cell", "channels_index", "channels_table", "channels_page_of",
    "channel_settings_cell", "channel_settings_index", "channel_settings_table",
    "repeat_allowed", "NO_REPEAT_LAYERS", "NAV_DUP_HELD_MS", "NAV_DUP_PATH_MS", "press_is_duplicate", "VIDEO_CELLS", "VIDEO_RADIOS", "VIDEO_SLIDERS", "video_cell", "video_index", "video_table",
    "THEME_LETTERBOX", "THEME_STATIC", "THEME_SLIDERS", "theme_cell", "theme_index", "theme_row2_column", "theme_table",
    "CONFIRM_YES", "CONFIRM_NO", "CONFIRM_CHECK_YES", "CONFIRM_CHECK_NO", "SPLASH_BACK", "SPLASH_NEXT",
    "confirm_cell", "confirm_answer", "confirm_focus", "confirm_table", "splash_cell", "splash_focus", "splash_table",
    "CHANNEL_SUB_MAX_COL", "channel_sub_layout", "channel_sub_close_row", "channel_sub_cell", "channel_sub_index", "channel_sub_table",
    "TAB_PANEL_LAYERS", "tabs_cell", "tabs_table",
    "SERVER_SLOT_CELLS", "column_move", "ring_move", "server_slots_cell", "server_slots_index", "server_slots_table",
    "GUIDE_ROW", "GUIDE_TIME", "guide_move", "guide_row_step",
    "DECK_PREV", "DECK_NEXT", "DECK_TO_ARROWS", "DECK_TO_LIST", "DECK_ACTIVATE", "DECK_SELECT_UP", "DECK_SELECT_DOWN",
    "deck_browse_step", "deck_list_step", "deck_arrows_step", "deck_confirm_choice",
    "HOLD_STARTED", "HOLD_WAIT", "HOLD_REPEAT", "HOLD_STUCK", "DROP_FIRST", "DROP_SECOND", "DROP_BOTH", "DROP_OLDER",
    "HoldRule", "HOLD_RULES", "HOLD_STUCK_MS", "VOLUME_STEP", "volume_direction", "volume_next", "HoldState", "hold_rule", "hold_due", "hold_stuck", "hold_step", "hold_pick", "hold_released", "hold_conflict",
    "PARENT", "EXPLORER_WHEEL_ROWS", "EXPLORER_DOUBLE_CLICK_MS", "EXPLORER_DRAG_START_PX", "EXPLORER_DRAG_EDGE_ZONE_PX", "EXPLORER_DRAG_SCROLL_SLOW_MS", "EXPLORER_DRAG_SCROLL_FAST_MS",
    "EXPLORER_THUMB_MIN_H", "EXPLORER_SB_WIDTH", "EXPLORER_SB_PAD", "explorer_hit", "explorer_in_band", "explorer_visible_rows", "explorer_row", "explorer_row_at", "explorer_list_pick",
    "explorer_in_remove_zone", "explorer_wheel", "explorer_wheel_button", "explorer_thumb", "explorer_scroll_from_thumb", "explorer_scrollbar_track", "explorer_scrollbar_press",
    "explorer_scrollbar_drag", "explorer_modifiers", "explorer_select", "explorer_double_click", "explorer_drag_moved", "explorer_drag_step", "explorer_button_at",
    "JOY_AXIS_THRESHOLD", "JOY_VERTICAL_AXIS", "JOY_HORIZONTAL_AXIS", "JOY_ACCEPT_BUTTONS", "JOY_BACK_BUTTONS", "joystick_action",
    "DIGIT_ENTRY_MAX", "DIGIT_ENTRY_SINGLE_MS", "DIGIT_ENTRY_CONFIRM_MS", "key_to_digit", "digit_entry_step", "digit_entry_due", "digit_entry_channel",
    "NAME_SAVE", "NAME_CANCEL", "NAME_EDIT", "NAME_ENTRY_CHANNEL_MAX_DEFAULT", "NAME_ENTRY_SLOT_MAX", "name_entry_step",
    "DVD_PLAY_PAUSE", "DVD_REWIND", "DVD_FAST_FORWARD", "DVD_RETURN_MENU", "DVD_ACTIONS", "DVD_RETURN_IGNORE", "DVD_RETURN_REISSUE", "DVD_RETURN_CONFIRM",
    "DVD_CANON_PLAY_PAUSE", "DVD_CANON_REWIND", "DVD_CANON_FAST_FORWARD", "DVD_REMOTE_PLAY_PAUSE", "DVD_REMOTE_REWIND", "DVD_REMOTE_FAST_FORWARD",
    "DVD_SKIP_TAP_MS", "DVD_SKIP_HOLD_MS", "DVD_SKIP_END_GUARD_MS", "DVD_SKIP_COOLDOWN_MS", "DVD_SKIP_HOLD_START_MS",
    "dvd_canonical", "dvd_default_binding", "dvd_action", "dvd_translate", "dvd_return_menu_step", "dvd_swallows_transport", "dvd_skip_blocked",
    "dvd_tap_seek", "dvd_hold_seek", "dvd_hold_direction", "dvd_hold_due",
]
