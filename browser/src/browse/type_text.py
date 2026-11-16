"""Type text into the field a person has clicked in an agent browser window, from a terminal.

The live viewer can't always paste (before steel/viewer's patch is live, and from browsers that block clipboard
reads), but a terminal can: run this, paste into its hidden prompt, and the text goes into whatever field has focus
in the matching window, as typed text (CDP Input.insertText). The text is never printed or logged.

    agent-browser-type ghost                       # the window whose URL or title contains "ghost" (default profile)
    agent-browser-type --to production ghost
    printf %s "$TOKEN" | agent-browser-type --stdin --to production ghost

Click into the field in the live viewer first. MATCH must pick out exactly one window: every agent's windows are in
the same browser, and text meant for your sign-in must not land in someone else's form.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import itertools
import json
import sys

import websockets

from .import_cookies import agent_cdp

FOCUSED = """(() => {
  let el = document.activeElement, frames = 0;
  while (el && el.tagName === 'IFRAME') {
    try { el = el.contentDocument.activeElement; frames++; } catch (e) { return {tag: 'IFRAME', crossOrigin: true}; }
  }
  if (!el || el === document.body) return null;
  return {tag: el.tagName, type: el.type || null, name: el.name || el.id || null, frames,
          editable: el.isContentEditable || /^(INPUT|TEXTAREA)$/.test(el.tagName)};
})()"""


async def type_into(cdp_url: str, match: str, text: str, enter: bool = False) -> str:
    async with websockets.connect(cdp_url, max_size=None, open_timeout=10) as ws:
        ids = itertools.count(1)

        async def send(method: str, params: dict | None = None, session: str | None = None) -> dict:
            i = next(ids)
            msg: dict = {"id": i, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            await ws.send(json.dumps(msg))
            while True:
                r = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if r.get("id") == i:
                    if "error" in r:
                        raise RuntimeError(f"{method}: {r['error'].get('message')}")
                    return r["result"]

        pages = [t for t in (await send("Target.getTargets"))["targetInfos"] if t["type"] == "page"]
        hits = [t for t in pages if match.lower() in (t["url"] + " " + t["title"]).lower()]
        if len(hits) != 1:
            listing = "\n".join(f"  {t['title'][:50]:<50} {t['url'][:80]}" for t in pages)
            sys.exit(f"{len(hits)} windows match {match!r}; give a MATCH that picks exactly one:\n{listing}")
        page = hits[0]
        s = (await send("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True}))["sessionId"]
        try:
            focus = (await send("Runtime.evaluate", {"expression": FOCUSED, "returnByValue": True}, s))["result"]
            f = focus.get("value")
            if not f:
                sys.exit(f"nothing has focus in {page['title'][:60]!r}: click into the field in the live viewer first")
            if f.get("crossOrigin"):
                pass  # a field inside a cross-origin frame (a payment or sign-in widget): insertText still reaches it
            elif not f.get("editable"):
                sys.exit(f"the focused element in {page['title'][:60]!r} is a {f['tag']}, not a text field: click "
                         "into the field first")
            await send("Input.insertText", {"text": text}, s)
            if enter:
                key = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13}
                await send("Input.dispatchKeyEvent", {"type": "keyDown", "text": "\r", **key}, s)
                await send("Input.dispatchKeyEvent", {"type": "keyUp", **key}, s)
        finally:
            await send("Target.detachFromTarget", {"sessionId": s})
        what = "a field in a cross-origin frame" if f.get("crossOrigin") else \
            f"{f['tag'].lower()}{' type=' + f['type'] if f.get('type') else ''}{' ' + f['name'] if f.get('name') else ''}"
        return f"typed {len(text)} characters into {what} on {page['title'][:60]!r}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 epilog="Click into the field in the live viewer first.")
    ap.add_argument("match", help="text in the window's URL or title that picks out exactly one window")
    ap.add_argument("--to", metavar="AGENT_PROFILE", help="agent browser profile (default: the default one)")
    ap.add_argument("--cdp", help="the browser's CDP websocket, instead of --to")
    ap.add_argument("--stdin", action="store_true", help="read the text from stdin instead of a hidden prompt")
    ap.add_argument("--enter", action="store_true", help="press Enter after typing")
    a = ap.parse_args()
    text = sys.stdin.read() if a.stdin else getpass.getpass("Paste the text (hidden), then press Enter: ")
    text = text.rstrip("\r\n")
    if not text:
        sys.exit("nothing to type")
    print(asyncio.run(type_into(a.cdp or agent_cdp(a.to), a.match, text, a.enter)))


if __name__ == "__main__":
    main()
