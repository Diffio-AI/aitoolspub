"""agent-mail CLI: serve, status, sync, tokens, and a quick search from the shell."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .config import load_config


def _app(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from .service import App

    return App(load_config(args.config))


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .mcp_app import build_asgi

    logging.basicConfig(level=os.environ.get("AGENT_MAIL_LOG", "INFO"),
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = _app(args)

    async def main() -> None:
        await app.start()
        server = uvicorn.Server(uvicorn.Config(build_asgi(app), host=app.cfg.listen_host, port=app.cfg.listen_port,
                                               log_level="warning", lifespan="on"))
        try:
            await server.serve()
        finally:
            await app.stop()

    asyncio.run(main())


def cmd_status(args: argparse.Namespace) -> None:
    from .service import status

    print(json.dumps(status(_app(args)), indent=2))


def cmd_sync(args: argparse.Namespace) -> None:
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    app = _app(args)
    mbs = [m.lower() for m in args.mailbox] or list(app.cfg.mailboxes)

    async def main() -> None:
        for mb in mbs:
            await app.syncer.refresh_metadata(mb)
        print(json.dumps(await app.sync_now(mbs, full=args.full), indent=2))

    asyncio.run(main())


def cmd_token(args: argparse.Namespace) -> None:
    app = _app(args)
    if args.action == "issue":
        token = app.tokens.issue("client", args.name, Path(args.out).expanduser() if args.out else None)
        if args.out:
            print(f"token for {args.name} written to {args.out}")
        else:
            print(token)
    elif args.action == "revoke":
        print("revoked" if app.tokens.revoke("client", args.name) else "no such client")
    else:
        print("\n".join(app.tokens.names()) or "(no tokens)")


def cmd_search(args: argparse.Namespace) -> None:
    from . import queries

    app = _app(args)
    res = queries.search(app.cfg, app.store, args.query, limit=args.limit, mailbox=args.mailbox)
    for r in res["results"]:
        print(f"{r['date']}  {r['mailbox']:<36} {r['from'][:40]:<40} {r['subject']}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="agent-mail", description=__doc__)
    ap.add_argument("--config", default=None, help="config.toml (default: $AGENT_MAIL_CONFIG, then ./config.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the service (systemd does this)").set_defaults(fn=cmd_serve)
    sub.add_parser("status", help="per-mailbox counts, sync and watch state").set_defaults(fn=cmd_status)
    s = sub.add_parser("sync", help="one-off sync (normally the service does it)")
    s.add_argument("mailbox", nargs="*")
    s.add_argument("--full", action="store_true")
    s.set_defaults(fn=cmd_sync)
    t = sub.add_parser("token", help="client tokens for the MCP endpoint")
    t.add_argument("action", choices=["issue", "revoke", "list"])
    t.add_argument("name", nargs="?", default="claude-code")
    t.add_argument("--out", help="write the token to this file (mode 600) instead of printing it")
    t.set_defaults(fn=cmd_token)
    q = sub.add_parser("search", help="search the mirror from the shell")
    q.add_argument("query", nargs="?")
    q.add_argument("--mailbox")
    q.add_argument("--limit", type=int, default=20)
    q.set_defaults(fn=cmd_search)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
