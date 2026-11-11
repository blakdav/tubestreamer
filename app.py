import os
import copy
import json
import time
import re
import shutil
import uuid
import threading
import multiprocessing
from flask import Flask, render_template, request, jsonify
import yt_dlp

import sorter

app = Flask(__name__)

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
DB_FILE = os.path.join(DOWNLOAD_DIR, ".ytdl_db.json")
CONFIG_FILE = os.path.join(DOWNLOAD_DIR, ".ytdl_config.json")
QUEUE_FILE = os.path.join(DOWNLOAD_DIR, ".ytdl_queue.json")

# How many finished (done/error/cancelled) jobs to keep visible in the queue
QUEUE_HISTORY = 50

# The DB is written by the download worker and by API requests; serialize it.
db_lock = threading.Lock()
# Same for the config, which sorting also writes (learned channels).
config_lock = threading.RLock()

FILENAME_TEMPLATE = "%(upload_date>%Y-%m-%d)s %(title)s.%(ext)s"
VIDEO_EXTS = (".mp4", ".mkv", ".webm")

DEFAULT_CONFIG = {
    "auto_delete_days": 30,
    "auto_delete_enabled": True,
    "comments_enabled": True,
    "comments_max": 500,
    # Sorting into category folders (see sorter.py). Categories are set in
    # the UI; every subfolder of the download folder is one too.
    "sorting_enabled": True,
    "ai_enabled": True,
    "fallback_category": "General",
    "categories": [],
    # {channel id: {"category": ..., "channel": name}}, from your moves
    "learned_channels": {},
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
    config = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            config.update(json.load(f))
    return config

def save_config(config):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)

# ---------------------------------------------------------------------------
# Paths
#
# Videos sit either directly in DOWNLOAD_DIR (older downloads, or sorting off)
# or one level down in a category folder. DB keys and API filenames are paths
# relative to DOWNLOAD_DIR with "/" ("Warcraft/2026-11-10 Title.mp4").
# ---------------------------------------------------------------------------

def resolve(rel):
    """Absolute path for a relative video path, or None if it could point
    anywhere but a file in DOWNLOAD_DIR or one of its category folders."""
    if not isinstance(rel, str):
        return None
    parts = rel.split("/")
    if not 1 <= len(parts) <= 2 or any(not p or p.startswith(".") or "\\" in p for p in parts):
        return None
    return os.path.join(DOWNLOAD_DIR, *parts)

def rel_of(path):
    return os.path.relpath(path, DOWNLOAD_DIR).replace(os.sep, "/")

def folder_of(rel):
    return rel.split("/", 1)[0] if "/" in rel else ""

def category_folders():
    """Subfolders of DOWNLOAD_DIR that are valid category names."""
    try:
        return sorted(e.name for e in os.scandir(DOWNLOAD_DIR)
                      if e.is_dir() and sorter.valid_name(e.name))
    except OSError:
        return []

def video_dirs():
    return [DOWNLOAD_DIR] + [os.path.join(DOWNLOAD_DIR, n) for n in category_folders()]

def all_categories(config):
    """Configured categories, then folders you made that have no rules yet.
    The fallback folder isn't one: it's where everything else goes."""
    cats = sorter.clean_categories(config.get("categories"))
    fallback = config.get("fallback_category") or "General"
    cats = [c for c in cats if c["name"] != fallback]
    have = {c["name"].lower() for c in cats} | {fallback.lower()}
    for name in category_folders():
        if name.lower() not in have:
            cats.append({"name": name, "description": "", "keywords": [], "channels": [],
                         "languages": [], "from_folder": True})
    return cats

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
                filepath = resolve(filename)
                if not filepath:
                    continue
                if os.path.exists(filepath):
                    os.remove(filepath)
                remove_sidecars(filename)
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
    if "comments_enabled" in data:
        config["comments_enabled"] = bool(data["comments_enabled"])
    if "comments_max" in data:
        config["comments_max"] = min(10000, max(10, int(data["comments_max"])))
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
        filepath = resolve(filename)
        if not filepath or not os.path.exists(filepath):
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
            "size_mb": round(os.path.getsize(filepath) / 1024 / 1024, 1),
            "has_comments": os.path.exists(sidecar_paths(filename)[0]),
            "has_url": bool(entry.get("url")),
            "category": folder_of(filename),
            "sort_reason": entry.get("sort_reason", ""),
            "channel": entry.get("channel", ""),
            "can_learn": bool(entry.get("channel_id")),
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


