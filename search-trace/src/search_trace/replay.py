"""Two tools that drive the open model ourselves instead of through OpenRouter's server tools:

- test_page_change: rebuild a real open-model trace's conversation, exactly as the model saw it, up to the first
  step where it saw a page on one of your domains; swap that page's text for your dev site's version (variant) or
  re-extract the production page the same way (control); then let the model carry on, several times per arm.
- sample_queries: run the model only until it asks for its first search, many times, and count the queries.

Our tools copy OpenRouter's exactly: the same names, descriptions and parameters, and the same result JSON
(recorded from DeepSeek on 2026-11-14), so the model can't tell the difference. Searches after the branch point go
to OpenRouter's own search tool (exact production results and excerpts); page text for the swapped pages comes from
Exa's live crawl.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import core

# ---------------------------------------------------------------- OpenRouter's tools, verbatim

SEARCH_TOOL = {"type": "function", "function": {
    "name": "openrouter_web_search",
    "description": (
        "Search the web and return ranked excerpts from matching pages.\n\n"
        "- Provides up-to-date information for current events, recent releases, and data past your knowledge cutoff\n"
        "- Returns each result as a title, URL, and excerpt of the page's relevant content\n"
        "- Searches execute automatically within a single API call\n\n"
        "CRITICAL REQUIREMENT - You MUST follow this:\n"
        "  - A query is a single string, and each call is scored independently. Results from separate calls are "
        "NEVER intersected.\n"
        "  - Therefore you MUST put every criterion that must hold at the same time into ONE query, together with any "
        "named entities, qualifiers, and time or place bounds you already know.\n"
        "  - NEVER split a multi-criteria question into one query per criterion. Doing so returns pages matching only "
        "a single criterion and is the most common cause of a wrong answer.\n"
        "  - Example - to find a round red fruit that grows on trees:\n"
        "    CORRECT: one search for \"round red fruit that grows on trees\"\n"
        "    WRONG: three searches for \"round fruit\", \"red fruit\", and \"fruit that grows on trees\"\n"
        "  - Start specific. Only broaden a query after a specific one returns nothing useful, and when you broaden, "
        "drop the least essential criterion rather than all of them.\n\n"
        "Usage notes:\n"
        "  - Each result is an excerpt, not the full page. If an excerpt is insufficient, search again with added "
        "detail rather than repeating the same query.\n"
        "  - When searching for recent information, documentation, or current events, use the current year in the "
        "query rather than a past year. If you are unsure of the current date and a datetime tool is available, call "
        "it first."),
    "parameters": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                   "properties": {"query": {"type": "string"}}, "required": ["query"],
                   "additionalProperties": False}}}

FETCH_TOOL = {"type": "function", "function": {
    "name": "openrouter_web_fetch",
    "description": "Fetch the full content of a web page or PDF document at a given URL. Returns the text content, "
                   "title, and URL.",
    "parameters": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                   "properties": {"url": {"type": "string"}}, "required": ["url"],
                   "additionalProperties": False}}}

MAX_TURNS = 8


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def search_result(results: list[dict]) -> str:
    return _dumps({"status": "ok", "error": None, "results": [
        {"id": r["url"], "url": r["url"], "title": r["title"], "text": r["text"]} for r in results]})


def fetch_result(url: str, content: str, title: str, http_status: int | None, retrieved_at: str | None) -> str:
    return _dumps({"url": url, "content": content, "title": title, "status": "completed",
                   "http_status": http_status, "retrieved_at": retrieved_at or datetime.now(UTC).isoformat()})


# ---------------------------------------------------------------- domains


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def match_domain(url: str, domains: dict[str, str]) -> str | None:
    h = _host(url)
    for d in domains:
        d0 = d.lower().removeprefix("www.")
        if h == d0 or h.endswith("." + d0):
            return d
    return None


def to_dev(url: str, domains: dict[str, str]) -> str:
    d = match_domain(url, domains)
    s = urlsplit(url)
    return urlunsplit((s.scheme or "https", domains[d], s.path, s.query, "")) if d else url


def _undev(text: str, domains: dict[str, str]) -> str:
    """The dev site's own host in its text (links, canonical) back to production, so nothing says 'dev'."""
    for prod, dev in domains.items():
        text = text.replace(dev, prod.removeprefix("www."))
    return text


