import os
import json
import time
import uuid
import threading
from flask import Flask, render_template, request, jsonify
import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
DB_FILE = os.path.join(DOWNLOAD_DIR, ".ytdl_db.json")
CONFIG_FILE = os.path.join(DOWNLOAD_DIR, ".ytdl_config.json")
QUEUE_FILE = os.path.join(DOWNLOAD_DIR, ".ytdl_queue.json")

# How many finished (done/error/cancelled) jobs to keep visible in the queue
QUEUE_HISTORY = 50

# The DB is written by the download worker and by API requests; serialize it.
db_lock = threading.Lock()

DEFAULT_CONFIG = {
    "auto_delete_days": 30,
    "auto_delete_enabled": True
}

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {}

def save_db(db):
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(db, f, indent=2)
    os.replace(tmp, DB_FILE)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return DEFAULT_CONFIG.copy()

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

def run_cleanup():
    """Background thread that checks for expired files every hour."""
    while True:
        time.sleep(3600)
        cleanup_expired()

def cleanup_expired():
    config = load_config()
    if not config.get("auto_delete_enabled", True):
        return
    days = config.get("auto_delete_days", 30)
    cutoff = time.time() - (days * 86400)
    with db_lock:
        db = load_db()
        to_delete = []
        for filename, entry in db.items():
            if entry.get("downloaded_at", 0) < cutoff:
                filepath = os.path.join(DOWNLOAD_DIR, filename)
                if os.path.exists(filepath):
                    os.remove(filepath)
                to_delete.append(filename)
        for filename in to_delete:
            del db[filename]
        if to_delete:
            save_db(db)
    return to_delete

@app.before_request
def require_json_for_changes():
    """Block cross-site requests: any web page you visit could otherwise make
    your browser send a plain POST to TubeStreamer (e.g. trigger a cleanup).
    Requiring a JSON content type makes the browser ask the server first, and
    since we never allow other sites, those requests are refused. The UI, the
    extension and the iPhone Shortcut all send JSON."""
    if request.method in ("POST", "DELETE") and not request.is_json:
        return jsonify({"error": "Requests must use Content-Type: application/json"}), 415

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())

@app.route("/api/config", methods=["POST"])
def update_config():
    data = request.json
    config = load_config()
    if "auto_delete_days" in data:
        config["auto_delete_days"] = max(1, int(data["auto_delete_days"]))
    if "auto_delete_enabled" in data:
        config["auto_delete_enabled"] = bool(data["auto_delete_enabled"])
    save_config(config)
    return jsonify(config)

@app.route("/api/videos", methods=["GET"])
def list_videos():
    db = load_db()
    config = load_config()
    days = config.get("auto_delete_days", 30)
    now = time.time()
    result = []
    for filename, entry in db.items():
        filepath = os.path.join(DOWNLOAD_DIR, filename)
        if not os.path.exists(filepath):
            continue
        age_days = (now - entry.get("downloaded_at", now)) / 86400
        expires_in = days - age_days
        result.append({
            "filename": filename,
            "title": entry.get("title", filename),
            "thumbnail": entry.get("thumbnail", ""),
            "duration": entry.get("duration", 0),
            "downloaded_at": entry.get("downloaded_at", 0),
            "age_days": round(age_days, 1),
            "expires_in_days": round(expires_in, 1),
            "size_mb": round(os.path.getsize(filepath) / 1024 / 1024, 1)
        })
    result.sort(key=lambda x: x["downloaded_at"], reverse=True)
    return jsonify(result)

# ---------------------------------------------------------------------------
# Download queue
#
# Links are added to an in-memory job list and processed one at a time by a
# single background worker, so the UI never blocks on a download. The list is
# mirrored to .ytdl_queue.json so queued links survive a container restart
# (a job that was mid-download when the container stopped is re-queued).
# ---------------------------------------------------------------------------

jobs = []                       # ordered oldest -> newest
jobs_cond = threading.Condition()

ACTIVE_STATES = ("queued", "downloading")


class DownloadCancelled(Exception):
    pass


def _public(job):
    return {k: v for k, v in job.items() if not k.startswith("_")}


def save_queue():
    """Persist the job list. Caller must hold jobs_cond."""
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump([_public(j) for j in jobs], f, indent=2)
    os.replace(tmp, QUEUE_FILE)


