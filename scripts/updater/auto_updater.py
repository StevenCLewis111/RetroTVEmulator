"""Small, dependency-free updater for the Retro TV Emulator.

Owns: the update source (the GitHub Releases of the project), package download,
SHA-256 and zip-safety checks, staging, the background "is there an update"
check, and handing a staged payload to update_helper.py.
Does not own: the file swap itself (update_helper.py, a separate process) or
any update UI (retro_tv_emulator.py).

WARNING: this module must never replace a running executable itself. It
downloads and validates a hash-checked zip, stages it in a private update
directory, and asks update_helper.py to do the Windows file swap after this
process exits.

How an update is found: the app lists the repository's releases (pre-releases
included), takes every zip with a V1.2.3 style number in its name, picks the
highest, and offers it when that number is higher than the installed one. Nothing
inside the zip is compared. The download is checked against the SHA-256 that
GitHub reports for the uploaded file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# ============================================================================
# SECTION: Server-path bootstrap
# ============================================================================
# WARNING: server_diagnostics.py is in scripts/server: one folder up from this file in a source copy (../server), under scripts/server beside a build; its import must stay after _add_server_path().
def _add_server_path():
    if getattr(sys, "frozen", False):
        _base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        _base = os.path.dirname(os.path.abspath(__file__))
    for _candidate in (os.path.join(_base, "scripts", "server"), os.path.join(_base, "..", "server")):
        if os.path.isdir(_candidate):
            sys.path.insert(0, _candidate)
            return
    sys.path.insert(0, os.path.join(_base, "..", "server"))


_add_server_path()
from server_diagnostics import breadcrumb as _server_breadcrumb

try:
    import performance_governor as _perf_governor
except Exception as _governor_error:
    _perf_governor = None
    logging.getLogger("retro_tv.auto_updater").warning("[FAILURE] auto_updater: performance_governor did not import, so the built-in update numbers are used: %s", _governor_error)


def _log_swallowed(where, error):
    """Writes the shared, counted [FAILURE] line through performance_governor (a plain warning when it did not import)."""
    if _perf_governor is not None:
        return _perf_governor.log_swallowed(where, error)
    logging.getLogger("retro_tv.auto_updater").warning("[FAILURE] %s: %s: %s", where, type(error).__name__, error)
    return None


# ============================================================================
# SECTION: Constants & errors
# ============================================================================


log = logging.getLogger("retro_tv.auto_updater")

UPDATE_DIR_NAME = ".retro_tv_updates"
STAGED_RECORD_NAME = "staged_update.json"
PENDING_RECORD_NAME = "pending_update.json"
# WARNING: the fallback numbers below must match UPDATE_LEFTOVER_MIN_AGE_S, UPDATE_PENDING_MAX_AGE_S, UPDATE_HELPER_ALIVE_CHECK_S in performance_governor.py; they are used only when that module fails to import.
UPDATE_LEFTOVER_MIN_AGE_S = _perf_governor.UPDATE_LEFTOVER_MIN_AGE_S if _perf_governor is not None else 1800.0
UPDATE_PENDING_MAX_AGE_S = _perf_governor.UPDATE_PENDING_MAX_AGE_S if _perf_governor is not None else 600.0
UPDATE_HELPER_ALIVE_CHECK_S = _perf_governor.UPDATE_HELPER_ALIVE_CHECK_S if _perf_governor is not None else 3.0
# WARNING: the fallback numbers below must match UPDATE_MAX_DOWNLOAD_BYTES, UPDATE_DOWNLOAD_TIMEOUT_SECONDS, UPDATE_CHECK_TIMEOUT_SECONDS, UPDATE_CHECK_INTERVAL_SECONDS and UPDATE_GITHUB_RELEASES_PER_PAGE in performance_governor.py; they are used only when that module fails to import.
MAX_DOWNLOAD_BYTES = _perf_governor.UPDATE_MAX_DOWNLOAD_BYTES if _perf_governor is not None else 4294967296
DOWNLOAD_TIMEOUT_SECONDS = _perf_governor.UPDATE_DOWNLOAD_TIMEOUT_SECONDS if _perf_governor is not None else 60
DEFAULT_TIMEOUT_SECONDS = _perf_governor.UPDATE_CHECK_TIMEOUT_SECONDS if _perf_governor is not None else 15
DEFAULT_CHECK_INTERVAL_SECONDS = _perf_governor.UPDATE_CHECK_INTERVAL_SECONDS if _perf_governor is not None else 21600
VERSION_RE = re.compile(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$")

# WARNING: a release zip is found by the V1.2.3 style number in its file name, e.g. "Retro TV Emulator Alpha V1.0.1.zip"; a zip without one is never offered.
ZIP_VERSION_RE = re.compile(r"[vV](\d+\.\d+\.\d+)")

# VALUES CHANGE HERE: the GitHub update source; the owner/repository whose Releases (pre-releases included) hold the release zips (the environment RETRO_TV_UPDATE_GITHUB_REPO or the config update_github_repo overrides the repository; a blank repository turns this source off)
UPDATE_GITHUB_REPO = "StevenCLewis111/RetroTVEmulator"
GITHUB_API_BASE = "https://api.github.com"
GITHUB_RELEASES_PER_PAGE = _perf_governor.UPDATE_GITHUB_RELEASES_PER_PAGE if _perf_governor is not None else 30


class UpdateError(RuntimeError):
    """An expected, user-displayable update failure."""


# ============================================================================
# SECTION: Local state helpers
# ============================================================================


def _update_dir(base_dir):
    """The update work folder, root/logs/.retro_tv_updates, so a download never shows beside the exe."""
    return os.path.join(os.path.abspath(base_dir), "logs", UPDATE_DIR_NAME)


def _atomic_json_write(path, payload):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".update-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError as fsync_error:
                _log_swallowed("auto_updater: flushing %s to the disk" % os.path.basename(path), fsync_error)
        os.replace(tmp_path, path)
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            # WARNING: the normal end: os.replace already moved the temporary file into place, so there is nothing to remove.
            pass
        except OSError as unlink_error:
            _log_swallowed("auto_updater: removing the temporary copy of %s" % os.path.basename(path), unlink_error)


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return default


# ============================================================================
# SECTION: Build info & version stamp
# ============================================================================

try:
    import build_info as _build_info
except ImportError:
    _build_info = None

BUILD_TYPES = ("Alpha", "Beta", "Release")
VERSION_STAMP_NAME = "version.json"
# VALUES CHANGE HERE: True offers only zips of the installed build type (an Alpha build sees only Alpha zips, a Beta build only Beta zips; a zip name with neither word is Release); False offers the highest number of any type.
UPDATE_MATCHES_BUILD_TYPE = True
_BUILD_TYPE_RE = re.compile(r"(?<![A-Za-z])(alpha|beta)(?![A-Za-z])", re.IGNORECASE)


def build_type_from_name(name):
    """Returns Alpha, Beta or Release from the words in a file name; Release when it holds neither Alpha nor Beta."""
    match = _BUILD_TYPE_RE.search(str(name or ""))
    return match.group(1).capitalize() if match else "Release"


def _baked(attribute):
    return str(getattr(_build_info, attribute, "") or "").strip()


def current_build_type(base_dir):
    """Returns the installed build type: the one built into the exe, then the one in version.json, then the Alpha or Beta word in the exe's name; empty when none is known."""
    baked = _baked("BUILD_TYPE").capitalize()
    if baked in BUILD_TYPES:
        return baked
    stamp = _read_json(os.path.join(base_dir, VERSION_STAMP_NAME), {})
    if isinstance(stamp, dict):
        stamped = str(stamp.get("build", "") or "").strip().capitalize()
        if stamped in BUILD_TYPES:
            return stamped
    for name in (sys.executable, sys.argv[0] if sys.argv else ""):
        match = _BUILD_TYPE_RE.search(os.path.basename(str(name or "")))
        if match:
            return match.group(1).capitalize()
    return ""


