"""A trace as markdown files for an agent: each engine's search in full, in the order it did things. Nothing is
cut; the only additions are labels (which results were opened, fetched or cited), and page text is quoted (> ) so
its own headings stay inside it. Whole pages the open model fetched are in their own files, named in the trace.
The tool returns a short summary pointing at the files (summary), or every file's text (render_full)."""

from __future__ import annotations


def _quote(text: str, empty: str = "(no excerpt)") -> str:
    """Page text as a blockquote, so its own headings and lists can't be mistaken for the trace's."""
    return "\n".join("> " + line if line.strip() else ">" for line in (text or empty).strip().splitlines())


def _mark(url: str, cited: set[str], read: set[str], read_label: str = "opened") -> str:
    f = ([read_label] if url in read else []) + (["CITED"] if url in cited else [])
    return f" [{', '.join(f)}]" if f else ""


def _openai(t: dict) -> list[str]:
    out = ["## Trace 1: OpenAI web search"]
    if t.get("error"):
        return out + [f"Failed: {t['error']}"]
    out.append(f"{t['model']}, {t['seconds']}s, {t.get('web_search_requests')} web search requests, "
               f"{t.get('input_tokens')} input / {t.get('output_tokens')} output tokens. OpenAI hides its real "
               f"reasoning; only its reasoning summaries (often absent) are shown. A round's results are pooled "
               f"across its queries, so which query found which result isn't exposed.")
    cited = set(t["cited"])
    opened = {s["url"] for s in t["steps"] if s["type"] in ("open_page", "find_in_page")}
    n = 0
    for s in t["steps"]:
        typ = s["type"]
        if typ == "reasoning_summary":
            out.append(f"\n### Reasoning summary\n{s['text']}")
        elif typ == "search":
            n += 1
            out.append(f"\n### Search round {n}\nQueries:\n" + "\n".join(f"- {q}" for q in s["queries"]))
            out.append(f"Results ({len(s['results'])}, OpenAI's order):")
            for r in s["results"]:
                dates = "; ".join(f"{k} {v}" for k, v in (r.get("dates") or {}).items())
                out.append(f"\n{r['rank']}. {r['title']} <{r['url']}>{_mark(r['url'], cited, opened)}"
                           + (f"\n({dates})" if dates else "") + "\n" + _quote(r["excerpt"]))
            if s.get("sources_without_excerpt"):
                out.append("\nAlso listed as sources this round, given to the model without text: "
                           + ", ".join(f"<{u}>{_mark(u, cited, opened)}" for u in s["sources_without_excerpt"]))
        elif typ == "open_page":
            out.append(f"\n### Opened page <{s['url']}>")
        elif typ == "find_in_page":
            out.append(f"\n### Searched within page <{s['url']}> for: {s.get('pattern')}")
        else:
            out.append(f"\n### {typ}: {s.get('action')}")
    out.append("\n### Answer\n" + _quote(t["answer"], "(empty)"))
    out.append("\nCited: " + (", ".join(f"<{u}>" for u in t["cited"]) or "none"))
    return out


def _open(t: dict) -> list[str]:
    out = [f"## Trace 2: {t.get('model', 'open model')} with OpenRouter web search ({t.get('engine')} engine)"]
    if t.get("error"):
        return out + [f"Failed: {t['error']}"]
    cost = f"${t['cost_usd']:.4f}" if t.get("cost_usd") is not None else "cost unknown"
    out.append(f"{t['seconds']}s, {t.get('web_search_requests')} searches, {t.get('tool_calls')} tool calls, "
               f"{t.get('input_tokens')} input / {t.get('output_tokens')} output tokens "
               f"({t.get('reasoning_tokens')} reasoning), {cost}. Independent of trace 1: it wrote its own queries "
               f"and searched {t.get('engine')}'s index, not OpenAI's. Its reasoning is shown in full.")
    out.append(f"System instructions it was given: {t['instructions']}")
    cited = set(t["cited"])
    fetched = {s["url"] for s in t["steps"] if s["type"] == "fetch"}
    n = 0
    for s in t["steps"]:
        typ = s["type"]
        if typ == "reasoning":
            out.append(f"\n### Reasoning\n{s['text']}")
        elif typ == "text":
            out.append(f"\n### Said\n{s['text']}")
        elif typ == "search":
            n += 1
            out.append(f"\n### Search {n}: {s['query']}\nResults ({len(s['results'])}, {t['engine']}'s order):")
            for r in s["results"]:
                mark = _mark(r["url"], cited, fetched, "fetched")
                if r.get("note"):
                    mark += f" [{r['note']}]"
                out.append(f"\n{r['rank']}. {r['title']} <{r['url']}>{mark}\n" + _quote(r["excerpt"]))
        elif typ == "fetch":
            status = f", HTTP {s['http_status']}" if s.get("http_status") else ""
            note = f" [{s['note']}]" if s.get("note") else ""
            head = (f"\n### Fetched <{s['url']}> ({s['title']}{status}, {len(s['content'])} chars)"
                    f"{' [CITED]' if s['url'] in cited else ''}{note}")
            if s.get("file"):
                out.append(f"{head}\nThe model read this whole page. Full text, unchanged: {s['file']} (in this folder)")
            else:
                out.append(f"{head}\n" + _quote(s["content"], "(empty)"))
        else:
            out.append(f"\n### {typ}: {s.get('item')}")
    out.append("\n### Answer\n" + _quote(t["answer"], "(empty)"))
    out.append("\nCited: " + (", ".join(f"<{u}>" for u in t["cited"]) or "none"))
    return out


