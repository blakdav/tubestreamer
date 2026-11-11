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
2. Hit **Add to Queue** or press Enter — the box clears right away so you can paste the next one
3. Links download one at a time in the background; the **Queue** panel shows live progress for each
4. Finished videos appear in the video list automatically

### Queue

- Paste several links at once (separated by spaces or newlines) to queue them all in one go.
- **Remove** drops a link that hasn't started; **Cancel** stops the current download and deletes its partial files.
- Failed or cancelled items can be **Retry**'d; **Clear finished** tidies up the list.
- Downloads run server-side, so you can close the tab and they keep going.
- The queue is saved to `.ytdl_queue.json`, so pending links survive a container restart (an interrupted download resumes).

---

## One-click sending

### Brave / Chrome extension (desktop)

The `extension/` folder has a small extension that queues videos without copy-pasting:

- **Toolbar button** (or **Alt+Shift+Y**): sends the current tab.
- **Right-click a link → Send link to TubeStreamer**: queue videos straight from a feed or playlist without opening them.
- **Right-click a page → Send this page to TubeStreamer**.
- The icon shows the state of the video **in the current tab**: **✓** already downloaded,
  **↓** queued or downloading, **!** the last attempt failed, nothing if it isn't in
  TubeStreamer. Hover the icon for the title or error. It updates as you switch tabs and videos.
- Desktop notifications when you send something, and again when it finishes downloading or fails.

Install:

1. Open `brave://extensions` (or `chrome://extensions`) and turn on **Developer mode**.
2. Click **Load unpacked** and pick the `extension/` folder.
3. The options page opens: enter the address you use for TubeStreamer (e.g. `https://tubestreamer.example.lan`), click **Save & test**, and allow access when asked. Brave asks for that site and YouTube; YouTube access lets the icon check the video in the current tab, and the extension can't see any other sites.

To update it after a `git pull`, click the reload icon on the extension's card. If an update needs new access, the options page opens: click **Save & test** again.

### iPhone (Shortcuts share sheet)

A Shortcut puts **Send to TubeStreamer** in the share sheet of the YouTube app and Safari.

1. Open **Shortcuts**, tap **+**, and name it `Send to TubeStreamer`.
2. Tap the **ⓘ** (details) button, turn on **Show in Share Sheet**, and set the input to **URLs** and **Safari web pages**.
3. In the *Receive* block at the top, set *If there's no input* to **Get Clipboard**. Running it from the home screen or widget then sends whatever link you copied.
4. Add a **Get Contents of URL** action:
   - URL: `https://tubestreamer.example.lan/api/queue` (your address)
   - Method: **POST**
   - Request Body: **JSON**, add a **Text** field with key `url` and value **Shortcut Input**
5. Add a **Show Notification** action with the text `Queued for TubeStreamer`.

Then in YouTube: **Share → More (…) → Send to TubeStreamer**. The phone has to be able to reach the server, so be on home Wi-Fi or the VPN. Turning on *On Demand* in the WireGuard app makes that automatic.

The API accepts several links in one go too: `{"urls": ["...", "..."]}`.

---

## Sorting into folders

Each download is filed into a category folder (`/downloads/<Category>/`) before it
starts, so a media server can give each folder its own library. Categories are yours:
set them under **Sorting rules** in the UI (saved in `.ytdl_config.json`), and any
subfolder you create in the downloads folder becomes one too. Nothing about them is
in the code or the image.

A video goes through these checks and stops at the first that decides:

1. **Learned channel**: you moved a video from this channel before (Move to… on a
   video), so its later uploads follow. Shown under *Learned channels*; "forget" undoes it.
2. **Listed channel**: the channel's name, @handle or ID is in a category's channel list.
   The most reliable rule, since clickbait titles ("THIS IS HUGE") say nothing.
3. **Language**: the video's language matches a category's languages. `zh`, `ja`, `ko`
   and `ru` also match that script in the title (e.g. Chinese characters).
4. **Keywords**, scored by where they appear: title and tags 3 points, channel name 2,
   description, YouTube category and the first 3 minutes of captions 1. A category needs
   3 points and a clear lead, so one spoken "wow" doesn't sort a video.
5. **Local AI** (optional, Ollama): gets the title, channel, tags, description, captions
   and your categories with their descriptions, and must answer with one of the folder
   names. Runs only when the steps above didn't decide.
6. The **fallback folder** (`General` unless you rename it).

Each video shows its folder, and hovering it shows why it landed there. The queue shows
the same. You can also pick a folder in the dropdown next to **Add to Queue** to skip
the rules for those links. **Sort unsorted videos** files everything still sitting in
the main folder (from before sorting, or added by hand) using the same rules. Moving a
video takes its comments and any other files named after it along.

The API takes an optional folder too: `{"url": "...", "category": "Warcraft"}`.