def _set_stamp_attributes(path, protected):
    """Marks version.json hidden and system (protected) or plain; does nothing off Windows."""
    # WARNING: never add the read-only flag (0x1) here: Windows refuses to delete a read-only file, so a build folder that had been run could not be replaced by the next build (auto-py-to-exe stopped with Access is denied on version.json); hidden and system already keep it out of sight and out of accidental deletes.
    if os.name != "nt":
        return
    import ctypes
    ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2 | 0x4 if protected else 0x80)


def write_version_stamp(base_dir, version, build="", from_exe=False):
    """Writes version.json with the version and build type and protects it from accidental deletion; returns True when written. from_exe marks a stamp the app wrote from the number built into its own exe; the update helper's stamp never has the mark."""
    path = os.path.join(base_dir, VERSION_STAMP_NAME)
    payload = {"version": str(version)}
    if build:
        payload["build"] = build
    if from_exe:
        payload["from_exe"] = True
    try:
        _set_stamp_attributes(path, False)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return True
    except OSError as exc:
        log.warning("[FAILURE] write_version_stamp: could not write %s: %s", path, exc)
        return False
    finally:
        _set_stamp_attributes(path, True)


_stamp_ahead_warned = set()


def _stamp_ahead_of_exe(base_dir):
    """Returns the version in version.json when the update helper wrote it (no from_exe mark) and it is newer than the number built into this exe, else an empty string.

    WARNING: this is the guard against an update loop. A package built with the wrong number inside its exe (the V1.5.4 zip carried 1.5.3) installs fine, then the exe calls itself the old version and the same release is offered again at every start. The helper's stamp holds the version it really installed, so that number wins and the mistake is logged loudly."""
    baked_version = _baked("BUILD_VERSION")
    if not VERSION_RE.fullmatch(baked_version):
        return ""
    stamp = _read_json(os.path.join(base_dir, VERSION_STAMP_NAME), {})
    if not isinstance(stamp, dict) or stamp.get("from_exe"):
        return ""
    stamped = str(stamp.get("version", "") or "").strip()
    if not VERSION_RE.fullmatch(stamped):
        return ""
    try:
        ahead = parse_version(stamped) > parse_version(baked_version)
    except UpdateError as version_error:
        _log_swallowed("auto_updater: comparing the stamped version %r with the built-in %r" % (stamped, baked_version), version_error)
        return ""
    if not ahead:
        return ""
    if stamped not in _stamp_ahead_warned:
        _stamp_ahead_warned.add(stamped)
        log.warning("[UPDATE DIAG] This exe says it is %s but the update that was installed is %s (version.json, written by the update helper): the package was built with the wrong number inside it. Treating the install as %s so the same update is not offered again; rebuild with write_build_info.py %s.",
                    baked_version, stamped, stamped, stamped)
    return stamped