def _head(trace: dict) -> str:
    return (f"# Search trace for: {trace['query']}\n"
            f"Trace id {trace['trace_id']} (country {trace['country']}). Two independent searches of the same query; "
            f"neither saw the other. Each is one sample: queries, results and citations vary between runs. "
            f"[CITED] = linked in that engine's answer; opened/fetched = it read the full page (a fetched page's "
            f"text is in the file named at its Fetched step), otherwise only the excerpt shown. Page text is quoted "
            f"with '> '.")


def render_engine(trace: dict, key: str) -> str:
    """One engine's file: openai.md or open-model.md."""
    body = _openai(trace["openai"]) if key == "openai" else _open(trace["open_model"])
    return _head(trace) + "\n\n" + "\n".join(body) + "\n"


def _status(t: dict, label: str) -> str:
    if t.get("error"):
        return f"- {label}: FAILED: {t['error']}"
    searches = [s for s in t["steps"] if s["type"] == "search"]
    queries = sum(len(s.get("queries") or [s.get("query")]) for s in searches)
    results = sum(len(s["results"]) for s in searches)
    reads = sum(s["type"] in ("open_page", "find_in_page", "fetch") for s in t["steps"])
    cost = f", ${t['cost_usd']:.4f}" if t.get("cost_usd") is not None else ""
    return (f"- {label}: {queries} queries in {len(searches)} searches, {results} results, {reads} pages opened, "
            f"{len(t['cited'])} cited; {t['seconds']}s{cost}")


def _cache_line(trace: dict) -> str:
    c = trace.get("cache") or {}
    exp = (c.get("expires") or "")[:10]
    if c.get("hit"):
        return (f"From the cache: run {c['created'][:16].replace('T', ' ')} UTC; the same query returns this trace "
                f"until {exp}, then runs fresh.")
    return f"Run just now; cached until {exp} (the same query returns this trace until then)."


def summary(trace: dict) -> str:
    oa, op = trace["openai"], trace["open_model"]
    files = trace["files"]
    lines = [f"Search trace {trace['trace_id']} for: {trace['query']}",
             _status(oa, f"OpenAI ({oa.get('model') or 'web search'})"),
             _status(op, f"{op.get('model') or 'open model'} on {op.get('engine')}"),
             _cache_line(trace),
             f"\nSaved in {trace['dir']}/:"]
    notes = {"openai": "OpenAI's trace: reasoning summaries, each round's queries, every result with the excerpt "
                       "it read, pages opened, answer, citations",
             "open_model": "the open model's trace: full reasoning, each query with its own results and excerpts, "
                           "pages fetched, answer, citations",
             "trace.json": "both traces as structured data", "raw.json": "both raw API responses"}
    for k, f in files.items():
        name = f["path"].rsplit("/", 1)[-1]
        lines.append(f"- {name} ({f['chars']:,} chars): {notes.get(k, 'full text of a page the open model fetched')}")
    lines.append("\nIn the .md files each step is a '### ' heading; each result is a line '<rank>. <title> <<url>> "
                 "[flags]' followed by its excerpt quoted with '> '.")
    return "\n".join(lines)


def render_full(trace: dict) -> str:
    """Every file's text inline: the summary, both engines' traces and every fetched page."""
    parts = [summary(trace)]
    for k, f in trace["files"].items():
        if k in ("trace.json", "raw.json"):
            continue
        with open(f["path"]) as fh:
            parts.append(f"\n\n======== {f['path']} ========\n\n" + fh.read())
    return "".join(parts)


# ---------------------------------------------------------------- test_page_change and sample_queries