# ---------------------------------------------------------------- Exa


class Exa:
    def __init__(self, client: httpx.AsyncClient, key: str):
        self.c, self.key, self.cost = client, key, 0.0

    async def _post(self, path: str, body: dict) -> dict:
        r = await self.c.post(f"https://api.exa.ai/{path}", headers={"x-api-key": self.key}, json=body, timeout=120)
        if r.status_code != 200:
            raise core.TraceError(f"Exa {path} answered {r.status_code}: {r.text[:200]}")
        j = r.json()
        self.cost += (j.get("costDollars") or {}).get("total") or 0
        return j

    async def search(self, query: str) -> list[dict]:
        j = await self._post("search", {"query": query, "type": "auto", "numResults": 10,
                                        "contents": {"highlights": True}})
        return [{"url": x["url"], "title": x.get("title") or "", "text": "\n...\n".join(x.get("highlights") or [])}
                for x in j.get("results") or []]

    async def excerpt(self, url: str, query: str, max_chars: int) -> tuple[str, str]:
        """Exa's query-matched passages from a live crawl of url, about max_chars long -> (text, title)."""
        j = await self._post("contents", {"urls": [url], "livecrawl": "always",
                                          "highlights": {"query": query, "maxCharacters": max(max_chars, 200)}})
        res = (j.get("results") or [{}])[0]
        return "\n...\n".join(res.get("highlights") or []), res.get("title") or ""

    async def page(self, url: str) -> tuple[str, str]:
        j = await self._post("contents", {"urls": [url], "livecrawl": "always", "text": True})
        res = (j.get("results") or [{}])[0]
        return res.get("text") or "", res.get("title") or ""


# ---------------------------------------------------------------- the recorded conversation


def recorded_turns(raw: dict) -> list[dict]:
    """OpenRouter's Responses output -> assistant turns as the model produced them:
    [{"reasoning", "text", "calls": [{"kind", "args", "results"|"fetch"}]}]; the last turn has no calls."""
    out = raw.get("output") or []
    anns = [a for o in out if o.get("type") == "message" for c in o.get("content") or []
            for a in c.get("annotations") or [] if a.get("type") == "url_citation"]
    n_sources = sum(len((o.get("action") or {}).get("sources") or [])
                    for o in out if o.get("type") == "openrouter:web_search")
    by_url = {a["url"]: a for a in anns if a.get("url")}
    in_order = iter(anns) if len(anns) == n_sources else None
    turns: list[dict] = []
    cur = {"reasoning": "", "text": "", "calls": []}
    for o in out:
        typ = o.get("type")
        if typ in ("reasoning", "message") and cur["calls"]:
            turns.append(cur)
            cur = {"reasoning": "", "text": "", "calls": []}
        if typ == "reasoning":
            cur["reasoning"] += "\n".join(c.get("text", "") for c in o.get("content") or [])
        elif typ == "message":
            cur["text"] += "".join(c.get("text", "") for c in o.get("content") or [])
        elif typ == "openrouter:web_search":
            a = o.get("action") or {}
            results = []
            for s in a.get("sources") or []:
                ann = next(in_order, None) if in_order else by_url.get(s["url"])
                if ann is not None and ann.get("url") != s["url"]:
                    ann = by_url.get(s["url"])
                results.append({"url": s["url"], "title": (ann or {}).get("title") or "",
                                "text": (ann or {}).get("content") or ""})
            cur["calls"].append({"kind": "search", "args": {"query": a.get("query")}, "results": results})
        elif typ == "openrouter:web_fetch":
            cur["calls"].append({"kind": "fetch", "args": {"url": o.get("url")},
                                 "fetch": {"url": o.get("url"), "content": o.get("content") or "",
                                           "title": o.get("title") or "", "http_status": o.get("httpStatus"),
                                           "retrieved_at": o.get("retrieved_at")}})
    turns.append(cur)
    return turns