def _heal_version_stamp(base_dir):
    """Rewrites version.json from the version and build type built into this exe when the file is missing or differs (never over a newer version the update helper recorded)."""
    baked_version = _baked("BUILD_VERSION")
    if not VERSION_RE.fullmatch(baked_version):
        return
    if _stamp_ahead_of_exe(base_dir):
        return
    baked_build = _baked("BUILD_TYPE").capitalize()
    baked_build = baked_build if baked_build in BUILD_TYPES else ""
    stamp = _read_json(os.path.join(base_dir, VERSION_STAMP_NAME), {})
    stamp = stamp if isinstance(stamp, dict) else {}
    if str(stamp.get("version", "") or "").strip() == baked_version and (not baked_build or stamp.get("build") == baked_build):
        return
    write_version_stamp(base_dir, baked_version, baked_build, from_exe=True)


# ============================================================================
# SECTION: Version handling & configuration
# ============================================================================


def parse_version(value):
    """Return a comparable three-integer version tuple."""
    text = str(value or "").strip()
    match = VERSION_RE.fullmatch(text)
    if not match:
        raise UpdateError(f"Invalid version in update manifest: {text or '(blank)'}")
    return tuple(int(part or 0) for part in match.groups())


def version_is_newer(candidate, current):
    return parse_version(candidate) > parse_version(current)


def current_version(base_dir, config=None):
    """Read the installed version without requiring pygame or the media DB: the RETRO_TV_VERSION override, then the version built into the exe, then version.json (written by update_helper.py), then the V1.2.3 number in the running exe's own name; 0.0.0 when none is found."""
    env_version = os.environ.get("RETRO_TV_VERSION")
    if env_version:
        return str(env_version).strip()

    baked_version = _baked("BUILD_VERSION")
    if VERSION_RE.fullmatch(baked_version):
        return _stamp_ahead_of_exe(base_dir) or baked_version

    value = _read_json(os.path.join(base_dir, VERSION_STAMP_NAME), {})
    if isinstance(value, dict):
        value = str(value.get("version", "") or "").strip()
        if VERSION_RE.fullmatch(value):
            return value

    # WARNING: every build's exe name carries its number (Retro TV Emulator Alpha V1.0.1.exe), so a build with no version.json still knows what it is and is not offered an update that would replace it with an older or equal one.
    for name in (sys.executable, sys.argv[0] if sys.argv else ""):
        match = ZIP_VERSION_RE.search(os.path.basename(str(name or "")))
        if match:
            return match.group(1)

    # WARNING: development/source copies have no release stamp, so 0.0.0 is the "no update" baseline.
    return "0.0.0"


def github_settings(config=None):
    """Returns (owner/repository, api_base) for the GitHub update source; the repository is an empty string when none is set."""
    config = config if isinstance(config, dict) else {}
    repo = str(os.environ.get("RETRO_TV_UPDATE_GITHUB_REPO") or config.get("update_github_repo") or UPDATE_GITHUB_REPO or "").strip()
    base = str(os.environ.get("RETRO_TV_UPDATE_GITHUB_API_BASE") or GITHUB_API_BASE).strip().rstrip("/")
    return repo, base


def update_source_configured(config=None):
    """True when there is somewhere to look for an update: a GitHub repository is set."""
    repo, _github_base = github_settings(config)
    return bool(repo)


def version_from_zip_name(name):
    """Returns the 'major.minor.patch' text in a release zip's file name, or None when the name is not a .zip with a V1.2.3 style number."""
    text = str(name or "")
    if not text.lower().endswith(".zip"):
        return None
    match = ZIP_VERSION_RE.search(text)
    return match.group(1) if match else None


# ============================================================================
# SECTION: Release fetch, validation & update check
# ============================================================================


def _validate_url(url, label="URL"):
    parsed = urllib.parse.urlparse(str(url))
    if parsed.scheme.lower() != "https":
        # WARNING: plain HTTP is allowed for a local test server only; never permit a remote plaintext update source.
        host = (parsed.hostname or "").lower()
        if not (parsed.scheme.lower() == "http" and host in ("127.0.0.1", "localhost", "::1")):
            raise UpdateError(f"{label} must use HTTPS")
    if not parsed.netloc:
        raise UpdateError(f"{label} is malformed")
    return str(url)



