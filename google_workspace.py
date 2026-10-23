"""Read-only access to Gmail, Calendar, Drive, Docs and Sheets for the agent's Google tools.

Setup once: create an OAuth client of type "Desktop app" in Google Cloud Console, enable the Gmail, Calendar,
Drive, Docs and Sheets APIs, and save the client's JSON as credentials.json. The first Google tool call opens a
browser to sign in; the token is then cached in token.json.
"""

import base64
import os
from datetime import datetime, timedelta, timezone

import google_format as fmt

CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS", "credentials.json")  # OAuth client secret from Google Cloud
TOKEN = os.getenv("GOOGLE_TOKEN", "token.json")  # cached sign-in; delete it to sign in again
SCOPES = [  # read-only: the agent gathers context, it can't send, edit or delete anything
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

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


# --- Gmail ---

def gmail_search(query, max_results=10):
    """List messages matching a Gmail search query (same syntax as the Gmail search box)."""
    gmail = _service("gmail", "v1")
    found = gmail.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    blocks = []
    for m in found.get("messages", []):
        msg = gmail.users().messages().get(userId="me", id=m["id"], format="metadata",
                                           metadataHeaders=["From", "To", "Subject", "Date"]).execute()
        h = {x["name"]: x["value"] for x in msg["payload"]["headers"]}
        blocks.append(fmt.block(f"[message_id {m['id']}] {h.get('Subject') or '(no subject)'}", {
            "from": fmt.person(h.get("From")),
            "to": fmt.person(h.get("To")),
            "date": fmt.when(datetime.fromtimestamp(int(msg["internalDate"]) / 1000, timezone.utc).isoformat()),
            "flags": ", ".join(l.lower() for l in msg.get("labelIds", []) if l in ("UNREAD", "STARRED", "IMPORTANT")),
            "preview": fmt.unescape(msg.get("snippet")),
        }))
    more = "more exist; raise max_results or narrow the query" if found.get("nextPageToken") else None
    return fmt.results("emails", blocks, more, "Try fewer or different words, or Gmail operators such as "
                       "from:name, subject:word, newer_than:30d, has:attachment, in:anywhere.")


def _body(part, mime):
    """Collect the parts of a (possibly nested) email body with the given MIME type."""
    if part.get("mimeType") == mime and part["body"].get("data"):
        return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", "replace")
    return "".join(_body(p, mime) for p in part.get("parts", []))


def _attachments(part):
    names = [part["filename"]] if part.get("filename") else []
    return names + [n for p in part.get("parts", []) for n in _attachments(p)]


def gmail_read(message_id):
    """Return one message's headers and readable body, without the quoted thread below a reply."""
    msg = _service("gmail", "v1").users().messages().get(userId="me", id=message_id, format="full").execute()
    p = msg["payload"]
    h = {x["name"]: x["value"] for x in p["headers"]}
    body = _body(p, "text/plain") or fmt.html_to_text(_body(p, "text/html")) or fmt.unescape(msg.get("snippet"))
    head = fmt.block(f"[message_id {message_id}] {h.get('Subject') or '(no subject)'}", {
        "from": fmt.person(h.get("From")), "to": fmt.person(h.get("To")), "cc": fmt.person(h.get("Cc")),
        "date": fmt.when(datetime.fromtimestamp(int(msg["internalDate"]) / 1000, timezone.utc).isoformat()),
        "thread_id": msg.get("threadId"),
        "attachments": ", ".join(_attachments(p)),
    })
    return fmt.cut(f"{head}\n\n{fmt.tidy(fmt.strip_quoted(body))}")


# --- Calendar ---

def calendar_events(start=None, end=None, query=None, max_results=25):
    """List events across the user's calendars. Defaults to the next 7 days, or the past year and next 6 months when searching."""
    now = datetime.now(timezone.utc)
    if query and not (start or end):  # searching: look well back and ahead, since the model rarely knows the date
        start_dt, end_dt = now - timedelta(days=365), now + timedelta(days=180)
    else:
        start_dt = _parse(start) if start else now
        end_dt = _parse(end) if end else start_dt + timedelta(days=7)
    cal = _service("calendar", "v3")
    calendars = [c for c in cal.calendarList().list().execute().get("items", []) if c.get("selected") or c.get("primary")]
    events = []
    for c in calendars:  # the user's own calendar plus any shared ones they show in Google Calendar
        items = cal.events().list(calendarId=c["id"], timeMin=start_dt.isoformat(), timeMax=end_dt.isoformat(), q=query,
                                  maxResults=max_results, singleEvents=True, orderBy="startTime").execute().get("items", [])
        events += [(e, c.get("summaryOverride") or c.get("summary", c["id"])) for e in items if e.get("status") != "cancelled"]
    events.sort(key=lambda ec: ec[0]["start"].get("dateTime") or ec[0]["start"].get("date"))
    shown = events[:max_results]
    blocks = [_event(e, name if len(calendars) > 1 else None) for e, name in shown]
    window = f"{start_dt.astimezone():%b %d %Y} to {end_dt.astimezone():%b %d %Y}"
    more = f"showing the first {max_results} of {len(events)}" if len(events) > max_results else None
    return fmt.results(f"events, {window}", blocks, more, "Try a wider date range, or one distinctive word "
                       "(a person's name or email, or a word from the title) as the query.")


def _event(e, calendar_name):
    s, en = e["start"], e["end"]
    people = [f"{a.get('displayName') or a['email']} ({a.get('responseStatus', 'unknown').replace('needsAction', 'no reply')})"
              for a in e.get("attendees", []) if not a.get("resource")]
    return fmt.block(f"[event_id {e['id']}] {e.get('summary', '(no title)')}", {
        "when": fmt.when(s.get("dateTime") or s.get("date")),
        "duration": fmt.duration(s["dateTime"], en["dateTime"]) if "dateTime" in s else None,
        "calendar": calendar_name,
        "where": e.get("location"),
        "video": e.get("hangoutLink"),
        "organizer": (e.get("organizer") or {}).get("email"),
        "attendees": ", ".join(people[:15]) + (f", +{len(people) - 15} more" if len(people) > 15 else ""),
        "description": fmt.cut(fmt.html_to_text(e.get("description", "")), 500),
    })


def _parse(value):
    """Accept '2026-10-23' or a full timestamp; no timezone given means the user's local time."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.astimezone()


# --- Drive, Docs, Sheets ---

KINDS = {  # what the model can pass as `kind` -> Drive MIME type
    "doc": "application/vnd.google-apps.document", "sheet": "application/vnd.google-apps.spreadsheet",
    "slides": "application/vnd.google-apps.presentation", "folder": "application/vnd.google-apps.folder",
    "pdf": "application/pdf",
}


def drive_search(query, kind=None, max_results=10):
    """Find Drive files by words in their name or contents, all words first, then any word."""
    words = [w for w in query.split() if w]
    quote = lambda w: "'" + w.replace("\\", "\\\\").replace("'", "\\'") + "'"
    match = lambda w: f"(name contains {quote(w)} or fullText contains {quote(w)})"
    base = "trashed = false" + (f" and mimeType = '{KINDS[kind]}'" if kind in KINDS else "")
    files, note = [], None
    for joiner in (" and ", " or "):  # every word must match; if nothing does, any word may
        q = base + (" and (" + joiner.join(match(w) for w in words) + ")" if words else "")
        files = _service("drive", "v3").files().list(
            q=q, pageSize=max_results, supportsAllDrives=True, includeItemsFromAllDrives=True, corpora="allDrives",
            fields="files(id, name, mimeType, modifiedTime, webViewLink, owners(displayName))").execute().get("files", [])
        if files or len(words) < 2:
            break
        note = "no file matched every word, so these match any word"
    blocks = [fmt.block(f"[file_id {f['id']}] {f['name']}", {
        "type": next((k for k, v in KINDS.items() if v == f["mimeType"]), f["mimeType"]),
        "modified": fmt.when(f["modifiedTime"]),
        "owner": ", ".join(o["displayName"] for o in f.get("owners", [])),
        "link": f.get("webViewLink"),
    }) for f in files]
    hint = "Read docs with docs_read and sheets with sheets_read, passing the file_id."
    return fmt.results("files", blocks, note, "Try fewer or different words, or drop the kind filter.") + (
        f"\n\n{hint}" if blocks else "")


def docs_read(document_id):
    """Return a Google Doc as markdown (headings, lists, tables). Accepts an id or a URL."""
    doc = _service("docs", "v1").documents().get(documentId=fmt.doc_id_from(document_id)).execute()
    return fmt.cut(f"# {doc['title']}\n\n{fmt.doc_to_markdown(doc)}")


def sheets_read(spreadsheet_id, range=None):
    """Return cells as a markdown table. With no range, list the tabs and read the first one. Accepts an id or a URL."""
    sheet_id = fmt.doc_id_from(spreadsheet_id)
    sheets = _service("sheets", "v4").spreadsheets()
    meta = sheets.get(spreadsheetId=sheet_id, fields="properties.title,sheets.properties(title,gridProperties)").execute()
    tabs = [s["properties"] for s in meta["sheets"]]
    tab_list = ", ".join(f"{t['title']} ({t['gridProperties']['rowCount']} rows)" for t in tabs)
    rng = range or f"'{tabs[0]['title']}'"
    rows = sheets.values().get(spreadsheetId=sheet_id, range=rng).execute().get("values", [])
    return fmt.cut(f"# {meta['properties']['title']}\ntabs: {tab_list}\nrange: {rng}\n\n{fmt.md_table(rows)}")
