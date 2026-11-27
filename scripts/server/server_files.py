"""Server-side file facts for Retro TV networking.

Owns: finding which Windows file shares cover the server's media folders, and
reading the server's media library (read-only) so a User PC gets, for every
scheduled file, the same facts the server holds: length, loudness gain,
letterbox box, stream layout and where the file lives on a share.
Does not own: the wire protocol (server_sync.py) or the User side (remote_files.py).

WARNING: only facts and share-relative locations leave this module. A media
file's bytes never cross the app's connection; the User PC plays the file
straight from the server's shared folder over Windows file sharing.
"""
from __future__ import annotations

import json
import logging
import ntpath
import os
import sqlite3
import sys
import time

log = logging.getLogger(__name__)

try:
    import performance_governor as _gov
except Exception as _governor_error:
    _gov = None
    log.warning("[FAILURE] server_files: performance_governor did not import, so the built-in numbers are used: %s", _governor_error)


def _log_swallowed(where, error):
    """Writes the shared, counted [FAILURE] line through performance_governor (a plain warning when it did not import)."""
    if _gov is not None:
        return _gov.log_swallowed(where, error)
    log.warning("[FAILURE] %s: %s: %s", where, type(error).__name__, error)
    return None

# WARNING: the library columns a User PC receives. The owner's own choices (audio track picked, loudness learning, captions text) are left out on purpose: they belong to the server's owner, not to the file.
FACT_COLUMNS = (
    "duration", "duration_source", "width", "height", "dar_num", "dar_den",
    "audio_streams", "subtitle_streams", "header_probed_at",
    "picture_box", "picture_box_detector_version",
    "estimated_duration", "estimated_at", "estimate_basis",
    "gain", "gain_target_lufs", "gain_scan_version", "gain_convergence_db",
    "manual_gain_db", "gain_checked", "gain_windows", "track_gains",
    "confirmed_unplayable", "confirmed_no_audio", "no_audio_reason",
    "captions_source", "captions_reason",
)

_MEDIA_SUFFIXES = (
    ".avi", ".mkv", ".mp4", ".m4v", ".mpg", ".mpeg", ".wmv", ".mov", ".flv",
    ".webm", ".ts", ".m2ts", ".vob", ".divx", ".ogv", ".3gp", ".asf",
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".wma", ".aac",
)


def library_db_path(base_dir):
    """The media library file the program really writes: its 'main' folder (inside the program's own folder when installed), not the program folder itself."""
    # WARNING: owner 2026-11-10: the server read <program folder>/media_library, where the library never is (the program keeps it under its main folder), so no server ever sent a file fact and every User saw `facts_saved=0` and unknown lengths; the same path must be used by the program, the service and the background scan.
    try:
        import media_library
        path = media_library.default_db_path()
        if os.path.isfile(path):
            return path
    except Exception as exc:
        log.warning("[SERVER DIAG] could not find the program's media library folder: %s", exc)
    return os.path.join(str(base_dir or ""), "media_library", "library.sqlite3")


def _norm(path) -> str:
    return os.path.normpath(os.path.abspath(str(path))).lower()


def _win(path) -> str:
    """A Windows path in one comparable form (separators, case, dots) whatever system this runs on."""
    return ntpath.normcase(ntpath.normpath(str(path)))


def _looks_like_local_media(text) -> bool:
    value = str(text or "")
    return (len(value) > 3 and value[1:3] in (":\\", ":/")
            and value.lower().endswith(_MEDIA_SUFFIXES))


def collect_paths(value, found=None, depth=0):
    """Every drive-letter media file path written anywhere inside a slot's channels."""
    if found is None:
        found = {}
    if isinstance(value, str):
        if _looks_like_local_media(value):
            found[value] = True
    elif depth < 12 and isinstance(value, dict):
        for item in value.values():
            collect_paths(item, found, depth + 1)
    elif depth < 12 and isinstance(value, (list, tuple)):
        for item in value:
            collect_paths(item, found, depth + 1)
    return list(found)


# ============================================================================
# SECTION: Shares
# ============================================================================


def list_shares(base_dir=None):
    """{share name: folder} of this PC's disk shares. Windows keeps them in the registry, readable without administrator rights; an optional server_shares.json beside the program ({"name": "folder"}) adds or overrides."""
    shares = {}
    if sys.platform.startswith("win"):
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Services\LanmanServer\Shares")
            with key:
                index = 0
                while True:
                    try:
                        name, data, _kind = winreg.EnumValue(key, index)
                    except OSError:
                        # WARNING: the normal end: Windows has no more values in this key.
                        break
                    index += 1
                    if str(name).endswith("$"):
                        continue
                    fields = {}
                    for line in (data if isinstance(data, (list, tuple)) else []):
                        if "=" in str(line):
                            k, v = str(line).split("=", 1)
                            fields[k.strip().lower()] = v.strip()
                    if fields.get("type", "0") == "0" and fields.get("path"):
                        shares[str(name)] = fields["path"]
        except Exception as exc:
            log.warning("[SERVER DIAG] could not read this PC's shared folders: %s", exc)
    if not shares and sys.platform.startswith("win"):
        shares.update(_shares_from_powershell())
    if base_dir:
        try:
            with open(os.path.join(base_dir, "server_shares.json"), "r", encoding="utf-8") as fh:
                extra = json.load(fh)
            for name, folder in dict(extra).items():
                shares[str(name)] = str(folder)
        except FileNotFoundError:
            # WARNING: the normal end: no extra shared folders were saved.
            pass
        except (OSError, ValueError, TypeError) as caught_error:
            _log_swallowed("server_files: reading the extra shared folders file", caught_error)
    return shares


WARM_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".flv", ".mpg", ".mpeg", ".wmv", ".ts")