def render_continuation(run: dict, model: str) -> str:
    """One continuation of a replayed conversation: everything the model did after the branch point."""
    t = {"model": model, "engine": "OpenRouter search", "seconds": run["seconds"], "steps": run["steps"],
         "answer": run["answer"], "cited": run["cited"], "cost_usd": run["cost_usd"],
         "instructions": "(as in the source trace)", "web_search_requests": None, "tool_calls": None,
         "input_tokens": None, "output_tokens": None, "reasoning_tokens": None}
    body = _open(t)[2:]   # skip the trace header lines; this is a continuation
    o = run["outcome"]
    head = (f"# {run['name']}: continuation after the branch point ({run['arm']})\n"
            f"Everything before the branch point is the source trace, unchanged; this is what the model did "
            f"next. Domain pages cited: {', '.join(o['cited']) or 'none'}; named in the answer: "
            f"{'yes' if o['named_in_answer'] else 'no'}; fetched: {', '.join(o['fetched']) or 'none'}. "
            f"{run['seconds']}s, ${run['cost_usd']:.4f}.")
    return head + "\n" + "\n".join(body) + "\n"


def render_test_summary(test: dict) -> str:
    runs = test["runs"]
    lines = [f"Page-change test {test['test_id']} for: {test['query']}",
             (f"Source trace {test['source_trace']} ({test['model']}). Branch point: the model's tool-calling step "
             f"{test['branch']['turn']} of {test['branch']['of_turns']} in the source trace; everything up to and "
             f"including these tool calls is replayed exactly as recorded:")]
    for c in test["branch"]["steps_before"]:
        lines.append(f"  - {c['kind']}: {c['args'].get('query') or c['args'].get('url')}")
    lines.append("Linked = the answer links a page on the domain, which can be a warning as easily as a pick. Quoted "
                 "under each run, verbatim: the answer's lines naming the domain, and the sentences of the model's "
                 "reasoning naming it (what it made of the page, and why it preferred others).")
    lines.append("Swapped at the branch point (control = production page re-extracted by Exa live crawl; variant = "
                 "dev page extracted the same way; the model always sees the production URL). Exa picks the "
                 "passages and their length, as it does for OpenRouter; these run about twice as long as what "
                 "OpenRouter gives, and match its excerpt about as well as OpenRouter matches itself on a rerun.")
    for s in test["swaps"]:
        if s["kind"] == "insert":
            lines.append(f"  - INSERTED {s['url']} as result #{s['rank']} for '{s['query']}' (the trace's search "
                         f"didn't return it; the result list keeps its length): control "
                         + (f"has the production page inserted the same way ({len(s['control'])} chars)"
                            if s["control"] else "is the search as recorded, without it")
                         + f"; variant has the dev page ({len(s['variant'])} chars).")
            for k in (("control", "variant") if s["control"] else ("variant",)):
                text = " ".join(s[k].split())
                lines.append(f"    {k} passage the model read: \"{text[:700]}{'…' if len(text) > 700 else ''}\"")
            continue
        where = f"result #{s['rank']} for '{s['query']}'" if s["kind"] == "search" else "fetched page"
        lines.append(f"  - {s['url']} ({where}): original {len(s['original'])} chars, control {len(s['control'])}, "
                     f"variant {len(s['variant'])}; original vs control similarity "
                     f"{s['similarity_original_vs_control']}")
        if s["kind"] == "search":
            for k in ("original", "variant"):
                text = " ".join(s[k].split())
                lines.append(f"    {k} passage the model read: \"{text[:700]}{'…' if len(text) > 700 else ''}\"")
    lines.append("")
    for arm in ("control", "variant"):
        rs = [r for r in runs if r["arm"] == arm]
        good = [r for r in rs if not r.get("error")]
        cited = sum(bool(r["outcome"]["cited"]) for r in good)
        named = sum(r["outcome"]["named_in_answer"] for r in good)
        lines.append(f"{arm}: domain page linked in {cited}/{len(good)} answers, brand named in its own words in "
                     f"{named}/{len(good)}" + (f" ({len(rs) - len(good)} failed)" if len(good) < len(rs) else ""))
        for r in rs:
            if r.get("error"):
                lines.append(f"  - {r['name']}: FAILED {r['error']}")
                continue
            o = r["outcome"]
            lines.append(f"  - {r['file']}: linked {', '.join(o['cited']) or 'none'}; named in its own words: "
                         f"{'yes' if o['named_in_answer'] else 'no'}; domain pages fetched: {len(o['fetched'])}; "
                         f"new searches: {sum(s['type'] == 'search' for s in r['steps'])}")
            lines += [f"      answer: \"{m[:300]}\"" for m in o.get("mentions") or []]
            lines += [f"      reasoning: \"{m[:300]}\"" for m in (o.get("reasoning_mentions") or [])[:5]]
            if not o.get("reasoning_mentions"):
                lines.append("      reasoning: never names the domain")
    ls = test.get("later_searches") or {}
    if ls:
        lines.append("\nSearches after the branch point (shared by all runs): " + ", ".join(
            f"{n} from {k}" for k, n in ls.items()) + ". From OpenRouter = exactly production's results and excerpts; "
            "on the domains, control keeps production's excerpt and variant gets the dev page's passages.")
    lines.append(f"\nWith {sum(r['arm'] == 'control' for r in runs)} runs per arm, small differences between arms "
                 f"can be chance. In swaps.md, check whether the swapped text says anything about the page's own "
                 f"subject: an excerpt without it can't show an effect.")
    lines.append(f"\nCost: ${test['openrouter_cost_usd']:.4f} OpenRouter + ${test['exa_cost_usd']:.4f} Exa. "
                 f"Saved in {test['dir']}/: summary.md, swaps.md (original, control and variant text of each "
                 f"swapped page), control-N.md and variant-N.md (each continuation in full), test.json.")
    return "\n".join(lines)


