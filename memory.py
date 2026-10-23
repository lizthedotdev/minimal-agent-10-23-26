"""The agent's memory: three Google Sheets in its own Drive folder, one per layer.

- Core Memory: a few key facts, always in the system prompt. Kept small (CORE_MAX_CHARS).
- Recent Memory: a log of notes. The latest RECENT_IN_PROMPT go in the system prompt; anything older than
  RECENT_DAYS moves to long-term memory when the agent starts.
- Long-term Memory: everything older, plus tabs for structured records (People, Projects, and any the agent adds).
  Not in the prompt; the model finds it with search_memory.

The folder and sheets are created on first use. Their ids are cached in agent_memory.json; if that file is lost, the
folder is found again by name (with the drive.file scope the agent only sees files it created itself).
"""

import json
import os
import re
from datetime import datetime, timedelta

import google_format as fmt
import google_workspace as google

FOLDER_NAME = os.getenv("AGENT_FOLDER", "Agent")
IDS_FILE = "agent_memory.json"
CORE_MAX_CHARS = 4_000  # core memory rides along in every prompt, so it has to stay small
RECENT_IN_PROMPT = 15  # latest recent notes shown in the prompt
RECENT_DAYS = 14  # recent notes older than this move to long-term memory

LAYERS = {  # spreadsheet name -> {tab: column headers}
    "core": ("Core Memory", {"Core": ["key", "fact", "updated_at"]}),
    "recent": ("Recent Memory", {"Log": ["saved_at", "note"]}),
    "long_term": ("Long-term Memory", {
        "Notes": ["saved_at", "note"],
        "People": ["name", "email", "relationship", "notes", "updated_at"],
        "Projects": ["name", "status", "notes", "updated_at"],
    }),
    "clips": ("Clips", {"Clips": ["saved_at", "recording", "start", "end", "category", "title", "why", "quote", "link"]}),
}
SHEET_MIME, FOLDER_MIME = "application/vnd.google-apps.spreadsheet", "application/vnd.google-apps.folder"

_ids = {}


def ids():
    """{'folder', 'core', 'recent', 'long_term'} -> Drive ids, creating whatever doesn't exist yet."""
    if not _ids and os.path.exists(IDS_FILE):
        _ids.update(json.load(open(IDS_FILE)))
    if all(k in _ids for k in ("folder", "clips_folder", *LAYERS)):
        return _ids
    drive = google._service("drive", "v3")
    find = lambda q: drive.files().list(q=q + " and trashed = false", fields="files(id)").execute()["files"]
    if "folder" not in _ids:
        found = find(f"name = '{FOLDER_NAME}' and mimeType = '{FOLDER_MIME}' and 'root' in parents")
        _ids["folder"] = found[0]["id"] if found else drive.files().create(
            body={"name": FOLDER_NAME, "mimeType": FOLDER_MIME}, fields="id").execute()["id"]
    if "clips_folder" not in _ids:  # video clips cut from class recordings (recordings.py)
        found = find(f"name = 'Clips' and mimeType = '{FOLDER_MIME}' and '{_ids['folder']}' in parents")
        _ids["clips_folder"] = found[0]["id"] if found else drive.files().create(
            body={"name": "Clips", "mimeType": FOLDER_MIME, "parents": [_ids["folder"]]}, fields="id").execute()["id"]
    for layer, (name, tabs) in LAYERS.items():
        if layer not in _ids:
            found = find(f"name = '{name}' and mimeType = '{SHEET_MIME}' and '{_ids['folder']}' in parents")
            _ids[layer] = found[0]["id"] if found else _create_sheet(drive, name, tabs)
    with open(IDS_FILE, "w") as f:
        json.dump(_ids, f, indent=2)
    return _ids


def _create_sheet(drive, name, tabs):
    """Create a spreadsheet inside the agent's folder with the given tabs and header rows."""
    sid = drive.files().create(body={"name": name, "mimeType": SHEET_MIME, "parents": [_ids["folder"]]},
                               fields="id").execute()["id"]
    sheets = google._service("sheets", "v4").spreadsheets()
    first = sheets.get(spreadsheetId=sid, fields="sheets.properties.sheetId").execute()["sheets"][0]["properties"]
    names = list(tabs)
    sheets.batchUpdate(spreadsheetId=sid, body={"requests": [  # rename the default tab, add the rest
        {"updateSheetProperties": {"properties": {"sheetId": first["sheetId"], "title": names[0]}, "fields": "title"}},
        *({"addSheet": {"properties": {"title": n}}} for n in names[1:]),
    ]}).execute()
    for tab, headers in tabs.items():
        google._write(sid, f"'{tab}'!A1", [headers])
    return sid


def _rows(layer, tab):
    """All rows of a tab, header first."""
    return google._service("sheets", "v4").spreadsheets().values().get(
        spreadsheetId=ids()[layer], range=f"'{tab}'").execute().get("values", [])


def _now():
    return datetime.now().isoformat(timespec="seconds")


# --- what goes in the system prompt ---