# ============================================================================
# SECTION: HTTPS with a fallback set of trusted roots
# ============================================================================


def _is_certificate_failure(exc):
    """True when `exc` is a failed check of the server's certificate (a missing or unknown issuer), not a network failure."""
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc)


def _fallback_ssl_contexts():
    """Contexts that still check the server's certificate but trust a bundled set of public roots (certifi's, when installed, then cacert.pem next to this file); used only after Windows' own list failed."""
    contexts = []
    try:
        import certifi
        contexts.append(ssl.create_default_context(cafile=certifi.where()))
    except ImportError:
        # WARNING: the normal end when certifi is not installed: the bundled cacert.pem below is used instead.
        pass
    except Exception as certifi_error:
        _log_swallowed("auto_updater: loading certifi's certificate list", certifi_error)
    bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cacert.pem")
    if os.path.isfile(bundled):
        try:
            contexts.append(ssl.create_default_context(cafile=bundled))
        except Exception as exc:
            log.warning("[UPDATE DIAG] the bundled certificate file could not be read: %s", exc)
    return contexts


def _urlopen(request, timeout):
    """urllib's urlopen; when the server's certificate is not accepted (a PC whose Windows root list is out of date, as on a weak PC that rarely updates) it retries with the bundled public roots. The check is never switched off."""
    # WARNING: owner log 2026-11-06 12:10: a PC online through a dongle failed every update check with CERTIFICATE_VERIFY_FAILED (unable to get local issuer certificate); certificates stay verified, only the set of trusted roots grows.
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except (urllib.error.URLError, OSError) as exc:
        if not _is_certificate_failure(exc):
            raise
        first = exc
    for context in _fallback_ssl_contexts():
        try:
            response = urllib.request.urlopen(request, timeout=timeout, context=context)
            log.info("[UPDATE DIAG] the update server's certificate was accepted with the bundled public roots (Windows' own list rejected it: %s)", first)
            return response
        except (urllib.error.URLError, OSError) as exc:
            if not _is_certificate_failure(exc):
                raise
    raise first


