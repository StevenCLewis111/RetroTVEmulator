"""The TV guide's prepared table: each channel's cells for the guide's 7 visible days, built in the background from the schedule the hub keeps, so the guide only reads it and never works a schedule out while it is on screen."""
# WARNING: owner 2026-11-23: "why is the tv guide not just grabbing the already known schedule, caching the 7 visible days of scheduling, and updating it once a day at midnight"; the table is built at boot, moved on at midnight, and a channel is built again only when the hub says that channel's schedule changed. The guide reads it and nothing else.
import bisect
import threading

# ============================================================================
# SECTION: Layers and the stored tables
# ============================================================================
# WARNING: how a hub row is ranked when two rows claim the same time: what aired (or is airing) wins over the block's plan, and the plan wins over the projected future.
LAYER_PAST = "past"
LAYER_PLAN = "plan"
LAYER_FUTURE = "future"
LAYERS = (LAYER_PAST, LAYER_PLAN, LAYER_FUTURE)

_tables = {}
_lock = threading.Lock()
_version = [0]


def _key(channel):
    return str(channel).rsplit(":", 1)[-1].zfill(2)


# ============================================================================
# SECTION: Building one channel's cells
# ============================================================================


def row_cell(row):
    """One hub row as a cell (start epoch, end epoch, entry), the entry holding the row's saved guide facts; None for a row with no facts, no file or no length."""
    facts = row.get("guide_entry")
    if not facts or not row.get("path"):
        return None
    length = float(row.get("used_length") or 0)
    if length <= 0 or row.get("planned_start") is None:
        return None
    start = float(row["planned_start"])
    entry = dict(facts)
    entry.setdefault("path", row["path"])
    entry["duration"] = length
    entry.setdefault("is_commercial", row.get("kind") == "commercial")
    return (start, start + length, entry)


def layer_cells(rows, now_epoch=None):
    """The cells of one layer's rows in start order, overlaps handled as tidy_cells does."""
    return tidy_cells((c for c in (row_cell(r) for r in rows) if c is not None), now_epoch)


def _moved_to(cell, start):
    """The cell moved to begin at start, keeping its length and entry."""
    return (start, start + (cell[1] - cell[0]), cell[2])


def tidy_cells(cells, now_epoch=None):
    """Cells in start order. A cell that starts at or after now_epoch and overlaps the one before it is moved to begin when that one ends, and the cells after it move with it; a cell before now_epoch (or with no now_epoch) that overlaps the one before it is left out, and so is one before now_epoch that runs into a cell still to come."""
    # WARNING: owner 2026-11-23: "it should never skip the next show bc it didnt fit anymore, you shift the schedule to make it fit". Only what is still to come is moved; history is drawn as it aired.
    # WARNING: a layer's rows come from one timeline and follow each other; a row that began before now and runs into one still to come never aired, so it is the one left out and it moves nothing.
    out = []
    for cell in sorted(cells, key=lambda c: c[0]):
        to_come = now_epoch is not None and cell[0] >= now_epoch
        if out and cell[0] < out[-1][1] - 0.5:
            if not to_come:
                continue
            while out and out[-1][0] < now_epoch and cell[0] < out[-1][1] - 0.5:
                out.pop()
            if out and cell[0] < out[-1][1] - 0.5:
                cell = _moved_to(cell, out[-1][1])
        out.append(cell)
    return out


def combine(layers, now_epoch=None):
    """The cells of the past, plan and future layers laid together in that order of rank. A lower-ranked cell that starts at or after now_epoch and runs into a higher-ranked cell is moved to begin when that cell ends (the higher one overflows into it), and the cells after it move with it; one that starts before now_epoch is kept only where no higher-ranked cell already claims its time. Returns the cells in start order."""
    # WARNING: owner 2026-11-23: a show that runs over keeps playing and the schedule after it moves to fit; the guide dropped a show that overlapped the one before it by a few seconds and drew OFF AIR in its place.
    kept, starts = [], []
    for name in LAYERS:
        for cell in sorted(layers.get(name) or [], key=lambda c: c[0]):
            to_come = now_epoch is not None and cell[0] >= now_epoch
            while True:
                i = bisect.bisect_right(starts, cell[0])
                if i > 0 and kept[i - 1][1] > cell[0] + 0.5:
                    blocker = kept[i - 1]
                elif i < len(kept) and kept[i][0] < cell[1] - 0.5:
                    blocker = kept[i]
                else:
                    break
                if not to_come:
                    cell = None
                    break
                cell = _moved_to(cell, blocker[1])
            if cell is None:
                continue
            kept.insert(i, cell)
            starts.insert(i, cell[0])
    return kept


# ============================================================================
# SECTION: Storing and reading the table
# ============================================================================


def put(channel, start_epoch, end_epoch, cells):
    """Replaces a channel's table with these cells (start epoch, end epoch, entry) in start order, for the span start_epoch to end_epoch. Returns True when the channel's cells differ from the ones it had, False when nothing a viewer would see changed (the table and the version are then left as they were)."""
    cells = list(cells)
    table = {"start": float(start_epoch), "end": float(end_epoch), "cells": cells, "ends": [c[1] for c in cells],
             "shape": [(round(c[0], 1), round(c[1], 1), c[2].get("path"), bool(c[2].get("is_commercial"))) for c in cells]}
    with _lock:
        held = _tables.get(_key(channel))
        # WARNING: a show starting or ending moves its row from plan to history without changing any cell; the guide is drawn again only when a cell or the span changed.
        if held is not None and held["shape"] == table["shape"] and held["start"] == table["start"] and held["end"] == table["end"]:
            return False
        _version[0] += 1
        table["version"] = _version[0]
        _tables[_key(channel)] = table
        return True


def forget(channel=None):
    """Drops one channel's table, or every table."""
    with _lock:
        _version[0] += 1
        if channel is None:
            _tables.clear()
        else:
            _tables.pop(_key(channel), None)


def version():
    """A number that changes whenever any channel's table is replaced or dropped."""
    return _version[0]


def table(channel):
    """The channel's table as last built, or None when it has none yet."""
    return _tables.get(_key(channel))


def cells_between(channel, start_epoch, end_epoch):
    """The cells of the channel's table that touch the span, in start order, or None when the channel has no table yet."""
    found = _tables.get(_key(channel))
    if found is None:
        return None
    cells, ends = found["cells"], found["ends"]
    out = []
    i = bisect.bisect_right(ends, float(start_epoch))
    while i < len(cells) and cells[i][0] < end_epoch:
        out.append(cells[i])
        i += 1
    return out
