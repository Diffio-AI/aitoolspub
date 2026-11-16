"""search-trace from the shell: the same output as the MCP tool.

  search-trace "What is the best app for scanning sheet music?" [--engine exa|parallel|perplexity|firecrawl]
               [--save-dir DIR] [--full]
  search-trace "<question>" --sample-queries 20
  search-trace ["<question>"] --test-change example.com=dev.example.com [--trace-id ID] [--runs 3]
  search-trace --preview https://example.com/pricing [--dev example.com=dev.example.com]
               [--queries "query one" "query two"]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import core, replay
from .render import render_full, render_queries, summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="search-trace", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?")
    ap.add_argument("--engine", default=core.DEFAULT_ENGINE, choices=core.ENGINES)
    ap.add_argument("--save-dir", help=f"directory for the trace folder (default {core.STATE_DIR})")
    ap.add_argument("--full", action="store_true", help="print every file's text, not just the summary")
    ap.add_argument("--sample-queries", type=int, metavar="N", help="sample the open model's first searches N times")
    ap.add_argument("--test-change", nargs="+", metavar="PROD=DEV", help="replay with dev pages, e.g. "
                    "example.com=dev.example.com")
    ap.add_argument("--trace-id", help="for --test-change: the trace to replay (else the query's trace)")
    ap.add_argument("--runs", type=int, default=3, help="for --test-change: continuations per arm")
    ap.add_argument("--insert", metavar="URL", help="for --test-change: insert URL into a search it didn't show up in")
    ap.add_argument("--rank", type=int, default=3, help="for --insert: position in the results")
    ap.add_argument("--insert-query", help="for --insert: the trace search to insert into (default the first)")
    ap.add_argument("--insert-control", default="absent", choices=("absent", "production"),
                    help="for --insert: control without the page, or with the production page inserted")
    ap.add_argument("--preview", metavar="URL", help="passages the search engine hands the model from URL, per query")
    ap.add_argument("--dev", nargs="+", metavar="PROD=DEV", help="for --preview: also preview the dev version")
    ap.add_argument("--queries", nargs="+", help="for --preview: the search queries (default: from cached traces)")
    a = ap.parse_args(argv)
    try:
        if a.preview:
            domains = dict(x.split("=", 1) for x in a.dev or [])
            save = str(Path(a.save_dir).expanduser().resolve()) if a.save_dir else None
            print(asyncio.run(replay.preview_excerpts(a.preview, domains=domains, queries=a.queries,
                                                      save_dir=save))["summary"])
            return 0
        if a.sample_queries:
            print(render_queries(asyncio.run(replay.sample_queries(a.query or "", n=a.sample_queries))))
            return 0
        if a.test_change:
            domains = dict(x.split("=", 1) for x in a.test_change)
            save = str(Path(a.save_dir).expanduser().resolve()) if a.save_dir else None
            print(asyncio.run(replay.test_page_change(domains, trace_id=a.trace_id, query=a.query, runs=a.runs,
                                                      save_dir=save, insert_url=a.insert, insert_rank=a.rank,
                                                      insert_query=a.insert_query,
                                                      insert_control=a.insert_control))["summary"])
            return 0
        if not a.query:
            ap.error("give a query")
        save = str(Path(a.save_dir).expanduser().resolve()) if a.save_dir else None
        trace = core.run(a.query, engine=a.engine, save_dir=save)
        print(render_full(trace) if a.full else summary(trace))
    except core.TraceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
