"""The tools the agent can call, and the code that runs them."""

import json

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


TOOLS = {"get_time": get_time, "save_memory": save_memory, "search_memory": search_memory}  # name -> python function

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
