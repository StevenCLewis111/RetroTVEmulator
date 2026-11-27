"""Send Logs, shared by the app's Send Logs button and the server tray's Send logs row (owner 2026-11-24: one copy of the steps, so the tray sends exactly what the button sends).

It picks the log files, keeps the personal parts out (_LogScrubber), keeps the zip under Discord's size limit and posts it to the Discord webhook. It also finishes a Send Logs upload after the app has closed (owner 2026-11-16): the app writes a small job file when a Send Logs starts, and when the app is closed while the upload is still going, the app starts this module in a separate process (the same program with --finish-send-logs <job file>) and closes at once; that process only posts the finished zip and removes the job. It uses nothing but the standard library, so the closing app and the server service can both load it.
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile


# ============================================================================
# SECTION: Swallowed-failure lines
# ============================================================================


def _log_swallowed(where, error):
    """Writes the [FAILURE] line through performance_governor when it loads; in the finisher process there is no log, so the line is lost."""
    try:
        from performance_governor import log_swallowed
    except Exception:
        return None
    return log_swallowed(where, error)


# ============================================================================
# SECTION: Settings
# ============================================================================


def _governor_value(name, fallback):
    """A value from performance_governor when the program has it loaded already (the app and the server service), else the fallback; the finisher process never loads it, so this module stays standard library only."""
    governor = sys.modules.get("performance_governor")
    return getattr(governor, name, fallback) if governor is not None else fallback


JOB_FILE_NAME = "send_logs_job.json"
# WARNING: the fallback numbers below must match SEND_LOGS_JOB_MAX_AGE_HOURS, SEND_LOGS_COPY_CHUNK_BYTES, SEND_LOGS_COPY_PAUSE_SECONDS, SEND_LOGS_SCRUB_PIECE_CHARS and SEND_LOGS_SCRUB_PAUSE_SECONDS in performance_governor.py; the finisher process always uses them.
JOB_MAX_AGE_SECONDS = float(_governor_value("SEND_LOGS_JOB_MAX_AGE_HOURS", 24.0)) * 3600.0
COPY_CHUNK_BYTES = int(_governor_value("SEND_LOGS_COPY_CHUNK_BYTES", 262144))
COPY_PAUSE_SECONDS = float(_governor_value("SEND_LOGS_COPY_PAUSE_SECONDS", 0.002))
SCRUB_PIECE_CHARS = int(_governor_value("SEND_LOGS_SCRUB_PIECE_CHARS", 48000))
SCRUB_PAUSE_SECONDS = float(_governor_value("SEND_LOGS_SCRUB_PAUSE_SECONDS", 0.003))

# WARNING: these decide which files a Send Logs carries; config files and their backups hold the webhook address and the owner's folders, so they never go.
SKIP_NAMES = ("emulator_config.json", "retro.config.json", "staged_update.json")
KEEP_EXTENSIONS = (".txt", ".log", ".marker", ".json")
SKIP_FOLDERS = ("share_index",)
WEBHOOK_STARTS = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/",
                  "https://canary.discord.com/api/webhooks/", "https://ptb.discord.com/api/webhooks/")


# ============================================================================
# SECTION: Picking and copying the logs
# ============================================================================


def pick_webhook_url(config_url, default_url):
    """The Discord webhook the logs go to: retro_config.json's send_logs_webhook_url when set, else the governor's SEND_LOGS_WEBHOOK_URL; empty when neither is set or the address is not a Discord webhook."""
    url = str(config_url or "").strip() or str(default_url or "").strip()
    return url if url.startswith(WEBHOOK_STARTS) else ""


def copy_logs(logs_dir, snapshot_dir, deadline):
    """Copies every log into snapshot_dir until the time.time() deadline passes; a text log copied in the middle of a line is cut back to its last whole line. Returns how many files were copied."""
    copied = 0
    for arc, full in logs_file_list(logs_dir):
        if time.time() > deadline:
            break
        dest = os.path.join(snapshot_dir, arc.replace("/", os.sep))
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            last = b""
            with open(full, "rb") as src, open(dest, "wb") as dst:
                while True:
                    chunk = src.read(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    dst.write(chunk)
                    last = chunk
                    time.sleep(COPY_PAUSE_SECONDS)
            if last and not last.endswith(b"\n") and arc.lower().endswith((".txt", ".log")):
                cut_back_to_whole_line(dest)
            copied += 1
        except OSError as swallowed_error:
            _log_swallowed("copy_logs: copying one log", swallowed_error)
    return copied


def logs_wanted(arc_name):
    """True when the file at this logs-folder-relative name is a log worth sending: a text, log, marker or json file, not a config backup, not in a hidden folder or the media index cache."""
    parts = str(arc_name).replace("\\", "/").lower().split("/")
    name = parts[-1]
    if any(part in SKIP_FOLDERS or part.startswith(".") for part in parts[:-1]):
        return False
    if name.startswith("retro_config") or name in SKIP_NAMES:
        return False
    return name.endswith(KEEP_EXTENSIONS)


def logs_file_list(logs_dir, skip_names=()):
    """Every wanted file under the logs folder as (name inside the zip, full path), smallest folders first; zips, the names in skip_names and the files logs_wanted refuses are left out."""
    _found = []
    for _root, _dirs, _files in os.walk(logs_dir):
        for _name in _files:
            if _name.lower().endswith(".zip") or _name in skip_names:
                continue
            if not logs_wanted(os.path.relpath(os.path.join(_root, _name), logs_dir)):
                continue
            _full = os.path.join(_root, _name)
            if os.path.isfile(_full):
                _found.append((os.path.relpath(_full, logs_dir).replace(os.sep, "/"), _full))
    _found.sort(key=lambda _pair: (_pair[0].count("/"), _pair[0]))
    return _found


def read_tail(path, tail_bytes):
    """The bytes of one log: all of it when it is within tail_bytes, else only its newest tail_bytes behind a one-line note saying the start was cut."""
    with open(path, "rb") as _f:
        _size = os.fstat(_f.fileno()).st_size
        if _size <= tail_bytes:
            return _f.read()
        _f.seek(_size - tail_bytes)
        _data = _f.read()
    _cut = _data.find(b"\n")
    if _cut >= 0:
        _data = _data[_cut + 1:]
    return ("[the first %d bytes of this %d byte file were left out to keep the zip small]\n" % (_size - len(_data), _size)).encode("utf-8") + _data


def cut_back_to_whole_line(path):
    """Cuts a copied log back to its last whole line (a line being written while the copy ran)."""
    try:
        with open(path, "rb+") as _fh:
            _size = _fh.seek(0, 2)
            _back = min(_size, 65536)
            _fh.seek(_size - _back)
            _tail = _fh.read(_back)
            _nl = _tail.rfind(b"\n")
            if _nl != -1:
                _fh.truncate(_size - _back + _nl + 1)
    except OSError as _swallowed_error:
        _log_swallowed("cut_back_to_whole_line", _swallowed_error)


# ============================================================================
# SECTION: Taking the personal parts out
# ============================================================================


class LogScrubber:
    """Takes the personal parts out of log text before it is sent (owner 2026-11-14): the Windows user name, this PC's name, server computer names (same name, same label everywhere), IP and MAC addresses, e-mail addresses, Discord webhook links and the device id. The CPU, graphics card, RAM, cores and threads are not touched."""

    _USER_PATH = re.compile(r"([A-Za-z]:(?:\\\\|\\|/)Users(?:\\\\|\\|/))([^\\/\s\"'<>|:*?]+)", re.I)
    _UNC_HOST = re.compile(r"(?<![\\\w:])\\{2,4}([A-Za-z0-9][A-Za-z0-9_.\-]*)(?=\\)")
    _HOST_EQUALS = re.compile(r"\bhost=([A-Za-z0-9][A-Za-z0-9_.\-]*)", re.I)
    _IPV4 = re.compile(r"(?<![\d.])((?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3})(?!\d)(?!\.\d)")
    _MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
    _EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
    _WEBHOOK = re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w\-]+", re.I)
    _KEEP_IPS = ("127.0.0.1", "0.0.0.0")
    _COMMON_USER_NAMES = ("user", "users", "admin", "administrator", "owner", "public", "default", "system", "guest", "retro", "python", "vlc", "temp")

    def __init__(self, logs_dir=None):
        self._servers = {}
        self._ips = {}
        self._this_pc = set()
        self._user = ""
        self._device_id = ""
        for _var in ("COMPUTERNAME", "HOSTNAME"):
            _val = os.environ.get(_var, "").strip()
            if len(_val) >= 3:
                self._this_pc.add(_val.lower())
        _user = (os.environ.get("USERNAME") or os.environ.get("USER") or "").strip()
        if len(_user) >= 4 and _user.lower() not in self._COMMON_USER_NAMES:
            self._user = _user
        if logs_dir:
            _id_path = os.path.join(logs_dir, ".retro_tv_device_id")
            if os.path.isfile(_id_path):
                try:
                    with open(_id_path, "r", encoding="utf-8", errors="replace") as _f:
                        _val = _f.read().strip()
                    if len(_val) >= 8:
                        self._device_id = _val
                except OSError as _swallowed_error:
                    _log_swallowed("LogScrubber: reading the device id", _swallowed_error)

    def learn(self, text):
        """Notes the server computer names in this text so every file can swap them for the same label."""
        for _pat in (self._UNC_HOST, self._HOST_EQUALS):
            for _m in _pat.finditer(text):
                _name = _m.group(1).lower()
                if _name in ("localhost", "127.0.0.1") or self._IPV4.fullmatch(_name) or _name in self._this_pc or _name in self._servers:
                    continue
                self._servers[_name] = "<server-%d>" % (len(self._servers) + 1)

    def _ip_label(self, match):
        _ip = match.group(1)
        if _ip in self._KEEP_IPS:
            return _ip
        return self._ips.setdefault(_ip, "<ip-%d>" % (len(self._ips) + 1))

    def clean(self, text):
        """The text with the personal parts replaced by labels."""
        text = self._WEBHOOK.sub("<webhook>", text)
        text = self._USER_PATH.sub(lambda m: m.group(1) + "<user>", text)
        for _name, _label in sorted(self._servers.items(), key=lambda kv: -len(kv[0])):
            text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(_name) + r"(?![A-Za-z0-9])", _label, text, flags=re.I)
        for _name in sorted(self._this_pc, key=len, reverse=True):
            text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(_name) + r"(?![A-Za-z0-9])", "<this-pc>", text, flags=re.I)
        if self._user:
            text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(self._user) + r"(?![A-Za-z0-9])", "<user>", text, flags=re.I)
        if self._device_id:
            text = text.replace(self._device_id, "<device-id>")
        text = self._IPV4.sub(self._ip_label, text)
        text = self._MAC.sub("<mac>", text)
        text = self._EMAIL.sub("<email>", text)
        return text