### Local AI

Set `OLLAMA_URL` (as seen from the container) and optionally `OLLAMA_MODEL` in
`docker-compose.yml`. The default model is `qwen3:4b` (about 3 GB): picking one label
from a short list doesn't need a big model. It's unloaded right after each answer
(`keep_alive: 0`), so it doesn't hold GPU memory another app (e.g. a transcriber)
needs. If Ollama can't be reached, the video goes to the fallback folder and the reason
says why.

### Other apps

Files move between folders, so tools that track videos by path lose track unless they
re-link. Vidscribe re-attaches a transcript when its file turns up under the same name
in another folder.

---

## Auto-Delete

Auto-delete runs in the background every hour. Settings are saved to `.ytdl_config.json` in your downloads folder and persist across container restarts.

| Setting | Default | Description |
|---|---|---|
| Enabled | Yes | Toggle auto-delete on/off |
| Delete after | 30 days | Age at which videos are removed |

Videos expiring within 7 days are highlighted in the UI. You can also trigger a manual cleanup run from the settings panel at any time.

---

## Comments

After each download, TubeStreamer saves the video's top comments next to it as
`<video name>.comments.json`, e.g. `2026-11-04 Some Title.comments.json`. Videos
with saved comments show 💬 in the list. Deleting a video (by hand or by
auto-delete) deletes its comments file too.

Settings panel → **Comments**:

| Setting | Default | Description |
|---|---|---|
| Save comments | On | Fetch comments after each download |
| Max comments per video | 500 | Total saved, in YouTube's "top" order; replies use at most a quarter of this (3 per thread) |

Comments are fetched as their own queue item after the video finishes, on a
separate worker, so the next video never waits for them. If fetching fails,
the video is unaffected and the queue shows why.

On each video in the list:

- **Fetch comments** / **Refresh comments**: top comments, up to the limit above.
  Also works for videos downloaded before comments existed.
- **All comments**: every comment and every reply. YouTube serves roughly 25
  comments a second, so 10k comments take about 6 minutes and a video with
  millions would take days. Progress shows in the queue, **Cancel** stops it,
  and the existing comments file is kept unless the fetch finishes.

Settings → **Fetch Missing Comments** queues top comments for every video that
doesn't have any yet. Videos added by **Rescan Folder** have no source URL, so
they're skipped.

File format (for other tools, such as Vidscribe):

```json
{
  "source": "youtube",
  "url": "https://www.youtube.com/watch?v=...",
  "video_id": "...",
  "title": "...",
  "channel": "...",
  "fetched_at": 1794008253.97,
  "sort": "top",
  "mode": "top",
  "total_on_site": 12345,
  "saved": 500,
  "comments": [
    {
      "id": "...", "author": "@name", "text": "...", "likes": 120,
      "timestamp": 1793000000, "is_uploader": false, "is_pinned": false,
      "replies": [
        { "id": "...", "author": "@other", "text": "...", "likes": 4,
          "timestamp": 1793000100, "is_uploader": true, "is_pinned": false }
      ]
    }
  ]
}
```

`mode` is `"top"` (limited) or `"all"` (every comment and reply).
`total_on_site` is the video's comment count on YouTube (may be `null`).
Replies to replies are flattened into their thread's `replies`. `likes` is
YouTube's rounded count (e.g. 4.8M becomes 4800000).

---

## Plex / Jellyfin Integration

Point a library at the same folder you mounted as `/downloads`, or, with sorting on, one library per category folder (e.g. `/youtube/Warcraft`). Recommended library type: **Movies** or **Other Videos** (Plex), **Home Videos and Photos** or **Mixed** (Jellyfin).

Plex will pick up new files automatically if you have library auto-scan enabled, or you can trigger a manual scan after downloading.

---


## Files

```
tubestreamer/
├── app.py                  # Flask backend
├── sorter.py               # Picks a category folder for each download
├── Dockerfile
├── docker-compose.yml
├── templates/
│   └── index.html          # Web UI
└── extension/              # "Send to TubeStreamer" browser extension
```

Generated at runtime in your downloads folder:
```
/your/downloads/folder/
├── .ytdl_db.json           # Download history + timestamps
├── .ytdl_config.json       # Settings, categories, learned channels
├── .ytdl_queue.json        # Download queue (pending + recent)
├── <Category>/             # Sorted videos (and their .comments.json)
└── General/                # Fallback folder
```

---

## Updating yt-dlp

YouTube changes frequently. If downloads start failing, rebuild the image to pull the latest yt-dlp:

```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```

---

## Notes

- Downloads best available MP4. ffmpeg merges video and audio tracks automatically.
- Only single video URLs are supported — no playlist handling.
- The `.ytdl_db.json` file is the source of truth for the video list. If you manually delete a file from disk, it will be removed from the UI on the next refresh.
