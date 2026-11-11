"""Pick a category folder for a video before it downloads.

Categories are yours: they live in .ytdl_config.json (edited in the UI), plus
any subfolder you create in the download folder. Nothing about them is in the
code. A video goes through these checks in order and stops at the first that
decides:

1. Learned channel: you moved a video from this channel before, so its
   uploads follow (see "learned_channels" in the config).
2. Listed channel: the channel is in a category's channel list.
3. Language: the video's language, or its title's script (e.g. Chinese
   characters), matches a category's languages.
4. Keywords, scored by where they appear: title and tags count most, the
   first few minutes of captions least, so one stray word ("wow!" said out
   loud) doesn't sort a video. Needs a clear winner.
5. A local model (Ollama), given the title, tags, description, captions and
   your categories. Only runs when the steps above didn't decide.
6. The fallback folder ("General" unless you rename it).
"""

import gpulock
import json
import os
import re
import urllib.error
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "").strip().rstrip("/")
# A small model is plenty to pick one label from a short list, and leaves the
# GPU to Vidscribe's transcriptions if they share it.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b").strip()
OLLAMA_TIMEOUT = 120
GPU = gpulock.GpuLock("TubeStreamer")

CAPTION_SECONDS = 180       # how much of the captions to read
CAPTION_CHARS = 4000

# Points per distinct keyword, by where it was found. A category needs
# THRESHOLD points, and more than any other category, to win on keywords.
WEIGHTS = {"title": 3, "tags": 3, "channel": 2, "description": 1, "captions": 1, "youtube_category": 1}
THRESHOLD = 3

# Writing systems a "languages" entry can also match in the title.
SCRIPTS = {
    "zh": re.compile(r"[一-鿿]"),
    "ja": re.compile(r"[぀-ヿ]"),
    "ko": re.compile(r"[가-힯]"),
    "ru": re.compile(r"[Ѐ-ӿ]"),
}

NAME_MAX = 60


def valid_name(name):
    """A category is a folder directly inside the download folder."""
    return (isinstance(name, str) and 0 < len(name) <= NAME_MAX and name == name.strip()
            and not name.startswith(".") and not any(c in name for c in '/\\:*?"<>|'))


def _words(value):
    """A list of non-empty strings, from a list or a comma-separated string."""
    items = value.split(",") if isinstance(value, str) else (value or [])
    out = []
    for item in items:
        item = str(item).strip()
        if item and item.lower() not in (o.lower() for o in out):
            out.append(item)
    return out


def clean_categories(raw):
    """Normalize the category list sent from the UI; drops bad or duplicate names."""
    out, seen = [], set()
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "")).strip()
        if not valid_name(name) or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({
            "name": name,
            "description": str(c.get("description", "")).strip()[:500],
            "keywords": _words(c.get("keywords")),
            "channels": _words(c.get("channels")),
            "languages": [l.lower() for l in _words(c.get("languages"))],
        })
    return out