def for_prompt():
    """Core memory and the latest recent notes, formatted for the system prompt."""
    core = [r for r in _rows("core", "Core")[1:] if len(r) > 1]
    recent = [r for r in _rows("recent", "Log")[1:] if len(r) > 1][-RECENT_IN_PROMPT:]
    parts = ["## Core memory (always true unless the user says otherwise; change with core_memory_set)"]
    parts += [f"- {r[0]}: {r[1]}" for r in core] or ["(empty)"]
    parts += ["", "## Recent notes (newest last; older ones are in long-term memory, use search_memory)"]
    parts += [f"- {r[0][:10]}: {r[1]}" for r in recent] or ["(none)"]
    return "\n".join(parts)


# --- tools ---

def core_memory_set(key, fact):
    """Add or replace one core fact, by key."""
    rows = _rows("core", "Core")
    size = sum(len(" ".join(r)) for r in rows[1:] if r and r[0] != key) + len(key) + len(fact)
    if size > CORE_MAX_CHARS:
        return (f"Error: core memory would be {size} chars, over the {CORE_MAX_CHARS} limit. Remove or shorten a "
                "fact first (core_memory_remove), or save this to long-term memory instead.")
    for i, r in enumerate(rows[1:], start=2):
        if r and r[0] == key:
            google._write(ids()["core"], f"'Core'!A{i}", [[key, fact, _now()]])
            return f"Updated core fact '{key}'."
    google.sheets_append(ids()["core"], "Core", [[key, fact, _now()]])
    return f"Added core fact '{key}'."


def core_memory_remove(key):
    """Delete one core fact, by key."""
    rows = _rows("core", "Core")
    keep = [r for r in rows[1:] if r and r[0] != key]
    if len(keep) == len(rows) - 1:
        return f"No core fact with key '{key}'."
    _replace("core", "Core", rows[0], keep)
    return f"Removed core fact '{key}'."


def save_memory(note):
    """Add a note to recent memory."""
    google.sheets_append(ids()["recent"], "Log", [[_now(), note]])
    return "Saved to recent memory."


def search_memory(query, limit=10):
    """Rows from recent and long-term memory containing any word of `query`, more matching words first."""
    words = {w.lower() for w in re.findall(r"\w+", query) if len(w) > 1}
    sheets = google._service("sheets", "v4").spreadsheets()
    hits = []
    for layer in ("recent", "long_term"):
        sid = ids()[layer]
        tabs = [s["properties"]["title"] for s in sheets.get(spreadsheetId=sid, fields="sheets.properties.title").execute()["sheets"]]
        ranges = sheets.values().batchGet(spreadsheetId=sid, ranges=[f"'{t}'" for t in tabs]).execute()["valueRanges"]
        for tab, r in zip(tabs, ranges):
            rows = r.get("values", [])
            headers = rows[0] if rows else []
            for i, row in enumerate(rows[1:], start=2):  # sheet row numbers, so the model can fix the row later
                score = sum(w in " ".join(map(str, row)).lower() for w in words)
                if score:
                    fields = {(headers[j] if j < len(headers) and headers[j] else f"col{j + 1}"): v for j, v in enumerate(row)}
                    hits.append((score, fmt.block(f"[{LAYERS[layer][0]} > {tab}!A{i}]", fields)))
    hits.sort(key=lambda h: -h[0])
    return fmt.results("memories", [b for _, b in hits[:limit]],
                       f"best {limit} of {len(hits)}" if len(hits) > limit else None,
                       "Try other words, or memory_overview to see the tabs.")


def memory_overview():
    """Every memory spreadsheet with its file_id, tabs, columns and row counts."""
    sheets = google._service("sheets", "v4").spreadsheets()
    lines = [f"Agent folder '{FOLDER_NAME}' (file_id {ids()['folder']}):"]
    for layer, (name, _) in LAYERS.items():
        sid = ids()[layer]
        tabs = [s["properties"]["title"] for s in sheets.get(spreadsheetId=sid, fields="sheets.properties.title").execute()["sheets"]]
        ranges = sheets.values().batchGet(spreadsheetId=sid, ranges=[f"'{t}'!1:1" for t in tabs]).execute()["valueRanges"]
        lines.append(f"- {name} (file_id {sid})")
        lines += [f"    {t}: columns {', '.join((r.get('values') or [[]])[0]) or '(none)'}" for t, r in zip(tabs, ranges)]
    return "\n".join(lines)


# --- upkeep ---

def consolidate():
    """Move recent notes older than RECENT_DAYS into long-term Notes. Returns how many moved."""
    rows = _rows("recent", "Log")
    cutoff = (datetime.now() - timedelta(days=RECENT_DAYS)).isoformat()
    old = [r for r in rows[1:] if r and r[0] < cutoff]  # ISO timestamps sort as text
    if not old:
        return 0
    google.sheets_append(ids()["long_term"], "Notes", old)  # copy first, so a failure can't lose notes
    _replace("recent", "Log", rows[0], [r for r in rows[1:] if r and r[0] >= cutoff])
    return len(old)


def _replace(layer, tab, headers, rows):
    """Rewrite a whole tab: clear it, then write the header and rows."""
    values = google._service("sheets", "v4").spreadsheets().values()
    values.clear(spreadsheetId=ids()[layer], range=f"'{tab}'").execute()
    google._write(ids()[layer], f"'{tab}'!A1", [headers] + rows)