def new_job(url, kind="video", filename="", title="", mode="top", category=""):
    """kind "video" downloads url; kind "comments" only fetches comments for
    an already-downloaded file; kind "sort" files an already-downloaded video
    into a category folder. category: a folder picked when queueing ("" =
    sort automatically)."""
    return {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "mode": mode,
        "category": category,
        "sort_reason": "",
        "url": url,
        "status": "queued",
        "title": title,
        "thumbnail": "",
        "progress": 0,
        "msg": "Waiting...",
        "filename": filename,
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
    folder = os.path.dirname(base)
    prefix = os.path.basename(base) + "."
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        rest = name[len(prefix):]
        if name.startswith(prefix) and (rest.endswith("part") or rest.endswith("ytdl") or rest.startswith("f")):
            try:
                os.remove(os.path.join(folder, name))
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
        "outtmpl": os.path.join(DOWNLOAD_DIR, FILENAME_TEMPLATE),
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
            update_job(job, title=title, thumbnail=thumbnail, msg="Choosing a folder...")
            category, reason = choose_category(job, info, ydl)
            target = os.path.join(DOWNLOAD_DIR, category) if category else DOWNLOAD_DIR
            os.makedirs(target, exist_ok=True)
            ydl.params["outtmpl"]["default"] = os.path.join(target, FILENAME_TEMPLATE)
            base = os.path.splitext(ydl.prepare_filename(info))[0]
            update_job(job, category=category, sort_reason=reason,
                       msg=f"Starting download into {category or 'the main folder'}...")
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

        filename = rel_of(final_path)
        with db_lock:
            db = load_db()
            db[filename] = {
                "title": title,
                "thumbnail": thumbnail,
                "duration": duration,
                "downloaded_at": time.time(),
                "url": url,
                "channel": info.get("channel") or info.get("uploader") or "",
                "channel_id": info.get("channel_id") or info.get("uploader_id") or "",
                "sort_reason": reason,
            }
            save_db(db)
        finish_job(job, "done", "Download complete", progress=100, filename=filename)
        # Comments get their own queue item (and worker), so the next video
        # starts right away instead of waiting for them
        if load_config().get("comments_enabled", True):
            queue_comments([filename], total_on_site=result_info.get("comment_count"))

    except Exception as e:
        # yt-dlp wraps exceptions raised from hooks, so check the flag too
        if isinstance(e, DownloadCancelled) or job.get("_cancel"):
            remove_partials(base)
            finish_job(job, "cancelled", "Cancelled")
        else:
            msg = str(e).replace("ERROR: ", "", 1)
            finish_job(job, "error", msg)


# ---------------------------------------------------------------------------
# Comments
#
# After a download, the video's top comments are saved next to it as
# "<video name>.comments.json" (e.g. "2026-11-04 Title.comments.json"), for
# reading and for other tools (Vidscribe) to pick up. Fetched in a second pass
# so a slow comment fetch never delays the video itself. Replies are nested
# under their parent comment.
# ---------------------------------------------------------------------------

COMMENTS_SUFFIX = ".comments.json"


def sidecar_paths(filename):
    """filename: the video's path relative to DOWNLOAD_DIR."""
    base = os.path.splitext(filename)[0]
    return [os.path.join(DOWNLOAD_DIR, *(base + COMMENTS_SUFFIX).split("/"))]


def remove_sidecars(filename):
    for path in sidecar_paths(filename):
        if os.path.exists(path):
            os.remove(path)


class _CommentProgress:
    """yt-dlp logger that turns "Downloading comment API JSON page 3 (60/~4100)"
    lines into progress(done, estimated_total) calls."""
    PATTERN = re.compile(r"\((\d+)/~?(\d+)\)")

    def __init__(self, progress):
        self.progress = progress
        self.estimate = None

    def debug(self, msg):
        m = self.PATTERN.search(msg) if "comment" in msg.lower() else None
        if m:
            done, self.estimate = int(m.group(1)), int(m.group(2))
            if self.progress:
                self.progress(done, self.estimate)

    def info(self, msg):
        self.debug(msg)

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def save_comments(url, video_path, total_on_site=None, full=False, limit=500, progress=None):
    """Fetch comments for url and write them beside video_path. Returns the count.
    full=True fetches every comment and reply instead of the top `limit`."""
    if full:
        # max-comments, max-parents, max-replies, max-replies-per-thread
        max_comments = ["all", "all", "all", "all"]
    else:
        # Up to `limit` in total, in YouTube's "top" order. Replies are capped
        # at a quarter of the budget (and 3 per thread) so most of it goes to
        # distinct top-level comments.
        max_comments = [str(limit), "all", str(limit // 4), "3"]
    logger = _CommentProgress(progress)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "getcomments": True,
        "logger": logger,
        "extractor_args": {"youtube": {
            "max_comments": max_comments,
            "comment_sort": ["top"],
        }},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    # Replies can be nested (a reply to a reply); all of them go under the
    # top-level comment that started the thread.
    threads, thread_of = [], {}
    for c in info.get("comments") or []:
        item = {
            "id": c.get("id"),
            "author": c.get("author"),
            "text": c.get("text") or "",
            "likes": c.get("like_count") or 0,
            "timestamp": c.get("timestamp"),
            "is_uploader": bool(c.get("author_is_uploader")),
            "is_pinned": bool(c.get("is_pinned")),
            "replies": [],
        }
        parent = c.get("parent", "root")
        if parent in thread_of:
            item.pop("replies")
            thread = thread_of[parent]
            thread["replies"].append(item)
        else:
            thread = item
            threads.append(item)
        thread_of[item["id"]] = thread

    data = {
        "source": "youtube",
        "url": info.get("webpage_url") or url,
        "video_id": info.get("id"),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "fetched_at": time.time(),
        "sort": "top",
        "mode": "all" if full else "top",
        # With getcomments on, yt-dlp reports the fetched count here, so the
        # real total comes from the download's own info instead
        "total_on_site": total_on_site or logger.estimate,
        "saved": len(info.get("comments") or []),
        "comments": threads,
    }
    path = sidecar_paths(rel_of(video_path))[0]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return data["saved"]


def _comments_child(conn, url, path, total_on_site, full, limit):
    """Runs in a separate process (see fetch_comments_for) and reports back."""
    try:
        n = save_comments(url, path, total_on_site, full, limit,
                          progress=lambda done, est: conn.send(("progress", done, est)))
        conn.send(("done", n))
    except Exception as e:
        conn.send(("error", str(e).replace("ERROR: ", "", 1)))
    finally:
        conn.close()


def fetch_comments_for(job, url, path, total_on_site=None, full=False):
    """Fetch comments in a child process so Cancel can stop it: yt-dlp has no
    way to interrupt a comment fetch, and an "all comments" fetch on a big
    video can run for a long time. Returns the count saved; raises
    DownloadCancelled or RuntimeError."""
    limit = int(load_config().get("comments_max", 500))
    parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
    proc = multiprocessing.get_context("spawn").Process(
        target=_comments_child, args=(child_conn, url, path, total_on_site, full, limit), daemon=True)
    proc.start()
    child_conn.close()
    label = "all comments" if full else "comments"
    update_job(job, msg=f"Fetching {label}...")
    try:
        while True:
            if job.get("_cancel"):
                proc.terminate()
                proc.join()
                tmp = sidecar_paths(rel_of(path))[0] + ".tmp"
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise DownloadCancelled()
            if parent_conn.poll(0.5):
                try:
                    msg = parent_conn.recv()
                except EOFError:
                    raise RuntimeError("comment fetcher stopped unexpectedly")
                if msg[0] == "progress":
                    done, est = msg[1], msg[2]
                    target = est if full else min(limit, est or limit)
                    pct = min(99, int(done * 100 / target)) if target else 0
                    update_job(job, progress=pct, msg=f"Fetching {label}: {done:,} of ~{target:,}")
                elif msg[0] == "done":
                    return msg[1]
                else:
                    raise RuntimeError(msg[1])
            elif not proc.is_alive() and not parent_conn.poll():
                raise RuntimeError("comment fetcher stopped unexpectedly")
    finally:
        proc.join(timeout=5)
        parent_conn.close()


def run_comments_job(job):
    filename = job["filename"]
    path = resolve(filename)
    if not path or not os.path.exists(path):
        finish_job(job, "error", "The video file no longer exists.")
        return
    full = job.get("mode") == "all"
    try:
        n = fetch_comments_for(job, job["url"], path, job.get("total_on_site"), full=full)
        finish_job(job, "done", f"{n:,} comments saved" + (" (all)" if full else ""), progress=100)
    except DownloadCancelled:
        finish_job(job, "cancelled", "Cancelled; any earlier comments file was kept")
    except Exception as e:
        finish_job(job, "error", f"Comments failed: {e}")


def queue_comments(filenames, mode="top", total_on_site=None):
    """Queue comment fetches for downloaded files that have a source URL,
    skipping any already waiting. Returns the new jobs."""
    with db_lock:
        db = load_db()
    added = []
    with jobs_cond:
        pending = {j.get("filename") for j in jobs
                   if j.get("kind") == "comments" and j["status"] in ACTIVE_STATES}
        for filename in filenames:
            entry = db.get(filename) or {}
            if not entry.get("url") or filename in pending:
                continue
            job = new_job(entry["url"], "comments", filename, entry.get("title", filename), mode)
            if total_on_site:
                job["total_on_site"] = total_on_site
            jobs.append(job)
            added.append(job)
        if added:
            save_queue()
            jobs_cond.notify_all()
    return added


# ---------------------------------------------------------------------------
# Sorting into category folders
#
# Each download is filed into a category folder before it starts (sorter.py
# decides; a folder picked when queueing wins). Moving a video yourself
# teaches TubeStreamer its channel, so later uploads from that channel follow.
# "Sort" jobs do the same for videos already sitting in the main folder.
# ---------------------------------------------------------------------------

# Files that travel with a video when it moves: our comments, and anything
# else named after it (yt-dlp metadata, subtitles, thumbnails, Jellyfin .nfo)
SIDECAR_ENDS = (COMMENTS_SUFFIX, ".info.json", ".description", ".srt", ".vtt", ".ass",
                ".jpg", ".jpeg", ".png", ".webp", ".nfo")


def choose_category(job, info, ydl=None):
    """(folder name or "" for the main folder, reason) for a video."""
    if job.get("category"):
        return job["category"], "picked when queued"
    config = load_config()
    if not config.get("sorting_enabled", True):
        return "", ""
    fallback = config.get("fallback_category") or "General"
    return sorter.classify(info, all_categories(config), fallback,
                           learned=config.get("learned_channels"), ydl=ydl,
                           use_ai=config.get("ai_enabled", True))


def learn_channel(channel_id, channel, category):
    with config_lock:
        config = load_config()
        config.setdefault("learned_channels", {})[channel_id] = {"category": category, "channel": channel}
        save_config(config)


def move_video(filename, category, reason, learn=False, extra=None):
    """Move a video and its sidecars to a category folder ("" = main folder).
    Returns the new relative path; raises ValueError with a message for the UI."""
    src = resolve(filename)
    if category and not sorter.valid_name(category):
        raise ValueError("Invalid folder name.")
    with jobs_cond:
        busy = any(j.get("filename") == filename and j["status"] in ACTIVE_STATES
                   and j.get("kind") == "comments" for j in jobs)
    if busy:
        raise ValueError("Its comments are being fetched; move it when that's done.")
    with db_lock:
        db = load_db()
        entry = db.get(filename)
        if not src or entry is None or not os.path.exists(src):
            raise ValueError("The video file no longer exists.")
        new_rel = (category + "/" if category else "") + os.path.basename(src)
        if new_rel != filename:
            dst = os.path.join(DOWNLOAD_DIR, *new_rel.split("/"))
            if os.path.exists(dst):
                raise ValueError(f"{new_rel} already exists.")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            folder, name = os.path.split(src)
            stem = os.path.splitext(name)[0] + "."
            for other in os.listdir(folder):
                if other != name and other.startswith(stem) and other.lower().endswith(SIDECAR_ENDS):
                    shutil.move(os.path.join(folder, other), os.path.join(os.path.dirname(dst), other))
            shutil.move(src, dst)
            del db[filename]
        entry.update(extra or {})
        entry["sort_reason"] = reason
        db[new_rel] = entry
        save_db(db)
    if learn and category and entry.get("channel_id"):
        learn_channel(entry["channel_id"], entry.get("channel", ""), category)
    return new_rel


def run_sort_job(job):
    """File one already-downloaded video into a category folder."""
    filename = job["filename"]
    with db_lock:
        entry = load_db().get(filename)
    if not entry or not resolve(filename) or not os.path.exists(resolve(filename)):
        finish_job(job, "error", "The video file no longer exists.")
        return
    try:
        extra = {}
        if entry.get("url"):
            update_job(job, msg="Fetching video info...")
            opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(entry["url"], download=False)
                update_job(job, msg="Choosing a folder...")
                category, reason = choose_category(job, info, ydl)
            extra = {"channel": info.get("channel") or info.get("uploader") or "",
                     "channel_id": info.get("channel_id") or info.get("uploader_id") or ""}
        else:
            # Found on disk with no source link: the title is all there is
            update_job(job, msg="Choosing a folder (title only)...")
            category, reason = choose_category(job, {"title": entry.get("title", "")})
        if job.get("_cancel"):
            raise DownloadCancelled()
        if not category:
            finish_job(job, "done", "Sorting is off; left in the main folder", progress=100)
            return
        new_rel = move_video(filename, category, reason, extra=extra)
        finish_job(job, "done", f"Moved to {category}", progress=100, filename=new_rel,
                   category=category, sort_reason=reason)
    except DownloadCancelled:
        finish_job(job, "cancelled", "Cancelled")
    except Exception as e:
        finish_job(job, "error", "Sort failed: " + str(e).replace("ERROR: ", "", 1))


@app.route("/api/categories", methods=["GET"])
def get_categories():
    config = load_config()
    fallback = config.get("fallback_category") or "General"
    counts = {}
    with db_lock:
        for filename in load_db():
            counts[folder_of(filename)] = counts.get(folder_of(filename), 0) + 1
    cats = all_categories(config)
    for c in cats:
        c["count"] = counts.get(c["name"], 0)
    learned = [{"id": cid, "channel": v.get("channel", ""), "category": v.get("category", "")}
               for cid, v in (config.get("learned_channels") or {}).items()]
    learned.sort(key=lambda x: (x["category"].lower(), x["channel"].lower()))
    return jsonify({
        "categories": cats,
        "fallback_category": fallback,
        "fallback_count": counts.get(fallback, 0),
        "unsorted_count": counts.get("", 0),
        "sorting_enabled": config.get("sorting_enabled", True),
        "ai_enabled": config.get("ai_enabled", True),
        "ai_configured": bool(sorter.OLLAMA_URL),
        "ai_model": sorter.OLLAMA_MODEL,
        "learned_channels": learned,
    })


@app.route("/api/categories", methods=["POST"])
def update_categories():
    """Any of: categories (the full list), fallback_category, sorting_enabled,
    ai_enabled, forget_channels (channel ids to unlearn)."""
    data = request.json or {}
    with config_lock:
        config = load_config()
        if "fallback_category" in data:
            name = str(data["fallback_category"]).strip()
            if not sorter.valid_name(name):
                return jsonify({"error": "Invalid fallback folder name"}), 400
            config["fallback_category"] = name
        if "categories" in data:
            # Folder-only entries come back from the UI too; keep them only
            # once they have rules, so a deleted folder really disappears
            cats = sorter.clean_categories(data["categories"])
            config["categories"] = [c for c in cats if c["keywords"] or c["channels"]
                                    or c["languages"] or c["description"]
                                    or c["name"] not in category_folders()]
        for key in ("sorting_enabled", "ai_enabled"):
            if key in data:
                config[key] = bool(data[key])
        for cid in data.get("forget_channels") or []:
            (config.get("learned_channels") or {}).pop(str(cid), None)
        save_config(config)
    return get_categories()


@app.route("/api/move", methods=["POST"])
def api_move():
    """{"filename": "...", "category": "Warcraft"} ("" = main folder).
    Teaches the channel unless "learn": false."""
    data = request.json or {}
    filename = data.get("filename", "")
    category = str(data.get("category") or "").strip()
    try:
        new_rel = move_video(filename, category, "moved by you", learn=data.get("learn", True))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"filename": new_rel})


@app.route("/api/sort", methods=["POST"])
def api_sort():
    """{"unsorted": true} queues a sort for every video still in the main
    folder; {"filename": "..."} sorts one video again."""
    data = request.json or {}
    with db_lock:
        db = load_db()
    if data.get("unsorted"):
        names = [n for n in db if not folder_of(n)]
    elif data.get("filename") in db:
        names = [data["filename"]]
    else:
        return jsonify({"error": "Give a filename or unsorted: true"}), 400
    added = []
    with jobs_cond:
        pending = {j.get("filename") for j in jobs
                   if j.get("kind") == "sort" and j["status"] in ACTIVE_STATES}
        for n in names:
            if n in pending or not resolve(n) or not os.path.exists(resolve(n)):
                continue
            entry = db[n]
            job = new_job(entry.get("url", ""), "sort", n, entry.get("title", n))
            jobs.append(job)
            added.append(job)
        if added:
            save_queue()
            jobs_cond.notify_all()
    return jsonify({"queued": len(added)})


def queue_worker(kind):
    """One worker per kind: videos download one at a time, and comment fetches
    (which can be slow) run alongside without holding videos up."""
    while True:
        with jobs_cond:
            while True:
                job = next((j for j in jobs if j["status"] == "queued"
                            and j.get("kind", "video") == kind), None)
                if job:
                    break
                jobs_cond.wait()
            job.update(status="downloading", progress=0, msg="Starting...")
            save_queue()
        if job.get("kind") == "comments":
            run_comments_job(job)
        elif job.get("kind") == "sort":
            run_sort_job(job)
        else:
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
    # Optional folder picked when queueing; "" or missing = sort automatically
    category = str(data.get("category") or "").strip()
    if category and not sorter.valid_name(category):
        return jsonify({"error": "Invalid category name"}), 400
    with jobs_cond:
        added = [new_job(u, category=category) for u in urls]
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
        fresh = new_job(job["url"], job.get("kind", "video"), job.get("filename", ""),
                        job.get("title", ""), job.get("mode", "top"), job.get("category", ""))
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


YOUTUBE_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")


def video_key(url):
    """Compare links by YouTube video id, so youtu.be, shorts, playlist and
    tracking-parameter variants of the same video match; other sites by URL."""
    m = YOUTUBE_ID.search(url or "")
    return ("yt", m.group(1)) if m else ("url", (url or "").strip())


@app.route("/api/status", methods=["GET"])
def video_status():
    """Is this video already in TubeStreamer? Used by the browser extension
    to badge the current tab. ?url=... -> {"state": "downloaded" | "queued" |
    "downloading" | "error" | "none", "title": ...}"""
    key = video_key(request.args.get("url", ""))
    failed = None
    with jobs_cond:
        for j in reversed(jobs):
            if j.get("kind", "video") != "video" or video_key(j["url"]) != key:
                continue
            if j["status"] in ACTIVE_STATES:
                return jsonify({"state": j["status"], "title": j.get("title") or ""})
            if j["status"] == "error" and not failed:
                failed = {"state": "error", "title": j.get("title") or "", "msg": j.get("msg", "")}
    with db_lock:
        db = load_db()
    for filename, entry in db.items():
        if entry.get("url") and video_key(entry["url"]) == key \
                and resolve(filename) and os.path.exists(resolve(filename)):
            return jsonify({"state": "downloaded", "title": entry.get("title", ""), "filename": filename})
    return jsonify(failed or {"state": "none"})


@app.route("/api/comments", methods=["POST"])
def fetch_comments():
    """{"filename": "..."} fetches (or refreshes) one video's top comments;
    add "mode": "all" for every comment and reply. {"missing": true} queues
    every video that has none yet (top comments)."""
    data = request.json or {}
    if data.get("missing"):
        with db_lock:
            names = list(load_db().keys())
        names = [n for n in names
                 if resolve(n) and os.path.exists(resolve(n))
                 and not os.path.exists(sidecar_paths(n)[0])]
    elif data.get("filename"):
        if not resolve(data["filename"]):
            return jsonify({"error": "Invalid filename"}), 400
        names = [data["filename"]]
        mode = "all" if data.get("mode") == "all" else "top"
    else:
        return jsonify({"error": "Give a filename or missing: true"}), 400
    added = queue_comments(names, mode if data.get("filename") else "top")
    if data.get("filename") and not added:
        return jsonify({"error": "No source URL for this video, or it's already queued"}), 400
    return jsonify({"queued": len(added)})


@app.route("/api/delete/<path:filename>", methods=["DELETE"])
def delete_video(filename):
    # Only files in DOWNLOAD_DIR or a category folder: no "../" or dotfiles
    filepath = resolve(filename)
    if not filepath:
        return jsonify({"error": "Invalid filename"}), 400
    with db_lock:
        db = load_db()
        if filename in db:
            del db[filename]
            save_db(db)
    if os.path.exists(filepath):
        os.remove(filepath)
    remove_sidecars(filename)
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

    # Remove stale entries (file no longer exists, or is a fragment leftover)
    removed = []
    for filename in list(db.keys()):
        filepath = resolve(filename)
        if not filename.lower().endswith(VIDEO_EXTS) or not filepath or not os.path.exists(filepath):
            del db[filename]
            removed.append(filename)

    # Add any untracked video files found on disk, in the main folder and
    # in category folders (e.g. ones you moved there by hand)
    added = []
    for folder in video_dirs():
        for name in os.listdir(folder):
            filepath = os.path.join(folder, name)
            filename = rel_of(filepath)
            if name.startswith(".") or not name.lower().endswith(VIDEO_EXTS) \
                    or filename in db or not os.path.isfile(filepath):
                continue
            _track_untracked(db, filename, filepath)
            added.append(filename)

    save_db(db)
    return jsonify({"added": added, "removed": removed})

def _track_untracked(db, filename, filepath):
    db[filename] = {
        "title": os.path.splitext(os.path.basename(filename))[0],
        "thumbnail": "",
        "duration": 0,
        "downloaded_at": os.path.getmtime(filepath),
        "url": ""
    }

# Clean up leftover audio/video fragments from old (pre-fix) downloads
@app.route("/api/cleanup-fragments", methods=["POST"])
def cleanup_fragments():
    removed = []
    for folder in video_dirs():
        for filename in os.listdir(folder):
            # yt-dlp fragment pattern: ...f<id>.m4a / .webm etc, left behind by old buggy version
            if ".f1" in filename or ".f2" in filename or ".f3" in filename:
                ext = os.path.splitext(filename)[1].lower()
                if ext in (".m4a", ".webm", ".m4v"):
                    path = os.path.join(folder, filename)
                    os.remove(path)
                    removed.append(rel_of(path))
    return jsonify({"removed": removed})

if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    load_queue()
    threading.Thread(target=queue_worker, args=("video",), daemon=True).start()
    threading.Thread(target=queue_worker, args=("comments",), daemon=True).start()
    threading.Thread(target=queue_worker, args=("sort",), daemon=True).start()
    cleanup_thread = threading.Thread(target=run_cleanup, daemon=True)
    cleanup_thread.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
