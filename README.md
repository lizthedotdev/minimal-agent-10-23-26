# Minimal Agent

This is about the simplest agent there is, in three small files. It reads what you type, sends it to a model, runs any tools the model asks for, and repeats until the model gives an answer.

Today you'll give it **memory**. The code has four places marked `MEMORY HOOK`, and those are where your work goes.

## Setup

You need two things:

1. **uv**, the Python tool we use to run the script. If `uv --version` doesn't work, install it:
   ```bash
   # macOS / Linux
   curl -LsSf https://astral.sh/uv/install.sh | sh
   # Windows (PowerShell)
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
2. **An OpenRouter API key.** Sign in at [openrouter.ai](https://openrouter.ai), then go to [openrouter.ai/keys](https://openrouter.ai/keys) and create a key. It starts with `sk-or-`.

Get the code:

```bash
git clone https://github.com/lizTheDeveloper/minimal-agent-10-23-26.git
cd minimal-agent-10-23-26
```

## Running it

Put your key in an environment variable, then run the agent:

```bash
# macOS / Linux
export OPENROUTER_API_KEY=sk-or-...
uv run agent.py
```

```powershell
# Windows (PowerShell)
$env:OPENROUTER_API_KEY = "sk-or-..."
uv run agent.py
```

uv installs the `openai` package the first time you run it, so you don't need `pip install` or a virtual environment.

You'll see a `you>` prompt. Try these:

```
you> hi, my name is Sam
you> what's my name?
you> what time is it?
```

To quit, press Enter on an empty line or type `quit`.

Asking for the time makes the model call the `get_time` tool. If you ask what your name is, it remembers, but only until you quit. Restart the agent and ask again: it has forgotten. You'll fix that today.

### Using a different model

The default model is `qwen/qwen3.8-27b`. To try another one, pick any model ID from [openrouter.ai/models](https://openrouter.ai/models) and set `MODEL`:

```bash
MODEL=openai/gpt-4o-mini uv run agent.py
```

The model has to support tool calling. If you get errors about tools, try a different model.

## Web search

The `web_search` and `web_read` tools use [Tavily](https://tavily.com). Create a key there and set it the same way as the OpenRouter key:

```bash
export TAVILY_API_KEY=tvly-...
```

Without it, the agent still runs; the web tools just report that they aren't set up. SafeSearch is always on, so adult and unsafe results are filtered out.

## Connecting Google (Gmail, Calendar, Drive, Docs, Sheets)

The agent can read your Gmail, Calendar, Drive and Docs, and read **and write** your Sheets. Its long-term memory is also a Google Sheet, so memory needs this setup too. To set it up once:

1. In [console.cloud.google.com](https://console.cloud.google.com) create (or pick) a project.
2. **APIs & Services → Library**: enable Gmail API, Google Calendar API, Google Drive API, Google Docs API and Google Sheets API.
3. **Google Auth Platform** (formerly "OAuth consent screen") → **Get started**: fill in an app name and support email. Choose Audience **Internal** if you're on Google Workspace (no review, and tokens don't expire). Otherwise choose External and add yourself under **Test users**. In Testing mode the sign-in expires every 7 days; delete `token.json` and sign in again.
4. **Google Auth Platform → Clients → Create client**, Application type **Desktop app** → **Create** → **Download JSON**. Save it in the project folder as `credentials.json` (or point `GOOGLE_CREDENTIALS` at it).
5. Run `uv run agent.py` and ask something like "what's on my calendar this week?". A browser opens to sign in and grant access. The token is saved to `token.json` (or `GOOGLE_TOKEN`). Delete it to switch accounts.

Access is read-only except for Sheets. Google lets the agent edit any sheet you can edit (see the scopes in `google_workspace.py`), but the code only lets it write to:

- its memory sheets,
- sheets it created with `sheets_create` (recorded in `agent_sheets.txt`),
- sheets you list in `sheets_allowlist.txt`, one id or URL per line. The file is created with instructions the first time the agent checks it; set `SHEETS_ALLOWLIST` to keep it elsewhere.

Any other write is refused, and the agent is told to ask you. Both files are per-user and in `.gitignore`. It writes values as plain text, so a formula copied from an email can't run, and `sheets_update` returns the old values so a bad edit can be undone. If the scopes change, the agent asks you to sign in again. `credentials.json` and `token.json` are secrets, so they're in `.gitignore`.

**Memory.** On first use the agent creates a folder called **Agent** in your Drive (set `AGENT_FOLDER` to rename it) with three spreadsheets, one per layer of memory:

| Spreadsheet | What goes in it | In the system prompt? |
|---|---|---|
| **Core Memory** | A few key facts (who you are, how you like to work), one row per `key`. Capped at 4,000 characters. | Always |
| **Recent Memory** | A dated log of notes from `save_memory`. Notes older than 14 days move to Long-term when the agent starts. | The latest 15 |
| **Long-term Memory** | Older notes (**Notes** tab) plus structured records: **People**, **Projects**, and any tabs the agent adds. | No; the agent uses `search_memory` |

You can open any of them in the browser to see or correct what the agent remembers; edits show up on the next message. Their ids are cached in `agent_memory.json`. If you delete that file, the agent finds the folder again by name. The agent creates these files with the `drive.file` scope, which lets it create and edit only files it made itself.

## Clipping class recordings

While the agent runs, a background watcher checks your Google Meet recordings folder every 5 minutes. When a new recording appears and its "Notes by Gemini" doc (which holds the timestamped transcript) is ready, the agent clips it in the background, in its own conversation, waiting while you're mid-question. It:

1. reads the transcript and chat log (`recording_transcript`),
2. picks moments, top priority first: profound mini-lectures and tight explanations, then funny moments, student breakthroughs, quotable lines and demos. Every transcript minute is labelled with your speaking pace, and the agent favors fast stretches (your published videos run at a median 163 wpm versus 144 wpm when leading class). Set `USER_SPEAKER` if your name in Meet transcripts isn't "Liz Howard".
   and checks each one against your published videos (`public_videos_search`) to prefer points you haven't made online yet,
3. cuts each clip with ffmpeg straight from Drive, reading only the part it needs instead of downloading the whole recording (`clips_save`),
4. uploads the clips to **Agent/Clips** in your Drive and logs each one in the **Clips** sheet (times, category, why, quote, link, and `public_match`: "new", or the closest public video if you've already said it; `pace_wpm`).

**What you've already said publicly.** The transcripts of your published videos (the `.txt` files in `PUBLIC_VIDEOS_FOLDER`) are split into short passages, embedded with `qwen/qwen3-embedding-8b` (set `EMBED_MODEL` to change it) and kept in a local FAISS index (`public_videos.faiss` + `public_videos.db`, per user, gitignored). The search is semantic, so it finds the same point in different words. Similarity 0.65 or more means you've very likely said it; 0.58–0.65 is related. Those cut-offs come from testing on your archive, where paraphrased repeats scored 0.66–0.76, class-only points 0.52–0.55 and unrelated text under 0.5. The index updates on every watcher check, and embedding the full archive costs well under a cent. You can also just ask the agent "have I talked about X publicly?".

You need `ffmpeg` installed (`brew install ffmpeg` or `apt install ffmpeg`).

- **Older recordings:** the first time it runs, the agent marks everything already in the folder as seen, so it doesn't clip your whole archive. To clip an older one, type `/clip <part of its name>`, e.g. `/clip Data Activism`.
- **Settings:** `RECORDINGS_FOLDER` (the folder id), `RECORDINGS_POLL_SECONDS` (default 300), and `RECORDINGS_WATCH=off` to turn the watcher off. What's been handled is kept in `recordings_state.json` (per user, gitignored).
- Transcripts are people talking, so they're screened for prompt injection like email.

## How it works

| File | What it does |
|---|---|
| `agent.py` | The main loop. It reads your input, calls the model, runs any tools the model asks for, and prints the answer. |
| `prompt.py` | Builds the list of messages the model sees: the system prompt (with core memory and recent notes), the conversation so far, and your new message. |
| `tools.py` | The tools the model can call: `get_time`; memory (`save_memory`, `search_memory`, `core_memory_set`, `core_memory_remove`, `memory_overview`); Google reads (`gmail_search`, `gmail_read`, `calendar_events`, `drive_search`, `docs_read`, `sheets_read`); Sheets writes (`sheets_create`, `sheets_add_tab`, `sheets_append`, `sheets_update`); and the web (`web_search`, `web_read`), plus the descriptions the model reads to decide when to use them. Tool errors go back to the model as text instead of crashing the agent. |
| `memory.py` | The agent's three layers of memory (core, recent, long-term), each a Google Sheet in its Drive folder. Puts core memory and recent notes into the prompt, and moves old notes to long-term. |
| `google_workspace.py` | Read access to Gmail, Calendar, Drive and Docs; read and write access to Sheets. Handles the Google sign-in and saves the token to `token.json`. |
| `web.py` | Web search (`web_search`) and page reading (`web_read`) through [Tavily](https://tavily.com). Set `TAVILY_API_KEY` to turn them on. |
| `guard.py` | Screens text from email, the web, Docs, Sheets, Calendar and memory for prompt injection before the model sees it. Each chunk is classified by `openai/gpt-oss-safeguard-20b` (set `GUARD_MODEL` to change it); flagged chunks become `[removed: possible prompt injection]`, and chunks that can't be checked are withheld. |
| `public_videos.py` | Semantic search (FAISS) over the transcripts of your published videos, so the agent knows what you've already said in public. |
| `recordings.py` | Watches the Meet recordings folder, queues new recordings, and provides the `recording_transcript` and `clips_save` tools. |
| `google_format.py` | Turns raw Google API responses into compact text for the model: one labelled block per result with its id, local times with "tomorrow"/"3 days ago", HTML and quoted replies stripped, docs and sheets as markdown, and a note whenever something is cut or nothing is found. |

One turn of the conversation goes like this:

1. You type a message.
2. `prompt.py` builds the messages: the system prompt, then the history, then your new message.
3. The model replies. If the reply asks for tools, `tools.py` runs them, their results go back to the model, and the model is called again, up to `MAX_TURNS` (10) times per message.
4. When the model replies without asking for a tool, that reply is the answer and gets printed.
5. Your message and the answer are added to `history`.

`history` is the agent's **short-term memory**. It's a Python list that lasts only while the program runs.

## Your task: add long-term memory

Search the code for `MEMORY HOOK`. Each hook is a different place memory can go:

| Hook | File | Idea |
|---|---|---|
| **1: recall into the system prompt** | `prompt.py` | Before each turn, look up relevant notes and add them to the system prompt. |
| **2: recall as a message** | `prompt.py` | Same as hook 1, but add the notes as a separate message just before the user's message. |
| **3: recall as a tool** | `tools.py` | Let the model decide when to search, by implementing `search_memory`. |
| **4: write** | `agent.py` | After each exchange, save it somewhere that lasts, such as a file or a vector store. |

Recall (hooks 1, 2 and 3) only works once there's something saved, so do hook 4 first.

### A suggested path

1. **Write to a file.** At hook 4, append each exchange to a JSON or text file.
2. **Recall everything.** At hook 1, read the whole file and paste it into the system prompt. Restart the agent and ask "what's my name?" It should remember now.
3. **Recall only what's relevant.** Pasting everything stops working once the file gets big. Change recall to search the file, first with plain keyword matching and then with embeddings and similarity search. That second step is RAG.
4. **Make it a tool.** Implement `search_memory` (hook 3) so the model searches only when it decides to. How is that different from always putting memory in the prompt?

### Questions to think about

- What should be saved: every message, or a summary, or only facts like "the user's name is Sam"?
- When does recall make the answers worse instead of better?
- What happens when the memory file says something that's no longer true?

## Troubleshooting

- **`KeyError: 'OPENROUTER_API_KEY'`**: the key isn't set in this terminal. Run the `export` (or `$env:`) line again in the same window you run the agent from.
- **`401` or "authentication" errors**: the key is wrong or has been revoked. Make a new one at [openrouter.ai/keys](https://openrouter.ai/keys).
- **`402` or "insufficient credits"**: your OpenRouter account needs credits, or you can switch to a free model (its ID ends in `:free`).
- **Model not found**: check the model ID on [openrouter.ai/models](https://openrouter.ai/models) and set it with `MODEL=...`.
- **`ModuleNotFoundError: No module named 'prompt'`**: run the agent from inside the project folder, so that `agent.py`, `prompt.py` and `tools.py` sit next to each other.
