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
# NOTE: server_diagnostics.py may be a sibling of this file (server/) or one
# directory up (../server), depending on how the build is packaged. The import
# below has to stay after _add_server_path() for that reason.
def _add_server_path():
    if getattr(sys, "frozen", False):
        _base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        _base = os.path.dirname(os.path.abspath(__file__))
    for _candidate in (os.path.join(_base, "server"), os.path.join(_base, "..", "server")):
        if os.path.isdir(_candidate):
            sys.path.insert(0, _candidate)
            return
    sys.path.insert(0, os.path.join(_base, "..", "server"))


_add_server_path()
from server_diagnostics import breadcrumb as _server_breadcrumb


# ============================================================================
# SECTION: Constants & errors
# ============================================================================


log = logging.getLogger("retro_tv.auto_updater")

UPDATE_DIR_NAME = ".retro_tv_updates"
STAGED_RECORD_NAME = "staged_update.json"
PENDING_RECORD_NAME = "pending_update.json"
# VALUES CHANGE HERE: the largest package the app will download (the build is close to 1 GiB, so a 1 GiB ceiling would reject it as it grows) and the seconds the download waits on the connection before giving up
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB safety ceiling
DOWNLOAD_TIMEOUT_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
VERSION_RE = re.compile(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$")

# WARNING: a release zip is found by the V1.2.3 style number in its file name, e.g. "Retro TV Emulator Alpha V1.0.1.zip"; a zip without one is never offered.
ZIP_VERSION_RE = re.compile(r"[vV](\d+\.\d+\.\d+)")

# VALUES CHANGE HERE: the GitHub update source; the owner/repository whose Releases (pre-releases included) hold the release zips, and how many of the newest releases are read per check (the environment RETRO_TV_UPDATE_GITHUB_REPO or the config update_github_repo overrides the repository; a blank repository turns this source off)
UPDATE_GITHUB_REPO = "StevenCLewis111/RetroTVEmulator"
GITHUB_API_BASE = "https://api.github.com"
GITHUB_RELEASES_PER_PAGE = 30


class UpdateError(RuntimeError):
    """An expected, user-displayable update failure."""


# ============================================================================
# SECTION: Local state helpers
# ============================================================================


def _update_dir(base_dir):
    return os.path.join(os.path.abspath(base_dir), UPDATE_DIR_NAME)


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
            except OSError:
                pass
        os.replace(tmp_path, path)
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return default


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
    """Read the installed version without requiring pygame or the media DB: the RETRO_TV_VERSION override, then version.json (written by update_helper.py), then the V1.2.3 number in the running exe's own name; 0.0.0 when none is found."""
    env_version = os.environ.get("RETRO_TV_VERSION")
    if env_version:
        return str(env_version).strip()

    value = _read_json(os.path.join(base_dir, "version.json"), {})
    if isinstance(value, dict):
        value = str(value.get("version", "") or "").strip()
        if VERSION_RE.fullmatch(value):
            return value

    # NOTE: every build's exe name carries its number (Retro TV Emulator Alpha V1.0.1.exe), so a build with no version.json still knows what it is and is not offered an update that would replace it with an older or equal one.
    for name in (sys.executable, sys.argv[0] if sys.argv else ""):
        match = ZIP_VERSION_RE.search(os.path.basename(str(name or "")))
        if match:
            return match.group(1)

    # NOTE: development/source copies have no release stamp, so 0.0.0 is the "no update" baseline.
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
        # WARNING: plain HTTP is allowed for a local test server only. Never
        # permit a remote plaintext update source.
        host = (parsed.hostname or "").lower()
        if not (parsed.scheme.lower() == "http" and host in ("127.0.0.1", "localhost", "::1")):
            raise UpdateError(f"{label} must use HTTPS")
    if not parsed.netloc:
        raise UpdateError(f"{label} is malformed")
    return str(url)


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
        with urllib.request.urlopen(request, timeout=timeout) as response:
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


def check_github_for_update(installed, repo, api_base=GITHUB_API_BASE, timeout=DEFAULT_TIMEOUT_SECONDS):
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
    log.info("[UPDATE] source=github repo=%s zips_with_a_version=%d newest_posted=%s installed=%s available=%s",
             repo, zips_seen, version, installed, available)
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
    repo, github_base = github_settings(config)
    if not repo:
        return {
            "available": False,
            "configured": False,
            "current_version": installed,
            "message": "Update source is not configured.",
        }
    return check_github_for_update(installed, repo, github_base, timeout=timeout)


# ============================================================================
# SECTION: Package download & extraction
# ============================================================================


def _download_package(url, destination, timeout=DEFAULT_TIMEOUT_SECONDS, progress_callback=None):
    _validate_url(url, "Update package URL")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "RetroTVUpdater/1"},
    )
    total = 0
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            with open(destination, "wb") as output:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise UpdateError("Update package exceeds the 4 GiB safety limit")
                    digest.update(block)
                    output.write(block)
                    if progress_callback is not None:
                        content_length = int(response.headers.get("Content-Length", "0") or 0)
                        progress_callback(total, content_length)
    except UpdateError:
        raise
    except (urllib.error.URLError, OSError) as exc:
        _server_breadcrumb(
            log, logging.WARNING, f"update-download:{url}",
            "[UPDATE DIAG] Update package download failed url=%s: %s", url, exc,
        )
        raise UpdateError(f"Could not download update package: {exc}") from exc
    return total, digest.hexdigest()


