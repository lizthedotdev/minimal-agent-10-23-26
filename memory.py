"""Long-term memory: the agent's own Google spreadsheet. It keeps notes on a "Notes" tab and can add any tabs it likes.

The spreadsheet ("Agent Memory") is created on first use and its id is saved in agent_sheet.txt; set AGENT_SHEET_ID to
use an existing one instead. The user can open it in the browser to read or correct what the agent remembers.
"""

import os
import re
from datetime import datetime

import google_format as fmt
import google_workspace as google

ID_FILE = "agent_sheet.txt"
NOTES_TAB, NOTES_HEADERS = "Notes", ["saved_at", "note"]


def sheet_id():
    """The agent's memory spreadsheet, created the first time it's needed."""
    if os.getenv("AGENT_SHEET_ID"):
        return os.environ["AGENT_SHEET_ID"]
    if os.path.exists(ID_FILE):
        return open(ID_FILE).read().strip()
    created = google._service("sheets", "v4").spreadsheets().create(body={
        "properties": {"title": "Agent Memory"}, "sheets": [{"properties": {"title": NOTES_TAB}}],
    }, fields="spreadsheetId").execute()["spreadsheetId"]
    google._write(created, f"'{NOTES_TAB}'!A1", [NOTES_HEADERS])
    with open(ID_FILE, "w") as f:
        f.write(created)
    return created


def save(note):
    """Append a note to the Notes tab and return where it landed."""
    stamp = datetime.now().isoformat(timespec="seconds")
    return google.sheets_append(sheet_id(), NOTES_TAB, [[stamp, note]])


def _tabs():
    """{tab name: rows (first row = headers)} for every tab in the memory spreadsheet."""
    sheets = google._service("sheets", "v4").spreadsheets()
    sid = sheet_id()
    names = [s["properties"]["title"] for s in sheets.get(spreadsheetId=sid, fields="sheets.properties.title").execute()["sheets"]]
    ranges = sheets.values().batchGet(spreadsheetId=sid, ranges=[f"'{n}'" for n in names]).execute()["valueRanges"]
    return {n: r.get("values", []) for n, r in zip(names, ranges)}


def overview():
    """The memory spreadsheet's tabs with their columns and sizes, so the model knows where things go."""
    tabs = _tabs()
    lines = [f"Memory spreadsheet file_id {sheet_id()}. Tabs:"]
    lines += [f"- {name}: {len(rows) - 1 if rows else 0} rows; columns: {', '.join(rows[0]) if rows else '(none)'}"
              for name, rows in tabs.items()]
    return "\n".join(lines)


def search(query, limit=10):
    """Rows from every tab that contain any word of `query`, rows with more matching words first."""
    words = {w.lower() for w in re.findall(r"\w+", query) if len(w) > 1}
    hits = []
    for name, rows in _tabs().items():
        headers = rows[0] if rows else []
        for i, row in enumerate(rows[1:], start=2):  # sheet row numbers, so the model can update the row in place
            text = " ".join(map(str, row)).lower()
            score = sum(w in text for w in words)
            if score:
                fields = {(headers[j] if j < len(headers) and headers[j] else f"col{j + 1}"): v for j, v in enumerate(row)}
                hits.append((score, fmt.block(f"[{name}!A{i}]", fields)))
    hits.sort(key=lambda h: -h[0])  # stable sort: equal scores keep sheet order
    return fmt.results("memories", [b for _, b in hits[:limit]],
                       f"best {limit} of {len(hits)}" if len(hits) > limit else None,
                       "Try other words, or call memory_overview to see what tabs exist.")
