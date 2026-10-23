"""Watches the Google Meet recordings folder and turns each new recording into clips.

A background thread checks the folder every POLL_SECONDS. A recording is ready once its "Notes by Gemini" doc (which
has the timestamped Transcript tab) exists. Ready recordings are queued; the agent reads the transcript, picks the
moments worth clipping, and calls clips_save, which cuts each clip with ffmpeg straight from Drive (no full download),
uploads it to the Agent folder's Clips folder, and logs it in the Clips sheet.

Which recordings have been handled is kept in recordings_state.json (per user, not in git). On the very first run the
recordings already in the folder are marked as seen, not processed; use /clip in the chat to process an older one.
"""

import json
import os
import queue
import re
import subprocess
import tempfile
import threading
from datetime import datetime

import google_format as fmt
import google_workspace as google
import memory

FOLDER_ID = os.getenv("RECORDINGS_FOLDER", "1K_O7LxhasfI8_AxyNkqtC0naC3xhQAJT")  # Drive > Meet Recordings
POLL_SECONDS = int(os.getenv("RECORDINGS_POLL_SECONDS", "300"))
STATE_FILE = "recordings_state.json"
MAX_CLIP_SECONDS = 180

events = queue.Queue()  # recordings waiting to be clipped, filled by the watcher thread or /clip
_lock = threading.Lock()  # guards the state file between the watcher and the worker


# --- finding recordings ---

def _stem(name):
    """'Class - 2026/08/14 09:58 PDT - Recording' -> 'Class - 2026/08/14 09:58 PDT', shared by a meeting's files."""
    return re.sub(r" - (Recording|Notes by Gemini|Chat|Transcript)\b.*$", "", name)


def _listing():
    """Videos in the folder, each with its notes doc and chat file ids when they exist."""
    files, token = [], None
    while True:
        page = google._service("drive", "v3").files().list(
            q=f"'{FOLDER_ID}' in parents and trashed = false", pageSize=200, pageToken=token,
            fields="nextPageToken, files(id, name, mimeType, createdTime, videoMediaMetadata)").execute()
        files += page["files"]
        token = page.get("nextPageToken")
        if not token:
            break
    by_stem = {}
    for f in files:
        by_stem.setdefault(_stem(f["name"]), {})[f["mimeType"]] = f
    out = []
    for stem, kinds in by_stem.items():
        video = kinds.get("video/mp4")
        if video:
            out.append({"video_id": video["id"], "name": stem, "created": video["createdTime"],
                        "seconds": int(video.get("videoMediaMetadata", {}).get("durationMillis", 0)) // 1000,
                        "notes_id": kinds.get("application/vnd.google-apps.document", {}).get("id"),
                        "chat_id": kinds.get("text/plain", {}).get("id")})
    return sorted(out, key=lambda r: r["created"])


def _state():
    return json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else None


def _save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def mark(video_id, status):
    with _lock:
        state = _state() or {}
        state[video_id] = {"status": status, "at": datetime.now().isoformat(timespec="seconds")}
        _save_state(state)


def check_once():
    """Queue recordings that are new and have their transcript. Returns how many were queued."""
    recs = _listing()
    with _lock:
        state = _state()
        if state is None:  # first run: everything already there is old news
            _save_state({r["video_id"]: {"status": "seen at first run", "at": r["created"]} for r in recs})
            return 0
    queued = 0
    for r in recs:
        if r["video_id"] not in state and r["notes_id"]:  # no notes doc yet: Gemini is still writing it, try later
            mark(r["video_id"], "queued")
            events.put(r)
            queued += 1
    return queued


def queue_by_name(text):
    """For /clip: queue the recording whose id or name matches `text`. Returns its name, or None."""
    for r in reversed(_listing()):
        if text in (r["video_id"], r["notes_id"]) or text.lower() in r["name"].lower():
            mark(r["video_id"], "queued")
            events.put(r)
            return r["name"]
    return None


def watch(on_error=print):
    """Run forever in a background thread: check the folder, sleep, repeat."""
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                check_once()
            except Exception as e:  # network blips shouldn't kill the watcher
                on_error(f"(recordings watcher: {type(e).__name__}: {e})")
            stop.wait(POLL_SECONDS)

    threading.Thread(target=loop, daemon=True, name="recordings-watcher").start()
    return stop


