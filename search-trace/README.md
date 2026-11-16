# search-trace

See how AI search engines answer a question, for SEO / AEO / GEO work: the queries they write, the pages they get
back, the text they read from each page, what they open, and what they cite. It collects data, unfiltered; it
doesn't analyse it.

## Tool (MCP server `search-trace`)

Four tools: `trace_query`, `test_page_change`, `sample_queries` and `preview_excerpts` (below).

`trace_query(query, engine="exa", save_dir=None, full=False)` runs the question through two independent AI web
searches, at the same time, saves both full traces as files and returns a few lines saying what each engine did
and where the files are. The agent reads and greps the files however it likes:

| File (in `<save_dir>/<trace_id>/`) | What's in it |
|---|---|
| `openai.md` | OpenAI's trace in full |
| `open-model.md` | The open model's trace in full |
| `fetch-N-<site>.md` | Full text of each page the open model fetched |
| `trace.json` | Both traces as structured data |
| `raw.json` | Both raw API responses |

Traces are cached for 14 days (`SEARCH_TRACE_CACHE_DAYS`): the same query and engine, ignoring case, spacing and
trailing punctuation, returns the cached trace instantly and the summary says when it was run; after 14 days it
runs fresh. A run where either engine failed isn't cached. The cache is `aitools/state/search-trace/<trace_id>/`
with `cache.json` as the index; expired folders are kept, as history. Changing `engine` is a different entry, and
reruns OpenAI too.

`save_dir` (absolute path): the trace folder is copied there from the cache (an agent can pass its working directory
so reading the files needs no extra permission). Without it, the returned paths are the cache's own files. `full=true` also returns every file's text inline: often 30-100k+
tokens, more than many clients accept in one result.

The two searches:

1. **OpenAI web search** (Responses API, `web_search` tool, default `gpt-6.1-terra`, country US): each search
   round's queries, every result in order with the ~200-word excerpt the model read and its published/crawled
   dates, pages opened or searched within, reasoning summaries, the answer and its citations. Not exposed: OpenAI's
   real reasoning (encrypted), and which query in a round found which result (pooled per round).
2. **An open-weight model on OpenRouter** (default `deepseek/deepseek-v4.1-flash`, reasoning on) with OpenRouter's
   `openrouter:web_search` and `openrouter:web_fetch` server tools: its full reasoning between steps, every query
   it wrote with that query's own results and the excerpt it read from each, every page it fetched (saved whole
   to a file the trace names), the answer and the pages it linked. It searches `engine`'s index, not OpenAI's.

Neither search sees the other. In the .md files page text is quoted (`> `) so its headings don't break the
trace's structure; nothing is cut.

`engine` is the search index behind the open model (OpenRouter has none of its own): `exa` (default), `parallel`,
`perplexity` or `firecrawl`. In a test on two questions (2026-11-14) DeepSeek wrote nearly the same queries on
every engine, but any two engines shared only 1-6 of ~30 result URLs, and only Exa returned the small site we were tracking.
Firecrawl cost 20-40x the others and was slower.

The open model is given a short ChatGPT-search-like brief that asks for markdown-link citations (without it,
DeepSeek links nothing); the trace prints it.

## test_page_change, sample_queries and preview_excerpts (open model only)

These drive DeepSeek ourselves (OpenRouter chat completions with function tools) instead of through OpenRouter's
server tools, using copies of OpenRouter's tools: the same names (`openrouter_web_search`, `openrouter_web_fetch`),
descriptions and parameters, and the same result JSON, recorded by having DeepSeek recite them on 2026-11-14.
OpenRouter adds no system prompt of its own. Searches the model makes after the branch point are run by OpenRouter's own search tool
(a small call that searches once with the exact query; checked word for word, Exa direct as a fallback), so their
results and excerpts are exactly production's, shared by every run in a test (~$0.007 each). Swapped page text
comes from Exa's live crawl. Key: `aitools/keys/exa.key` (or `EXA_API_KEY`).

`test_page_change(domains, trace_id | query, runs=3, save_dir)`: finds the first step in a trace's open-model run
where DeepSeek saw a page on one of the domains (a search result or a fetch), rebuilds the conversation up to it
exactly as recorded (passing its reasoning back), swaps that page's text and lets DeepSeek carry on `runs` times
per arm. Control: the production page re-extracted by Exa's live crawl (query-matched passages at Exa's own length,
or the whole page for a fetch). Variant: the dev page (same path on the mapped host), extracted
the same way, with the dev host rewritten to production; the model sees the production URL. In later searches,
control keeps production's excerpt for domain pages and variant gets the dev page's passages for that query; later
fetches of domain pages read the production or dev page. The summary shows the passage the model read in the original and the variant,
and per run whether the answer linked the domain or named the brand in its own words, quoting its lines and the
model's reasoning sentences about it; files: `swaps.md`, `control-N.md`,
`variant-N.md`, `test.json`, in `aitools/state/search-trace/tests/` or `save_dir`. `trace_id` can be a trace
folder's absolute path.