def _fetch_json(url, timeout=DEFAULT_TIMEOUT_SECONDS, what="GitHub releases list", expect_list=False):
    _validate_url(url, "Update source URL")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "RetroTVUpdater/1",
        },
    )
    try:
        with _urlopen(request, timeout) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
    except (urllib.error.URLError, OSError) as exc:
        _server_breadcrumb(
            log, logging.WARNING, f"update-fetch:{url}",
            "[UPDATE DIAG] Update source fetch failed url=%s: %s", url, exc,
        )
        raise UpdateError(f"Could not reach {what}: {exc}") from exc
    if len(raw) > 2 * 1024 * 1024:
        raise UpdateError(f"The {what} is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError(f"The {what} is not valid UTF-8 JSON") from exc
    if expect_list:
        if not isinstance(value, list):
            raise UpdateError(f"The {what} must be a JSON list")
        return value
    if not isinstance(value, dict):
        raise UpdateError(f"The {what} must be a JSON object")
    return value


def validate_manifest(raw):
    """Validates the record built from a GitHub release asset: a display version, an https (or local test) download address and the SHA-256 GitHub reports for the file."""
    if not isinstance(raw, dict):
        raise UpdateError("Update record must be a JSON object")
    version = str(raw.get("version", "") or "").strip() or "Latest Build"
    url = _validate_url(raw.get("url", ""), "Update package URL")
    digest = str(raw.get("sha256", "")).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise UpdateError("Update record has no valid SHA-256 digest")
    package_name = re.sub(
        r"[^A-Za-z0-9._ ()+-]", "_",
        os.path.basename(str(raw.get("file_name") or raw.get("package_name") or "")
                         or urllib.parse.urlparse(url).path),
    ) or "retro-tv-update.zip"
    return {
        "version": version,
        "url": url,
        "sha256": digest,
        "notes": str(raw.get("notes", "") or "").strip(),
        "package_name": package_name,
    }


def check_github_for_update(installed, repo, api_base=GITHUB_API_BASE, timeout=DEFAULT_TIMEOUT_SECONDS, build_type=""):
    """Lists the repository's releases (pre-releases included), picks the zip with the highest V1.2.3 number in its name and reports whether it is newer than the installed build; the download is checked against the SHA-256 GitHub reports for that file."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        raise UpdateError("The GitHub repository is not valid; it must look like owner/name")
    releases = _fetch_json(
        f"{api_base}/repos/{repo}/releases?per_page={GITHUB_RELEASES_PER_PAGE}",
        timeout=timeout, what="GitHub releases list", expect_list=True,
    )
    best = None
    zips_seen = 0
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        for asset in (release.get("assets") or []):
            if not isinstance(asset, dict) or str(asset.get("state", "uploaded")) != "uploaded":
                continue
            version = version_from_zip_name(asset.get("name", ""))
            if version is None:
                continue
            if UPDATE_MATCHES_BUILD_TYPE and build_type and build_type_from_name(asset.get("name", "")) != build_type:
                continue
            zips_seen += 1
            rank = (parse_version(version), str(release.get("published_at", "") or ""))
            if best is None or rank > best[0]:
                best = (rank, version, release, asset)
    if best is None:
        log.info("[UPDATE] source=github repo=%s releases_seen=%d zips_with_a_version=0 installed=%s available=False", repo, len(releases), installed)
        return {
            "available": False,
            "configured": True,
            "current_version": installed,
            "message": f"You are up to date ({installed}).",
        }
    _rank, version, release, asset = best
    digest = str(asset.get("digest", "") or "").strip().lower()
    sha256 = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise UpdateError(f"GitHub reports no SHA-256 for {asset.get('name', 'the update zip')}; upload the zip to the release again")
    record = validate_manifest({
        "version": version,
        "url": asset.get("browser_download_url", ""),
        "sha256": sha256,
        "file_name": asset.get("name", ""),
        "notes": str(release.get("body", "") or "").strip()[:500],
    })
    available = version_is_newer(version, installed)
    return {
        "available": available,
        "configured": True,
        "current_version": installed,
        "manifest": record,
        "message": (
            f"Version {version} is available."
            if available else
            f"You are up to date ({installed})."
        ),
    }


def check_for_update(base_dir, config=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Looks for a newer release on GitHub, returning a UI-friendly result."""
    installed = current_version(base_dir, config)
    try:
        _heal_version_stamp(base_dir)
    except Exception as heal_exc:
        log.warning("[FAILURE] check_for_update: could not refresh %s: %s", VERSION_STAMP_NAME, heal_exc)
    repo, github_base = github_settings(config)
    if not repo:
        return {
            "available": False,
            "configured": False,
            "current_version": installed,
            "message": "Update source is not configured.",
        }
    return check_github_for_update(installed, repo, github_base, timeout=timeout, build_type=current_build_type(base_dir))


# ============================================================================
# SECTION: Package download & extraction
# ============================================================================


def lower_this_thread_priority():
    """Puts the calling thread in Windows background mode (low CPU and low disk priority) so a big download cannot starve the playing video; returns True when it took effect."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        # WARNING: 0x00010000 is THREAD_MODE_BACKGROUND_BEGIN; it lowers the disk priority as well as the CPU priority of this thread only.
        return bool(kernel.SetThreadPriority(kernel.GetCurrentThread(), 0x00010000))
    except Exception as exc:
        log.warning("[UPDATE DIAG] could not lower the download thread priority: %s", exc)
        return False


def _download_package(url, destination, timeout=DEFAULT_TIMEOUT_SECONDS, progress_callback=None):
    _validate_url(url, "Update package URL")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "RetroTVUpdater/1"},
    )
    total = 0
    digest = hashlib.sha256()
    blocks = 0
    slowest_read = slowest_write = 0.0
    started = time.monotonic()
    try:
        with _urlopen(request, timeout) as response:
            content_length = int(response.headers.get("Content-Length", "0") or 0)
            with open(destination, "wb") as output:
                while True:
                    t0 = time.monotonic()
                    block = response.read(256 * 1024)
                    t1 = time.monotonic()
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise UpdateError("Update package exceeds the 4 GiB safety limit")
                    digest.update(block)
                    output.write(block)
                    t2 = time.monotonic()
                    blocks += 1
                    slowest_read = max(slowest_read, t1 - t0)
                    slowest_write = max(slowest_write, t2 - t1)
                    if progress_callback is not None:
                        progress_callback(total, content_length)
                    # WARNING: a short pause after every block hands the CPU and the disk back to the playing video and the main loop.
                    time.sleep(0.002)
    except UpdateError:
        raise
    except (urllib.error.URLError, OSError) as exc:
        _server_breadcrumb(
            log, logging.WARNING, f"update-download:{url}",
            "[UPDATE DIAG] Update package download failed url=%s: %s", url, exc,
        )
        raise UpdateError(f"Could not download update package: {exc}") from exc
    log.info("[UPDATE DIAG] download done: %.0f MB in %.1f s, %d blocks, slowest network read %.2f s, slowest disk write %.2f s",
             total / 1048576.0, time.monotonic() - started, blocks, slowest_read, slowest_write)
    return total, digest.hexdigest()


def _extended(absolute_windows_path):
    """Turns an absolute Windows path into the extended-length form (\\\\?\\...) that lifts the 260-character limit; an already extended path is returned unchanged."""
    if absolute_windows_path.startswith("\\\\?\\"):
        return absolute_windows_path
    if absolute_windows_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute_windows_path[2:]
    return "\\\\?\\" + absolute_windows_path


def _long(path):
    """Returns the path in a form that works past 260 characters on Windows (a deep staged update folder plus the model-cache names exceeds that); other systems get the path unchanged."""
    if os.name != "nt":
        return path
    return _extended(os.path.abspath(path))


def _safe_zip_member(info):
    name = info.filename.replace("\\", "/")
    if not name or name.startswith("/") or re.match(r"^[A-Za-z]:/", name):
        return False
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if ".." in parts:
        return False
    # WARNING: reject Unix symlink entries; extracting one would let the stage escape its folder when the helper copies it.
    mode = (info.external_attr >> 16) & 0xFFFF
    if (mode & 0o170000) == 0o120000:
        return False
    return True


def _extract_package(package_path, stage_root):
    try:
        with zipfile.ZipFile(package_path) as archive:
            members = archive.infolist()
            if not members:
                raise UpdateError("Update package is empty")
            if any(not _safe_zip_member(info) for info in members):
                raise UpdateError("Update package contains an unsafe path")
            # WARNING: members are written one by one through extended-length paths, because the staged folder sits deep inside the install and the whisper model-cache names push a path past Windows' 260-character limit (WinError 3); extractall cannot do that.
            for info in members:
                parts = [part for part in info.filename.replace("\\", "/").split("/") if part not in ("", ".")]
                if not parts:
                    continue
                target = os.path.join(stage_root, *parts)
                if info.is_dir():
                    os.makedirs(_long(target), exist_ok=True)
                    continue
                os.makedirs(_long(os.path.dirname(target)), exist_ok=True)
                with archive.open(info) as source, open(_long(target), "wb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
    except zipfile.BadZipFile as exc:
        raise UpdateError("Downloaded update is not a valid zip archive") from exc

    entries = [
        os.path.join(stage_root, name)
        for name in os.listdir(_long(stage_root))
        if name not in ("__MACOSX",)
    ]
    if len(entries) == 1 and os.path.isdir(_long(entries[0])):
        return entries[0]
    return stage_root


# ============================================================================
# SECTION: Staging
# ============================================================================


def _tree_size(path):
    """Total bytes of the files under a folder (0 when it cannot be read)."""
    total = 0
    for root, _dirs, files in os.walk(_long(path)):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError as size_error:
                _log_swallowed("auto_updater: reading the size of %s" % name, size_error)
    return total


def prune_update_folder(base_dir, now=None, min_age=None, keep_staged=True):
    """Removes what earlier updates left in root/logs/.retro_tv_updates: download folders nothing needs (each holds a whole unpacked copy of the app, close to 1 GiB), a staged record for a version that is already installed, and a pending-install record from a hand-off that failed. Keeps the folder of a staged update that is still newer than the installed version (unless `keep_staged` is False), and any folder younger than `min_age` seconds (a download may be running). Returns (folders removed, bytes freed)."""
    update_dir = _update_dir(base_dir)
    if not os.path.isdir(update_dir):
        return 0, 0
    now = time.time() if now is None else now
    min_age = UPDATE_LEFTOVER_MIN_AGE_S if min_age is None else min_age
    keep_name = None
    record_path = os.path.join(update_dir, STAGED_RECORD_NAME)
    try:
        with open(record_path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        record = None
    if isinstance(record, dict):
        payload = str(record.get("staged_path", ""))
        try:
            still_wanted = bool(payload) and os.path.isdir(_long(payload)) and version_is_newer(str(record.get("version", "")), current_version(base_dir))
        except Exception:
            still_wanted = False
        if still_wanted and keep_staged:
            relative = os.path.relpath(os.path.abspath(payload), update_dir)
            keep_name = relative.split(os.sep)[0]
        else:
            try:
                os.remove(record_path)
            except OSError as record_error:
                _log_swallowed("auto_updater: removing the old staged-update record", record_error)
    pending_path = os.path.join(update_dir, PENDING_RECORD_NAME)
    try:
        if now - os.path.getmtime(pending_path) > UPDATE_PENDING_MAX_AGE_S:
            os.remove(pending_path)
    except FileNotFoundError:
        # WARNING: the normal end: no update is waiting to be installed, so there is no pending record.
        pass
    except OSError as pending_error:
        _log_swallowed("auto_updater: removing an old pending-install record", pending_error)
    removed, freed = 0, 0
    try:
        names = os.listdir(update_dir)
    except OSError:
        return 0, 0
    for name in names:
        if not (name.startswith("download-") or (name.startswith(".update-") and name.endswith(".tmp"))):
            continue
        if name == keep_name:
            continue
        full = os.path.join(update_dir, name)
        try:
            if now - os.path.getmtime(full) < min_age:
                continue
        except OSError as age_error:
            _log_swallowed("auto_updater: reading the age of the leftover %s" % name, age_error)
            continue
        size = _tree_size(full) if os.path.isdir(full) else 0
        if os.path.isdir(full):
            shutil.rmtree(_long(full), ignore_errors=True)
        else:
            try:
                size = os.path.getsize(full)
                os.remove(full)
            except OSError as leftover_error:
                _log_swallowed("auto_updater: removing the leftover %s" % name, leftover_error)
        if not os.path.exists(full):
            removed += 1
            freed += size
    return removed, freed


def stage_update(base_dir, manifest, timeout=DEFAULT_TIMEOUT_SECONDS, progress_callback=None):
    """Download, hash-check, zip-check, and atomically record a staged update."""
    manifest = validate_manifest(manifest)
    update_dir = _update_dir(base_dir)
    os.makedirs(update_dir, exist_ok=True)
    # WARNING: this is the only place a download starts, so every older download folder is a leftover; clearing them first keeps the folder from growing by a full copy of the app on each attempt.
    prune_update_folder(base_dir, min_age=0.0, keep_staged=False)
    work_dir = tempfile.mkdtemp(prefix="download-", dir=update_dir)
    package_path = os.path.join(work_dir, manifest["package_name"])
    stage_path = os.path.join(work_dir, "payload")
    try:
        _bytes, digest = _download_package(manifest["url"], package_path, timeout=max(timeout, DOWNLOAD_TIMEOUT_SECONDS), progress_callback=progress_callback)
        if digest.lower() != manifest["sha256"].lower():
            _server_breadcrumb(
                log, logging.WARNING, f"update-sha-mismatch:{manifest['version']}",
                "[UPDATE DIAG] Update package SHA-256 mismatch version=%s", manifest["version"],
            )
            raise UpdateError("Update package SHA-256 does not match what GitHub reports")
        os.makedirs(_long(stage_path), exist_ok=True)
        payload_root = _extract_package(package_path, stage_path)
        # WARNING: the helper deletes only the folder holding the payload, so a package with one root folder would leave this near-1 GiB zip behind on every update.
        try:
            os.remove(package_path)
        except OSError as package_error:
            _log_swallowed("auto_updater: removing the unpacked download's zip", package_error)
        record = {
            "version": manifest["version"],
            "notes": manifest.get("notes", ""),
            "staged_path": os.path.abspath(payload_root),
            "created_at": time.time(),
        }
        _atomic_json_write(os.path.join(update_dir, STAGED_RECORD_NAME), record)
        return record
    except Exception:
        shutil.rmtree(_long(work_dir), ignore_errors=True)
        raise


# ============================================================================
# SECTION: Background check
# ============================================================================


def background_check_loop(base_dir, config_provider, result_callback, stop_event,
                          initial_delay=120, interval=DEFAULT_CHECK_INTERVAL_SECONDS):
    """Check the update source quietly; never download or install in the background.

    The first check waits until normal boot work has settled.  Subsequent
    checks are deliberately infrequent, and each check reads configuration
    again so changing the repository takes effect without a
    restart.  A manual button press is still required to download and stage
    anything.
    """
    if stop_event.wait(initial_delay):
        return
    while not stop_event.is_set():
        try:
            config = config_provider() or {}
            if update_source_configured(config):
                result = check_for_update(base_dir, config=config)
                if result_callback is not None:
                    result_callback(result)
        except Exception as exc:
            # WARNING: this loop ticks hours apart, so a failure here cannot spam the log; interval=0.0 writes every one.
            _server_breadcrumb(
                log, logging.WARNING, "background-update-check",
                "[UPDATE DIAG] Background update check failed: %s", exc,
                interval=0.0,
            )
            if result_callback is not None:
                result_callback({
                    "available": False,
                    "configured": True,
                    "error": str(exc),
                    "message": f"Background update check failed: {exc}",
                })
        if stop_event.wait(interval):
            return


# ============================================================================
# SECTION: Hand-off to update_helper
# ============================================================================


HELPER_EXE_NAME = "RetroTVUpdateHelper.exe"


def _helper_exe_candidates(base_dir):
    # WARNING: PyInstaller 6 onedir builds put add-data folders under _internal, so the helper can sit beside the exe, in scripts/updater/ or updater/, or under _internal (owner 2026-11-25: the script folders moved into scripts).
    roots = [base_dir, os.path.join(base_dir, "_internal")]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(meipass)
    roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    candidates = []
    for root in roots:
        for path in (os.path.join(root, HELPER_EXE_NAME),
                     os.path.join(root, "scripts", "updater", HELPER_EXE_NAME),
                     os.path.join(root, "updater", HELPER_EXE_NAME)):
            path = os.path.abspath(path)
            if path not in candidates:
                candidates.append(path)
    return candidates


def _helper_command(base_dir):
    if getattr(sys, "frozen", False):
        for helper in _helper_exe_candidates(base_dir):
            if os.path.isfile(helper):
                return [helper]
        raise UpdateError(
            f"{HELPER_EXE_NAME} is missing from this installation. Build it from "
            "update_helper.py and place it in the updater folder."
        )
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_helper.py")
    if not os.path.isfile(helper):
        raise UpdateError("update_helper.py is missing")
    return [sys.executable, helper]


def _helper_outside_install(command):
    """Runs the helper exe from a private temp copy: Windows refuses to rename the install folder while an exe inside it is running, and the helper renames that folder to carry the new version. Falls back to the original exe when the copy fails."""
    if len(command) != 1 or not str(command[0]).lower().endswith(".exe"):
        return command
    try:
        parent = tempfile.gettempdir()
        for name in os.listdir(parent):
            stale = os.path.join(parent, name)
            if name.startswith("retro-tv-update-helper-") and time.time() - os.path.getmtime(stale) > 24 * 3600:
                shutil.rmtree(_long(stale), ignore_errors=True)
        folder = tempfile.mkdtemp(prefix="retro-tv-update-helper-")
        copy = os.path.join(folder, os.path.basename(command[0]))
        shutil.copy2(_long(command[0]), _long(copy))
        return [copy]
    except OSError as exc:
        _server_breadcrumb(
            log, logging.WARNING, "update-helper-copy",
            "[UPDATE DIAG] Could not copy the helper outside the install folder, so the folder keeps its name: %s", exc,
        )
        return command


def launch_staged_update(base_dir, staged, relaunch_command, parent_pid=None, reboot_after=False, bar_start=0.0, progress_path=None):
    """Hand a staged payload to the detached swap helper and return its PID. With progress_path (the server tray's Check for updates) the helper is started before the download and follows that file: it shows one bar for the download, the unpacking and the install, and takes the staged folder from the file when the download is done."""
    if not isinstance(staged, dict):
        raise UpdateError("No staged update is available")
    if progress_path:
        staged_path = ""
    else:
        staged_path = os.path.abspath(str(staged.get("staged_path", "")))
        if not os.path.isdir(_long(staged_path)):
            raise UpdateError("The staged update payload is missing")
    if not isinstance(relaunch_command, (list, tuple)) or not relaunch_command:
        raise UpdateError("The application relaunch command is invalid")

    base_dir = os.path.abspath(base_dir)
    pending_path = os.path.join(_update_dir(base_dir), PENDING_RECORD_NAME)
    pending = {
        "install_dir": base_dir,
        "staged_path": staged_path,
        "relaunch_command": [str(value) for value in relaunch_command],
        "parent_pid": int(parent_pid or os.getpid()),
        "version": staged.get("version", ""),
        "created_at": time.time(),
    }
    if progress_path:
        # WARNING: owner 2026-11-16: the server tray's update button starts the helper first and the helper's one bar follows this file through the download and the unpacking.
        pending["tray_progress"] = os.path.abspath(progress_path)
    if reboot_after:
        # WARNING: owner 2026-11-13: set only in shell mode; the helper then continues the app's download bar from bar_start and ends by restarting the computer.
        pending["reboot_after"] = True
        pending["bar_start"] = float(bar_start or 0.0)
    _atomic_json_write(pending_path, pending)
    command = _helper_outside_install(_helper_command(base_dir)) + ["--apply", pending_path]
    # WARNING: the helper must not start with the install folder as its working folder, or Windows will not let it rename that folder.
    kwargs = {"cwd": tempfile.gettempdir(), "close_fds": True}
    _detached = 0
    if os.name == "nt":
        _detached = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        # WARNING: the app puts itself and its children in a kill-on-close job object, so a helper started without breakaway dies the moment the app exits and never swaps the files.
        kwargs["creationflags"] = _detached | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    try:
        try:
            process = subprocess.Popen(command, **kwargs)
        except OSError:
            if os.name != "nt":
                raise
            # WARNING: a job that forbids breakaway refuses the flag; the retry without it keeps the helper starting at all.
            kwargs["creationflags"] = _detached
            process = subprocess.Popen(command, **kwargs)
    except (OSError, ValueError) as exc:
        _server_breadcrumb(
            log, logging.WARNING, "update-helper-launch",
            "[UPDATE DIAG] Could not start update helper: %s", exc, interval=0.0,
        )
        raise UpdateError(f"Could not start update helper: {exc}") from exc
    # WARNING: the app quits right after this returns, so a helper that ends at once (blocked, damaged, missing file) used to leave the user with a closed app and nothing installed. Wait a moment and report it instead, so the app stays open.
    poll = getattr(process, "poll", None)
    if callable(poll):
        deadline = time.monotonic() + UPDATE_HELPER_ALIVE_CHECK_S
        while time.monotonic() < deadline:
            code = poll()
            if code is not None:
                try:
                    os.remove(pending_path)
                except OSError as done_error:
                    _log_swallowed("auto_updater: removing the pending record after the helper ended", done_error)
                _server_breadcrumb(
                    log, logging.ERROR, "update-helper-ended",
                    "[UPDATE DIAG] The update helper ended right after it started (exit code %s); the update was not installed and the app stays open", code, interval=0.0,
                )
                raise UpdateError(f"The update helper closed as soon as it started (exit code {code}), so nothing was installed")
            time.sleep(0.1)
    return process.pid


# ============================================================================
# SECTION: Exports
# ============================================================================
__all__ = [
    "UpdateError", "parse_version", "version_is_newer", "current_version",
    "github_settings", "update_source_configured", "version_from_zip_name",
    "validate_manifest", "check_github_for_update", "check_for_update",
    "stage_update", "background_check_loop", "launch_staged_update", "prune_update_folder",
]
