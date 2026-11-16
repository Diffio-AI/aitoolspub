# /// script
# requires-python = ">=3.13"
# dependencies = ["fastbrowse==0.9.0", "websockets>=14", "httpx>=0.28"]
# ///
"""`run.py`'s setup behind fastbrowse's own command line, so anything that runs `fastbrowse` can run this instead.

    BROWSE_NAVIGATOR_CMD="python captcha-gate/fastbrowse_cli.py"   # the browse MCP's navigator (its default when
                                                                   # this file exists)

It takes the flags of `fastbrowse TASK` that the bots use and prints the same `--json` result, plus `gate`: what the
gate did on each attempt. Everything else is `run.py`: MiniMax M3.1 Flash through openrouter_route.py (unless
FASTBROWSE_LLM_* or a caller's proxy is already set), bot_check_above 0.97, barren_reads 3, no dollar cap unless
given, one retry on a `blocked` the gate had already solved, and the run in the foreground tab.
With AGENT_BROWSER_NO_CAPTCHAS=1 (set by `agent-browser-mcp --no-captchas`) it runs without the captcha gate.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import run as gated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task")
    parser.add_argument("--start")
    parser.add_argument("--context")
    parser.add_argument("--cdp-url", required=True, help="the browser's CDP websocket, e.g. ws://localhost:3101/")
    parser.add_argument("--target-match", help="drive the open tab whose title or URL contains this (implies --attach)")
    parser.add_argument("--attach", action="store_true", help="accepted for fastbrowse compatibility; needs --target-match")
    parser.add_argument("--foreground", action="store_true", help="accepted for fastbrowse compatibility; always on")
    parser.add_argument("--authorize", action="store_true", help="allow submit, pay, delete and send")
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--max-dollars", type=float)
    parser.add_argument("--max-seconds", type=float, default=600)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args()
    result, record = asyncio.run(gated.run(
        args.task, start=args.start, context=args.context, cdp_url=args.cdp_url, target_match=args.target_match,
        authorize=args.authorize, max_steps=args.max_steps, max_dollars=args.max_dollars,
        max_seconds=args.max_seconds, trace=args.trace,
        use_gate=os.environ.get("AGENT_BROWSER_NO_CAPTCHAS") != "1",
    ))
    if args.json:
        print(json.dumps({**result.model_dump(mode="json"), "gate": record}))
    else:
        print(f"status: {result.status}")
        if result.error:
            print(f"error: {result.error}")
        print(result.answer or "")
    sys.exit(0 if result.status == "complete" else 1)


if __name__ == "__main__":
    main()
