import os
import json
import time
import threading
import subprocess
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, Response
import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
DB_FILE = os.path.join(DOWNLOAD_DIR, ".ytdl_db.json")
CONFIG_FILE = os.path.join(DOWNLOAD_DIR, ".ytdl_config.json")

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
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)

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
    db = load_db()
    cutoff = time.time() - (days * 86400)
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

@app.route("/api/download", methods=["POST"])
def download():
    import queue as queue_module
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    q = queue_module.Queue()

    def do_download():
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(url, download=False)
                title = info.get("title", "Unknown")
                duration = info.get("duration", 0)
                thumbnail = info.get("thumbnail", "")

            q.put({"status": "downloading", "msg": f"Downloading: {title}"})

            downloaded_file = None
            last_pct = [-1]

            def progress_hook(d):
                nonlocal downloaded_file
                if d["status"] == "downloading":
                    try:
                        pct_val = int(float(d.get("downloaded_bytes", 0)) / float(d.get("total_bytes") or d.get("total_bytes_estimate") or 1) * 100)
                        if pct_val - last_pct[0] >= 5:
                            last_pct[0] = pct_val
                            speed = d.get("_speed_str", "").strip()
                            eta = d.get("_eta_str", "").strip()
                            q.put({"status": "downloading", "msg": f"{title} — {pct_val}% at {speed}, ETA {eta}"})
                    except Exception:
                        pass
                elif d["status"] == "finished":
                    downloaded_file = d["filename"]

            ydl_opts = {
                "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "merge_output_format": "mp4",
                "quiet": True,
                "no_warnings": True,
                "progress_hooks": [progress_hook],
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            if downloaded_file:
                if not os.path.exists(downloaded_file):
                    base = os.path.splitext(downloaded_file)[0]
                    for ext in [".mp4", ".mkv", ".webm"]:
                        if os.path.exists(base + ext):
                            downloaded_file = base + ext
                            break

                filename = os.path.basename(downloaded_file)
                db = load_db()
                db[filename] = {
                    "title": title,
                    "thumbnail": thumbnail,
                    "duration": duration,
                    "downloaded_at": time.time(),
                    "url": url
                }
                save_db(db)
                q.put({"status": "done", "msg": "Download complete!", "filename": filename, "title": title})
            else:
                q.put({"status": "error", "msg": "Download finished but could not locate file."})

        except Exception as e:
            q.put({"status": "error", "msg": str(e)})

    t = threading.Thread(target=do_download, daemon=True)
    t.start()

    def generate():
        yield f"data: {json.dumps({'status': 'starting', 'msg': 'Fetching video info...'})}\n\n"
        while True:
            try:
                msg = q.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["status"] in ("done", "error"):
                    break
            except Exception:
                yield f"data: {json.dumps({'status': 'error', 'msg': 'Timed out waiting for download.'})}\n\n"
                break

    return Response(generate(), mimetype="text/event-stream")

@app.route("/api/delete/<path:filename>", methods=["DELETE"])
def delete_video(filename):
    filepath = os.path.join(DOWNLOAD_DIR, filename)
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

if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    cleanup_thread = threading.Thread(target=run_cleanup, daemon=True)
    cleanup_thread.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