def trim_history():
    """Drop the oldest finished jobs beyond QUEUE_HISTORY. Caller holds lock."""
    finished = [j for j in jobs if j["status"] not in ACTIVE_STATES]
    excess = len(finished) - QUEUE_HISTORY
    for j in finished[:max(excess, 0)]:
        jobs.remove(j)


def load_queue():
    if not os.path.exists(QUEUE_FILE):
        return
    try:
        with open(QUEUE_FILE, "r") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    with jobs_cond:
        for j in saved:
            if j.get("status") == "downloading":
                j.update(status="queued", progress=0, msg="Re-queued after restart")
            jobs.append(j)
        jobs_cond.notify_all()


def new_job(url):
    return {
        "id": uuid.uuid4().hex[:12],
        "url": url,
        "status": "queued",
        "title": "",
        "thumbnail": "",
        "progress": 0,
        "msg": "Waiting...",
        "filename": "",
        "added_at": time.time(),
        "finished_at": 0,
    }


def update_job(job, **fields):
    with jobs_cond:
        job.update(fields)


def finish_job(job, status, msg, **fields):
    with jobs_cond:
        job.update(status=status, msg=msg, finished_at=time.time(), **fields)
        trim_history()
        save_queue()


def remove_partials(base):
    """Delete yt-dlp leftovers (.part, .ytdl, per-format fragments) for a cancelled job."""
    if not base:
        return
    prefix = os.path.basename(base) + "."
    for name in os.listdir(DOWNLOAD_DIR):
        rest = name[len(prefix):]
        if name.startswith(prefix) and (rest.endswith("part") or rest.endswith("ytdl") or rest.startswith("f")):
            try:
                os.remove(os.path.join(DOWNLOAD_DIR, name))
            except OSError:
                pass


def run_job(job):
    url = job["url"]
    base = None

    def progress_hook(d):
        if job.get("_cancel"):
            raise DownloadCancelled()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if not total:
                return
            pct = int(float(d.get("downloaded_bytes", 0)) / float(total) * 100)
            speed = (d.get("_speed_str") or "").strip()
            eta = (d.get("_eta_str") or "").strip()
            update_job(job, progress=pct, msg=f"{pct}% at {speed}, ETA {eta}")
        elif d["status"] == "finished":
            update_job(job, msg="Merging...")

    ydl_opts = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(upload_date>%Y-%m-%d)s %(title)s.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "progress_hooks": [progress_hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            update_job(job, msg="Fetching video info...")
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "Unknown")
            duration = info.get("duration", 0)
            thumbnail = info.get("thumbnail", "")
            base = os.path.splitext(ydl.prepare_filename(info))[0]
            update_job(job, title=title, thumbnail=thumbnail, msg="Starting download...")
            if job.get("_cancel"):
                raise DownloadCancelled()

            result_info = ydl.process_ie_result(info, download=True)
            final_path = None
            rds = result_info.get("requested_downloads")
            if rds:
                final_path = rds[0].get("filepath") or rds[0].get("_filename")
            if not final_path:
                for ext in ["mp4", "mkv", "webm"]:
                    candidate = base + "." + ext
                    if os.path.exists(candidate):
                        final_path = candidate
                        break

        if not (final_path and os.path.exists(final_path)):
            finish_job(job, "error", "Download finished but could not locate final file.")
            return

        filename = os.path.basename(final_path)
        with db_lock:
            db = load_db()
            db[filename] = {
                "title": title,
                "thumbnail": thumbnail,
                "duration": duration,
                "downloaded_at": time.time(),
                "url": url,
            }
            save_db(db)
        finish_job(job, "done", "Download complete", progress=100, filename=filename)

    except Exception as e:
        # yt-dlp wraps exceptions raised from hooks, so check the flag too
        if isinstance(e, DownloadCancelled) or job.get("_cancel"):
            remove_partials(base)
            finish_job(job, "cancelled", "Cancelled")
        else:
            msg = str(e).replace("ERROR: ", "", 1)
            finish_job(job, "error", msg)


def queue_worker():
    while True:
        with jobs_cond:
            while True:
                job = next((j for j in jobs if j["status"] == "queued"), None)
                if job:
                    break
                jobs_cond.wait()
            job.update(status="downloading", progress=0, msg="Starting...")
            save_queue()
        run_job(job)


def find_job(job_id):
    return next((j for j in jobs if j["id"] == job_id), None)


@app.route("/api/queue", methods=["GET"])
def get_queue():
    with jobs_cond:
        return jsonify([_public(j) for j in jobs])


