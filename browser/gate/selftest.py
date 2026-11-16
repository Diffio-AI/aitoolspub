# /// script
# requires-python = ">=3.13"
# dependencies = ["websockets>=14", "httpx>=0.28"]
# ///
"""Check the gate alone, with no agent: open a 2captcha demo, let the gate solve it, click Check, read the verdict.

    ../.venv/bin/python selftest.py mtcaptcha geetest-v4 ... [--steel 3103]

A pass here means the solver and the token hand-off work; a failure in a full run is then the agent's.
"""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from captcha_gate import CaptchaGate

KEYS = Path(os.environ.get("AGENT_BROWSER_KEYS") or Path(__file__).resolve().parents[2] / "keys")

CLICK_CHECK = r"""(() => {
  const b = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Check');
  if (!b) return false; b.click(); return true;
})()"""

VERDICT = r"""(() => {
  const t = document.body.innerText;
  const m = t.match(/Captcha is passed successfully!|[^\n]*(solved incorrectly|incorrect captcha|please try again)[^\n]*/i);
  return m ? m[0].slice(0, 160) : null;
})()"""


async def check(gate: CaptchaGate, slug: str) -> dict:
    target = (await gate._send("Target.createTarget", {"url": "about:blank"}))["targetId"]
    gate._preexisting.discard(target)
    session = await gate._session(target)
    await gate._send("Target.activateTarget", {"targetId": target})
    began = time.monotonic()
    await gate._send("Page.navigate", {"url": f"https://2captcha.com/demo/{slug}"}, session)
    await asyncio.sleep(6)
    first = len(gate.events)
    await gate()
    gate_seconds = time.monotonic() - began
    clicked = await gate._eval(session, CLICK_CHECK)
    verdict = None
    for _ in range(10):
        await asyncio.sleep(1)
        if verdict := await gate._eval(session, VERDICT):
            break
    await gate._send("Target.closeTarget", {"targetId": target})
    return {"demo": slug, "verdict": verdict, "clicked": clicked, "seconds": round(gate_seconds, 1),
            "gate": [{k: v for k, v in e.items() if k not in ("at", "url")} for e in gate.events[first:]]}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("demos", nargs="+")
    parser.add_argument("--steel", type=int, default=3103)
    args = parser.parse_args()
    key = os.environ.get("TWOCAPTCHA_API_KEY") or (KEYS / "2captcha.key").read_text().strip()
    async with CaptchaGate(f"ws://localhost:{args.steel}/", key, log=lambda m: None) as gate:
        for slug in args.demos:
            print(json.dumps(await check(gate, slug)), flush=True)
        print(json.dumps({"twocaptcha_solves": gate.solves, "twocaptcha_dollars": round(gate.dollars, 5)}))


asyncio.run(main())