def make_warm_allowed(base_dir=None, shares_fn=None):
    """Returns a function path -> True when a User's ask to read that file into this server's memory is allowed: a video file that exists inside a shared folder (the share list is looked at again each minute). Anything else is refused."""
    import time as _time
    state = {"at": -1e9, "shares": {}}

    def allowed(path):
        try:
            if _time.monotonic() - state["at"] > 60.0:
                state["shares"] = shares_fn() if shares_fn else list_shares(base_dir)
                state["at"] = _time.monotonic()
            clean = os.path.normpath(str(path))
            if os.path.splitext(clean)[1].lower() not in WARM_EXTENSIONS:
                return False
            return share_location(clean, state["shares"])[0] is not None and os.path.isfile(clean)
        except Exception as exc:
            log.warning("[SERVER FILES] could not tell whether %r may be warmed, so it is refused: %s", path, exc)
            return False
    return allowed


def _shares_from_powershell():
    """The same list from Windows' own share command, for a PC whose registry list could not be read."""
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-SmbShare | Where-Object { -not $_.Special } | Select-Object Name,Path | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        data = json.loads(out) if out else []
        if isinstance(data, dict):
            data = [data]
        return {str(d["Name"]): str(d["Path"]) for d in data if d.get("Name") and d.get("Path")}
    except Exception as exc:
        log.warning("[SERVER DIAG] could not list shares through PowerShell: %s", exc)
        return {}


def share_location(path, shares):
    """(share name, path inside the share with backslashes) for the share holding `path`, using the deepest matching shared folder; (None, None) when none covers it."""
    target = _win(path)
    best = None
    for name, folder in shares.items():
        root = _win(folder)
        if target == root or target.startswith(root.rstrip("\\") + "\\"):
            if best is None or len(root) > len(best[1]):
                best = (name, root)
    if best is None:
        return None, None
    return best[0], ntpath.normpath(str(path))[len(best[1]):].lstrip("\\")


# ============================================================================
# SECTION: Library facts
# ============================================================================


def read_file_facts(db_path, paths, shares):
    """One entry per path: {"p": server path, "s": share or None, "r": path inside the share, and when the library knows the file "size", "mtime", "key", "f": {fact: value}}. Reads the library read-only."""
    entries = []
    conn = None
    if db_path and os.path.isfile(db_path):
        try:
            uri = "file:" + db_path.replace("\\", "/").replace("?", "%3f") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        except sqlite3.Error as exc:
            log.warning("[SERVER DIAG] could not open the media library to share file facts: %s", exc)
            conn = None
    columns = ", ".join("l." + c for c in FACT_COLUMNS)
    try:
        for path in sorted(paths, key=str.lower):
            share, rel = share_location(path, shares)
            entry = {"p": path, "s": share, "r": rel}
            if conn is not None and share is not None:
                try:
                    row = conn.execute(
                        "SELECT k.size, k.mtime, k.content_key, " + columns +
                        " FROM path_keys k JOIN library l ON l.content_key = k.content_key"
                        " WHERE k.norm_path = ?", (_norm(path),)).fetchone()
                except sqlite3.Error as exc:
                    log.warning("[SERVER DIAG] media library read failed: %s", exc)
                    row = None
                if row is not None:
                    entry["size"], entry["mtime"], entry["key"] = row[0], row[1], row[2]
                    entry["f"] = {c: v for c, v in zip(FACT_COLUMNS, row[3:]) if v is not None}
                    # WARNING: owner 2026-11-14: only the burned-in caption verdict travels in these two columns; anything else saved there is the server owner's own.
                    if not str(entry["f"].get("captions_reason") or "").startswith("burned_in_v"):
                        entry["f"].pop("captions_source", None)
                        entry["f"].pop("captions_reason", None)
            entries.append(entry)
    finally:
        if conn is not None:
            conn.close()
    return entries


def make_provider(db_path, base_dir=None, shares_fn=None):
    """A files provider for ScheduleBroadcaster: given a slot's channels, returns its file entries."""
    shares_fn = shares_fn or (lambda: list_shares(base_dir))

    def provide(channels):
        shares = shares_fn()
        paths = collect_paths(channels)
        entries = read_file_facts(db_path, paths, shares)
        missing = [e["p"] for e in entries if e["s"] is None]
        if missing:
            log.warning("[SERVER DIAG] %d scheduled file(s) are in no shared folder, for example %s; shares this PC lists: %s",
                        len(missing), missing[0], dict(shares) or "none")
        return entries
    return provide


# ============================================================================
# SECTION: Sharing the folders from inside the app
# ============================================================================


def _is_system_drive(drive):
    """True for the Windows drive, which is never shared whole."""
    return drive.strip("\\").lower() == os.environ.get("SystemDrive", "C:").strip("\\").lower()


def local_share_drives(drive_types=None):
    """The root of every local fixed drive except the Windows drive ("D:\\", "E:\\", ...), the drives a blanket Yes shares; none off Windows. drive_types is replaceable for tests: {"D:\\": 3, ...} with Windows' drive type numbers (3 is a fixed drive)."""
    if drive_types is None:
        if sys.platform != "win32":
            return []
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        drive_types = {}
        for i in range(26):
            if mask & (1 << i):
                root = "%s:\\" % chr(65 + i)
                drive_types[root] = ctypes.windll.kernel32.GetDriveTypeW(root)
    return [root for root, kind in sorted(drive_types.items()) if kind == 3 and not _is_system_drive(root[:2])]


