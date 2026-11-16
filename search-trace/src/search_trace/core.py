"""Run one query through two independent AI web searches and keep everything each one exposes:

- OpenAI: the Responses API with its own web_search tool (queries, results with the excerpts the model read,
  pages opened, a reasoning summary, the answer and its citations; its real reasoning is hidden).
- An open-weight model on OpenRouter (default DeepSeek V4.1 Flash) with OpenRouter's web_search and web_fetch
  server tools on a chosen search engine (default Exa): its full reasoning, every query it wrote, each query's
  results with the excerpts it read, every page it fetched, its answer and citations.

Neither sees the other. Nothing here filters or analyses. Each trace is saved as a folder under the state directory,
which doubles as a cache: the same query (and engine) within CACHE_DAYS (default 14) returns the saved trace.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

AITOOLS = Path(__file__).resolve().parents[3]
KEYS_DIR = Path(os.environ.get("SEARCH_TRACE_KEYS", AITOOLS / "keys"))
STATE_DIR = Path(os.environ.get("SEARCH_TRACE_STATE", AITOOLS / "state" / "search-trace"))
CACHE_INDEX = STATE_DIR / "cache.json"
CACHE_DAYS = float(os.environ.get("SEARCH_TRACE_CACHE_DAYS", "14"))

OPENAI_MODEL = os.environ.get("SEARCH_TRACE_OPENAI_MODEL", "gpt-6.1-terra")
OPEN_MODEL = os.environ.get("SEARCH_TRACE_OPEN_MODEL", "deepseek/deepseek-v4.1-flash")
COUNTRY = os.environ.get("SEARCH_TRACE_COUNTRY", "US")
ENGINES = ("exa", "parallel", "perplexity", "firecrawl")
DEFAULT_ENGINE = "exa"

# A search-assistant brief like ChatGPT search's, plus OpenRouter's own citation wording (without it DeepSeek
# answers with no links, so nothing could be counted as cited).
OPEN_INSTRUCTIONS = (
    "You are a web search assistant like ChatGPT search. Search the web to answer the user's question; open pages "
    "when the search excerpts aren't enough. Give a short, direct answer that picks the few best options or facts.\n"
    "IMPORTANT: Cite every source you rely on with a markdown link named by the source's domain, right after the "
    "claim. Example: [nytimes.com](https://nytimes.com/some-page)."
)

# OpenAI wraps each excerpt in private-use citation markers and a word-limit tag:
#   "citeturn158066search4 [wordlim: 200] Published: 6 days ago; Crawled: today; ..."
_MARKER = re.compile(r"[-]cite[-]\S*?[-]\s*")
_WORDLIM = re.compile(r"^\s*\[wordlim:\s*\d+\]\s*")
_DATES = re.compile(r"^\s*((?:Published|Updated|Crawled|Date):[^;]*;\s*)+")
_MD_LINK = re.compile(r"\]\((https?://[^)\s]+)\)")


class TraceError(RuntimeError):
    """An error worded for the agent calling the tool: what went wrong and what to do."""


@dataclass
class Keys:
    openai: str | None
    openrouter: str | None


def _read_key(env: str, file: str) -> str | None:
    if os.environ.get(env):
        return os.environ[env].strip()
    p = KEYS_DIR / file
    return p.read_text().strip() if p.is_file() else None


def load_keys() -> Keys:
    return Keys(_read_key("OPENAI_API_KEY", "openai.key"), _read_key("OPENROUTER_API_KEY", "openrouter.key"))


def norm_url(url: str) -> str:
    """The URL without the utm_source tag OpenAI adds, a lower-case host and no trailing slash, so the same page
    matches across results, fetches and citations."""
    s = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=True)
         if not (k == "utm_source" and v in ("openai", "chatgpt.com"))]
    return urlunsplit((s.scheme, s.netloc.lower(), s.path.rstrip("/") or "/", urlencode(q), s.fragment))


def split_excerpt(raw: str) -> tuple[str, dict]:
    """OpenAI's excerpt text -> (text without its internal markers, {"published": .., "crawled": ..})."""
    text = _WORDLIM.sub("", _MARKER.sub("", raw or ""))
    dates: dict[str, str] = {}
    m = _DATES.match(text)
    if m:
        for part in m.group(0).split(";"):
            if ":" in part:
                k, v = part.split(":", 1)
                dates[k.strip().lower()] = v.strip()
        text = text[m.end():]
    return text.strip(), dates


def slug(text: str, n: int = 50) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].rstrip("-") or "query"


def _api_error(r: httpx.Response) -> str:
    try:
        e = r.json().get("error")
        return (e.get("message") if isinstance(e, dict) else str(e))[:300]
    except Exception:  # noqa: BLE001
        return r.text[:300]