def _assistant(turn: dict, ids: list[str]) -> dict:
    m = {"role": "assistant", "content": turn["text"] or "",
         "tool_calls": [{"id": i, "type": "function",
                         "function": {"name": "openrouter_web_search" if c["kind"] == "search"
                                      else "openrouter_web_fetch", "arguments": _dumps(c["args"])}}
                        for i, c in zip(ids, turn["calls"])]}
    if turn["reasoning"]:
        m["reasoning"] = turn["reasoning"]
    return m


def _tool_text(call: dict) -> str:
    if call["kind"] == "search":
        return search_result(call["results"])
    f = call["fetch"]
    return fetch_result(f["url"], f["content"], f["title"], f["http_status"], f["retrieved_at"])


def find_branch(turns: list[dict], domains: dict[str, str]) -> tuple[int, list[tuple[int, int | None]]] | None:
    """The first turn whose tool results include a page on one of the domains -> (turn index, [(call index,
    result index or None for a fetch)]) for every such page in that turn."""
    for ti, t in enumerate(turns):
        hits: list[tuple[int, int | None]] = []
        for ci, c in enumerate(t["calls"]):
            if c["kind"] == "search":
                hits += [(ci, ri) for ri, r in enumerate(c["results"]) if match_domain(r["url"], domains)]
            elif match_domain(c["fetch"]["url"] or "", domains):
                hits.append((ci, None))
        if hits:
            return ti, hits
    return None


# ---------------------------------------------------------------- continuing the conversation


class OpenRouterSearch:
    """Searches after the branch point, run by OpenRouter's own web_search tool so the results and excerpts are
    exactly what production gives: a small call asks a model to search once with the exact query, and the results
    are read back as in trace_query. Results are shared by every run in a test (one call per distinct query)."""

    PROMPT = ("Call the web search tool exactly once with this exact query string, character for character, and "
              "nothing else: {q}\nAfter the search, reply only with the word done.")

    def __init__(self, client: httpx.AsyncClient, key: str, model: str, engine: str, exa: Exa):
        self.c, self.key, self.model, self.engine, self.exa = client, key, model, engine, exa
        self.cost, self.cache, self.locks = 0.0, {}, {}

    async def _once(self, query: str) -> list[dict] | None:
        body = {"model": self.model, "input": self.PROMPT.format(q=json.dumps(query, ensure_ascii=False)),
                "tools": [{"type": "openrouter:web_search", "parameters": {"engine": self.engine,
                                                                          "max_results": 10}}],
                "tool_choice": "required", "max_tool_calls": 1}
        resp, _, err = await core._post(self.c, "https://openrouter.ai/api/v1/responses", self.key, body)
        if err:
            return None
        self.cost += (resp.get("usage") or {}).get("cost") or 0
        turns = recorded_turns(resp)
        calls = [c for t in turns for c in t["calls"] if c["kind"] == "search"]
        if len(calls) != 1 or calls[0]["args"].get("query") != query:
            return None
        return calls[0]["results"]

    async def search(self, query: str) -> tuple[list[dict], str]:
        """-> (results [{url, title, text}], source: 'openrouter' or 'exa direct (fallback)')."""
        async with self.locks.setdefault(query, asyncio.Lock()):
            if query not in self.cache:
                results = await self._once(query) or await self._once(query)
                self.cache[query] = (results, "openrouter") if results is not None else \
                    (await self.exa.search(query), "exa direct (fallback)")
            results, source = self.cache[query]
            return [dict(r) for r in results], source