def _pattern(keyword):
    # Whole words for Latin text; plain substring for CJK and other scripts
    # without spaces between words (中文 should match inside 学中文).
    if keyword.isascii():
        return re.compile(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", re.IGNORECASE)
    return re.compile(re.escape(keyword), re.IGNORECASE)


def _fields(info, captions=""):
    return {
        "title": info.get("title") or "",
        "tags": " | ".join(info.get("tags") or []),
        "channel": info.get("channel") or info.get("uploader") or "",
        "description": info.get("description") or "",
        "youtube_category": " | ".join(info.get("categories") or []),
        "captions": captions,
    }


def score(categories, fields):
    """[(points, name, [matched keywords])], best first."""
    results = []
    for cat in categories:
        points, hits = 0, []
        for kw in cat.get("keywords") or []:
            pat = _pattern(kw)
            best = max((WEIGHTS[f] for f, text in fields.items() if text and pat.search(text)), default=0)
            if best:
                points += best
                hits.append(kw)
        if points:
            results.append((points, cat["name"], hits))
    results.sort(key=lambda r: -r[0])
    return results


def _keyword_winner(categories, fields):
    ranked = score(categories, fields)
    if ranked and ranked[0][0] >= THRESHOLD and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
        return ranked[0][1], "keywords: " + ", ".join(ranked[0][2][:4])
    return None, None


def _channel_keys(info):
    keys = {info.get("channel_id"), info.get("uploader_id"), info.get("channel"),
            info.get("uploader"), info.get("channel_url")}
    return {str(k).lower().lstrip("@") for k in keys if k}


def captions_text(ydl, info, seconds=CAPTION_SECONDS):
    """The first few minutes of the video's captions as plain text ("" if none).
    Prefers uploaded subtitles, then YouTube's automatic ones, in the video's
    own language when known."""
    lang = (info.get("language") or "").lower()
    for source in ("subtitles", "automatic_captions"):
        tracks = info.get(source) or {}
        if not tracks:
            continue
        keys = list(tracks)
        prefer = [k for k in keys if k.endswith("-orig")]
        prefer += [k for k in keys if lang and k.lower().split("-")[0] == lang.split("-")[0]]
        prefer += [k for k in keys if k.lower().startswith("en")] + keys
        for key in prefer:
            formats = {f.get("ext"): f.get("url") for f in tracks.get(key) or [] if f.get("url")}
            for ext in ("json3", "vtt"):
                if ext not in formats:
                    continue
                try:
                    raw = ydl.urlopen(formats[ext]).read().decode("utf-8", "replace")
                    text = _parse_json3(raw, seconds) if ext == "json3" else _parse_vtt(raw, seconds)
                except Exception:
                    continue
                if text:
                    return text[:CAPTION_CHARS]
            break  # tried the best track of this source; don't walk every translation
    return ""


def _parse_json3(raw, seconds):
    parts = []
    for ev in json.loads(raw).get("events") or []:
        if (ev.get("tStartMs") or 0) > seconds * 1000:
            break
        parts.append("".join(s.get("utf8", "") for s in ev.get("segs") or []))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


_VTT_TIME = re.compile(r"^(?:(\d+):)?(\d\d):(\d\d)\.\d+\s+-->")


def _parse_vtt(raw, seconds):
    lines, last = [], None
    for line in raw.splitlines():
        m = _VTT_TIME.match(line)
        if m:
            h, mi, s = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
            if h * 3600 + mi * 60 + s > seconds:
                break
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        # Automatic captions repeat each line as the next one scrolls in
        if line and line != last and not line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            lines.append(line)
            last = line
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def ask_ollama(categories, fallback, info, captions):
    """Ask the local model to pick a category name. Returns (name or None, error)."""
    names = [c["name"] for c in categories]
    if fallback not in names:
        names.append(fallback)
    menu = "\n".join(
        f"- {c['name']}: {c.get('description') or 'no description'}"
        + (f" (hints: {', '.join(c['keywords'][:12])})" if c.get("keywords") else "")
        for c in categories)
    video = "\n".join(filter(None, [
        f"Title: {info.get('title') or ''}",
        f"Channel: {info.get('channel') or info.get('uploader') or ''}",
        f"Tags: {', '.join((info.get('tags') or [])[:30])}" if info.get("tags") else "",
        f"YouTube category: {', '.join(info.get('categories') or [])}" if info.get("categories") else "",
        f"Description: {(info.get('description') or '')[:1500]}" if info.get("description") else "",
        f"Start of the captions: {captions[:3000]}" if captions else "",
    ]))
    system = ("You sort downloaded videos into folders. Answer with the single folder that fits "
              f"the video's main topic. If none clearly fits, answer \"{fallback}\". Titles are "
              "often clickbait, so weigh the channel, tags, description and captions.")
    body = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": f"Folders:\n{menu}\n- {fallback}: anything else\n\nVideo:\n{video}"}],
        "stream": False,
        "think": False,
        # Structured output: the reply can only be one of the folder names
        "format": {"type": "object", "properties": {"folder": {"type": "string", "enum": names}},
                   "required": ["folder"]},
        "options": {"temperature": 0, "num_ctx": 8192},
        # Unload right away so the GPU is free for transcription
        "keep_alive": 0,
    }

    def post(payload):
        req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
            return json.loads(r.read())

    try:
        try:
            reply = post(body)
        except urllib.error.HTTPError as e:
            if e.code == 400 and "think" in e.read().decode("utf-8", "replace").lower():
                body.pop("think")  # model or Ollama version without a thinking switch
                reply = post(body)
            else:
                raise
        content = (reply.get("message") or {}).get("content") or ""
        choice = json.loads(content).get("folder")
    except urllib.error.HTTPError as e:
        return None, f"Ollama HTTP {e.code}" + (" (is the model pulled?)" if e.code == 404 else "")
    except urllib.error.URLError as e:
        return None, f"can't reach Ollama: {e.reason}"
    except Exception as e:
        return None, f"Ollama: {e}"
    return (choice, None) if choice in names else (None, f"Ollama answered {choice!r}")


def classify(info, categories, fallback, learned=None, ydl=None, use_ai=True):
    """Return (category name, reason). categories: cleaned list (config plus
    folders); learned: {channel id: {"category": ...}}; ydl: a YoutubeDL to
    fetch captions with (None skips captions)."""
    names = {c["name"] for c in categories} | {fallback}
    channel_id = info.get("channel_id") or info.get("uploader_id")

    learned_cat = ((learned or {}).get(channel_id) or {}).get("category") if channel_id else None
    if learned_cat in names:
        return learned_cat, "learned channel"

    keys = _channel_keys(info)
    for cat in categories:
        if any(ch.lower().lstrip("@") in keys for ch in cat.get("channels") or []):
            return cat["name"], "listed channel"

    title = info.get("title") or ""
    lang = (info.get("language") or "").lower()
    for cat in categories:
        for want in cat.get("languages") or []:
            if lang and lang.split("-")[0] == want.split("-")[0]:
                return cat["name"], f"language: {lang}"
            if want in SCRIPTS and SCRIPTS[want].search(title):
                return cat["name"], f"{want} script in title"

    name, reason = _keyword_winner(categories, _fields(info))
    if name:
        return name, reason

    captions = captions_text(ydl, info) if ydl is not None else ""
    if captions:
        name, reason = _keyword_winner(categories, _fields(info, captions))
        if name:
            return name, reason + " (incl. captions)"

    ai_note = ""
    if use_ai and OLLAMA_URL and categories:
        # The GPU is shared with Vidscribe and reddit2md (GPU_LOCK_FILE, see
        # gpulock.py). Sorting runs before a download starts, so it never
        # waits: if the GPU is busy (e.g. a transcription), skip the AI and
        # use the fallback folder; "Re-sort General" can sort it later.
        if not GPU.try_acquire("sorting a video"):
            holder = GPU.holder() or "another app"
            return fallback, f"no match; AI skipped: GPU busy ({holder})"
        try:
            name, err = ask_ollama(categories, fallback, info, captions)
        finally:
            GPU.release()
        if name:
            return name, f"AI ({OLLAMA_MODEL})"
        ai_note = f"; AI failed: {err}"

    return fallback, "no match" + ai_note