async def _post(client: httpx.AsyncClient, url: str, key: str, body: dict) -> tuple[dict | None, float, str | None]:
    t = time.monotonic()
    try:
        r = await client.post(url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=600)
    except httpx.HTTPError as e:
        return None, time.monotonic() - t, f"request failed ({type(e).__name__}: {str(e)[:200]})"
    if r.status_code != 200:
        return None, time.monotonic() - t, f"answered {r.status_code}: {_api_error(r)}"
    return r.json(), round(time.monotonic() - t, 1), None


# ---------------------------------------------------------------- OpenAI


async def openai_trace(client: httpx.AsyncClient, key: str | None, query: str, model: str,
                       openrouter_key: str | None = None) -> tuple[dict, dict | None]:
    """OpenAI's Responses API with its web_search tool. Without an OpenAI key, the same request goes through
    OpenRouter's Responses API (model openai/<model>), which passes OpenAI's web_search tool through and returns the
    same web_search_call items, results and excerpts included."""
    url, via = "https://api.openai.com/v1/responses", "OpenAI"
    if not key and openrouter_key:
        url, via, key = "https://openrouter.ai/api/v1/responses", "OpenRouter", openrouter_key
        model = model if "/" in model else f"openai/{model}"
    if not key:
        return {"engine": "OpenAI web_search", "error": f"No OpenAI or OpenRouter key: set OPENAI_API_KEY or put "
                                                        f"it in {KEYS_DIR / 'openai.key'} (or an OpenRouter key)."}, None
    tool: dict = {"type": "web_search"}
    if COUNTRY:
        tool["user_location"] = {"type": "approximate", "country": COUNTRY.upper()}
    body = {"model": model, "input": query, "tools": [tool], "reasoning": {"summary": "auto"},
            "include": ["web_search_call.action.sources", "web_search_call.results"]}
    resp, secs, err = await _post(client, url, key, body)
    if err:
        return {"model": model, "engine": "OpenAI web_search", "error": f"{via} {err}"}, None
    trace = parse_openai(resp, secs)
    trace["via"] = via
    return trace, resp


def parse_openai(resp: dict, seconds: float) -> dict:
    steps: list[dict] = []
    answer = ""
    cited: list[str] = []
    for item in resp.get("output", []):
        typ = item.get("type")
        if typ == "reasoning":
            summary = "\n".join(s.get("text", "") for s in item.get("summary") or []).strip()
            if summary:
                steps.append({"type": "reasoning_summary", "text": summary})
        elif typ == "web_search_call":
            a = item.get("action") or {}
            kind = a.get("type")
            if kind == "search":
                results = []
                for i, res in enumerate(item.get("results") or []):
                    text, dates = split_excerpt(res.get("snippet", ""))
                    results.append({"rank": i + 1, "url": norm_url(res.get("url", "")),
                                    "title": res.get("title") or "", "dates": dates, "excerpt": text})
                with_text = {r["url"] for r in results}
                no_text = [norm_url(s["url"]) for s in a.get("sources") or []
                           if s.get("url") and norm_url(s["url"]) not in with_text]
                queries = a.get("queries") or ([a["query"]] if a.get("query") else [])
                steps.append({"type": "search", "queries": queries, "results": results,
                              "sources_without_excerpt": no_text})
            elif kind == "open_page":
                steps.append({"type": "open_page", "url": norm_url(a.get("url") or "")})
            elif kind == "find_in_page":
                steps.append({"type": "find_in_page", "url": norm_url(a.get("url") or ""),
                              "pattern": a.get("pattern")})
            else:
                steps.append({"type": f"web_search_{kind}", "action": a})
        elif typ == "message":
            for c in item.get("content") or []:
                if c.get("type") != "output_text":
                    continue
                answer += c.get("text", "")
                for ann in c.get("annotations") or []:
                    if ann.get("type") == "url_citation" and ann.get("url"):
                        cited.append(norm_url(ann["url"]))
    usage = resp.get("usage") or {}
    return {"engine": "OpenAI web_search", "model": resp.get("model"), "seconds": seconds,
            "web_search_requests": ((resp.get("tool_usage") or {}).get("web_search") or {}).get("num_requests"),
            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "steps": steps, "answer": answer, "cited": list(dict.fromkeys(cited))}


# ---------------------------------------------------------------- open model with OpenRouter's search tools


