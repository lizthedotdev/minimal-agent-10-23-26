"""What the user has already said in public: semantic search over the transcripts of their published videos.

The published videos live in a Drive folder (PUBLIC_VIDEOS_FOLDER) as .mp4 files with .txt transcripts next to them.
sync() splits each new transcript into short passages, embeds them (EMBED_MODEL via OpenRouter) and adds them to a
local FAISS index, so a search finds the same point even when it was worded differently. Passage text and video
details live in public_videos.db, vectors in public_videos.faiss; both are per user and kept out of git.

The clipping agent uses this to prefer moments the user hasn't already posted, and the user can ask
"have I talked about X publicly?".
"""

import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import faiss
import numpy as np
from openai import OpenAI

import google_format as fmt
import google_workspace as google

FOLDER_ID = os.getenv("PUBLIC_VIDEOS_FOLDER", "183F7pTuktwOfm6-yJ2fABmUZMCYnRWi5")  # lizthedeveloper.com videos
EMBED_MODEL = os.getenv("EMBED_MODEL", "qwen/qwen3-embedding-8b")
DB_PATH, INDEX_PATH = "public_videos.db", "public_videos.faiss"
PASSAGE_CHARS = 600  # about 30 seconds of speech: one point, not a whole video
MIN_WORDS = 8  # skip near-empty transcripts like "Thanks for watching!"
SAME_POINT = 0.65  # at or above: very likely a point already made publicly (paraphrased repeats scored 0.66-0.76)
RELATED = 0.58  # at or above: related; class-only points scored 0.52-0.55, unrelated text 0.45-0.49

_client = None


def _db():
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS transcripts (file_id TEXT PRIMARY KEY, title TEXT, uploaded TEXT, video_id TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS passages (id INTEGER PRIMARY KEY, file_id TEXT, text TEXT)")
    return db


def _index():
    if os.path.exists(INDEX_PATH):
        return faiss.read_index(INDEX_PATH)
    return None  # created on first add, once the embedding size is known


def _embed(texts):
    """Unit-length embeddings, so inner product = cosine similarity."""
    global _client
    _client = _client or OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])
    vectors = []
    for i in range(0, len(texts), 64):
        data = _client.embeddings.create(model=EMBED_MODEL, input=texts[i:i + 64]).data
        vectors += [d.embedding for d in data]
    v = np.array(vectors, dtype="float32")
    faiss.normalize_L2(v)
    return v


def _passages(text):
    """Split a transcript into ~PASSAGE_CHARS pieces at sentence ends."""
    out, piece = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        if piece and len(piece) + len(sentence) > PASSAGE_CHARS:
            out.append(piece)
            piece = ""
        piece = f"{piece} {sentence}".strip()
    return out + ([piece] if piece else [])


def _title(name):
    """'Some caption #tag [1SMZTw].txt' -> 'Some caption #tag'."""
    return re.sub(r"\s*\[\w+\]$", "", name.rsplit(".", 1)[0]).strip()


def sync():
    """Fetch, split and embed transcripts that aren't indexed yet. Returns how many transcripts were added."""
    drive = google._service("drive", "v3")
    files, token = [], None
    while True:
        page = drive.files().list(
            q=f"'{FOLDER_ID}' in parents and trashed = false and (mimeType = 'text/plain' or mimeType = 'video/mp4')",
            pageSize=1000, pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True,
            fields="nextPageToken, files(id, name, mimeType, createdTime)").execute()
        files += page["files"]
        token = page.get("nextPageToken")
        if not token:
            break
    videos = {f["name"].rsplit(".", 1)[0]: f["id"] for f in files if f["mimeType"] == "video/mp4"}
    with closing(_db()) as db:
        known = {row[0] for row in db.execute("SELECT file_id FROM transcripts")}
        new = [f for f in files if f["mimeType"] == "text/plain" and f["id"] not in known]
        if not new:
            return 0

        def fetch(f):  # _service gives each worker thread its own client
            return f, google._service("drive", "v3").files().get_media(fileId=f["id"]).execute().decode("utf-8", "replace")

        with ThreadPoolExecutor(max_workers=8) as pool:
            fetched = list(pool.map(fetch, new))
        start = (db.execute("SELECT max(id) FROM passages").fetchone()[0] or 0) + 1
        rows = []  # (passage id, file_id, text)
        for f, text in fetched:
            if len(text.split()) >= MIN_WORDS:
                for p in _passages(text):
                    rows.append((start + len(rows), f["id"], p))
        index = _index()
        if rows:
            vectors = _embed([r[2] for r in rows])
            index = index or faiss.IndexIDMap(faiss.IndexFlatIP(vectors.shape[1]))
            index.add_with_ids(vectors, np.array([r[0] for r in rows], dtype="int64"))
        with db:  # record transcripts only after embedding worked, so a failure is retried next sync
            db.executemany("INSERT INTO passages VALUES (?, ?, ?)", rows)
            db.executemany("INSERT INTO transcripts VALUES (?, ?, ?, ?)", [
                (f["id"], _title(f["name"]), f["createdTime"][:10], videos.get(f["name"].rsplit(".", 1)[0], ""))
                for f, _ in fetched])
        if index is not None:
            faiss.write_index(index, INDEX_PATH)
    return len(new)


def closest(query, limit=5):
    """[{'score', 'title', 'uploaded', 'link', 'passage'}] for the public videos closest to `query`, best first."""
    index = _index()
    if index is None:
        sync()
        index = _index()
    if index is None:
        return []
    scores, ids = index.search(_embed([query]), limit * 4)  # several passages may come from one video
    best = {}  # file_id -> (score, passage), first hit per video is its best
    with closing(_db()) as db:
        for score, pid in zip(scores[0], ids[0]):
            if pid >= 0:
                file_id, text = db.execute("SELECT file_id, text FROM passages WHERE id = ?", (int(pid),)).fetchone()
                best.setdefault(file_id, (float(score), text))
        hits = []
        for file_id, (score, text) in list(best.items())[:limit]:
            title, uploaded, vid = db.execute(
                "SELECT title, uploaded, video_id FROM transcripts WHERE file_id = ?", (file_id,)).fetchone()
            hits.append({"score": score, "title": title or "(untitled)", "uploaded": uploaded, "passage": text,
                         "link": f"https://drive.google.com/file/d/{vid}/view" if vid else None})
    return hits


def public_videos_search(query, limit=5):
    """The user's published videos that make the closest point to `query`, best first."""
    if not query.strip():
        return "Give some text to search for."
    blocks = [fmt.block(f"[public video] {h['title']}", {
        "similarity": f"{h['score']:.2f}" + (" (very likely the same point)" if h["score"] >= SAME_POINT
                                              else " (related)" if h["score"] >= RELATED else " (different)"),
        "uploaded_to_drive": h["uploaded"],
        "link": h["link"],
        "passage": h["passage"],
    }) for h in closest(query, limit)]
    return fmt.results("closest public videos", blocks, None, "No public video transcripts are indexed.")