class Arm:
    """One way of answering tool calls after the branch point. Searches come from OpenRouter (exact production
    results); on the domains, control keeps production's excerpt and variant gets the dev page's passages for the
    same query (Exa live crawl, sized like production's excerpt). Fetches of domain pages read the production or
    dev page through Exa's live crawl in both arms."""

    def __init__(self, name: str, exa: Exa, domains: dict[str, str], ors: OpenRouterSearch | None = None):
        self.name, self.exa, self.domains, self.ors = name, exa, domains, ors

    def source(self, url: str) -> str:
        return to_dev(url, self.domains) if self.name == "variant" else url

    async def domain_excerpt(self, url: str, query: str, size: int) -> tuple[str, str]:
        text, title = await self.exa.excerpt(self.source(url), query, size)
        return _undev(text, self.domains), _undev(title, self.domains)

    async def search(self, query: str) -> tuple[list[dict], dict[str, str], str]:
        """-> (results, {url: note} for domain pages, where the results came from)."""
        if self.ors:
            results, origin = await self.ors.search(query)
        else:
            results, origin = await self.exa.search(query), "exa direct"
        notes = {}
        for r in results:
            if not match_domain(r["url"], self.domains):
                continue
            if self.name == "control" and origin == "openrouter":
                notes[r["url"]] = "control: production excerpt as OpenRouter gave it"
            else:
                r["text"], _ = await self.domain_excerpt(r["url"], query, len(r["text"]) or 1000)
                notes[r["url"]] = f"{self.name}: {'dev' if self.name == 'variant' else 'production'} page re-extracted"
        return results, notes, origin

    async def fetch(self, url: str) -> tuple[dict, bool]:
        dom = bool(match_domain(url, self.domains))
        content, title = await self.exa.page(self.source(url) if dom else url)
        if dom:
            content, title = _undev(content, self.domains), _undev(title, self.domains)
        return {"url": url, "content": content, "title": title, "http_status": 200 if content else None,
                "retrieved_at": None}, dom


async def _chat(client: httpx.AsyncClient, key: str, model: str, messages: list[dict]) -> dict:
    body = {"model": model, "messages": messages, "tools": [SEARCH_TOOL, FETCH_TOOL],
            "reasoning": {"enabled": True}, "usage": {"include": True}}
    resp, _, err = await core._post(client, "https://openrouter.ai/api/v1/chat/completions", key, body)
    if err:
        raise core.TraceError(f"OpenRouter {err}")
    return resp