async def open_trace(client: httpx.AsyncClient, key: str | None, query: str, model: str,
                     engine: str) -> tuple[dict, dict | None]:
    if not key:
        return {"engine": engine, "error": f"No OpenRouter key: set OPENROUTER_API_KEY or put it in "
                                           f"{KEYS_DIR / 'openrouter.key'}."}, None
    search = {"type": "openrouter:web_search",
              "parameters": {"engine": engine, "max_results": 10, "max_total_results": 60}}
    body = {"model": model, "input": query, "instructions": OPEN_INSTRUCTIONS,
            "tools": [search, {"type": "openrouter:web_fetch"}], "reasoning": {"enabled": True}}
    resp, secs, err = await _post(client, "https://openrouter.ai/api/v1/responses", key, body)
    if err:
        hint = " The OpenRouter account may be out of credit." if " 402" in err else ""
        return {"model": model, "engine": engine, "error": f"OpenRouter {err}.{hint}"}, None
    return parse_open(resp, secs, model, engine), resp


def parse_open(resp: dict, seconds: float, model: str, engine: str) -> dict:
    out = resp.get("output", [])
    # OpenRouter attaches each search's results, with the excerpt the model read, as url_citation annotations on
    # the next assistant message, in search order. Pair them back up with the searches.
    anns = [a for o in out if o.get("type") == "message" for c in o.get("content") or []
            for a in c.get("annotations") or [] if a.get("type") == "url_citation"]
    n_sources = sum(len((o.get("action") or {}).get("sources") or [])
                    for o in out if o.get("type") == "openrouter:web_search")
    by_url = {norm_url(a["url"]): a for a in anns if a.get("url")}
    in_order = iter(anns) if len(anns) == n_sources else None

    steps: list[dict] = []
    messages = [o for o in out if o.get("type") == "message"]
    for o in out:
        typ = o.get("type")
        if typ == "reasoning":
            text = "\n".join(c.get("text", "") for c in o.get("content") or []).strip() or \
                "\n".join(s.get("text", "") for s in o.get("summary") or []).strip()
            if text:
                steps.append({"type": "reasoning", "text": text})
        elif typ == "openrouter:web_search":
            a = o.get("action") or {}
            results = []
            for i, s in enumerate(a.get("sources") or []):
                u = norm_url(s.get("url", ""))
                ann = next(in_order, None) if in_order else by_url.get(u)
                if ann is not None and norm_url(ann.get("url", "")) != u:
                    ann = by_url.get(u)
                results.append({"rank": i + 1, "url": u, "title": (ann or {}).get("title") or "",
                                "excerpt": (ann or {}).get("content") or ""})
            steps.append({"type": "search", "query": a.get("query"), "results": results})
        elif typ == "openrouter:web_fetch":
            steps.append({"type": "fetch", "url": norm_url(o.get("url") or ""), "title": o.get("title") or "",
                          "http_status": o.get("httpStatus"), "content": o.get("content") or ""})
        elif typ == "message" and o is not messages[-1]:
            text = "".join(c.get("text", "") for c in o.get("content") or []).strip()
            if text:
                steps.append({"type": "text", "text": text})
        elif typ not in ("message",):
            steps.append({"type": typ, "item": o})
    answer = "".join(c.get("text", "") for c in (messages[-1].get("content") if messages else []) or [])
    usage = resp.get("usage") or {}
    tools = usage.get("server_tool_use_details") or {}
    return {"engine": engine, "model": resp.get("model") or model, "seconds": seconds,
            "web_search_requests": tools.get("web_search_requests"),
            "tool_calls": tools.get("tool_calls_executed"),
            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
            "cost_usd": usage.get("cost"), "instructions": OPEN_INSTRUCTIONS,
            "steps": steps, "answer": answer,
            "cited": list(dict.fromkeys(norm_url(u) for u in _MD_LINK.findall(answer)))}


# ---------------------------------------------------------------- whole trace


def cache_key(query: str, engine: str, openai_model: str, open_model: str) -> str:
    """Queries that differ only in case, spacing or trailing punctuation share a cache entry; so do runs with the
    same engine, models and country."""
    q = re.sub(r"\s+", " ", query.lower()).strip().rstrip("?!. ")
    return json.dumps([q, engine, openai_model, open_model, COUNTRY])


_locks: dict[str, asyncio.Lock] = {}


def _read_index() -> dict:
    try:
        return json.loads(CACHE_INDEX.read_text())
    except (OSError, ValueError):
        return {}


def _write_index(index: dict) -> None:
    tmp = CACHE_INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1))
    tmp.replace(CACHE_INDEX)


def _cached(key: str) -> dict | None:
    e = _read_index().get(key)
    if not e:
        return None
    created = datetime.fromisoformat(e["created"])
    d = STATE_DIR / e["trace_id"]
    if datetime.now(UTC) - created > timedelta(days=CACHE_DAYS) or not (d / "trace.json").is_file():
        return None
    return json.loads((d / "trace.json").read_text())