def plan_share_roots(paths, max_roots=20):
    """The folders to share so every path is inside one: each local drive other than the Windows drive is shared whole (one permission prompt covers it for good); on the Windows drive, and for a network path, the deepest folder holding all of that drive's files, or each file's own folder when that is the drive's top."""
    by_drive = {}
    for path in paths:
        drive, rest = ntpath.splitdrive(ntpath.normpath(str(path)))
        folder = ntpath.dirname(drive + rest)
        by_drive.setdefault(drive.lower(), []).append(folder)
    roots = []
    for drive, folders in sorted(by_drive.items()):
        if len(drive) == 2 and drive[1] == ":" and not _is_system_drive(drive):
            roots.append(drive.upper() + "\\")
            continue
        try:
            common = ntpath.commonpath(folders)
        except ValueError:
            common = ""
        if common and ntpath.normpath(common).strip("\\").lower() != drive.strip("\\"):
            roots.append(common)
        else:
            roots.extend(sorted(set(folders), key=str.lower))
    unique = []
    seen = set()
    for r in roots:
        if _win(r) not in seen:
            seen.add(_win(r)); unique.append(r)
    return unique[:max_roots]


def unshared_paths(paths, shares):
    """The paths no existing share covers."""
    return [p for p in paths if share_location(p, shares)[0] is None]


def _share_name(folder, taken):
    if len(folder) == 3 and folder[1:] == ":\\":
        base = "Drive " + folder[0].upper()
    else:
        base = "".join(c for c in ntpath.basename(folder.rstrip("\\")) if c.isalnum() or c in " _-") or "RetroTV"
    name, n = base[:40], 2
    while name.lower() in taken:
        name = "%s%d" % (base[:36], n); n += 1
    taken.add(name.lower())
    return name


# WARNING: owner 2026-11-19: the account every User PC signs in with to stream from the server's shared folders, with one fixed password that is the same on every computer; nothing is handed from the server or saved, so the two computers can never hold different passwords. It can only read the folders the app shared and is hidden from the sign-in screen. server_remote_files.py signs in with these two values.
SHARE_USER = "RetroTV"
SHARE_PASSWORD = "RetroTVshare1"


STREAM_TASK_OFF = "RetroTV Streaming Off"
STREAM_TASK_ON = "RetroTV Streaming On"
# WARNING: the first Yes also registers these two tasks; later sharing work (the switch tasks made again after an update, a newly added drive) is done by starting the upkeep task, which Windows runs with its permission already given, so no permission prompt appears again. Changing what the upkeep task does means a new UPKEEP_VERSION, which asks once.
STREAM_TASK_UPKEEP = "RetroTV Sharing Upkeep"
STREAM_TASK_REMOVE = "RetroTV Sharing Remove"
# WARNING: owner 2026-11-19: v3 sets the fixed SHARE_PASSWORD; the v2 tasks set a random password from a file, so a server with v2 tasks asks Windows once more and the account gets the fixed password.
UPKEEP_VERSION = "retrotv-upkeep-v3"
# WARNING: owner 2026-11-18: Windows Defender removed the upkeep task as a Trojan because it ran a hidden console with an encoded PowerShell command; every task now runs a plain script file from this folder, which only administrators can change, and runs out of sight as a task that runs whether or not the owner is signed in.
TASK_FOLDER_EXPR = "(Join-Path $env:ProgramData 'RetroTV\\tasks')"
TASK_SCRIPT_NAMES = {STREAM_TASK_OFF: "streaming_off.ps1", STREAM_TASK_ON: "streaming_on.ps1",
                     STREAM_TASK_UPKEEP: "sharing_upkeep.ps1", STREAM_TASK_REMOVE: "sharing_remove.ps1"}
UPKEEP_RESULT_NAME = "upkeep_result.txt"
# WARNING: the fallback numbers in this file must match their SERVER_FILES_ names in performance_governor.py; they are used only when that module fails to import.
UPKEEP_WAIT_SECONDS = _gov.SERVER_FILES_UPKEEP_WAIT_SECONDS if _gov is not None else 90
STREAM_SWITCH_TIMEOUT_SECONDS = _gov.SERVER_FILES_STREAM_SWITCH_TIMEOUT_SECONDS if _gov is not None else 15
# WARNING: giving the streaming account read access to a whole drive makes Windows walk every file on it, which takes minutes on a big drive, so the elevated sharing script may run this long.
SHARE_SCRIPT_TIMEOUT_SECONDS = _gov.SERVER_FILES_SHARE_SCRIPT_TIMEOUT_SECONDS if _gov is not None else 900
PERMISSION_BOX_POLL_SECONDS = _gov.SERVER_FILES_PERMISSION_BOX_POLL_SECONDS if _gov is not None else 0.05


def _stream_task_commands():
    """The two PowerShell commands behind the streaming switch: off disables the streaming sign-in and closes every connection made with it; on enables the sign-in again."""
    off = ("Disable-LocalUser -Name '%s'; Get-SmbSession | Where-Object { $_.ClientUserName -like '*\\%s' } | Close-SmbSession -Force"
           % (SHARE_USER, SHARE_USER))
    on = "Enable-LocalUser -Name '%s'" % SHARE_USER
    return off, on


def _task_program():
    return "powershell.exe"


def _task_arguments(script_name):
    """The PowerShell expression for a task's arguments: run the named plain script file from the task folder."""
    return "('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"' + (Join-Path $taskdir %s) + '\"')" % _q(script_name)