def task_prompt(r):
    """The message the agent gets for one new recording."""
    hours = f"{r['seconds'] // 3600}h{r['seconds'] % 3600 // 60:02d}m"
    return f"""New class recording to clip: "{r['name']}" ({hours} long).
video_id: {r['video_id']}

1. Read the transcript with recording_transcript(video_id).
2. Pick the moments worth clipping for social media and the school's library: genuinely funny moments, great \
teaching explanations, student breakthroughs or great questions, quotable insights, and impressive demos. \
Skip logistics, setup, silence and private or sensitive moments (personal details, money, health, anyone upset).
3. Save them all with one clips_save call. Each clip should stand on its own: start a few seconds before the moment \
and end after the payoff, usually 20 to 90 seconds. Aim for 3 to 10 clips, the best first; fewer is fine for a \
dull recording.
4. Reply with a short summary: how many clips, and a line on each."""


# --- tools ---

def recording_transcript(video_id):
    """The recording's timestamped transcript and chat, from its Gemini notes and chat files."""
    rec = next((r for r in _listing() if r["video_id"] == video_id), None)
    if not rec or not rec["notes_id"]:
        return f"No transcript found for video {video_id} (Gemini notes may not exist yet)."
    doc = google._service("docs", "v1").documents().get(documentId=rec["notes_id"], includeTabsContent=True).execute()
    tab = next((t for t in doc.get("tabs", []) if t["tabProperties"].get("title") == "Transcript"), None)
    if not tab:
        return f"The notes doc for {rec['name']} has no Transcript tab."
    text = fmt.doc_to_markdown(tab["documentTab"])
    if rec["chat_id"]:
        chat = google._service("drive", "v3").files().get_media(fileId=rec["chat_id"]).execute().decode("utf-8", "replace")
        text += "\n\n# Chat (start,end timestamps)\n\n" + chat
    return fmt.cut(text, 400_000)  # a full day's class is ~180k chars; Qwen's context fits it


def _seconds(t):
    """'01:02:03', '62:03' or '3723' -> seconds."""
    parts = [float(p) for p in str(t).split(":")]
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


def _hms(s):
    return f"{int(s) // 3600:02d}:{int(s) % 3600 // 60:02d}:{int(s) % 60:02d}"


def clips_save(video_id, clips):
    """Cut each clip from the recording, upload it to Agent/Clips and log it in the Clips sheet."""
    from googleapiclient.http import MediaFileUpload

    rec = next((r for r in _listing() if r["video_id"] == video_id), {"name": video_id, "seconds": 0})
    folder = memory.ids()["clips_folder"]
    drive = google._service("drive", "v3")
    lines, rows = [], []
    for c in clips:
        start, end = _seconds(c["start"]), _seconds(c["end"])
        if not 0 <= start < end or end - start > MAX_CLIP_SECONDS or (rec["seconds"] and end > rec["seconds"] + 5):
            lines.append(f"skipped '{c.get('title')}': bad range {c['start']}-{c['end']} (max {MAX_CLIP_SECONDS}s)")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "clip.mp4")
            run = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-headers", f"Authorization: Bearer {google.access_token()}\r\n",
                 "-ss", _hms(start), "-to", _hms(end),  # before -i: seeks with HTTP range requests, no full download
                 "-i", f"https://www.googleapis.com/drive/v3/files/{video_id}?alt=media&supportsAllDrives=true",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-movflags", "+faststart", out],
                capture_output=True, text=True, timeout=600)
            if run.returncode:
                lines.append(f"failed '{c.get('title')}': ffmpeg: {run.stderr.strip()[-200:]}")
                continue
            name = f"{rec['name']} - {_hms(start).replace(':', '.')} - {c.get('title', 'clip')}.mp4"
            up = drive.files().create(body={"name": name, "parents": [folder]}, fields="id, webViewLink",
                                      media_body=MediaFileUpload(out, mimetype="video/mp4", resumable=True)).execute()
        rows.append([datetime.now().isoformat(timespec="seconds"), rec["name"], _hms(start), _hms(end),
                     c.get("category", ""), c.get("title", ""), c.get("why", ""), c.get("quote", ""), up["webViewLink"]])
        lines.append(f"saved '{c.get('title')}' {_hms(start)}-{_hms(end)}: {up['webViewLink']}")
    if rows:
        google.sheets_append(memory.ids()["clips"], "Clips", rows)
    return "\n".join(lines) or "No clips given."
