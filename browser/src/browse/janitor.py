"""agent-browser-janitor: keeps the shared Steel Chromes from filling up with tabs nobody uses.

Run every few minutes by a systemd user timer (systemd/agent-browser-janitor.timer). Each run, for every Steel
container Docker is running, it lists the Chrome's tabs and closes:

  - a tab whose owner, a browser MCP server listed in the registry (registry.py), has exited;
  - a tab whose session has been unused for --idle (an hour), unless it is held for a recording or a handoff;
  - a tab no live server lists (a server from before the registry, a navigator's stray tab, a script) whose URL and
    title haven't changed for --stale (two hours). Blank tabs and the agent-browser launcher are left alone.

Chrome also grows on its own over days (renderers, service workers), so when a container's memory is over
--recycle-gb and nothing in it is in use (no tab of a live server, nothing unowned changed in the last half hour),
it restarts the container (cleanly: steel-init.sh closes Chrome first, so sign-ins are kept), at most every 6 hours.

    agent-browser-janitor --dry-run      # say what it would do
    agent-browser-janitor                # do it
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import websockets

from .registry import DEFAULT_DIR, load_all, owner_alive

LAUNCHER_TITLE = "agent-browser launcher (keep open)"
STATE = DEFAULT_DIR.parent / "janitor.json"
ACTIVE_WINDOW = 30 * 60      # an unowned tab that changed this recently means someone is using the browser
RECYCLE_EVERY = 6 * 3600     # at most one restart per container in this long
RECYCLE_MIN_UPTIME = 3600    # and never within an hour of the container starting


def log(msg: str) -> None:
    print(msg, flush=True)


def steel_containers() -> list[dict]:
    out = subprocess.run(["docker", "ps", "--format", "{{json .}}"], capture_output=True, text=True, check=True)
    found = []
    for line in out.stdout.splitlines():
        c = json.loads(line)
        if "steel-browser" not in c.get("Image", ""):
            continue
        ports = {int(inner): int(host) for host, inner in re.findall(r":(\d+)->(\d+)/tcp", c.get("Ports", ""))}
        if 3000 in ports and 9223 in ports:
            found.append({"name": c["Names"], "api": ports[3000], "devtools": ports[9223]})
    return found


def memory_gb() -> dict[str, float]:
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],
                         capture_output=True, text=True, check=True)
    units = {"B": 1e-9, "KiB": 2**10 / 2**30, "MiB": 2**20 / 2**30, "GiB": 1.0, "TiB": 1024.0}
    res = {}
    for line in out.stdout.splitlines():
        name, _, usage = line.partition("\t")
        m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", usage.strip())
        if m:
            res[name] = float(m.group(1)) * units.get(m.group(2), 0)
    return res


def started_at(name: str) -> float:
    out = subprocess.run(["docker", "inspect", name, "--format", "{{.State.StartedAt}}"], capture_output=True,
                         text=True, check=True).stdout.strip()
    from datetime import datetime
    return datetime.fromisoformat(re.sub(r"\.(\d{6})\d*", r".\1", out).replace("Z", "+00:00")).timestamp()


class Browser:
    """Just enough CDP on the browser endpoint: list the tabs, close one."""

    def __init__(self, devtools_port: int):
        no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with no_proxy.open(f"http://127.0.0.1:{devtools_port}/json/version", timeout=10) as r:
            path = urlsplit(json.load(r)["webSocketDebuggerUrl"]).path
        self.url = f"ws://127.0.0.1:{devtools_port}{path}"
        self.ws = None
        self.n = 0

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, max_size=None, open_timeout=15)
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def send(self, method: str, **params) -> dict:
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), 20))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message"))
                return msg.get("result", {})

    async def pages(self) -> list[dict]:
        return [t for t in (await self.send("Target.getTargets"))["targetInfos"] if t.get("type") == "page"]

    async def close(self, target_id: str) -> None:
        await self.send("Target.closeTarget", targetId=target_id)


def is_blank(t: dict) -> bool:
    return t.get("url") in ("about:blank", "") and t.get("title") in ("about:blank", "")


async def sweep(c: dict, registries: list[tuple[Path, dict]], state: dict, mem: float | None, a) -> None:
    now = time.time()
    st = state.setdefault(c["name"], {"targets": {}})
    seen = st["targets"]
    # The registry files for this Chrome: servers name it by the port they connect to (Steel's API port, normally).
    mine = [(p, e) for p, e in registries
            if urlsplit(e.get("cdp", "")).port in (c["api"], c["devtools"])]
    owned: dict[str, tuple[dict, dict]] = {}
    for _, e in mine:
        for tid, info in (e.get("targets") or {}).items():
            owned[tid] = (e, info)

    async with Browser(c["devtools"]) as b:
        pages = await b.pages()
        for t in pages:  # how long each tab has looked the same
            sig = f"{t.get('url')}|{t.get('title')}"
            rec = seen.get(t["targetId"])
            if not rec or rec["sig"] != sig:
                seen[t["targetId"]] = {"sig": sig, "first_seen": (rec or {}).get("first_seen", now),
                                       "last_change": now}
        live_ids = {t["targetId"] for t in pages}
        for tid in list(seen):
            if tid not in live_ids:
                del seen[tid]

        closing: list[tuple[dict, str]] = []
        in_use = False
        for t in pages:
            tid = t["targetId"]
            if t.get("title") == LAUNCHER_TITLE:
                continue
            if tid in owned:
                e, info = owned[tid]
                if not owner_alive(e):
                    closing.append((t, f"owner pid {e.get('pid')} ({e.get('agent') or 'server'}) has exited"))
                elif now - info.get("last_used", now) > a.idle and now > info.get("hold_until", 0):
                    closing.append((t, f"session {info.get('session')} unused for "
                                       f"{(now - info['last_used']) / 60:.0f} min"))
                else:
                    in_use = True
                continue
            if is_blank(t):
                continue
            unchanged = now - seen[tid]["last_change"]
            if unchanged > a.stale:
                closing.append((t, f"no live server owns it; unchanged for {unchanged / 3600:.1f} h"))
            elif unchanged < ACTIVE_WINDOW:
                in_use = True

        # Never close a Chrome's last tab: a browser with no windows may quit.
        if closing and len(closing) >= len(pages):
            closing = closing[: len(pages) - 1]
        for t, why in closing:
            log(f"{c['name']}: {'would close' if a.dry_run else 'closing'} {t.get('url', '')[:100]!r} - {why}")
            if not a.dry_run:
                try:
                    await b.close(t["targetId"])
                    seen.pop(t["targetId"], None)
                except Exception as ex:  # noqa: BLE001 - closed meanwhile
                    log(f"{c['name']}: close failed: {ex}")

    if not a.dry_run:
        for p, e in mine:
            if not owner_alive(e):
                p.unlink(missing_ok=True)

    log(f"{c['name']}: {len(pages)} tabs, closed {len(closing)}, memory {mem if mem is not None else '?'} GB"
        f"{', in use' if in_use else ''}")

    if mem is not None and mem > a.recycle_gb and not in_use and a.recycle_gb > 0:
        last = st.get("last_recycle", 0)
        up = now - started_at(c["name"])
        if now - last < RECYCLE_EVERY or up < RECYCLE_MIN_UPTIME:
            log(f"{c['name']}: over {a.recycle_gb} GB but restarted recently; leaving it")
            return
        log(f"{c['name']}: {'would restart' if a.dry_run else 'restarting'} ({mem:.1f} GB, nothing in use)")
        if not a.dry_run:
            subprocess.run(["docker", "restart", "-t", "45", c["name"]], check=False)
            st["last_recycle"] = now
            st["targets"] = {}


async def run(a) -> None:
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    containers = everything = steel_containers()
    if a.only:
        containers = [c for c in containers if c["name"] in a.only]
    registries = load_all()
    mem = memory_gb()
    for c in containers:
        try:
            await sweep(c, registries, state, round(mem[c["name"]], 1) if c["name"] in mem else None, a)
        except Exception as ex:  # noqa: BLE001 - one broken Chrome mustn't stop the others' sweep
            log(f"{c['name']}: skipped: {type(ex).__name__}: {ex}")
    state = {k: v for k, v in state.items() if k in {c["name"] for c in everything}}
    if not a.dry_run:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(STATE)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--idle", type=float, default=3600, help="seconds a session may sit unused (default 3600)")
    ap.add_argument("--stale", type=float, default=7200,
                    help="seconds an unowned tab may stay unchanged (default 7200)")
    ap.add_argument("--recycle-gb", type=float, default=6.0,
                    help="restart a container over this much memory when nothing in it is in use (0: never)")
    ap.add_argument("--only", nargs="*", help="container names to sweep (default: every Steel container)")
    ap.add_argument("--dry-run", action="store_true", help="report, close nothing, restart nothing")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
