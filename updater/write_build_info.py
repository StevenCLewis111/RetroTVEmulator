"""Writes build_info.py (built into the exe) and, with --stamp-dir, a protected version.json; run it before building the exe.

Usage: python write_build_info.py 1.0.3 Alpha [--stamp-dir "C:\\path\\to\\Retro TV Emulator"]
"""

import argparse
import json
import os
import re
import sys

BUILD_TYPES = ("Alpha", "Beta", "Release")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("build_type", type=str.capitalize, choices=BUILD_TYPES)
    parser.add_argument("--stamp-dir", dest="stamp_dir")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        print("The version must look like 1.0.3", file=sys.stderr)
        return 2
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "build_info.py"), "w", encoding="utf-8") as handle:
        handle.write(
            "# VALUES CHANGE HERE: written by write_build_info.py before each build; empty in a source run, which then reads version.json instead.\n"
            f'BUILD_VERSION = "{args.version}"\n'
            f'BUILD_TYPE = "{args.build_type}"\n'
        )
    print(f"build_info.py written: version {args.version}, {args.build_type}")
    if args.stamp_dir:
        path = os.path.join(args.stamp_dir, "version.json")
        os.makedirs(args.stamp_dir, exist_ok=True)
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(path, 0x80)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": args.version, "build": args.build_type}, handle)
        if os.name == "nt":
            ctypes.windll.kernel32.SetFileAttributesW(path, 0x1 | 0x2 | 0x4)
        print(f"version.json written and protected in {args.stamp_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
