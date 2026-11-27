"""Low-noise diagnostics shared by the Retro TV server feature.

Emits the first occurrence of a diagnostic event and at most one reminder
per key per interval, so repeated retries don't flood the log. Dependency-free
and safe to call from worker threads.

Owns: the coalescing breadcrumb logger.
Does not own: logger configuration (each service configures its own handlers).

WARNING: diagnostics must never become another failure path, so logging errors
are swallowed inside breadcrumb().
"""
from __future__ import annotations

import logging
import threading
import time


# ============================================================================
# SECTION: State
# ============================================================================
import sys as _sys

# WARNING: the update helper exe loads this file on its own, so it never imports performance_governor; it reads SERVER_DIAGNOSTICS_REMINDER_SECONDS only when the program has it loaded, and the fallback must match it.
_loaded_governor = _sys.modules.get("performance_governor")
DEFAULT_INTERVAL_SECONDS = getattr(_loaded_governor, "SERVER_DIAGNOSTICS_REMINDER_SECONDS", 60.0) if _loaded_governor is not None else 60.0
_state = {}
_lock = threading.Lock()


# ============================================================================
# SECTION: Public API
# ============================================================================


def breadcrumb(logger: logging.Logger, level: int, key: str, message: str,
               *args, interval: float = DEFAULT_INTERVAL_SECONDS) -> bool:
    """Log a bounded diagnostic event.

    Returns True when a record was emitted, False when this occurrence was
    coalesced. The next emitted record includes the number of suppressed
    occurrences.
    """
    now = time.monotonic()
    key = str(key)
    with _lock:
        previous = _state.get(key)
        if previous is not None and now - previous[0] < max(0.0, float(interval)):
            _state[key] = (previous[0], previous[1] + 1)
            return False
        suppressed = 0 if previous is None else previous[1]
        _state[key] = (now, 0)

    if suppressed:
        message = message + " suppressed_since_last=%d"
        args = (*args, suppressed)
    try:
        logger.log(level, message, *args)
    except Exception:
        # WARNING: swallowed on purpose; see module docstring.
        pass
    return True


__all__ = ["DEFAULT_INTERVAL_SECONDS", "breadcrumb"]