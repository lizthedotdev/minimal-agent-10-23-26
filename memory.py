"""Long-term memory: notes saved to a SQLite file, searched with SQLite's built-in full-text search (FTS5)."""

import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime

DB_PATH = os.getenv("MEMORY_DB", "memory.db")  # survives restarts; delete the file to forget everything


def _connect():
    """Open the database, creating the table the first time."""
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories USING fts5(content, created_at UNINDEXED)")
    return db


def save(content):
    """Store one note and return its id."""
    with closing(_connect()) as db, db:  # closing() closes the file; `db` commits on success
        cur = db.execute("INSERT INTO memories (content, created_at) VALUES (?, ?)",
                         (content, datetime.now().isoformat(timespec="seconds")))
    return cur.lastrowid


def search(query, limit=5):
    """Return up to `limit` (id, content, created_at) rows matching any word in `query`, best match first."""
    words = re.findall(r"\w+", query)  # raw text can break FTS5 syntax (quotes, hyphens), so keep only words
    if not words:
        return []
    match = " OR ".join(f'"{w}"' for w in words)  # any word matches; bm25 ranks notes with more/rarer hits higher
    with closing(_connect()) as db:
        return db.execute("SELECT rowid, content, created_at FROM memories WHERE memories MATCH ? ORDER BY rank LIMIT ?",
                          (match, limit)).fetchall()
