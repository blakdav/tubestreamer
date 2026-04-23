# TubeStreamer

A self-hosted web app that downloads YouTube videos directly to a folder on your NAS, ready for Plex. Paste a link from your phone, it downloads in the background, and old videos are automatically cleaned up after a configurable number of days.

## Stack

- **yt-dlp** — YouTube downloading
- **ffmpeg** — audio/video merging
- **Flask** — lightweight web backend
- **Docker** — single container deployment

## Setup

### 1. Clone / copy the folder

```bash
cp -r tubestreamer ~/docker/tubestreamer
cd ~/docker/tubestreamer
```

### 2. Edit the volume path in `docker-compose.yml`

```yaml
volumes:
  - /mnt/user/media/youtube:/downloads
```

Point this at whatever share you want Plex to index. The container writes all videos there, along with two hidden metadata files (`.ytdl_db.json` and `.ytdl_config.json`) that track download timestamps and settings.

### 3. Build and start

```bash
docker compose up -d --build
```

### 4. Access the UI

```
http://<your-server-ip>:5005
```

Default port is `5005`. Change it in `docker-compose.yml` if it conflicts with anything.

---

## Usage

1. Paste a YouTube URL into the input field
2. Hit **Download** or press Enter
3. Progress updates live — the video downloads directly to your configured folder
4. The video list refreshes automatically every 30 seconds

---

## Auto-Delete

Auto-delete runs in the background every hour. Settings are saved to `.ytdl_config.json` in your downloads folder and persist across container restarts.

| Setting | Default | Description |
|---|---|---|
| Enabled | Yes | Toggle auto-delete on/off |
| Delete after | 30 days | Age at which videos are removed |

Videos expiring within 7 days are highlighted in the UI. You can also trigger a manual cleanup run from the settings panel at any time.

---

## Plex Integration

Point a Plex library at the same folder you mounted as `/downloads`. Recommended library type: **Movies** or **Other Videos** depending on what you're downloading.

Plex will pick up new files automatically if you have library auto-scan enabled, or you can trigger a manual scan after downloading.

---


## Files

```
tubestreamer/
├── app.py                  # Flask backend
├── Dockerfile
├── docker-compose.yml
└── templates/
    └── index.html          # Web UI
```

Generated at runtime in your downloads folder:
```
/your/downloads/folder/
├── .ytdl_db.json           # Download history + timestamps
└── .ytdl_config.json       # Auto-delete settings
```

---

## Updating yt-dlp

YouTube changes frequently. If downloads start failing, rebuild the image to pull the latest yt-dlp:

```bash
docker compose down
docker compose up -d --build --no-cache
```

---

## Notes

- Downloads best available MP4. ffmpeg merges video and audio tracks automatically.
- Only single video URLs are supported — no playlist handling.
- The `.ytdl_db.json` file is the source of truth for the video list. If you manually delete a file from disk, it will be removed from the UI on the next refresh.