def streaming_switch_ready():
    """True when the two Windows tasks behind the tray's Stop Server exist (always True off Windows, where there is nothing to switch)."""
    if sys.platform != "win32":
        return True
    import subprocess
    try:
        for name in (STREAM_TASK_OFF, STREAM_TASK_ON):
            done = subprocess.run(["schtasks", "/query", "/tn", name, "/xml"], capture_output=True, timeout=STREAM_SWITCH_TIMEOUT_SECONDS,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if done.returncode != 0:
                log.info("[SERVER FILES] the Windows task %r is missing (schtasks exit %s)", name, done.returncode)
                return False
            # WARNING: tasks made by an older version run an encoded command that Windows Defender removes as a Trojan; they are made again once (one permission prompt) as plain script files.
            if UPKEEP_VERSION.encode("ascii") not in (done.stdout or b"").replace(b"\x00", b""):
                log.info("[SERVER FILES] the Windows task %r was made by an older version and is made again", name)
                return False
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("[SERVER FILES] could not ask Windows whether the streaming tasks exist: %s", exc)
        return False
    return True


def set_streaming(on, log=None):
    """Starts the Windows task that turns streaming from this computer on or off (no permission prompt: the tasks were registered with the sharing script); returns True when Windows started it."""
    if sys.platform != "win32":
        return False
    import subprocess
    name = STREAM_TASK_ON if on else STREAM_TASK_OFF
    try:
        done = subprocess.run(["schtasks", "/run", "/tn", name], capture_output=True, timeout=STREAM_SWITCH_TIMEOUT_SECONDS,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        if log is not None:
            log.warning("[SERVER SERVICE] could not start the %r task: %s", name, exc)
        return False
    if done.returncode != 0 and log is not None:
        log.info("[SERVER SERVICE] the %r task did not start (exit %s); run the folder sharing step again to create it", name, done.returncode)
    return done.returncode == 0


def stop_streaming_verified(log=None, tries=2, switch=None):
    """Turns streaming off and checks Windows really started the task; tries again once when it did not. Returns True when streaming is known to be off, False when it could not be switched (logged, so a sign-in left open is never a silent failure)."""
    switch = switch or set_streaming
    for attempt in range(1, int(tries) + 1):
        if switch(False, log):
            return True
        if log is not None:
            log.warning("[SERVER SERVICE] streaming could not be switched off (try %d of %d)", attempt, tries)
    if log is not None:
        log.warning("[SERVER SERVICE] streaming may still be on: run the folder sharing step again to recreate the switch-off task")
    return False


def _q(text):
    """A PowerShell single-quoted string."""
    return "'" + str(text).replace("'", "''") + "'"


def _account_lines():
    """PowerShell lines that make or update the read-only streaming account with SHARE_PASSWORD and hide it from the sign-in screen."""
    # WARNING: owner log 2026-11-18: a viewer PC that kept trying an old password locked the streaming account (error 1909); the account is unlocked whenever its password is set.
    return ["  $pw = ConvertTo-SecureString %s -AsPlainText -Force" % _q(SHARE_PASSWORD),
            "  if (Get-LocalUser -Name %s -ErrorAction SilentlyContinue) { Set-LocalUser -Name %s -Password $pw -PasswordNeverExpires $true } else { New-LocalUser -Name %s -Password $pw -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires -Description 'Retro TV Emulator streaming (read only)' | Out-Null }" % (_q(SHARE_USER), _q(SHARE_USER), _q(SHARE_USER)),
            "  New-Item -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon\\SpecialAccounts\\UserList' -Force | Out-Null",
            "  Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon\\SpecialAccounts\\UserList' -Name %s -Value 0 -Type DWord" % _q(SHARE_USER),
            "  Get-CimInstance Win32_UserAccount -Filter %s -ErrorAction SilentlyContinue | Set-CimInstance -Property @{Lockout = $false} -ErrorAction SilentlyContinue" % _q("LocalAccount = True AND Name = '%s'" % SHARE_USER)]


def _task_folder_lines():
    """PowerShell lines that make the task folder and let only administrators and Windows itself change it; everyone else may only read it."""
    # WARNING: the tasks run these files with administrator rights, so a folder an ordinary program could write to would let it run anything as administrator; the folder's owner is the Administrators group, not the person, so a program without the permission prompt cannot change who may write.
    return ["  $taskdir = %s" % TASK_FOLDER_EXPR,
            "  New-Item -ItemType Directory -Path $taskdir -Force | Out-Null",
            "  icacls $taskdir /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' '*S-1-3-4:(OI)(CI)RX' | Out-Null",
            "  icacls $taskdir /setowner '*S-1-5-32-544' /T | Out-Null"]


def _task_setup_lines():
    """PowerShell lines that make the task folder and define the task principal and settings every registered task uses."""
    # WARNING: owner 2026-11-18: a task that runs whether or not the owner is signed in (S4U, no password kept) runs out of sight, so no window shows and no hidden console is needed.
    return _task_folder_lines() + [
        "  $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
        "  $prin = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Highest",
        "  $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2)"]


def _write_task_script_lines(script_name, command):
    """PowerShell lines that write a task's script as a plain file in the task folder, one quoted line per line of the script."""
    lines = command.splitlines() or [""]
    return ["  Set-Content -LiteralPath (Join-Path $taskdir %s) -Encoding UTF8 -Value @(%s)" % (_q(script_name), ", ".join(_q(line) for line in lines))]


def _register_task_lines(name, command, description=UPKEEP_VERSION):
    """PowerShell lines that write one task's script file and register the task that runs it; the description is how the program later tells which version of the task is installed."""
    var = "$act_" + "".join(c for c in name if c.isalnum())
    script_name = TASK_SCRIPT_NAMES[name]
    return _write_task_script_lines(script_name, command) + [
        "  %s = New-ScheduledTaskAction -Execute %s -Argument %s" % (var, _q(_task_program()), _task_arguments(script_name)),
        "  Register-ScheduledTask -TaskName %s -Action %s -Principal $prin -Settings $set -Force -Description %s | Out-Null" % (_q(name), var, _q(description))]


def _stream_task_lines():
    off_cmd, on_cmd = _stream_task_commands()
    return _register_task_lines(STREAM_TASK_OFF, off_cmd) + _register_task_lines(STREAM_TASK_ON, on_cmd)


def _firewall_lines():
    return ["  Enable-NetFirewallRule -DisplayGroup 'File and Printer Sharing' -ErrorAction SilentlyContinue",
            # WARNING: file sharing answers only computers on this network, never the internet.
            "  Get-NetFirewallRule -DisplayGroup 'File and Printer Sharing' -ErrorAction SilentlyContinue | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress LocalSubnet -ErrorAction SilentlyContinue"]


def _grant_read_lines(root_expr, detail_path_expr):
    """PowerShell lines that give the streaming account read access to a drive root only when it does not already have it through its own entry or through Users, Authenticated Users or Everyone, and no Deny entry blocks it; the grant rewrites every file on the drive, so it is skipped whenever it is not needed."""
    # WARNING: owner 2026-11-17: icacls with (OI)(CI) on a drive root rewrites the permissions of every file on the drive and ran for minutes on a big drive; it runs only when the root's own permissions do not already let the account read everything below it.
    sids = "@('S-1-5-32-545','S-1-5-11','S-1-1-0')"
    return [
        "    $acc = (Get-Acl -LiteralPath %s).Access | ForEach-Object { $sid = try { $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { '' }; [pscustomobject]@{ Mine = (($_.IdentityReference.Value -match '(^|\\\\)%s$') -or ($sid -in %s)); Type = $_.AccessControlType; Rights = [int]$_.FileSystemRights; Flags = [int]$_.InheritanceFlags } }" % (root_expr, SHARE_USER, sids),
        "    $denied = @($acc | Where-Object { $_.Type -eq 'Deny' -and $_.Mine }).Count",
        "    $has = @($acc | Where-Object { $_.Type -eq 'Allow' -and $_.Mine -and (($_.Rights -band 131241) -eq 131241) -and (($_.Flags -band 3) -eq 3) }).Count",
        "    if ($has -gt 0 -and $denied -eq 0) { Add-Content -Path %s -Value ('read access already there, grant skipped: ' + %s) } else { icacls %s /grant %s | Out-Null; Add-Content -Path %s -Value ('read access granted: ' + %s) }" % (detail_path_expr, root_expr, root_expr, _q(SHARE_USER + ":(OI)(CI)RX"), detail_path_expr, root_expr),
    ]


def _drive_share_lines():
    """PowerShell lines that share every local fixed drive except the Windows drive, read only to the streaming account (what the first Yes does)."""
    name_expr = "('Drive ' + $d.DeviceID.Substring(0, 1))"
    return ["  foreach ($d in (Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3')) {",
            "    if ($d.DeviceID -eq $env:SystemDrive) { continue }",
            "    $n = %s; $root = $d.DeviceID + '\\'" % name_expr,
            "    if (-not (Get-SmbShare -Name $n -ErrorAction SilentlyContinue)) { New-SmbShare -Name $n -Path $root -ReadAccess %s | Out-Null }" % _q(SHARE_USER),
            ] + _grant_read_lines("$root", "($env:TEMP + '\\retrotv_share_detail.txt')") + ["  }"]


def _wrap_script(body_lines, result_path):
    return (["$ErrorActionPreference = 'Stop'", "try {"] + body_lines +
            ["  Set-Content -Path %s -Value 'ok'" % _q(result_path),
             "} catch { Set-Content -Path %s -Value (\"error: \" + $_.Exception.Message) }" % _q(result_path)])


def upkeep_command(result_path):
    """The PowerShell the upkeep task runs: sets the streaming account's password, shares every local fixed drive, makes the streaming tasks again and opens the file-sharing firewall rule; it never takes a folder or a command from anywhere."""
    body = (_account_lines() + _drive_share_lines() + _task_setup_lines() + _stream_task_lines() + _firewall_lines())
    return "\r\n".join(_wrap_script(body, result_path))


def remove_command(result_path):
    """The PowerShell the remove task runs: removes the folder shares that only the streaming account can read, its file permissions, the account itself and the tasks."""
    user = _q(SHARE_USER)
    body = [
        "  foreach ($s in (Get-SmbShare | Where-Object { -not $_.Special })) {",
        "    $acc = @(Get-SmbShareAccess -Name $s.Name)",
        "    $mine = @($acc | Where-Object { $_.AccountName -like ('*\\' + %s) })" % user,
        "    if ($mine.Count -gt 0 -and $mine.Count -eq $acc.Count) { Remove-SmbShare -Name $s.Name -Force; icacls $s.Path /remove %s | Out-Null }" % user,
        "  }",
        "  Get-SmbSession | Where-Object { $_.ClientUserName -like ('*\\' + %s) } | Close-SmbSession -Force" % user,
        "  if (Get-LocalUser -Name %s -ErrorAction SilentlyContinue) { Remove-LocalUser -Name %s }" % (user, user),
        "  Remove-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon\\SpecialAccounts\\UserList' -Name %s -ErrorAction SilentlyContinue" % user,
    ]
    lines = _wrap_script(body, result_path)
    # WARNING: the result is written first and the tasks go last; the remove task takes itself away at the very end.
    lines.append("try { foreach ($t in @(%s)) { Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue }; Remove-Item -LiteralPath %s -Recurse -Force -ErrorAction SilentlyContinue } catch { }" % (", ".join(_q(n) for n in (STREAM_TASK_OFF, STREAM_TASK_ON, STREAM_TASK_UPKEEP, STREAM_TASK_REMOVE)), TASK_FOLDER_EXPR))
    return "\r\n".join(lines)


def build_share_script(roots, existing_shares, result_path, upkeep_result_path=None):
    """PowerShell that makes a read-only account (SHARE_USER), shares each folder to that account only (share and file permissions), makes the streaming tasks and (with upkeep_result_path) the upkeep and remove tasks, opens the file-sharing firewall rule, and writes 'ok' or the error to result_path."""
    taken = {n.lower() for n in existing_shares}
    lines = _account_lines()
    detail_path = result_path + ".detail"
    names = []
    for folder in roots:
        name = _share_name(folder, taken)
        names.append(name)
        lines.append("  if (-not (Get-SmbShare -Name %s -ErrorAction SilentlyContinue)) { New-SmbShare -Name %s -Path %s -ReadAccess %s | Out-Null }" % (_q(name), _q(name), _q(folder), _q(SHARE_USER)))
        lines += _grant_read_lines(_q(folder), _q(detail_path))
    lines += _task_setup_lines() + _stream_task_lines()
    if upkeep_result_path:
        lines += _register_task_lines(STREAM_TASK_UPKEEP, upkeep_command(upkeep_result_path))
        lines += _register_task_lines(STREAM_TASK_REMOVE, remove_command(upkeep_result_path))
    lines += ["  Enable-LocalUser -Name %s -ErrorAction SilentlyContinue" % _q(SHARE_USER)] + _firewall_lines()
    started = "Set-Content -Path %s -Value 'started'" % _q(result_path)
    return "\r\n".join([started] + _wrap_script(lines, result_path)) + "\r\n", names


def permission_box_visible():
    """True while Windows' permission box (consent.exe) is running, False when it is not, None when the process list cannot be read."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _Entry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        snapshot = kernel32.CreateToolhelp32Snapshot(_SNAPSHOT_PROCESS, 0)
        if snapshot in (None, wintypes.HANDLE(-1).value):
            log.warning("[SERVER FILES] the process list could not be read (error %d)", ctypes.get_last_error())
            return None
        try:
            entry = _Entry()
            entry.dwSize = ctypes.sizeof(_Entry)
            more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                if entry.szExeFile.lower() == "consent.exe":
                    return True
                more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            return False
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception as exc:
        log.warning("[SERVER FILES] could not look for Windows' permission box: %s", exc)
        return None


# WARNING: Windows codes used by the permission request: process snapshot of every process, hide the window, keep the process handle, run without waiting on the shell, the user said No, and the wait result for a finished process.
_SNAPSHOT_PROCESS = 0x00000002
_SW_HIDE = 0
_SEE_NOCLOSEPROCESS_NOASYNC = 0x00000040 | 0x00000100
_ERROR_CANCELLED = 1223
_WAIT_OBJECT_0 = 0


def _elevated_launch_arguments(script_path):
    """(program, arguments) that run the sharing script file with a hidden window."""
    return "powershell.exe", '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "%s"' % script_path


def _run_elevated_direct(program, arguments, on_step):
    """Asks Windows for permission itself (no PowerShell in between) and waits for the script; returns '' or what Windows said. on_step gets 'appeared', 'answered_yes' or 'cancelled'."""
    import ctypes
    import threading
    from ctypes import wintypes

    class _Info(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
                    ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_Info)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 2)
    except Exception as exc:
        log.warning("[SERVER FILES] COM could not be started for the permission request: %s", exc)
    stop = threading.Event()

    def _watch_for_the_box():
        while not stop.is_set():
            seen = permission_box_visible()
            if seen is None:
                return
            if seen:
                log.info("[SERVER FILES] Windows' permission box is on screen")
                if on_step is not None:
                    on_step("appeared")
                return
            stop.wait(PERMISSION_BOX_POLL_SECONDS)
    threading.Thread(target=_watch_for_the_box, daemon=True, name="PermissionBoxWatch").start()
    info = _Info()
    info.cbSize = ctypes.sizeof(_Info)
    info.fMask = _SEE_NOCLOSEPROCESS_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = program
    info.lpParameters = arguments
    info.nShow = _SW_HIDE
    asked_at = time.monotonic()
    try:
        ok = shell32.ShellExecuteExW(ctypes.byref(info))
        error = ctypes.get_last_error()
    finally:
        stop.set()
    if not ok:
        if error == _ERROR_CANCELLED:
            log.info("[SERVER FILES] the owner answered No to Windows' permission box after %.1f s", time.monotonic() - asked_at)
            if on_step is not None:
                on_step("cancelled")
            return "Windows' permission box was answered No"
        log.error("[SERVER FILES] Windows did not start the sharing script (error %d)", error)
        return "Windows did not start the sharing script (error %d)" % error
    log.info("[SERVER FILES] the owner answered Yes to Windows' permission box after %.1f s", time.monotonic() - asked_at)
    if on_step is not None:
        on_step("answered_yes")
    if info.hProcess:
        kernel32.WaitForSingleObject(info.hProcess, int(SHARE_SCRIPT_TIMEOUT_SECONDS * 1000))
        kernel32.CloseHandle(info.hProcess)
    return ""


def run_script_elevated(script_path, result_path, on_step=None):
    """Runs the script with the Windows permission prompt; returns '' or, when Windows did not start it, what Windows said."""
    import subprocess
    # WARNING: owner 2026-11-18: the script file is run as it is, never packed into an encoded command: Windows Defender treats a hidden encoded PowerShell command with administrator rights as an attack. The path is quoted, so a folder such as "Program Files" stays one argument.
    program, arguments = _elevated_launch_arguments(script_path)
    if sys.platform == "win32":
        try:
            return _run_elevated_direct(program, arguments, on_step)
        except Exception as exc:
            log.warning("[SERVER FILES] asking Windows for permission directly failed (%s); asking through PowerShell", exc)
    inner = "$ErrorActionPreference = 'Stop'; Start-Process %s -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList %s" % (_q(program), _q(arguments))
    done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", inner],
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=SHARE_SCRIPT_TIMEOUT_SECONDS,
                          capture_output=True, text=True, errors="replace")
    if done.returncode != 0:
        said = " ".join((done.stderr or done.stdout or "").split())
        if on_step is not None and "canceled by the user" in said.lower():
            on_step("cancelled")
        return "Windows did not start the sharing script (exit %s): %s" % (done.returncode, said[:300] or "no message")
    if on_step is not None:
        on_step("answered_yes")
    return ""


def _task_xml(name):
    """The saved definition of a Windows task as text, or None when the task does not exist."""
    import subprocess
    try:
        done = subprocess.run(["schtasks", "/query", "/tn", name, "/xml"], capture_output=True, timeout=STREAM_SWITCH_TIMEOUT_SECONDS,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("[SERVER FILES] could not read the Windows task %r: %s", name, exc)
        return None
    if done.returncode != 0:
        log.info("[SERVER FILES] the Windows task %r does not exist (schtasks exit %s)", name, done.returncode)
        return None
    return (done.stdout or b"").replace(b"\x00", b"").decode("utf-8", errors="replace")


def upkeep_ready():
    """True when the upkeep and remove tasks exist in the version this program writes (always False off Windows), so sharing work can be done without a permission prompt."""
    if sys.platform != "win32":
        return False
    for name in (STREAM_TASK_UPKEEP, STREAM_TASK_REMOVE):
        xml = _task_xml(name)
        if xml is None or UPKEEP_VERSION not in xml:
            return False
    return True


def _run_task_and_wait(task, result_path, wait_s=None, run=None, sleep=time.sleep):
    """Starts a registered task and waits for the result file it writes; returns the text it wrote ('ok' or 'error: ...'), or '' when Windows did not start it or it wrote nothing in time."""
    wait_s = UPKEEP_WAIT_SECONDS if wait_s is None else wait_s
    try:
        os.remove(result_path)
    except FileNotFoundError:
        # WARNING: the normal end: there was no earlier result file.
        pass
    except OSError as caught_error:
        _log_swallowed("server_files: removing the earlier upkeep result", caught_error)
    started = (run or (lambda name: set_streaming_task(name)))(task)
    if not started:
        log.warning("[SERVER FILES] Windows did not start the task %r", task)
        return ""
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        try:
            with open(result_path, "r", encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read().strip()
            if text:
                return text
        except OSError:
            # WARNING: the normal end: Windows still holds the result file open (the script finishing, or a virus scanner reading it); it is read again on the next pass.
            pass
        sleep(0.5)
    log.warning("[SERVER FILES] the task %r wrote no result to %s within %.0f s", task, result_path, wait_s)
    return ""


def set_streaming_task(name):
    """Starts one registered task by name; True when Windows started it."""
    if sys.platform != "win32":
        return False
    import subprocess
    try:
        done = subprocess.run(["schtasks", "/run", "/tn", name], capture_output=True, timeout=STREAM_SWITCH_TIMEOUT_SECONDS,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("[SERVER FILES] could not start the Windows task %r: %s", name, exc)
        return False
    if done.returncode != 0:
        log.warning("[SERVER FILES] the Windows task %r did not start (schtasks exit %s): %s", name, done.returncode,
                    (done.stderr or b"").replace(b"\x00", b"").decode("utf-8", errors="replace").strip()[:200])
    return done.returncode == 0


SHARE_RESULT_READ_WAIT_SECONDS = _gov.SERVER_FILES_SHARE_RESULT_READ_WAIT_SECONDS if _gov is not None else 10.0


def _read_share_result(result_path, wait_s=None, sleep=time.sleep):
    """(text, None) once the sharing script's result file can be read and holds 'ok' or an error, or (None, why) when it cannot be read in time; a file that is still held open or only says 'started' is read again."""
    # WARNING: owner log 2026-11-18: right after the script ended, reading its result failed with "Permission denied" because the file was still held open, and the app reported a sharing failure for a script that had worked.
    wait_s = SHARE_RESULT_READ_WAIT_SECONDS if wait_s is None else wait_s
    end = time.monotonic() + wait_s
    text, problem = None, None
    while True:
        try:
            with open(result_path, "r", encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read().strip()
            problem = None
            if text and text != "started":
                return text, None
        except FileNotFoundError as exc:
            return None, exc
        except OSError as exc:
            problem = exc
        if time.monotonic() >= end:
            return (text, None) if text else (None, problem)
        sleep(0.25)


def run_upkeep(base_dir, run=None, sleep=time.sleep):
    """Redoes the sharing set-up (password, drive shares, streaming tasks, firewall) by starting the upkeep task; returns (True, '') or (False, why). No permission prompt appears."""
    result_path = os.path.join(base_dir, "logs", UPKEEP_RESULT_NAME)
    outcome = _run_task_and_wait(STREAM_TASK_UPKEEP, result_path, run=run, sleep=sleep)
    if outcome == "ok":
        log.info("[SERVER FILES] the sharing upkeep task finished: ok")
        return True, ""
    log.error("[SERVER FILES] the sharing upkeep task failed: %s", outcome or "no result")
    return False, outcome or "Windows did not run the sharing upkeep task"


def remove_sharing(base_dir, run=None, sleep=time.sleep, ready=None):
    """Takes the sharing away (the folder shares for the streaming account, its permissions, the account, the tasks) by starting the remove task; returns (True, why) when it is done or there was nothing to remove, (False, why) when it could not be done."""
    ready = upkeep_ready if ready is None else ready
    if not ready():
        log.warning("[SERVER FILES] sharing was not removed: the remove task is not installed on this computer")
        return False, "the remove task is not installed on this computer"
    result_path = os.path.join(base_dir, "logs", UPKEEP_RESULT_NAME)
    outcome = _run_task_and_wait(STREAM_TASK_REMOVE, result_path, run=run, sleep=sleep)
    if outcome == "ok":
        log.info("[SERVER FILES] the sharing was removed")
        return True, ""
    log.error("[SERVER FILES] removing the sharing failed: %s", outcome or "no result")
    return False, outcome or "Windows did not run the remove task"


def _on_whole_drives(paths):
    """True when every path lies on a local fixed drive other than the Windows drive, which the upkeep task shares whole."""
    drives = [d.lower() for d in local_share_drives()]
    return bool(drives) and all(ntpath.splitdrive(ntpath.normpath(str(p)))[0].lower() + "\\" in drives for p in paths)


def grant_sharing(base_dir, paths, shares=None, run_elevated=None, share_checker=None, switch_ready=None, upkeep=None, on_step=None):
    """Shares the folders holding `paths` (one Windows permission prompt), waits, and returns (True, names) or (False, reason). run_elevated(script_path) is replaceable for tests."""
    logs = os.path.join(base_dir, "logs")
    os.makedirs(logs, exist_ok=True)
    shares = list_shares(base_dir) if shares is None else shares
    todo = unshared_paths(paths, shares)
    log.info("[SERVER FILES] sharing check: %d folder path(s) given, %d not shared yet", len(list(paths)), len(todo))
    if not todo and (switch_ready() if switch_ready is not None else streaming_switch_ready()):
        return True, []
    # WARNING: owner 2026-11-17: once the first Yes has installed the upkeep task, anything that only needs the whole drives shared or the streaming tasks made again is done by starting that task, with no permission prompt.
    if upkeep is None and (not todo or _on_whole_drives(todo)) and upkeep_ready():
        upkeep = lambda: run_upkeep(base_dir)
    if upkeep is not None and (not todo or _on_whole_drives(todo)):
        if on_step is not None:
            on_step("working")
        done, why = upkeep()
        if done:
            if todo and share_checker is not None:
                still = unshared_paths(todo, share_checker())
                if still:
                    log.error("[SERVER FILES] the upkeep task said ok but Windows lists no share over %d file(s), first: %s", len(still), still[0])
                    return False, "shared, but Windows does not list a share over %s" % still[0]
            return True, []
        log_upkeep = logging.getLogger(__name__)
        log_upkeep.warning("[SERVER SERVICE] the sharing upkeep task did not finish (%s); asking Windows for permission instead", why)
    roots = plan_share_roots(todo)
    log.info("[SERVER FILES] asking Windows for permission to share %d folder(s)", len(roots))
    if on_step is not None:
        on_step("asking")
    result_path = os.path.join(logs, "share_result.txt")
    script_path = os.path.join(logs, "share_folders.ps1")
    for _old in (result_path, result_path + ".detail"):
        try:
            os.remove(_old)
        except FileNotFoundError:
            # WARNING: the normal end: there was no earlier result file.
            pass
        except OSError as caught_error:
            _log_swallowed("server_files: removing an earlier sharing result", caught_error)
    script, names = build_share_script(roots, shares, result_path, upkeep_result_path=os.path.join(logs, UPKEEP_RESULT_NAME))
    with open(script_path, "w", encoding="utf-8-sig") as fh:
        fh.write(script)
    try:
        if run_elevated is None:
            if share_checker is None:
                share_checker = lambda: list_shares(base_dir)
            run_elevated = lambda path: run_script_elevated(path, result_path, on_step)
        try:
            launch_problem = run_elevated(script_path)
        except Exception as exc:
            log.error("[SERVER FILES] the elevated sharing script could not be started: %s", exc)
            return False, str(exc)
        outcome, read_problem = _read_share_result(result_path)
        if outcome is None:
            log.error("[SERVER FILES] the sharing script left no result (%s); launch problem: %s", read_problem, launch_problem)
            return False, launch_problem or "the Windows permission prompt was not accepted, or Windows blocked the sharing script"
        try:
            with open(result_path + ".detail", "r", encoding="utf-8-sig", errors="replace") as fh:
                for _line in fh.read().splitlines():
                    log.info("[SERVER FILES] %s", _line.strip())
        except OSError as exc:
            log.info("[SERVER FILES] the sharing script left no detail file (%s)", exc)
        if outcome != "ok":
            log.error("[SERVER FILES] the sharing script reported: %s", outcome[:300])
        if outcome == "started":
            return False, "the sharing script started but did not finish (see logs/share_result.txt)"
        if outcome != "ok":
            return False, outcome
        # WARNING: the script says ok, but the real test is whether Windows now lists a share over every file.
        if share_checker is not None and todo:
            still = unshared_paths(todo, share_checker())
            if still:
                log.error("[SERVER FILES] the script said ok but Windows lists no share over %d file(s), first: %s", len(still), still[0])
                return False, "shared, but Windows does not list a share over %s" % still[0]
        return True, names
    finally:
        try:
            os.remove(script_path)
        except FileNotFoundError:
            # WARNING: the normal end: the sharing script was already removed.
            pass
        except OSError as caught_error:
            _log_swallowed("server_files: removing the sharing script", caught_error)


__all__ = ["local_share_drives", "SHARE_USER", "SHARE_PASSWORD", "STREAM_TASK_OFF", "STREAM_TASK_ON", "streaming_switch_ready", "set_streaming", "plan_share_roots", "unshared_paths", "build_share_script", "grant_sharing", "upkeep_ready", "run_upkeep", "remove_sharing", "upkeep_command", "remove_command", "FACT_COLUMNS", "collect_paths", "list_shares", "share_location",
           "read_file_facts", "make_provider"]
