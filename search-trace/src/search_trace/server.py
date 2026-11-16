"""search-trace MCP server (stdio): one query through two independent AI web searches, returned in full."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import core, replay
from .render import render_full, render_queries, summary

INSTRUCTIONS = """\
See how AI search engines answer a question, for SEO / AEO / GEO work: the queries they write, the pages they get
back, the text they read from each, what they open, and what they cite. trace_query returns two independent full
traces, unfiltered. test_page_change replays the open model's trace up to where it saw your page and swaps in your
dev site's version, to see whether a change would alter its answer. sample_queries shows which first searches the
open model writes for a question. The tools collect data and do no analysis."""

TEST_DESCRIPTION = """\
Would a page change alter the open model's answer? Takes a trace_query trace (by trace_id, or a query, which uses
or makes one) in which DeepSeek saw a page on one of your domains, and replays DeepSeek's conversation exactly as it
was up to that point (same tool definitions, results and text as OpenRouter gave it). There it swaps the page's text
and lets DeepSeek carry on, `runs` times per arm:
- control: the production page, re-extracted with Exa's live crawl (passages matched to the same query, sized like
  the original excerpt);
- variant: your dev page (the domain mapped to its dev host, same path), extracted the same way. The model always
  sees the production URL, and the dev host in the text is rewritten to production.
Both arms extract the same way, so only the page content differs; continuing is random, hence several runs each.
After the branch point DeepSeek's new searches are run by OpenRouter's own search tool (exactly production's
results and excerpts); on your domains, control keeps production's excerpt and variant gets the dev page's passages.

Returns a summary: the branch point, the swapped text sizes, and per run whether the answer linked or named the
domain, with the answer's lines about it quoted (a link can be a warning). Files: swaps.md (original, control and
variant text), control-N.md and variant-N.md (each continuation in full), test.json.

Fails with a clear message if the trace's open model never saw a page on the domains (nothing to swap); see
sample_queries for what it searches for instead. OpenAI can't be replayed (its search runs inside OpenAI). About
10-90 seconds; roughly $0.002-0.01 per run in OpenRouter plus Exa ($0.007 per new search, $0.002 per page
extraction or fetch): a 3-run test is typically $0.02-0.10."""

QUERIES_DESCRIPTION = """\
Which searches does the open model (DeepSeek V4.1 Flash, with OpenRouter's exact search tool definitions) write
first for a question? Runs it n times only until it asks for its first search or page, and returns every query
with how often it was written, plus how many samples used each word. No search is run: about 3-10 seconds and
well under a cent for 20 samples. Only the first turn: later queries depend on what the results said (see
trace_query for those)."""

DESCRIPTION = """\
Run one question through two independent AI web searches, save both full traces as files, and return where they
are (unfiltered; read and search them however you like).

1. OpenAI web search (Responses API): each search round's queries, every result in order with the ~200-word
   excerpt the model read and its dates, pages it opened, reasoning summaries (often absent; OpenAI's real
   reasoning is hidden), the answer and the pages it cited. A round's results are pooled across its queries.
2. An open-weight model (DeepSeek V4.1 Flash) with OpenRouter's web search and page fetch tools: its full
   reasoning between steps, every query it wrote with that query's results and the excerpt it read from each,
   every page it fetched, the answer and the pages it cited. It searches the engine you pick (default Exa), not
   OpenAI's index, so it often finds different pages; its value is the visible reasoning about why it searched
   what it did and preferred the pages it chose.

Saved as a folder per trace: openai.md, open-model.md, fetch-N-<site>.md (each page the open model read whole),
trace.json (structured) and raw.json (raw API responses). Returns a few lines: what each engine did and the files.

Neither search sees the other. Each trace is one random sample (queries, results and citations vary between runs).
Traces are cached for 14 days: the same query and engine (ignoring case, spacing and trailing punctuation) returns
the cached trace instantly, and the summary says when it was run; rewording the query runs fresh. A fresh run
takes about 15-60 seconds and costs a few cents (OpenAI tokens and searches, plus about $0.01-0.03 for the open
model; firecrawl is far dearer, ~$0.25-0.50)."""


def build() -> MCPServer:
    mcp = MCPServer(name="search-trace", instructions=INSTRUCTIONS)

    @mcp.tool(description=DESCRIPTION, structured_output=False,
              annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True, idempotentHint=False))
    async def trace_query(
        query: Annotated[str, Field(description="The question exactly as a person would ask an AI assistant, "
                                                "e.g. 'What is the best app for scanning sheet music?'")],
        engine: Annotated[Literal["exa", "parallel", "perplexity", "firecrawl"], Field(
            description="Search engine behind the open model's searches (OpenAI always uses its own). exa "
                        "(default); parallel, perplexity or firecrawl to see whether a page is missing from one "
                        "index or simply wasn't chosen. Each has a different index with little overlap.")] = "exa",
        save_dir: Annotated[str | None, Field(
            description="Absolute path of a directory to save the trace folder in, e.g. your working directory "
                        "so you can read the files without extra permissions. The trace is "
                        f"copied there from the cache. Default: the cache itself, {core.STATE_DIR}")] = None,
        full: Annotated[bool, Field(
            description="Also return every file's text inline (both traces and all fetched pages). WARNING: often "
                        "30-100k+ tokens, more than many clients accept in one result; prefer reading the files.")
        ] = False,
    ) -> str:
        try:
            trace = await core.run_trace(query, engine=engine, save_dir=save_dir)
        except core.TraceError as e:
            raise ToolError(str(e))
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"search-trace failed: {type(e).__name__}: {str(e)[:300]}. Retry once.")
        return render_full(trace) if full else summary(trace)

    @mcp.tool(description=TEST_DESCRIPTION, structured_output=False,
              annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True, idempotentHint=False))
    async def test_page_change(
        domains: Annotated[dict[str, str], Field(description="Each production domain mapped to its dev host, e.g. "
                                                             "{'example.com': 'dev.example.com'}")],
        trace_id: Annotated[str | None, Field(description="A trace id from trace_query, e.g. "
                                                          "'20261114-210201-what-app-can-scan-piano-sheet-music', or "
                                                          "the absolute path of a trace folder")] = None,
        query: Annotated[str | None, Field(description="Instead of trace_id: a question; uses its cached "
                                                       "trace_query trace, or runs one")] = None,
        runs: Annotated[int, Field(description="Continuations per arm (control and variant), 1-10", ge=1, le=10)] = 3,
        save_dir: Annotated[str | None, Field(description="Absolute path of a directory to save the test folder "
                                                          f"in. Default {core.STATE_DIR / 'tests'}")] = None,
    ) -> str:
        try:
            test = await replay.test_page_change(domains, trace_id=trace_id, query=query, runs=runs,
                                                 save_dir=save_dir)
        except core.TraceError as e:
            raise ToolError(str(e))
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"test_page_change failed: {type(e).__name__}: {str(e)[:300]}. Retry once.")
        return test["summary"]

    @mcp.tool(description=QUERIES_DESCRIPTION, structured_output=False,
              annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True, idempotentHint=False))
    async def sample_queries(
        query: Annotated[str, Field(description="The question exactly as a person would ask an AI assistant")],
        n: Annotated[int, Field(description="Samples, 1-50", ge=1, le=50)] = 20,
    ) -> str:
        try:
            r = await replay.sample_queries(query, n=n)
        except core.TraceError as e:
            raise ToolError(str(e))
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"sample_queries failed: {type(e).__name__}: {str(e)[:300]}. Retry once.")
        return render_queries(r)

    return mcp


def main() -> None:
    build().run("stdio")


if __name__ == "__main__":
    main()