async def run_trace(query: str, engine: str = DEFAULT_ENGINE, save_dir: str | None = None,
                    openai_model: str = OPENAI_MODEL, open_model: str = OPEN_MODEL) -> dict:
    """The trace for this query: from the cache if it's under CACHE_DAYS old, otherwise run fresh and cached.
    With save_dir, the trace folder is also copied there and the returned paths point to the copy."""
    query = (query or "").strip()
    if not query:
        raise TraceError("query is empty: pass the question a person would type, e.g. "
                         "'What is the best app for scanning sheet music?'")
    engine = (engine or DEFAULT_ENGINE).strip().lower()
    if engine not in ENGINES:
        raise TraceError(f"engine '{engine}' isn't one of {', '.join(ENGINES)}.")
    dest_root = None
    if save_dir and save_dir.strip():
        dest_root = Path(save_dir.strip()).expanduser()
        if not dest_root.is_absolute():
            raise TraceError(f"save_dir must be an absolute path (got '{save_dir}'); the tool's working directory "
                             f"may not be yours.")
    key = cache_key(query, engine, openai_model, open_model)
    async with _locks.setdefault(key, asyncio.Lock()):   # parallel calls for one query run it once
        trace = _cached(key)
        hit = trace is not None
        if not hit:
            trace = await _run_fresh(query, engine, openai_model, open_model)
            if not (trace["openai"].get("error") or trace["open_model"].get("error")):
                index = _read_index()
                index[key] = {"trace_id": trace["trace_id"], "created": trace["created"]}
                _write_index(index)
    src = STATE_DIR / trace["trace_id"]
    base = src
    if dest_root and dest_root.resolve() != STATE_DIR.resolve():
        base = dest_root / trace["trace_id"]
        try:
            shutil.copytree(src, base, dirs_exist_ok=True)
        except OSError as e:
            raise TraceError(f"Can't copy the trace to {base} ({e.strerror or e}); pass a writable save_dir. "
                             f"It is also at {src}.") from e
    created = datetime.fromisoformat(trace["created"])
    trace["cache"] = {"hit": hit, "created": trace["created"],
                      "expires": (created + timedelta(days=CACHE_DAYS)).isoformat()}
    trace["dir"] = str(base)
    trace["files"] = {k: {"path": str(base / name), "chars": len((base / name).read_text())}
                      for k, name in trace["file_names"].items()}
    return trace


async def _run_fresh(query: str, engine: str, openai_model: str, open_model: str) -> dict:
    """Run both searches and save the trace folder in the cache (STATE_DIR/<trace_id>/)."""
    keys = load_keys()
    async with httpx.AsyncClient() as client:
        (oa, oa_raw), (op, op_raw) = await asyncio.gather(
            openai_trace(client, keys.openai, query, openai_model, keys.openrouter),
            open_trace(client, keys.openrouter, query, open_model, engine))
    if oa.get("error") and op.get("error"):
        raise TraceError(f"Both searches failed. {oa['error']} {op['error']}")
    now = datetime.now(UTC)
    trace = {"trace_id": f"{now:%Y%m%d-%H%M%S}-{slug(query)}", "query": query, "country": COUNTRY,
             "created": now.isoformat(), "openai": oa, "open_model": op}
    base = STATE_DIR / trace["trace_id"]
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise TraceError(f"Can't create {base} ({e.strerror}).") from e
    names: dict[str, str] = {"openai": "openai.md", "open_model": "open-model.md"}
    # Pages the open model fetched are often 5-50k characters each: each goes to its own file, unchanged.
    fetches = [s for s in op.get("steps") or [] if s["type"] == "fetch" and s.get("content")]
    for i, s in enumerate(fetches, 1):
        s["file"] = f"fetch-{i}-{slug(urlsplit(s['url']).netloc.removeprefix('www.'), 40)}.md"
        (base / s["file"]).write_text(f"# {s['title']}\n<{s['url']}>\n\n{s['content']}\n")
        names[s["file"]] = s["file"]
    names["trace.json"], names["raw.json"] = "trace.json", "raw.json"
    trace["file_names"] = names
    from .render import render_engine
    for k in ("openai", "open_model"):
        (base / names[k]).write_text(render_engine(trace, k))
    (base / "trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=1))
    (base / "raw.json").write_text(json.dumps({"openai": oa_raw, "open_model": op_raw}, ensure_ascii=False))
    return trace


def run(query: str, **kw) -> dict:
    return asyncio.run(run_trace(query, **kw))