def _safe_zip_member(info):
    name = info.filename.replace("\\", "/")
    if not name or name.startswith("/") or re.match(r"^[A-Za-z]:/", name):
        return False
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if ".." in parts:
        return False
    # WARNING: reject Unix symlink entries. Extracting one would let the stage
    # escape its directory when the helper copies it.
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
            archive.extractall(stage_root)
    except zipfile.BadZipFile as exc:
        raise UpdateError("Downloaded update is not a valid zip archive") from exc

    # NOTE: accepts either a flat package or a single-root-folder package.
    entries = [
        os.path.join(stage_root, name)
        for name in os.listdir(stage_root)
        if name not in ("__MACOSX",)
    ]
    if len(entries) == 1 and os.path.isdir(entries[0]):
        return entries[0]
    return stage_root


# ============================================================================
# SECTION: Staging
# ============================================================================


def stage_update(base_dir, manifest, timeout=DEFAULT_TIMEOUT_SECONDS, progress_callback=None):
    """Download, hash-check, zip-check, and atomically record a staged update."""
    manifest = validate_manifest(manifest)
    update_dir = _update_dir(base_dir)
    os.makedirs(update_dir, exist_ok=True)
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
        os.makedirs(stage_path, exist_ok=True)
        payload_root = _extract_package(package_path, stage_path)
        # WARNING: the helper deletes only the folder holding the payload, so a package with one root folder would leave this near-1 GiB zip behind on every update.
        try:
            os.remove(package_path)
        except OSError:
            pass
        record = {
            "version": manifest["version"],
            "notes": manifest.get("notes", ""),
            "staged_path": os.path.abspath(payload_root),
            "created_at": time.time(),
        }
        _atomic_json_write(os.path.join(update_dir, STAGED_RECORD_NAME), record)
        return record
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
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
            # NOTE: this loop ticks hours apart, so a failure here cannot spam
            # the log; interval=0.0 emits every one.
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
    # NOTE: PyInstaller 6 onedir builds put add-data folders under _internal,
    # so the helper can sit beside the exe, in updater/, or in _internal/updater/.
    roots = [base_dir, os.path.join(base_dir, "_internal")]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(meipass)
    roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    candidates = []
    for root in roots:
        for path in (os.path.join(root, HELPER_EXE_NAME),
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


def launch_staged_update(base_dir, staged, relaunch_command, parent_pid=None):
    """Hand a staged payload to the detached swap helper and return its PID."""
    if not isinstance(staged, dict):
        raise UpdateError("No staged update is available")
    staged_path = os.path.abspath(str(staged.get("staged_path", "")))
    if not os.path.isdir(staged_path):
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
    _atomic_json_write(pending_path, pending)
    command = _helper_command(base_dir) + ["--apply", pending_path]
    kwargs = {"cwd": base_dir, "close_fds": True}
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
    return process.pid


# ============================================================================
# SECTION: Exports
# ============================================================================
__all__ = [
    "UpdateError", "parse_version", "version_is_newer", "current_version",
    "github_settings", "update_source_configured", "version_from_zip_name",
    "validate_manifest", "check_github_for_update", "check_for_update",
    "stage_update", "background_check_loop", "launch_staged_update",
]
