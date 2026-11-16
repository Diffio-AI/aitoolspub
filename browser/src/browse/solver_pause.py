"""Pause the in-browser captcha solver (NopeCHA) for a site while a person handles a window.

NopeCHA solves every captcha it sees, at once, so a person given a handoff link never gets the chance to do one
themselves. Its settings live only in memory in its service worker (the automation build reads them from its
manifest at start; chrome.storage is empty), and they include `disabled_hosts`: page hosts it leaves alone. A pause
adds the session page's host there over CDP, and a keeper re-adds it every few seconds, because Chrome restarts an
idle extension service worker from its manifest. Each pause ends when the agent says the person is done, the
session closes, or it expires; whatever this process paused is removed again, and other processes' pauses are left
as they are (each adds and removes only its own hosts).

The pause is per host, browser-wide: another agent's window on the same site gets no automatic solving meanwhile.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time

import websockets

log = logging.getLogger(__name__)
KEEP_EVERY = 4.0  # seconds between re-applying pauses

# Finds NopeCHA's live settings object(s) (a plain object with hcaptcha_auto_solve and a boolean `enabled`; its
# schema object has the same keys with schema values) and adds/removes hosts in disabled_hosts.
_EDIT = """function(add, remove) {
  const out = [];
  for (const o of this) {
    let ok = false;
    try { ok = Object.prototype.hasOwnProperty.call(o, 'hcaptcha_auto_solve') && typeof o.enabled === 'boolean'
               && Array.isArray(o.disabled_hosts); } catch (e) {}
    if (!ok) continue;
    const hosts = o.disabled_hosts.filter(h => !remove.includes(h));
    for (const h of add) if (!hosts.includes(h)) hosts.push(h);
    o.disabled_hosts = hosts;
    out.push(hosts);
  }
  return out;
}"""


async def edit_disabled_hosts(cdp_url: str, add: list[str], remove: list[str]) -> list[list[str]] | None:
    """Add/remove hosts in NopeCHA's disabled_hosts. None when no NopeCHA service worker is running (nothing to
    pause: nothing will solve until it starts, and the keeper applies the pause then)."""
    async with websockets.connect(cdp_url, max_size=None, open_timeout=10) as ws:
        ids = itertools.count(1)

        async def send(method: str, params: dict | None = None, session: str | None = None) -> dict:
            i = next(ids)
            msg: dict = {"id": i, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            await ws.send(json.dumps(msg))
            while True:
                r = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if r.get("id") == i:
                    if "error" in r:
                        raise RuntimeError(f"{method}: {r['error'].get('message')}")
                    return r["result"]

        targets = (await send("Target.getTargets"))["targetInfos"]
        workers = [t for t in targets if t["type"] == "service_worker" and t["url"].startswith("chrome-extension://")]
        if not workers:
            return None
        results: list[list[str]] = []
        for w in workers:
            s = (await send("Target.attachToTarget", {"targetId": w["targetId"], "flatten": True}))["sessionId"]
            try:
                proto = (await send("Runtime.evaluate", {"expression": "Object.prototype"}, s))["result"]["objectId"]
                objs = (await send("Runtime.queryObjects", {"prototypeObjectId": proto}, s))["objects"]["objectId"]
                r = await send("Runtime.callFunctionOn", {
                    "objectId": objs, "functionDeclaration": _EDIT, "returnByValue": True,
                    "arguments": [{"value": add}, {"value": remove}]}, s)
                results += r["result"].get("value") or []
                await send("Runtime.releaseObjectGroup", {"objectGroup": ""}, s)
            finally:
                await send("Target.detachFromTarget", {"sessionId": s})
        return results


class SolverPause:
    """The hosts this process has paused, each with an expiry, kept applied by a background task."""

    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url
        self.until: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    def paused(self, host: str) -> bool:
        return self.until.get(host, 0) > time.time()

    async def pause(self, host: str, minutes: float) -> dict:
        self.until[host] = time.time() + minutes * 60
        applied = await edit_disabled_hosts(self.cdp_url, [host], [])
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._keep())
        return {"host": host, "minutes": minutes,
                "applied": applied is not None and any(host in hosts for hosts in applied)}

    async def resume(self, host: str) -> bool:
        if self.until.pop(host, None) is None:
            return False
        try:
            await edit_disabled_hosts(self.cdp_url, [], [host])
        except Exception as e:  # the keeper retries nothing for it; a worker restart clears it anyway
            log.warning("resuming the solver for %s failed: %s", host, e)
        return True

    async def _keep(self) -> None:
        while self.until:
            await asyncio.sleep(KEEP_EVERY)
            now = time.time()
            expired = [h for h, t in self.until.items() if t <= now]
            for h in expired:
                self.until.pop(h, None)
            try:
                await edit_disabled_hosts(self.cdp_url, list(self.until), expired)
            except Exception as e:
                log.warning("re-applying the solver pause failed: %s", e)
