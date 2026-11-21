"""Finishes a Send Logs upload after the app has closed (owner 2026-11-16).

The app writes a small job file when a Send Logs starts. When the app is closed while the upload is still going (or not yet started), the app starts this module in a separate process (the same program with --finish-send-logs <job file>) and closes at once; this process posts the finished zip to the Discord webhook and removes the job. It uses nothing but the standard library and never opens the app's logs, so it cannot slow the app's exit or clash with its files.
"""
import json
import os
import time
import urllib.error
import urllib.request
import uuid

JOB_FILE_NAME = "send_logs_job.json"
# WARNING: a job older than this is thrown away instead of sent; a zip from yesterday is not what the tester meant to send.
JOB_MAX_AGE_SECONDS = 24 * 3600


def read_job(job_path):
    """The job as a dict, or None when the file is missing or not readable."""
    try:
        with open(job_path, "r", encoding="utf-8") as fh:
            job = json.load(fh)
        return job if isinstance(job, dict) else None
    except (OSError, ValueError):
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
    except OSError:
        return False


def remove_job(job_path):
    for path in (job_path, job_path + ".tmp"):
        try:
            os.remove(path)
        except OSError:
            pass


def post_zip(url, zip_path, message, timeout):
    """Posts one zip to a Discord webhook as a file with a short message that pings nobody; returns (ok, text)."""
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
        except OSError:
            pass
    return ok
