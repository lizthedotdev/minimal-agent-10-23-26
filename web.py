"""Web search and page reading through Tavily (https://tavily.com). Needs TAVILY_API_KEY."""

import json
import os
import urllib.error
import urllib.request

import google_format as fmt  # same compact result format as the Google tools

API = "https://api.tavily.com"


def _post(path, body):
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("web search is not set up: TAVILY_API_KEY is not set (get a key at https://tavily.com)")
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:  # surface Tavily's own message (bad key, out of credits, ...)
        raise RuntimeError(f"Tavily {e.code}: {e.read().decode(errors='replace')[:300]}") from None


def web_search(query, max_results=5, topic="general", time_range=None):
    """Search the web. Returns a short answer plus sources with title, url and the relevant content."""
    body = {"query": query, "max_results": max_results, "topic": topic, "include_answer": True,
            "safe_search": True}  # filter adult/unsafe results; Tavily's default is off, and the model can't turn it off
    if time_range:
        body["time_range"] = time_range  # day, week, month or year
    data = _post("/search", body)
    blocks = [fmt.block(f"[{i}] {r['title']}", {
        "url": r["url"],
        "published": r.get("published_date"),
        "content": fmt.cut(fmt.tidy(r.get("content", "")), 800),
    }) for i, r in enumerate(data.get("results", []), start=1)]
    answer = f"Summary (from the sources below; check them before relying on it): {data['answer']}\n\n" if data.get("answer") else ""
    return answer + fmt.results("web results", blocks, None, "Try fewer or different words, or a wider time_range.") + (
        "\n\nRead a full page with web_read." if blocks else "")


def web_read(url):
    """Return the main text of a web page."""
    data = _post("/extract", {"urls": [url]})
    if not data.get("results"):
        failed = (data.get("failed_results") or [{}])[0].get("error", "no content returned")
        return f"Could not read {url}: {failed}"
    return fmt.cut(f"# {url}\n\n{fmt.tidy(data['results'][0].get('raw_content', ''))}")