def in_pieces(text, work, piece_chars=None):
    """Runs work(piece) on about piece_chars of whole lines at a time and yields to the other threads between pieces (owner 2026-11-16: Send Logs still made the picture stutter; one regex pass over several megabytes holds the interpreter for a few hundred milliseconds at a time). Returns the pieces' results joined."""
    # WARNING: pieces end on a line end, so a label swap never straddles two pieces; the short sleep is what lets the picture thread run.
    piece_chars = SCRUB_PIECE_CHARS if piece_chars is None else piece_chars
    out = []
    _at = 0
    _n = len(text)
    while _at < _n:
        _stop = min(_n, _at + piece_chars)
        if _stop < _n:
            _nl = text.find("\n", _stop)
            _stop = _n if _nl == -1 else _nl + 1
        _result = work(text[_at:_stop])
        if _result is not None:
            out.append(_result)
        _at = _stop
        time.sleep(SCRUB_PAUSE_SECONDS)
    return "".join(out)


# ============================================================================
# SECTION: Building the zip
# ============================================================================


def build_zip(out_path, note, logs_dir, max_bytes, tail_bytes, least_tail_bytes, version_text, machine_text):
    """Writes the zip of every log plus tester_note.txt (its head names the version and the machine given); halves the per-log size and starts again until the zip is within max_bytes. Returns the size in bytes, or None when even the smallest logs do not fit."""
    _stamp = datetime.datetime.now().replace(microsecond=0).isoformat()
    _head = [
        "RETRO TV LOGS",
        "sent: %s" % _stamp,
        "version: %s" % version_text,
        "machine: %s" % machine_text,
        "",
        "TESTER NOTE (what went wrong, which channel):",
        note.strip() if note and note.strip() else "(none given)",
    ]
    _skip = (os.path.basename(out_path),)
    _tail = max(int(tail_bytes), int(least_tail_bytes))
    while True:
        _blobs = []
        for _arc, _full in logs_file_list(logs_dir, _skip):
            try:
                _blobs.append((_arc, read_tail(_full, _tail).decode("utf-8", errors="replace")))
            except OSError as _swallowed_error:
                _log_swallowed("build_zip: reading one log", _swallowed_error)
        _scrub = LogScrubber(logs_dir)
        _scrub.learn("\n".join(_head))
        for _arc, _text in _blobs:
            in_pieces(_text, lambda _piece: _scrub.learn(_piece))
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as _zf:
            _zf.writestr("tester_note.txt", _scrub.clean("\n".join(_head) + "\n"))
            for _arc, _text in _blobs:
                _zf.writestr("logs/" + _arc, in_pieces(_text, _scrub.clean).encode("utf-8"))
        _size = os.path.getsize(out_path)
        if _size <= max_bytes:
            return _size
        if _tail <= least_tail_bytes:
            return None
        _tail = max(int(least_tail_bytes), _tail // 2)


# ============================================================================
# SECTION: The job file
# ============================================================================


def read_job(job_path):
    """The job as a dict, or None when the file is missing or not readable."""
    try:
        with open(job_path, "r", encoding="utf-8") as fh:
            job = json.load(fh)
        return job if isinstance(job, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        _log_swallowed("send_logs_finisher: reading the Send Logs job", error)
        return None


def write_job(job_path, job):
    """Writes the job whole (a temporary file then a rename, so a reader never sees half of it)."""
    job = dict(job)
    job["updated"] = time.time()
    tmp = job_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(job, fh)
        os.replace(tmp, job_path)
        return True
    except OSError as error:
        _log_swallowed("send_logs_finisher: writing the Send Logs job", error)
        return False


def remove_job(job_path):
    """Removes the job and its half-written copy; one that is not there is already gone."""
    for path in (job_path, job_path + ".tmp"):
        try:
            os.remove(path)
        except FileNotFoundError:
            continue
        except OSError as error:
            _log_swallowed("send_logs_finisher: removing the Send Logs job", error)


# ============================================================================
# SECTION: Posting to Discord
# ============================================================================


def post_zip(url, zip_path, message, timeout, urlopen=None):
    """Posts one zip to a Discord webhook as a file with a short message that pings nobody; returns (ok, text). urlopen is auto_updater._urlopen when the caller has it (it retries with the bundled certificate roots), else urllib's own is used."""
    boundary = "----RetroTV" + uuid.uuid4().hex
    payload = json.dumps({"content": str(message)[:1800], "allowed_mentions": {"parse": []}}).encode("utf-8")
    with open(zip_path, "rb") as fh:
        zip_bytes = fh.read()
    name = os.path.basename(zip_path)
    body = b"".join([
        ("--%s\r\nContent-Disposition: form-data; name=\"payload_json\"\r\nContent-Type: application/json\r\n\r\n" % boundary).encode("utf-8"),
        payload, b"\r\n",
        ("--%s\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"%s\"\r\nContent-Type: application/zip\r\n\r\n" % (boundary, name)).encode("utf-8"),
        zip_bytes, b"\r\n",
        ("--%s--\r\n" % boundary).encode("utf-8"),
    ])
    # WARNING: Discord answers 403 (Cloudflare error 1010) to urllib's own User-Agent, so the request names the app.
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "User-Agent": "RetroTVEmulator-SendLogs/1",
    })
    try:
        with (urlopen(request, timeout) if urlopen is not None else urllib.request.urlopen(request, timeout=timeout)) as response:
            code = getattr(response, "status", 200)
        return (200 <= int(code) < 300), "HTTP %s" % code
    except urllib.error.HTTPError as http_error:
        return False, "HTTP %s" % http_error.code
    except Exception as net_error:
        return False, str(net_error)[:200]


def run(job_path):
    """Posts the zip the job names and removes the job and the zip when it was accepted; returns True when the logs went. A job with no finished zip is left for the app's next start to finish."""
    job = read_job(job_path)
    if not job or job.get("state") not in ("ready", "posting"):
        return False
    if time.time() - float(job.get("updated", 0) or 0) > JOB_MAX_AGE_SECONDS:
        remove_job(job_path)
        return False
    zip_path = str(job.get("zip_path", "") or "")
    url = str(job.get("url", "") or "")
    if not url or not zip_path or not os.path.isfile(zip_path):
        remove_job(job_path)
        return False
    # WARNING: a finisher that is itself cut short leaves the job as it was, so the next app start tries again.
    ok, _why = post_zip(url, zip_path, job.get("message", ""), float(job.get("timeout", 60) or 60))
    if ok:
        remove_job(job_path)
        try:
            os.remove(zip_path)
        except OSError as error:
            _log_swallowed("send_logs_finisher: removing the sent zip", error)
    return ok
