"""The tools the agent can call, and the code that runs them."""

import json

import google_workspace as google
import memory


def get_time():
    """Return the current time. A toy tool so there's something to call."""
    from datetime import datetime  # imported here to keep the top of the file tiny
    return datetime.now().isoformat(timespec="minutes")


def save_memory(content):
    """Save a note to long-term memory (memory.py, a SQLite file)."""
    return f"Saved memory #{memory.save(content)}."


def search_memory(query):
    """Recall as a tool: the model decides when to look something up."""
    rows = memory.search(query)
    if not rows:
        return "No matching memories."
    return "\n".join(f"#{id} ({created_at}): {content}" for id, content, created_at in rows)


TOOLS = {  # name -> python function
    "get_time": get_time, "save_memory": save_memory, "search_memory": search_memory,
    "gmail_search": google.gmail_search, "gmail_read": google.gmail_read,
    "calendar_events": google.calendar_events, "drive_search": google.drive_search,
    "docs_read": google.docs_read, "sheets_read": google.sheets_read,
}

SCHEMAS = [  # what the model is told about each tool (OpenAI-style function schemas)
    {"type": "function", "function": {
        "name": "get_time",
        "description": "Get the current date and time.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "save_memory",
        "description": "Save a fact or note to long-term memory so it can be recalled in later conversations. "
                       "Save self-contained statements, e.g. \"The user's name is Sam.\"",
        "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
    }},
    {"type": "function", "function": {
        "name": "search_memory",
        "description": "Search long-term memory by keywords. Use it before answering questions about the user "
                       "or anything that may have come up in a past conversation.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "gmail_search",
        "description": "Search the user's Gmail. Returns message ids, date, sender, subject and a snippet. "
                       "Uses Gmail search syntax, e.g. 'from:sam newer_than:7d' or 'subject:invoice is:unread'.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "description": "Default 10."},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "gmail_read",
        "description": "Read one email by the message_id from gmail_search. Quoted earlier replies are removed.",
        "parameters": {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]},
    }},
    {"type": "function", "function": {
        "name": "calendar_events",
        "description": "List or search events across the user's Google Calendars. With no dates, shows the next 7 days; "
                       "with a query and no dates, searches the past year and next 6 months. The query matches "
                       "title, description, location and attendee names/emails, so search by a person or a key word.",
        "parameters": {"type": "object", "properties": {
            "start": {"type": "string", "description": "ISO date or datetime, e.g. 2026-10-23 or 2026-10-23T09:00."},
            "end": {"type": "string", "description": "ISO date or datetime."},
            "query": {"type": "string", "description": "Only events whose text contains these words, e.g. 'Sam'."},
            "max_results": {"type": "integer", "description": "Default 25."},
        }},
    }},
    {"type": "function", "function": {
        "name": "drive_search",
        "description": "Find Google Drive files (including shared drives) by words in their name or contents. "
                       "Use a few distinctive words, not a sentence. Returns file ids to pass to docs_read or sheets_read.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "kind": {"type": "string", "enum": ["doc", "sheet", "slides", "folder", "pdf"]},
            "max_results": {"type": "integer", "description": "Default 10."},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "docs_read",
        "description": "Read a Google Doc as markdown. Pass the file_id from drive_search, or the doc's URL.",
        "parameters": {"type": "object", "properties": {"document_id": {"type": "string"}}, "required": ["document_id"]},
    }},
    {"type": "function", "function": {
        "name": "sheets_read",
        "description": "Read a Google Sheet as a markdown table. Pass the file_id from drive_search, or the sheet's URL. "
                       "Without a range, lists the tabs and reads the first one.",
        "parameters": {"type": "object", "properties": {
            "spreadsheet_id": {"type": "string"},
            "range": {"type": "string", "description": "A1 notation, e.g. 'Sheet1!A1:D50'."},
        }, "required": ["spreadsheet_id"]},
    }},
]


def run_tool_call(call):
    """Run one tool call from the model and return the message that carries its result back."""
    func = TOOLS.get(call.function.name)  # look up the function by name
    try:
        if func is None:  # models sometimes invent tool names
            raise LookupError(f"no tool named {call.function.name!r}")
        args = json.loads(call.function.arguments or "{}")  # the model sends arguments as a JSON string
        result = func(**args)
    except json.JSONDecodeError as e:
        result = f"Error: arguments were not valid JSON ({e})."
    except Exception as e:  # bad arguments, database errors, ...: tell the model instead of crashing
        result = f"Error: {type(e).__name__}: {e}"
    return {"role": "tool", "tool_call_id": call.id, "content": str(result)}  # id ties result to the call
