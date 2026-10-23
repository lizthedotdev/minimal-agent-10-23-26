"""The tools the agent can call, and the code that runs them."""

import json

import google_workspace as google
import guard
import memory
import recordings
import web


def get_time():
    """Return the current time. A toy tool so there's something to call."""
    from datetime import datetime  # imported here to keep the top of the file tiny
    return datetime.now().isoformat(timespec="minutes")




UNTRUSTED = {  # tools whose results contain text other people wrote; screened for prompt injection first
    "gmail_search", "gmail_read", "calendar_events", "drive_search", "docs_read", "sheets_read",
    "web_search", "web_read", "search_memory", "recording_transcript",  # memory too: an injection saved once would come back every time
}

TOOLS = {  # name -> python function
    "get_time": get_time,
    "save_memory": memory.save_memory, "search_memory": memory.search_memory, "memory_overview": memory.memory_overview,
    "core_memory_set": memory.core_memory_set, "core_memory_remove": memory.core_memory_remove,
    "gmail_search": google.gmail_search, "gmail_read": google.gmail_read,
    "calendar_events": google.calendar_events, "drive_search": google.drive_search,
    "docs_read": google.docs_read, "sheets_read": google.sheets_read,
    "sheets_create": google.sheets_create, "sheets_add_tab": google.sheets_add_tab,
    "sheets_append": google.sheets_append, "sheets_update": google.sheets_update,
    "web_search": web.web_search, "web_read": web.web_read,
    "recording_transcript": recordings.recording_transcript, "clips_save": recordings.clips_save,
}

SCHEMAS = [  # what the model is told about each tool (OpenAI-style function schemas)
    {"type": "function", "function": {
        "name": "get_time",
        "description": "Get the current date and time.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "save_memory",
        "description": "Save a note to recent memory (the date is added for you). Write self-contained statements "
                       "with names, e.g. \"Sam agreed to send the Q4 budget by Friday Oct 30.\"",
        "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]},
    }},
    {"type": "function", "function": {
        "name": "search_memory",
        "description": "Search recent and long-term memory (every tab) by keywords. Use it before saying you don't "
                       "know something about the user, a person or a project. Results show each row's location "
                       "so you can correct it with sheets_update.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "core_memory_set",
        "description": "Add or replace a core fact, which is shown in every conversation. Only for things that "
                       "matter almost every time; core memory has a size limit.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "Short label, e.g. 'user_name' or 'company'."},
            "fact": {"type": "string"},
        }, "required": ["key", "fact"]},
    }},
    {"type": "function", "function": {
        "name": "core_memory_remove",
        "description": "Delete a core fact by its key.",
        "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
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
    {"type": "function", "function": {
        "name": "memory_overview",
        "description": "List your memory spreadsheets (core, recent, long-term) with their file_ids, tabs and "
                       "columns. Use the file_ids with sheets_append, sheets_update and sheets_add_tab.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "sheets_create",
        "description": "Create a new Google Sheet. Returns its file_id and link.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "tabs": {"type": "object", "description": "Tab name -> list of column headers, e.g. "
                     "{\"People\": [\"name\", \"email\", \"notes\"]}.",
                     "additionalProperties": {"type": "array", "items": {"type": "string"}}},
        }, "required": ["title"]},
    }},
    {"type": "function", "function": {
        "name": "sheets_add_tab",
        "description": "Add a tab to a Google Sheet you may edit (see sheets_update), optionally with column headers.",
        "parameters": {"type": "object", "properties": {
            "spreadsheet_id": {"type": "string"},
            "tab": {"type": "string"},
            "headers": {"type": "array", "items": {"type": "string"}},
        }, "required": ["spreadsheet_id", "tab"]},
    }},
    {"type": "function", "function": {
        "name": "sheets_append",
        "description": "Add rows to the end of a tab in a sheet you may edit (see sheets_update). Each row is a list of "
                       "cell values in column order.",
        "parameters": {"type": "object", "properties": {
            "spreadsheet_id": {"type": "string"},
            "tab": {"type": "string"},
            "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
        }, "required": ["spreadsheet_id", "tab", "rows"]},
    }},
    {"type": "function", "function": {
        "name": "sheets_update",
        "description": "Overwrite cells in a Google Sheet. You may only edit your memory sheets, sheets you "
                       "created with sheets_create, and sheets the user has put on their allow list; anything else is "
                       "refused. Read the range first. Returns the old values.",
        "parameters": {"type": "object", "properties": {
            "spreadsheet_id": {"type": "string"},
            "range": {"type": "string", "description": "A1 notation, e.g. 'People!A5:C5'."},
            "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
        }, "required": ["spreadsheet_id", "range", "rows"]},
    }},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the public web. Returns a short summary and sources with url and content. Cite the "
                       "urls you rely on. Use web_read to read a full page.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "description": "Default 5."},
            "topic": {"type": "string", "enum": ["general", "news", "finance"]},
            "time_range": {"type": "string", "enum": ["day", "week", "month", "year"]},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "web_read",
        "description": "Read the main text of a web page by url.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "recording_transcript",
        "description": "Read a class or meeting recording's timestamped transcript (from its Gemini notes) and chat log.",
        "parameters": {"type": "object", "properties": {"video_id": {"type": "string"}}, "required": ["video_id"]},
    }},
    {"type": "function", "function": {
        "name": "clips_save",
        "description": "Cut clips from a recording, upload them to the Clips folder and log them in the Clips sheet. "
                       "Pass every clip for a recording in one call. Max 180 seconds per clip.",
        "parameters": {"type": "object", "properties": {
            "video_id": {"type": "string"},
            "clips": {"type": "array", "items": {"type": "object", "properties": {
                "start": {"type": "string", "description": "HH:MM:SS from the transcript."},
                "end": {"type": "string", "description": "HH:MM:SS."},
                "category": {"type": "string", "enum": ["funny", "teaching", "student", "quote", "demo", "other"]},
                "title": {"type": "string", "description": "Short title, used in the file name."},
                "why": {"type": "string", "description": "One line on why it's worth clipping."},
                "quote": {"type": "string", "description": "The key line, verbatim from the transcript."},
            }, "required": ["start", "end", "category", "title", "why"]}},
        }, "required": ["video_id", "clips"]},
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
        if call.function.name in UNTRUSTED:
            result = guard.screen(str(result))
    except json.JSONDecodeError as e:
        result = f"Error: arguments were not valid JSON ({e})."
    except Exception as e:  # bad arguments, database errors, ...: tell the model instead of crashing
        result = f"Error: {type(e).__name__}: {e}"
    return {"role": "tool", "tool_call_id": call.id, "content": str(result)}  # id ties result to the call
