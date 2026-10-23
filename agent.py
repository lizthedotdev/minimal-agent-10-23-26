# /// script
# dependencies = ["openai", "google-api-python-client", "google-auth-oauthlib", "faiss-cpu", "numpy"]
# ///
"""The world's most basic agent: read input, call the model, run any tools, repeat."""

import os
import threading
import time

from openai import OpenAI, OpenAIError

import memory
import recordings
from prompt import build_messages
from tools import SCHEMAS, run_tool_call

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",  # OpenRouter speaks the OpenAI API
    api_key=os.environ["OPENROUTER_API_KEY"],  # get one at https://openrouter.ai/keys
)
MODEL = os.getenv("MODEL", "qwen/qwen3.8-27b")  # any model slug from https://openrouter.ai/models
MAX_TURNS = 10  # most model calls per user message, so a model stuck calling tools can't run forever


def respond(history, user_input, max_turns=MAX_TURNS):
    """Answer one user message, looping while the model asks for tools."""
    messages = build_messages(history, user_input)  # prompt.py decides what the model sees
    for _ in range(max_turns):
        completion = client.chat.completions.create(model=MODEL, messages=messages, tools=SCHEMAS)
        if not completion.choices:  # OpenRouter can report provider errors inside a 200 response
            raise RuntimeError(f"model returned no reply: {getattr(completion, 'error', completion)}")
        reply = completion.choices[0].message
        messages.append(reply)  # keep the model's turn, including any tool requests
        if not reply.tool_calls:  # no tools requested: this is the final answer
            return (reply.content or "").strip()  # some models pad replies with blank lines
        for call in reply.tool_calls:  # run each tool the model asked for
            messages.append(run_tool_call(call))  # tools.py does the work; result goes back to the model
    return f"(Stopped after {max_turns} model calls without a final answer.)"


user_busy = threading.Event()  # set while the agent is answering you, so background work waits its turn


def clip_worker():
    """Background thread: take new recordings off the queue and have the agent clip them, one at a time."""
    while True:
        rec = recordings.events.get()
        while user_busy.is_set():  # let your current question finish first
            time.sleep(1)
        print(f"\n[recordings] new recording: {rec['name']}. Clipping in the background...", flush=True)
        try:
            summary = respond([], recordings.task_prompt(rec), max_turns=15)  # its own conversation, not yours
            recordings.mark(rec["video_id"], "done")
            print(f"\n[recordings] {rec['name']}:\n{summary}\nyou> ", end="", flush=True)
        except Exception as e:
            recordings.mark(rec["video_id"], f"failed: {type(e).__name__}: {e}"[:300])
            print(f"\n[recordings] failed on {rec['name']}: {type(e).__name__}: {e}\nyou> ", end="", flush=True)


def main():
    """Chat loop in the terminal."""
    history = []  # short-term memory: just the transcript
    try:
        moved = memory.consolidate()  # recent notes older than RECENT_DAYS move to long-term memory
        if moved:
            print(f"(moved {moved} old note(s) from recent to long-term memory)")
    except Exception as e:
        print(f"(memory upkeep skipped: {type(e).__name__}: {e})")
    if os.getenv("RECORDINGS_WATCH", "on") != "off":
        recordings.watch()  # checks the Meet recordings folder every few minutes
        threading.Thread(target=clip_worker, daemon=True, name="clip-worker").start()
    while True:
        user_input = input("you> ")
        if user_input in ("", "quit", "exit"):
            break
        if user_input.startswith("/clip "):  # clip an older recording by name or id
            name = recordings.queue_by_name(user_input[6:].strip())
            print(f"(queued: {name})" if name else "(no recording matches that)")
            continue
        user_busy.set()
        try:
            answer = respond(history, user_input)
        except (OpenAIError, RuntimeError) as e:  # network, auth, rate limit, bad model ID, provider error, ...
            print(f"error> {type(e).__name__}: {e}")
            continue  # leave history unchanged so you can just try again
        finally:
            user_busy.clear()
        print("agent>", answer)
        history += [{"role": "user", "content": user_input}, {"role": "assistant", "content": answer}]


if __name__ == "__main__":
    main()