async def continue_run(client: httpx.AsyncClient, key: str, model: str, messages: list[dict], arm: Arm) -> dict:
    """Let the model carry on from messages, answering its tool calls with arm. Returns its steps in the same
    shape as an open-model trace, plus cost."""
    messages = list(messages)
    steps: list[dict] = []
    cost, t0, answer = 0.0, time.monotonic(), ""
    for turn in range(MAX_TURNS):
        resp = await _chat(client, key, model, messages)
        cost += (resp.get("usage") or {}).get("cost") or 0
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        if msg.get("reasoning"):
            steps.append({"type": "reasoning", "text": msg["reasoning"]})
        calls = msg.get("tool_calls") or []
        if not calls:
            answer = msg.get("content") or ""
            break
        if msg.get("content"):
            steps.append({"type": "text", "text": msg["content"]})
        a = {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
        if msg.get("reasoning"):
            a["reasoning"] = msg["reasoning"]
        messages.append(a)
        for c in calls:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            if fn.get("name") == "openrouter_web_search":
                results, notes, origin = await arm.search(args.get("query") or "")
                text = search_result(results)
                steps.append({"type": "search", "query": args.get("query"), "origin": origin, "results": [
                    {"rank": i + 1, "url": core.norm_url(r["url"]), "title": r["title"], "excerpt": r["text"],
                     "note": notes.get(r["url"])} for i, r in enumerate(results)]})
            elif fn.get("name") == "openrouter_web_fetch":
                f, dom = await arm.fetch(args.get("url") or "")
                text = fetch_result(f["url"], f["content"], f["title"], f["http_status"], None)
                steps.append({"type": "fetch", "url": core.norm_url(f["url"]), "title": f["title"],
                              "http_status": f["http_status"], "content": f["content"],
                              "note": f"{arm.name}: {'dev' if arm.name == 'variant' else 'production'} page"
                              if dom else None})
            else:
                text = _dumps({"status": "error", "error": f"unknown tool {fn.get('name')}"})
            messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": text})
    else:
        answer = f"(stopped: no answer after {MAX_TURNS} turns)"
    return {"steps": steps, "answer": answer, "cost_usd": cost, "seconds": round(time.monotonic() - t0, 1),
            "cited": list(dict.fromkeys(core.norm_url(u) for u in core._MD_LINK.findall(answer)))}


def _outcome(run: dict, domains: dict[str, str]) -> dict:
    labels = {d.lower().removeprefix("www.").split(".")[0] for d in domains}
    cited = [u for u in run["cited"] if match_domain(u, domains)]
    seen = [r["url"] for s in run["steps"] if s["type"] == "search" for r in s["results"]
            if match_domain(r["url"], domains)]
    fetched = [s["url"] for s in run["steps"] if s["type"] == "fetch" and match_domain(s["url"], domains)]
    # Named = the brand in the answer's own words, not just a link label or URL.
    prose = re.sub(r"\[[^\]]*\]\([^)]*\)|https?://\S+", " ", run["answer"]).lower()
    named = any(lb in prose for lb in labels)
    # The answer's lines that name or link the domain, verbatim: a link can be a warning as easily as a pick.
    mentions = [ln.strip() for ln in run["answer"].splitlines()
                if any(lb in ln.lower() for lb in labels) or any(d in ln.lower() for d in domains)]
    return {"cited": cited, "named_in_answer": named, "fetched": fetched, "seen_in_new_searches": seen,
            "mentions": mentions}


# ---------------------------------------------------------------- test_page_change


async def test_page_change(domains: dict[str, str], trace_id: str | None = None, query: str | None = None,
                           runs: int = 3, save_dir: str | None = None) -> dict:
    if not domains:
        raise core.TraceError("domains is empty: map each production domain to its dev host, e.g. "
                              "{'example.com': 'dev.example.com'}.")
    domains = {k.strip().lower(): v.strip().lower().removeprefix("https://").rstrip("/") for k, v in domains.items()}
    runs = max(1, min(int(runs), 10))
    keys = core.load_keys()
    exa_key = core._read_key("EXA_API_KEY", "exa.key")
    if not keys.openrouter or not exa_key:
        raise core.TraceError(f"Needs an OpenRouter key and an Exa key (EXA_API_KEY or {core.KEYS_DIR / 'exa.key'}).")
    if trace_id:
        tid = trace_id.strip()
        src = Path(tid).expanduser() if tid.startswith(("/", "~")) else core.STATE_DIR / tid
        if not (src / "raw.json").is_file():
            raise core.TraceError(f"No trace folder {src} with raw.json; pass a trace_id from trace_query, or a query.")
        trace = json.loads((src / "trace.json").read_text())
    elif query:
        trace = await core.run_trace(query)
        src = core.STATE_DIR / trace["trace_id"]
    else:
        raise core.TraceError("Pass trace_id (from trace_query) or query.")
    raw = (json.loads((src / "raw.json").read_text()) or {}).get("open_model")
    if not raw:
        raise core.TraceError(f"Trace {trace['trace_id']} has no open-model response to replay "
                              f"({trace['open_model'].get('error') or 'missing'}).")
    turns = recorded_turns(raw)
    branch = find_branch(turns, domains)
    if not branch:
        raise core.TraceError(f"In trace {trace['trace_id']} the open model never saw a page on "
                              f"{', '.join(domains)} (in results or fetched), so there's nothing to swap. Use "
                              f"sample_queries to see which queries the page would need to rank for.")
    ti, hits = branch
    model = trace["open_model"].get("model") or core.OPEN_MODEL
    instructions = trace["open_model"].get("instructions") or core.OPEN_INSTRUCTIONS

    async with httpx.AsyncClient() as client:
        exa = Exa(client, exa_key)
        # The conversation up to and including the branch turn's tool calls, exactly as recorded.
        prefix: list[dict] = [{"role": "system", "content": instructions},
                              {"role": "user", "content": trace["query"]}]
        for t in turns[:ti]:
            ids = [f"call_{len(prefix)}_{i}" for i in range(len(t["calls"]))]
            prefix.append(_assistant(t, ids))
            prefix += [{"role": "tool", "tool_call_id": i, "content": _tool_text(c)} for i, c in zip(ids, t["calls"])]
        bt = turns[ti]
        ids = [f"call_{len(prefix)}_{i}" for i in range(len(bt["calls"]))]
        prefix.append(_assistant(bt, ids))

        # The swapped pages, for each arm, re-extracted the same way.
        ors = OpenRouterSearch(client, keys.openrouter, model, trace["open_model"].get("engine") or "exa", exa)
        arms = {name: Arm(name, exa, domains, ors) for name in ("control", "variant")}
        swaps = []
        for ci, ri in hits:
            c = bt["calls"][ci]
            if ri is None:
                orig = c["fetch"]
                cont, _ = await arms["control"].fetch(orig["url"])
                var, _ = await arms["variant"].fetch(orig["url"])
                swaps.append({"kind": "fetch", "call": ci, "url": orig["url"], "original": orig["content"],
                              "control": cont["content"], "variant": var["content"],
                              "dev_url": to_dev(orig["url"], domains)})
            else:
                r = c["results"][ri]
                q = c["args"]["query"]
                ctext, _ = await arms["control"].domain_excerpt(r["url"], q, len(r["text"]) or 1000)
                vtext, _ = await arms["variant"].domain_excerpt(r["url"], q, len(r["text"]) or 1000)
                swaps.append({"kind": "search", "call": ci, "rank": ri + 1, "query": q, "url": r["url"],
                              "original": r["text"], "control": ctext, "variant": vtext,
                              "dev_url": to_dev(r["url"], domains)})

        def branch_messages(arm: str) -> list[dict]:
            calls = [json.loads(json.dumps(c)) for c in bt["calls"]]
            for s in swaps:
                if s["kind"] == "fetch":
                    calls[s["call"]]["fetch"]["content"] = s[arm]
                else:
                    calls[s["call"]]["results"][s["rank"] - 1]["text"] = s[arm]
            return prefix + [{"role": "tool", "tool_call_id": i, "content": _tool_text(c)}
                             for i, c in zip(ids, calls)]

        jobs = [(arm, i) for arm in ("control", "variant") for i in range(runs)]
        results = await asyncio.gather(*[continue_run(client, keys.openrouter, model, branch_messages(arm),
                                                      arms[arm]) for arm, _ in jobs], return_exceptions=True)
        exa_cost = exa.cost
        search_cost = ors.cost
        search_origins = Counter(src for _, src in ors.cache.values())

    now = datetime.now(UTC)
    test_id = f"{now:%Y%m%d-%H%M%S}-test-{core.slug(trace['query'], 40)}"
    root = Path(save_dir).expanduser() if save_dir else core.STATE_DIR / "tests"
    if not root.is_absolute():
        raise core.TraceError(f"save_dir must be an absolute path (got '{save_dir}').")
    base = root / test_id
    base.mkdir(parents=True, exist_ok=True)

    from .render import render_continuation, render_test_summary
    out_runs = []
    for (arm, i), res in zip(jobs, results):
        name = f"{arm}-{i + 1}"
        if isinstance(res, Exception):
            out_runs.append({"name": name, "arm": arm, "error": f"{type(res).__name__}: {str(res)[:300]}"})
            continue
        res.update({"name": name, "arm": arm, "outcome": _outcome(res, domains), "file": f"{name}.md"})
        (base / res["file"]).write_text(render_continuation(res, model))
        out_runs.append(res)
    for s in swaps:
        s["similarity_original_vs_control"] = round(SequenceMatcher(None, s["original"][:4000],
                                                                    s["control"][:4000]).ratio(), 2)
    test = {"test_id": test_id, "query": trace["query"], "source_trace": trace["trace_id"], "domains": domains,
            "model": model, "branch": {"turn": ti + 1, "of_turns": len(turns), "steps_before": [
                {"kind": c["kind"], "args": c["args"]} for t in turns[:ti + 1] for c in t["calls"]]},
            "swaps": swaps, "runs": out_runs, "exa_cost_usd": round(exa_cost, 4),
            "openrouter_cost_usd": round(sum(r.get("cost_usd") or 0 for r in out_runs) + search_cost, 4),
            "later_searches": dict(search_origins)}
    swap_md = []
    for n, s in enumerate(swaps, 1):
        where = (f"result #{s['rank']} for the search '{s['query']}'" if s["kind"] == "search" else "a page fetch")
        swap_md.append(f"# Swap {n}: {s['url']} ({where})\nDev source: {s['dev_url']}\n")
        for k in ("original", "control", "variant"):
            label = {"original": "Original: what the model actually saw (OpenRouter)",
                     "control": "Control: production page re-extracted by Exa live crawl",
                     "variant": "Variant: dev page extracted the same way"}[k]
            swap_md.append(f"## {label} ({len(s[k])} chars)\n" + "\n".join("> " + ln for ln in s[k].splitlines())
                           + "\n")
    (base / "swaps.md").write_text("\n".join(swap_md))
    (base / "test.json").write_text(json.dumps(test, ensure_ascii=False, indent=1))
    test["dir"] = str(base)
    summary = render_test_summary(test)
    (base / "summary.md").write_text(summary)
    test["summary"] = summary
    return test


# ---------------------------------------------------------------- sample_queries


async def sample_queries(query: str, n: int = 20, model: str = core.OPEN_MODEL) -> dict:
    query = (query or "").strip()
    if not query:
        raise core.TraceError("query is empty.")
    n = max(1, min(int(n), 50))
    keys = core.load_keys()
    if not keys.openrouter:
        raise core.TraceError(f"No OpenRouter key ({core.KEYS_DIR / 'openrouter.key'}).")
    sem = asyncio.Semaphore(10)
    messages = [{"role": "system", "content": core.OPEN_INSTRUCTIONS}, {"role": "user", "content": query}]

    async def one(client):
        async with sem:
            resp = await _chat(client, keys.openrouter, model, messages)
            msg = (resp.get("choices") or [{}])[0].get("message") or {}
            qs, fetches = [], []
            for c in msg.get("tool_calls") or []:
                fn = c.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                (qs if fn.get("name") == "openrouter_web_search" else fetches).append(
                    args.get("query") or args.get("url") or "")
            return {"queries": qs, "fetches": fetches, "reasoning": msg.get("reasoning") or "",
                    "answered_without_searching": not (qs or fetches),
                    "cost": (resp.get("usage") or {}).get("cost") or 0}

    t0 = time.monotonic()
    async with httpx.AsyncClient() as client:
        samples = await asyncio.gather(*[one(client) for _ in range(n)], return_exceptions=True)
    ok = [s for s in samples if not isinstance(s, Exception)]
    errors = [f"{type(s).__name__}: {str(s)[:200]}" for s in samples if isinstance(s, Exception)]
    counts = Counter(q for s in ok for q in s["queries"])
    norm = Counter(re.sub(r"\s+", " ", q.lower()).strip() for s in ok for q in s["queries"])
    words = Counter(w for s in ok for w in {w for q in s["queries"] for w in re.findall(r"[a-z0-9]+", q.lower())})
    return {"query": query, "model": model, "n": n, "ok": len(ok), "errors": errors, "samples": ok,
            "counts": counts.most_common(), "distinct_normalized": len(norm),
            "per_sample": Counter(len(s["queries"]) for s in ok),
            "word_counts": words.most_common(30), "no_search": sum(s["answered_without_searching"] for s in ok),
            "cost_usd": round(sum(s["cost"] for s in ok), 4), "seconds": round(time.monotonic() - t0, 1)}