def render_queries(r: dict) -> str:
    lines = [f"First searches {r['model']} wrote for: {r['query']}",
             (f"{r['ok']} of {r['n']} samples ({r['no_search']} answered without searching), {r['seconds']}s, "
             f"${r['cost_usd']:.4f}. Only the first turn's queries: later ones depend on the results it gets. "
             f"{r['distinct_normalized']} distinct queries; queries per sample: "
             + ", ".join(f"{k} in {v} samples" for k, v in sorted((r.get('per_sample') or {}).items()))
             + ". 'Nx' counts identical queries; most phrasings are written once, so look at the words too.")]
    if r["errors"]:
        lines.append(f"{len(r['errors'])} samples failed: {r['errors'][0]}")
    lines.append("\nQueries (times written):")
    lines += [f"- {n}x {q}" for q, n in r["counts"]]
    lines.append("\nWords across queries (number of samples using each): "
                 + ", ".join(f"{w} {n}" for w, n in r["word_counts"]))
    fetches = [f for s in r["samples"] for f in s["fetches"]]
    if fetches:
        lines.append("\nFetched straight away instead of searching: " + ", ".join(fetches))
    return "\n".join(lines)


def render_preview(p: dict) -> tuple[str, str]:
    """-> (the full preview file, the short summary the tool returns)."""
    head = [f"Excerpt preview for {p['url']}" + (f" (dev: {p['dev_url']})" if p["dev_url"] else ""),
            ("For each query, the passages Exa picks from the page now (live crawl, Exa's own length), which is what "
             "OpenRouter passes the open model when the page is a search result. They run about twice as long as "
             "what the model gets, and Exa's choice varies between calls: OpenRouter's excerpt matches its own rerun "
             "about 75% of the time, so read these as what the page offers for the query, not the exact text. "
             "'Recorded' is what the open model actually got from this page in a cached trace; 'recorded in "
             "production now' is how much of it today's production passages contain (high = the preview is a good "
             "guide for this page and query; low = the page or Exa's index has changed since).")]
    lines, full = list(head), list(head)
    for r in p["rows"]:
        bits = [f"production {len(r['production'])} chars"]
        if r["dev"] is not None:
            bits.append(f"dev {len(r['dev'])} chars, {round(r['dev_new'] * 100)}% new wording vs production")
        if r["recorded"] is not None:
            bits.append(f"recorded {len(r['recorded'])} chars ({r['recorded_trace']}), "
                        f"{round(r['recorded_in_production'] * 100)}% of it in production now")
        lines.append(f"\n- '{r['query']}': " + "; ".join(bits))
        show = r["dev"] if r["dev"] is not None else r["production"]
        text = " ".join(show.split())
        lines.append(f"    {'dev' if r['dev'] is not None else 'production'}: \"{text[:500]}{'…' if len(text) > 500 else ''}\"")
        full.append(f"\n# {r['query']}\n" + "; ".join(bits))
        for k, label in (("recorded", "Recorded: what the open model got"), ("production", "Production now"),
                         ("dev", "Dev now")):
            if r[k] is not None:
                full.append(f"\n## {label} ({len(r[k])} chars)\n" + "\n".join("> " + ln for ln in r[k].splitlines()))
    lines.append(f"\n{p['seconds']}s, ${p['exa_cost_usd']:.4f} Exa. Every passage in full: {p['file']}")
    return "\n".join(full) + "\n", "\n".join(lines)