@app.route("/api/queue", methods=["POST"])
def add_to_queue():
    """Accepts {"url": "..."} or {"urls": [...]}; a single string may hold
    several whitespace/newline-separated links (e.g. a multi-line paste)."""
    data = request.json or {}
    raw = data.get("urls") or [data.get("url", "")]
    urls = [u for chunk in raw for u in str(chunk).split() if u.strip()]
    if not urls:
        return jsonify({"error": "No URL provided"}), 400
    with jobs_cond:
        added = [new_job(u) for u in urls]
        jobs.extend(added)
        save_queue()
        jobs_cond.notify_all()
    return jsonify([_public(j) for j in added])


@app.route("/api/queue/<job_id>", methods=["DELETE"])
def remove_from_queue(job_id):
    """Queued -> removed; downloading -> cancelled; finished -> removed from list."""
    with jobs_cond:
        job = find_job(job_id)
        if not job:
            return jsonify({"error": "Not found"}), 404
        if job["status"] == "downloading":
            job["_cancel"] = True
            job["msg"] = "Cancelling..."
        else:
            jobs.remove(job)
            save_queue()
    return jsonify({"ok": True})


@app.route("/api/queue/<job_id>/retry", methods=["POST"])
def retry_job(job_id):
    with jobs_cond:
        job = find_job(job_id)
        if not job or job["status"] in ACTIVE_STATES:
            return jsonify({"error": "Not retryable"}), 400
        jobs.remove(job)
        fresh = new_job(job["url"])
        jobs.append(fresh)
        save_queue()
        jobs_cond.notify_all()
    return jsonify(_public(fresh))


@app.route("/api/queue/clear", methods=["POST"])
def clear_finished():
    with jobs_cond:
        jobs[:] = [j for j in jobs if j["status"] in ACTIVE_STATES]
        save_queue()
    return jsonify({"ok": True})


@app.route("/api/delete/<path:filename>", methods=["DELETE"])
def delete_video(filename):
    # Only plain file names inside DOWNLOAD_DIR: no "../", subfolders or dotfiles
    if filename != os.path.basename(filename) or filename.startswith("."):
        return jsonify({"error": "Invalid filename"}), 400
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    with db_lock:
        db = load_db()
        if filename in db:
            del db[filename]
            save_db(db)
    if os.path.exists(filepath):
        os.remove(filepath)
    return jsonify({"ok": True})

@app.route("/api/cleanup", methods=["POST"])
def manual_cleanup():
    deleted = cleanup_expired()
    return jsonify({"deleted": deleted or []})

@app.route("/api/rescan", methods=["POST"])
def rescan():
    with db_lock:
        return _rescan()

def _rescan():
    db = load_db()
    video_exts = (".mp4", ".mkv", ".webm")

    # Remove stale entries (file no longer exists, or is a fragment leftover)
    removed = []
    for filename in list(db.keys()):
        filepath = os.path.join(DOWNLOAD_DIR, filename)
        if not filename.lower().endswith(video_exts) or not os.path.exists(filepath):
            del db[filename]
            removed.append(filename)

    # Add any untracked video files found on disk
    added = []
    for filename in os.listdir(DOWNLOAD_DIR):
        if not filename.lower().endswith(video_exts):
            continue
        if filename in db:
            continue
        filepath = os.path.join(DOWNLOAD_DIR, filename)
        mtime = os.path.getmtime(filepath)
        title = os.path.splitext(filename)[0]
        db[filename] = {
            "title": title,
            "thumbnail": "",
            "duration": 0,
            "downloaded_at": mtime,
            "url": ""
        }
        added.append(filename)

    save_db(db)
    return jsonify({"added": added, "removed": removed})

# Clean up leftover audio/video fragments from old (pre-fix) downloads
@app.route("/api/cleanup-fragments", methods=["POST"])
def cleanup_fragments():
    removed = []
    for filename in os.listdir(DOWNLOAD_DIR):
        # yt-dlp fragment pattern: ...f<id>.m4a / .webm etc, left behind by old buggy version
        if ".f1" in filename or ".f2" in filename or ".f3" in filename:
            ext = os.path.splitext(filename)[1].lower()
            if ext in (".m4a", ".webm", ".m4v"):
                os.remove(os.path.join(DOWNLOAD_DIR, filename))
                removed.append(filename)
    return jsonify({"removed": removed})

if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    load_queue()
    threading.Thread(target=queue_worker, daemon=True).start()
    cleanup_thread = threading.Thread(target=run_cleanup, daemon=True)
    cleanup_thread.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
