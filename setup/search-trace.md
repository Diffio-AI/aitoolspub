# Set up the search-trace tool (for the agent)

Steps for the agent doing the install. Read [common.md](common.md) first: it covers where the repo goes,
how keys are stored and how to register an MCP server with each agent.

**What gets installed:** a stdio MCP server (`search-trace/`) for SEO / AEO / GEO work. For a question, it runs
two independent AI web searches and saves both full traces: the queries each engine wrote, every result with the
excerpt it read, the pages it opened, and what it cited. The two searches are OpenAI's web search and an
open-weight model (DeepSeek) using OpenRouter's search tools. It can also replay the open model with a
development version of a page swapped in (or inserted into results it didn't rank in), to see whether a change
would alter the answer, and preview which passages of a page the search engine hands the model for each
query. Nothing runs in the background; the cache is `state/search-trace/`.

## 1. Ask the user

Along with the questions in common.md, ask for:

1. **OpenRouter API key** (required): https://openrouter.ai/keys, with some credit. It runs the open model and its
   searches (about $0.01-0.03 a trace) and, without an OpenAI key, the OpenAI side too.
2. **Exa API key** (required for `test_page_change` and `preview_excerpts`): https://dashboard.exa.ai/api-keys.
   It re-reads the production and development pages. Plain traces work without it.
3. **OpenAI API key** (optional): https://platform.openai.com/api-keys. With it, the OpenAI side calls OpenAI
   directly; without it, the same model and web search tool are called through OpenRouter. The trace is the same
   (OpenRouter passes OpenAI's web search through); only billing and rate limits differ.

## 2. Install

```sh
cd "$AITOOLS/search-trace" && uv sync
```

Keys in `$AITOOLS/keys/` (see common.md): `openrouter.key`, `exa.key`, and `openai.key` if given.

Quick check, which spends a few cents:

```sh
.venv/bin/search-trace "What is the best free app for scanning sheet music?"
```

It should print one line per engine with queries, results and citations. The line starting `OpenAI` must not
say `error`. A line with `402` means the OpenRouter account needs credit.

## 3. Register with the agents

The stdio command is `$AITOOLS/search-trace/.venv/bin/search-trace-mcp`. Register it under the name
**`search-trace`** (common.md, step 4). In Codex set `tool_timeout_sec = 600`: a fresh trace takes up to a minute,
and a page-change test several.

## 4. Check it

From a new agent session, call `sample_queries` with any question and `n=5` (about 3 seconds, a fraction of a
cent). It returns the first searches the open model writes.

## 5. Tell the user

- **Using it:** ask any agent something like "use search-trace to see how AI search answers 'best CRM for
  plumbers', and whether my site shows up." It saves both traces as files and reads them for you.
- **Testing a change:** "use search-trace's test_page_change for example.com with my dev site dev.example.com":
  it compares the answers the model gives when it reads the live page against the dev page. If the page doesn't
  show up for a question, it can insert it into the results as if it ranked there.
- **While editing a page:** "preview the excerpts for https://example.com/pricing, dev at dev.example.com": in a
  few seconds it shows which passages the search engine would hand the model for each query, live and dev.
- **Cost:** a fresh trace is a few cents; the same question within 14 days comes from the cache, free.
- Keys are in `$AITOOLS/keys/`. Defaults (models, country, cache length) can be changed with environment
  variables; see `search-trace/README.md`.
