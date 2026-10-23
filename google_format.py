"""Turns raw Google API responses into compact, consistent text a language model can read and act on.

Rules every Google tool follows:
- One result per block, fields as `label: value`, ids always shown so the model can follow up.
- Times in the user's local timezone, with a relative hint ("tomorrow", "3 days ago").
- Boilerplate stripped: HTML, quoted replies, empty lines.
- Every cut says how much was cut, and empty results suggest what to try next.
"""

import html
import re
from datetime import date, datetime
from html.parser import HTMLParser

MAX_CHARS = 20_000  # long emails and docs get cut so they don't flood the model's context
MAX_ROWS = 200  # sheet rows shown per read


# --- text ---

def cut(text, limit=MAX_CHARS):
    """Trim text to `limit` chars, saying so if anything was dropped."""
    return text if len(text) <= limit else text[:limit] + f"\n[... cut, {len(text) - limit} more chars]"


def tidy(text):
    """Collapse runs of spaces and blank lines."""
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


class _HTMLText(HTMLParser):
    """Minimal HTML -> text: keeps line breaks and link targets, drops scripts and styles."""
    BREAKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__()
        self.out, self.skip, self.href = [], 0, None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BREAKS:
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")
        if tag == "a":
            self.href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.skip = max(0, self.skip - 1)
        if tag == "a" and self.href and self.href.startswith("http"):
            self.out.append(f" ({self.href})")
            self.href = None

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(markup):
    parser = _HTMLText()
    parser.feed(markup)
    return tidy("".join(parser.out))


def strip_quoted(body):
    """Drop the quoted thread under a reply ("On ... wrote:" and "> " lines); the earlier messages are separate emails."""
    lines = []
    for line in body.splitlines():
        if re.match(r"^\s*(On .{5,200} wrote:|-----Original Message-----|From: .+ Sent: )", line):
            lines.append("[... quoted earlier messages removed]")
            break
        if not line.lstrip().startswith(">"):
            lines.append(line)
    return "\n".join(lines)


# --- time ---

def when(value):
    """Render an ISO date or datetime as local time plus a relative hint, e.g. 'Fri Oct 24 2:00 PM (tomorrow)'."""
    if not value:
        return "unknown time"
    if len(value) == 10:  # all-day: just a date
        d = date.fromisoformat(value)
        return f"{d:%a %b %d} (all day, {_relative(d)})"
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()  # to the user's local timezone
    return f"{dt:%a %b %d %I:%M %p %Z}".replace(" 0", " ") + f" ({_relative(dt.date())})"


def _relative(d):
    days = (d - date.today()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    return f"in {days} days" if days > 0 else f"{-days} days ago"


def duration(start, end):
    """'45 min', '2 h', '1 h 30 min' between two ISO datetimes."""
    mins = int((datetime.fromisoformat(end.replace("Z", "+00:00")) -
                datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds() // 60)
    h, m = divmod(mins, 60)
    return " ".join(p for p in (f"{h} h" if h else "", f"{m} min" if m else "") if p) or "0 min"


# --- records ---

def block(title, fields):
    """One result: a title line, then indented `label: value` lines, skipping empty values."""
    return "\n".join([title] + [f"  {k}: " + str(v).replace("\n", "\n    ")  # keep multi-line values indented
                                 for k, v in fields.items() if v not in (None, "", [])])


def results(kind, blocks, total_hint=None, empty_hint=""):
    """Join result blocks under a count header, or explain an empty result."""
    if not blocks:
        return f"No {kind} found. {empty_hint}".strip()
    head = f"{len(blocks)} {kind}" + (f" ({total_hint})" if total_hint else "") + ":"
    return head + "\n\n" + "\n\n".join(blocks)


def person(header_value):
    """'Sam Lee <sam@x.com>' stays as is; bare quotes and extra spaces are dropped."""
    return re.sub(r'"', "", header_value or "").strip()


def md_table(rows, max_rows=MAX_ROWS, numbered=True):
    """Rows of cells -> a markdown table, first row as the header, optionally with the sheet's row numbers."""
    filled = lambda r: [i for i, v in enumerate(r) if str(v).strip()]
    width = max((filled(r)[-1] + 1 for r in rows if filled(r)), default=0)  # drop trailing empty columns
    if not width:
        return "(empty)"
    cell = lambda v: str(v).replace("|", "\\|").replace("\n", " ")
    pad = lambda r: [cell(v) for v in r[:width]] + [""] * (width - len(r))
    num = lambda i: f"| {i} " if numbered else ""
    lines = [num("row") + "| " + " | ".join(pad(rows[0])) + " |", "|---" * (width + numbered) + "|"]
    lines += [num(i) + "| " + " | ".join(pad(r)) + " |" for i, r in enumerate(rows[1:max_rows], start=2)
              if filled(r)]  # skip blank rows; row numbers still match the sheet
    if len(rows) > max_rows:
        lines.append(f"[... {len(rows) - max_rows} more rows; pass a range like 'Tab!A{max_rows + 1}:Z{max_rows * 2}' to read them]")
    return "\n".join(lines)


def doc_to_markdown(doc):
    """A Google Docs API document -> markdown, keeping headings, lists and tables."""
    lists = doc.get("lists", {})
    out = []
    for el in doc["body"]["content"]:
        if "paragraph" in el:
            out.append(_paragraph(el["paragraph"], lists))
        elif "table" in el:
            rows = [[tidy("".join(_paragraph(p["paragraph"], lists) for p in cell["content"] if "paragraph" in p))
                     for cell in row["tableCells"]] for row in el["table"]["tableRows"]]
            out.append(md_table(rows, numbered=False))
    return tidy("\n".join(out))


def _paragraph(p, lists):
    text = "".join(r["textRun"]["content"] for r in p["elements"] if "textRun" in r).rstrip("\n")
    if not text.strip():
        return ""
    style = p.get("paragraphStyle", {}).get("namedStyleType", "")
    if style == "TITLE":
        return f"# {text}"
    if style.startswith("HEADING_"):
        return "#" * min(int(style[-1]) + 1, 6) + f" {text}"  # HEADING_1 -> ##, since the doc title is #
    if "bullet" in p:
        depth = p["bullet"].get("nestingLevel", 0)
        return "  " * depth + f"- {text}"
    return text


def doc_id_from(value):
    """Accept a bare file id or any docs/sheets/drive URL and return the id."""
    m = re.search(r"/d/([a-zA-Z0-9_-]{20,})", value) or re.search(r"[?&]id=([a-zA-Z0-9_-]{20,})", value)
    return m.group(1) if m else value.strip()


def unescape(text):
    return html.unescape(text or "")