Insert mode (`insert_url`, `insert_rank=3`, `insert_query`, `insert_control="absent"`): for questions where the
page doesn't show up. It adds the page to one of the trace's searches (the first, or the one whose query matches
`insert_query`) at `insert_rank`, keeping the list's length, as if it ranked there. Variant: the dev page's passages
for that query. Control: the search as recorded (`absent`: would ranking there win anything?) or with the
production page inserted the same way (`production`: does the dev change win more once it ranks?). It can't make
the page rank; that's search, not the model.

`sample_queries(query, n=20)`: runs DeepSeek n times only until its first tool call and counts the queries (first
turn only). About 3 seconds and $0.003 for 20.

`preview_excerpts(url, domains, queries, save_dir)`: per query, the passages Exa picks from the production page and
its dev version now, plus what the open model got from the page in cached traces (default queries: every query
under which a cached trace got the URL). The fast loop while editing a page; a few seconds, ~$0.002 per page and
query. Files in `aitools/state/search-trace/previews/` or `save_dir`.

How the excerpts work (measured 2026-11-16 on 45-90 results from real traces): OpenRouter
sends the model's query to Exa search (mode auto) and passes Exa's highlights on unchanged, at Exa's adaptive length
(its docs say so; it doesn't trim or summarize). Exa's choice varies: the same query through OpenRouter hours later
matched its earlier excerpt ~75% (4-gram overlap; 40 of 83 near-identical). A dev page isn't in Exa's index, so it
can only go through `/contents`; with a live crawl and no length cap its passages match OpenRouter's ~75% too, about
twice as long. Capping the length to the original excerpt's (what the replay did before) made Exa pick other
passages (~55%). Exa's own search with OpenRouter's settings matches best (~88%) but only for indexed pages.

Limits found testing (2026-11-14, -16): re-extraction approximates OpenRouter's passages (above), so the control is
"the same kind of excerpt", not the original, and both arms see about twice as much of the page; very short
excerpts (~250 chars) can come out as table fragments; a change only shows if the passages Exa picks for that
query include it; it only works when the open model saw a page on the domain; OpenAI can't be replayed.
A test whose continuations search again takes about a minute (each new query is a ~2-5 s OpenRouter call).

## Output size

Traces are 50-150k characters per engine file (plus fetched pages), so by default the tool returns only the
summary (~1k tokens) and the files. Tested 2026-11-14: a Claude agent given `save_dir` = its working directory
read the files with no permission prompts. (Before, with everything inline, Claude Code saved over-limit results
to a file and Codex showed ~10k tokens with the middle elided.)

## Setup

See [`../setup/search-trace.md`](../setup/search-trace.md). In short:

```sh
cd search-trace && uv sync
claude mcp add --scope user search-trace -- "$PWD/.venv/bin/search-trace-mcp"
# Codex: [mcp_servers.search-trace] command = ".../search-trace-mcp", tool_timeout_sec = 600 in ~/.codex/config.toml
```

Keys: `keys/openrouter.key`, `keys/exa.key` (for `test_page_change`) and optionally `keys/openai.key` at the repo
root, or `OPENROUTER_API_KEY` / `EXA_API_KEY` / `OPENAI_API_KEY` (`SEARCH_TRACE_KEYS` for another directory).
Without an OpenAI key the OpenAI side goes through OpenRouter's Responses API (`openai/<model>`), which passes
OpenAI's `web_search` tool through and returns the same items; the trace records `via`. If one side fails (no
key, no credit) the other is still returned. The cache is `state/search-trace/` (`SEARCH_TRACE_STATE` moves it).
Models and country: `SEARCH_TRACE_OPENAI_MODEL`, `SEARCH_TRACE_OPEN_MODEL`, `SEARCH_TRACE_COUNTRY`.

## CLI

```sh
.venv/bin/search-trace "What is the best sheet music scanning app?" [--engine exa] [--save-dir DIR] [--full]
.venv/bin/search-trace --preview https://example.com/pricing --dev example.com=dev.example.com
```

## Cost and time

A fresh run takes about 15-60 seconds; a cached one is instant and free. OpenAI: roughly 15-30k input tokens and 1-3 web searches. Open model: about $0.01-0.03 with
exa, parallel or perplexity (searches are $0.005-0.007 each), $0.25-0.50 with firecrawl. Each trace is one sample; with the
cache, rewording a question is the way to get another within 14 days.
