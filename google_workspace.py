"""Read-only access to Gmail, Calendar, Drive, Docs and Sheets for the agent's Google tools.

Setup once: create an OAuth client of type "Desktop app" in Google Cloud Console, enable the Gmail, Calendar,
Drive, Docs and Sheets APIs, and save the client's JSON as credentials.json. The first Google tool call opens a
browser to sign in; the token is then cached in token.json.
"""

import base64
import os
from datetime import datetime, timedelta, timezone

CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS", "credentials.json")  # OAuth client secret from Google Cloud
TOKEN = os.getenv("GOOGLE_TOKEN", "token.json")  # cached sign-in; delete it to sign in again
SCOPES = [  # read-only: the agent gathers context, it can't send, edit or delete anything
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]
MAX_CHARS = 20_000  # long emails and docs get cut so they don't flood the model's context

_services = {}  # (name, version) -> API client, built once per run


def _service(name, version):
    """Sign in (first call only) and return a Google API client."""
    if (name, version) not in _services:
        from google.auth.transport.requests import Request  # imported here so the agent starts without Google set up
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(TOKEN, SCOPES) if os.path.exists(TOKEN) else None
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif not creds or not creds.valid:
            if not os.path.exists(CREDENTIALS):
                raise FileNotFoundError(f"Google is not set up ({CREDENTIALS} not found), so no Google tool will work "
                                        "until the user follows the Google setup steps in README.md")
            creds = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES).run_local_server(port=0)
        with open(TOKEN, "w") as f:
            f.write(creds.to_json())
        _services[(name, version)] = build(name, version, credentials=creds)
    return _services[(name, version)]


def _cut(text):
    """Trim text to MAX_CHARS, saying so if anything was dropped."""
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + f"\n[... cut, {len(text) - MAX_CHARS} more chars]"


# --- Gmail ---

def gmail_search(query, max_results=10):
    """List messages matching a Gmail search query (same syntax as the Gmail search box)."""
    gmail = _service("gmail", "v1")
    ids = gmail.users().messages().list(userId="me", q=query, maxResults=max_results).execute().get("messages", [])
    if not ids:
        return "No messages found."
    lines = []
    for m in ids:
        msg = gmail.users().messages().get(userId="me", id=m["id"], format="metadata",
                                           metadataHeaders=["From", "Subject", "Date"]).execute()
        h = {x["name"]: x["value"] for x in msg["payload"]["headers"]}
        lines.append(f"[{m['id']}] {h.get('Date', '')} | {h.get('From', '')} | {h.get('Subject', '')}\n  {msg['snippet']}")
    return "\n".join(lines)


def _plain_text(part):
    """Collect the text/plain parts of a (possibly nested) email body."""
    if part.get("mimeType") == "text/plain" and part["body"].get("data"):
        return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", "replace")
    return "".join(_plain_text(p) for p in part.get("parts", []))


def gmail_read(message_id):
    """Return one message's headers and plain-text body."""
    msg = _service("gmail", "v1").users().messages().get(userId="me", id=message_id, format="full").execute()
    h = {x["name"]: x["value"] for x in msg["payload"]["headers"]}
    head = "\n".join(f"{k}: {h[k]}" for k in ("From", "To", "Cc", "Date", "Subject") if k in h)
    body = _plain_text(msg["payload"]) or msg.get("snippet", "")  # HTML-only emails fall back to the snippet
    return _cut(f"{head}\n\n{body}")


# --- Calendar ---

def calendar_events(start=None, end=None, query=None, max_results=25):
    """List events on the primary calendar between two ISO dates/times (default: the next 7 days)."""
    now = datetime.now(timezone.utc)
    start = _iso(start) if start else now.isoformat()
    end = _iso(end) if end else (now + timedelta(days=7)).isoformat()
    events = _service("calendar", "v3").events().list(
        calendarId="primary", timeMin=start, timeMax=end, q=query, maxResults=max_results,
        singleEvents=True, orderBy="startTime").execute().get("items", [])
    if not events:
        return "No events found."
    lines = []
    for e in events:
        when = e["start"].get("dateTime", e["start"].get("date"))  # all-day events only have a date
        who = ", ".join(a["email"] for a in e.get("attendees", [])[:10])
        lines.append(f"{when} | {e.get('summary', '(no title)')}" + (f" | with {who}" if who else ""))
    return "\n".join(lines)


def _iso(value):
    """Accept '2026-10-23' or a full timestamp; the Calendar API wants RFC3339 with a timezone."""
    dt = datetime.fromisoformat(value)
    return (dt if dt.tzinfo else dt.astimezone()).isoformat()  # no timezone given: assume local time


# --- Drive, Docs, Sheets ---

def drive_search(query, max_results=10):
    """Find Drive files whose name or contents contain the given words."""
    q = "fullText contains '{}' and trashed = false".format(query.replace("\\", "\\\\").replace("'", "\\'"))
    files = _service("drive", "v3").files().list(
        q=q, pageSize=max_results, fields="files(id, name, mimeType, modifiedTime)").execute().get("files", [])
    if not files:
        return "No files found."
    kinds = {"application/vnd.google-apps.document": "doc", "application/vnd.google-apps.spreadsheet": "sheet"}
    return "\n".join(f"[{f['id']}] {kinds.get(f['mimeType'], f['mimeType'])} | {f['name']} | modified {f['modifiedTime']}"
                     for f in files)


def docs_read(document_id):
    """Return a Google Doc's title and plain text."""
    doc = _service("docs", "v1").documents().get(documentId=document_id).execute()
    text = "".join(run["textRun"]["content"]
                   for block in doc["body"]["content"] if "paragraph" in block
                   for run in block["paragraph"]["elements"] if "textRun" in run)
    return _cut(f"# {doc['title']}\n\n{text}")


def sheets_read(spreadsheet_id, range=None):
    """Return cell values from a sheet. With no range, list the tabs and read the first one."""
    sheets = _service("sheets", "v4").spreadsheets()
    meta = sheets.get(spreadsheetId=spreadsheet_id, fields="properties.title,sheets.properties.title").execute()
    tabs = [s["properties"]["title"] for s in meta["sheets"]]
    rows = sheets.values().get(spreadsheetId=spreadsheet_id, range=range or f"'{tabs[0]}'").execute().get("values", [])
    table = "\n".join("\t".join(map(str, row)) for row in rows) or "(empty)"
    return _cut(f"# {meta['properties']['title']} (tabs: {', '.join(tabs)})\n\n{table}")
