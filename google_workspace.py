"""Google access for the agent's tools: read Gmail, Calendar, Drive and Docs; read and write Sheets.

Setup once: create an OAuth client of type "Desktop app" in Google Cloud Console, enable the Gmail, Calendar,
Drive, Docs and Sheets APIs, and save the client's JSON as credentials.json. The first Google tool call opens a
browser to sign in; the token is then cached in token.json.
"""

import base64
import json
import os
from datetime import datetime, timedelta, timezone

import google_format as fmt

CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS", "credentials.json")  # OAuth client secret from Google Cloud
TOKEN = os.getenv("GOOGLE_TOKEN", "token.json")  # cached sign-in; delete it to sign in again
SCOPES = [  # read-only except Sheets: the agent can't send email, edit events or docs, or delete files
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",  # create files and folders; edit only the ones it created
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets",  # read AND write any sheet the user can access
]

_services = {}  # (name, version) -> API client, built once per run
_creds = None  # the signed-in user's credentials, shared by every client


def _service(name, version):
    """Sign in (first call only) and return a Google API client."""
    global _creds
    if (name, version) not in _services:
        from google.auth.transport.requests import Request  # imported here so the agent starts without Google set up
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(TOKEN, SCOPES) if os.path.exists(TOKEN) else None
        if creds and not set(SCOPES) <= set(json.load(open(TOKEN)).get("scopes", [])):
            creds = None  # SCOPES changed since the user signed in: sign in again to grant the new access
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif not creds or not creds.valid:
            if not os.path.exists(CREDENTIALS):
                raise FileNotFoundError(f"Google is not set up ({CREDENTIALS} not found), so no Google tool will work "
                                        "until the user follows the Google setup steps in README.md")
            creds = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES).run_local_server(port=0)
        with open(TOKEN, "w") as f:
            f.write(creds.to_json())
        _creds = creds
        _services[(name, version)] = build(name, version, credentials=creds)
    return _services[(name, version)]


def access_token():
    """A fresh OAuth access token, for calls made outside the API clients (ffmpeg reading a video from Drive)."""
    from google.auth.transport.requests import Request
    _service("drive", "v3")  # signs in if needed
    if not _creds.valid:
        _creds.refresh(Request())
    return _creds.token


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
    seen = set()  # an event shared to several calendars comes back once per calendar
    events = [ec for ec in events if not (ec[0]["id"] in seen or seen.add(ec[0]["id"]))]
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
        "organizer": (lambda o: o.get("displayName") or o.get("email"))(e.get("organizer") or {}),
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


# --- Sheets: writing ---
# Values are written RAW: text that looks like a formula (e.g. "=IMPORTXML(...)" copied from an email) stays text,
# so content the agent reads can't turn into formulas that run in the user's spreadsheet.
#
# The agent may only write to sheets it created (its memory sheets and anything from sheets_create) and sheets the
# user lists in the allow list file. Both lists are local, per-user files, kept out of git.

CREATED_FILE = "agent_sheets.txt"  # ids of sheets the agent created with sheets_create; maintained automatically
MEMORY_FILE = "agent_memory.json"  # memory.py's folder and sheet ids
ALLOWLIST = os.getenv("SHEETS_ALLOWLIST", "sheets_allowlist.txt")  # the user's own sheets the agent may edit
ALLOWLIST_TEMPLATE = """# Google Sheets the agent may edit, besides the ones it created itself.
# One sheet per line: its id or its full URL. Lines starting with # are ignored.
# Example:
# https://docs.google.com/spreadsheets/d/1AbC.../edit
"""


def _ids_in(path):
    if not os.path.exists(path):
        return set()
    return {fmt.doc_id_from(line) for line in open(path).read().splitlines() if line.strip() and not line.startswith("#")}


def _writable():
    """Ids of every sheet the agent may write to, read fresh each time so edits to the allow list apply at once."""
    if not os.path.exists(ALLOWLIST):
        with open(ALLOWLIST, "w") as f:  # give the user a file to fill in
            f.write(ALLOWLIST_TEMPLATE)
    memory_ids = set(json.load(open(MEMORY_FILE)).values()) if os.path.exists(MEMORY_FILE) else set()
    return memory_ids | _ids_in(CREATED_FILE) | _ids_in(ALLOWLIST)


def _check_writable(spreadsheet_id):
    """Return the sheet id if the agent may write to it, else raise with a message the model can relay."""
    sheet_id = fmt.doc_id_from(spreadsheet_id)
    if sheet_id not in _writable():
        raise PermissionError(f"not allowed to edit sheet {sheet_id}: you didn't create it and the user hasn't added it "
                              f"to {ALLOWLIST}. Don't retry; tell the user, who can add the sheet's URL to that file.")
    return sheet_id


def sheets_create(title, tabs=None):
    """Create a spreadsheet. `tabs` maps tab name -> list of column headers."""
    tabs = tabs or {"Sheet1": []}
    sheet = _service("sheets", "v4").spreadsheets().create(body={
        "properties": {"title": title},
        "sheets": [{"properties": {"title": name}} for name in tabs],
    }, fields="spreadsheetId,spreadsheetUrl").execute()
    with open(CREATED_FILE, "a") as f:  # remember it as the agent's own, so it can edit it later
        f.write(sheet["spreadsheetId"] + "\n")
    for name, headers in tabs.items():
        if headers:
            _write(sheet["spreadsheetId"], f"'{name}'!A1", [headers])
    return fmt.block(f"[file_id {sheet['spreadsheetId']}] {title} (created)",
                     {"tabs": ", ".join(tabs), "link": sheet["spreadsheetUrl"]})


def sheets_add_tab(spreadsheet_id, tab, headers=None):
    """Add a tab to an existing spreadsheet, optionally with a header row."""
    sheet_id = _check_writable(spreadsheet_id)
    _service("sheets", "v4").spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": [{"addSheet": {"properties": {"title": tab}}}]}).execute()
    if headers:
        _write(sheet_id, f"'{tab}'!A1", [headers])
    return f"Added tab '{tab}'" + (f" with columns: {', '.join(headers)}" if headers else "") + "."


def sheets_append(spreadsheet_id, tab, rows):
    """Add rows after the last row of a tab."""
    result = _service("sheets", "v4").spreadsheets().values().append(
        spreadsheetId=_check_writable(spreadsheet_id), range=f"'{tab}'", valueInputOption="RAW",
        insertDataOption="INSERT_ROWS", body={"values": rows}).execute()
    return f"Appended {len(rows)} row(s) at {result['updates']['updatedRange']}."


def sheets_update(spreadsheet_id, range, rows):
    """Overwrite a range with new values, and report what was there before so a mistake can be undone."""
    sheet_id = _check_writable(spreadsheet_id)
    before = _service("sheets", "v4").spreadsheets().values().get(spreadsheetId=sheet_id, range=range).execute()
    updated = _write(sheet_id, range, rows)
    return (f"Updated {updated['updatedRange']} ({updated.get('updatedCells', 0)} cells).\n"
            f"Previous values (to undo, write these back):\n{json.dumps(before.get('values', []))}")


def _write(sheet_id, range, rows):
    return _service("sheets", "v4").spreadsheets().values().update(
        spreadsheetId=sheet_id, range=range, valueInputOption="RAW", body={"values": rows}).execute()
