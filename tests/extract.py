"""
Pulls specific, self-contained functions out of retro_tv_emulator.py by exact
source line range, and execs each one into a minimal isolated namespace (with
only the module-level names it actually touches stubbed in) -- rather than
re-typing/paraphrasing the logic into a "mirror" that could quietly drift
from the real file. This is real code under test, just extracted instead of
imported, because the full module boots the actual app on import (real
windows/threads, a hard pygame.mixer audio-device dependency) and can't be
imported as a library -- confirmed directly earlier in this project.
"""
import ast
import datetime
import os
import sys

# Anchored to this file's own location, not the current working directory --
# so it works no matter where you run it from. This assumes extract.py itself
# lives in root/tests/, with retro_tv_emulator.py one folder up in the project
# root (root/retro_tv_emulator.py, root/tests/extract.py). If you move
# extract.py somewhere else, update this path.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_THIS_DIR)
SRC_PATH = os.path.join(_ROOT_DIR, "retro_tv_emulator.py")

# Test output goes to root/temp -- the same folder the app writes app_log.txt,
# vlc-debug.log and crash_log.txt to (see _log_output_dir in
# retro_tv_emulator.py) -- so a test run's output lands right next to them.
TEMP_DIR = os.path.join(_ROOT_DIR, "temp")


class _Tee:
    """Writes to the real stream and to the output file, so the console shows
    exactly what it always did."""

    def __init__(self, stream, file):
        self._stream = stream
        self._file = file

    def write(self, text):
        self._stream.write(text)
        try:
            self._file.write(text)
        except Exception:
            pass
        return len(text)

    def flush(self):
        self._stream.flush()
        try:
            self._file.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def run_logged(script_path, run):
    """Run `run()` (the test script's own runner, e.g. unittest.main) with
    everything it prints also saved to root/temp/<script>_output.txt. The
    previous run's file is kept as <script>_output_previous.txt, the same way
    the app keeps vlc-debug_previous.log. Exit status and console output are
    unchanged."""
    name = os.path.splitext(os.path.basename(script_path))[0]
    os.makedirs(TEMP_DIR, exist_ok=True)
    out_path = os.path.join(TEMP_DIR, name + "_output.txt")
    try:
        os.replace(out_path, os.path.join(TEMP_DIR, name + "_output_previous.txt"))
    except FileNotFoundError:
        pass
    old_out, old_err = sys.stdout, sys.stderr
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("=== %s run %s ===\n" % (name, datetime.datetime.now().isoformat(timespec="seconds")))
        sys.stdout, sys.stderr = _Tee(old_out, f), _Tee(old_err, f)
        try:
            run()
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            f.flush()
            print("[test output saved to %s]" % out_path)


def _get_function_source(name, start_hint=None):
    """Extract exactly one top-level function's source by name, using the
    real AST (not a fragile line-slice by hand) so indentation/boundaries
    are always exactly correct regardless of surrounding edits."""
    with open(SRC_PATH, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    candidates = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    if not candidates:
        raise LookupError(f"function {name!r} not found at module level in {SRC_PATH}")
    if len(candidates) > 1 and start_hint is not None:
        candidates.sort(key=lambda n: abs(n.lineno - start_hint))
    node = candidates[0]
    start = node.lineno - 1
    end = node.end_lineno
    return "".join(lines[start:end])


def load_function(name, namespace=None, start_hint=None):
    """Exec the extracted source into a fresh namespace (seeded with
    `namespace`, e.g. a stub `log`) and return the callable."""
    src = _get_function_source(name, start_hint=start_hint)
    ns = dict(namespace or {})
    exec(compile(src, filename=f"<extracted:{name}>", mode="exec"), ns)
    return ns[name]


if __name__ == "__main__":
    class _StubLog:
        def warning(self, *a, **k): pass
        def info(self, *a, **k): pass
        def error(self, *a, **k): pass

    fn = load_function("_reconcile_cached_lineup", {"log": _StubLog()})
    print("Extracted OK:", fn)
    fn2 = load_function("_media_probe_fingerprint_stale")
    print("Extracted OK:", fn2)
