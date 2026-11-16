# /// script
# requires-python = ">=3.13"
# dependencies = ["fastbrowse==0.9.0", "websockets>=14", "httpx>=0.28"]
# ///
"""fastbrowse on a Steel instance, with captchas solved by the gate before each step.

    browser/.venv/bin/python browser/gate/run.py "Task..." --start https://example.com/ [--steel 3103] [--authorize] [--json]

Keys come from the environment, else from the key files in aitools/keys (openrouter.key,
2captcha.key). The LLM is MiniMax M3.1 Flash, through openrouter_route.py, unless FASTBROWSE_LLM_* variables are
already set.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastbrowse import run_task, traced
from fastbrowse.config import Config, StallRules, Thresholds
from fastbrowse.models import Authorization, Limits, RunResult

import openrouter_route
from captcha_gate import CaptchaGate

# Key files (typesafe.key, openrouter.key, 2captcha.key): $AGENT_BROWSER_KEYS, else aitools/keys.
KEYS_DIR = Path(os.environ.get("AGENT_BROWSER_KEYS") or Path(__file__).resolve().parents[2] / "keys")

KEY_FILES = {"TYPESAFE_API_KEY": "typesafe.key", "OPENROUTER_API_KEY": "openrouter.key", "TWOCAPTCHA_API_KEY": "2captcha.key"}

GATED_BOT_CHECK_ABOVE = 0.97
"""fastbrowse's default is 0.70."""

BARREN_READS = 3
"""Reads of an unchanged page that add nothing, before the run must act or stop; fastbrowse's default is 2."""

# Open-weight models only: MiniMax M3.1 Flash for every purpose. It takes images, so recover and verify (which send
# screenshots) need no second model, and through openrouter_route.py it cites its evidence reliably, which fastbrowse
# needs or the run ends `stuck` on a page that shows the answer. Bench round 9, gate on, every demo twice: 19/22
# passed, against 16/22 for GLM-5.3 planning + MiniMax reading + GLM-5.3-Flash (which also overran fastbrowse's
# 1,600-token cap on verify twice). The only failures left were GeeTest v3 (unsolvable, 2) and one page timeout.
MODEL_DEFAULTS = {
    "FASTBROWSE_LLM_PROVIDER": "openrouter",
    "FASTBROWSE_LLM_MODEL": "minimax/minimax-m3.1-flash",
}


def load_environment() -> None:
    for name, file in KEY_FILES.items():
        path = KEYS_DIR / file
        if not os.environ.get(name) and path.exists():
            os.environ[name] = path.read_text().strip()
    for name, value in MODEL_DEFAULTS.items():
        os.environ.setdefault(name, value)
    # Send OpenRouter calls through openrouter_route.py, which prefers the providers each model cites correctly on.
    if os.environ["FASTBROWSE_LLM_PROVIDER"] == "openrouter" and not os.environ.get("FASTBROWSE_LLM_BASE_URL"):
        os.environ["FASTBROWSE_LLM_PROVIDER"] = "custom"
        os.environ["FASTBROWSE_LLM_BASE_URL"] = openrouter_route.start()
        os.environ["FASTBROWSE_LLM_API_KEY"] = os.environ["OPENROUTER_API_KEY"]


async def run(
    task: str,
    *,
    start: str | None = None,
    context: str | None = None,
    steel_port: int = 3103,
    cdp_url: str | None = None,
    target_match: str | None = None,
    authorize: bool = False,
    max_steps: int | None = 30,
    max_dollars: float | None = None,
    max_seconds: float | None = 600,
    foreground: bool = True,
    use_gate: bool = True,
    retry_blocked: bool = True,
    trace: Path | None = None,
    bot_check_above: float | None = None,
    login_above: float | None = None,
) -> tuple[RunResult, dict]:
    """One task with the gate. Returns fastbrowse's result and a record of what the gate did, per attempt.

    `cdp_url` overrides `steel_port`; with `target_match` the run drives the open tab whose title or URL contains it
    (fastbrowse's attach), and the gate watches that tab too."""
    load_environment()
    cdp_url = cdp_url or f"ws://localhost:{steel_port}/"
    # With the gate on, every captcha it knows is solved before the agent looks, and one it cannot solve stops the
    # run itself (CaptchaUnsolved). fastbrowse's own bot-check verdict is then mostly Jev misreading a page about
    # captchas, or a solved widget, as a wall, so it has to be near certain (bench round 2 -> 3).
    if use_gate and bot_check_above is None:
        bot_check_above = GATED_BOT_CHECK_ABOVE
    walls = {k: v for k, v in (("bot_check_above", bot_check_above), ("login_required_above", login_above)) if v is not None}
    # One more read of an unchanged page before giving up: an open-weight reader sometimes returns nothing the first
    # time on a page that shows the answer, and fastbrowse's 2 then ends the run `stuck` (bench round 6, Text demo).
    config = Config(thresholds=Thresholds(**walls), stall=StallRules(barren_reads=BARREN_READS))
    attempts = []
    for attempt in (1, 2):
        gate = CaptchaGate(cdp_url, os.environ.get("TWOCAPTCHA_API_KEY"), attach_match=target_match) if use_gate else None
        if gate:
            await gate.start()
        began = time.monotonic()
        try:
            with traced() as fastbrowse_trace:
                result = await run_task(
                    task,
                    start=start,
                    # The agent is told nothing about captchas: a line saying they are handled made Jev read pages as
                    # bot walls (bench round 1: 7 of 11 blocked with it, 3 of 11 without the gate at all).
                    context=context,
                    cdp_url=cdp_url,
                    attach=bool(target_match),
                    target_match=target_match,
                    foreground=foreground,
                    check_access=gate,
                    authorization=Authorization(irreversible_actions=authorize),
                    # No dollar cap by default: OpenRouter sometimes returns a call without its cost, and fastbrowse
                    # then stops a capped run as budget_exceeded (bench rounds 5 and 7). Steps and time bound it.
                    limits=Limits(max_steps=max_steps, max_dollars=max_dollars, max_seconds=max_seconds),
                    config=config,
                )
        finally:
            if gate:
                await gate.close()
        if gate and gate.unsolved and result.status == "error":
            # The gate stopped the run at a captcha it could not solve: that is a bot wall, reported as one.
            result = result.model_copy(update={
                "status": "blocked",
                "error": f"bot check: unsolved captcha ({', '.join(sorted({u['type'] for u in gate.unsolved}))}) "
                         f"at {gate.unsolved[0]['url']}; " + "; ".join(g["reason"] for g in gate.summary()["gave_up"]),
            })
        attempts.append({
            "attempt": attempt,
            "status": result.status,
            "seconds": round(time.monotonic() - began, 1),
            "agent_dollars": round(sum(line.dollars or 0 for line in result.cost.lines), 5),
            "gate": gate.summary() if gate else None,
            "gate_events": gate.events if gate else [],
        })
        if trace:
            with trace.open("a") as f:
                f.write(json.dumps({"task": task, "start": start, **attempts[-1], "error": result.error}) + "\n")
                for event in fastbrowse_trace:
                    f.write(json.dumps({"attempt": attempt, "fastbrowse": event}, default=str) + "\n")
        # fastbrowse can call a page `blocked` after the gate solved its captcha: reCAPTCHA leaves its challenge
        # frame in the DOM, hidden, and the run reads it as a live bot check. The solve is in the profile's
        # cookies and the site's session by then, so one fresh attempt usually goes straight through.
        wall = result.wall
        if not (retry_blocked and gate and result.status == "blocked" and wall and wall.kind == "bot_check"):
            break
        origin = f"{urlsplit(wall.origin).scheme}://{urlsplit(wall.origin).netloc}"
        if not gate.solved_on(origin):
            break
        if attempt == 1:
            print(f"[captcha-gate] run ended blocked on {origin} after the gate solved a captcha there; retrying once", file=sys.stderr)
    return result, {"attempts": attempts}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task")
    parser.add_argument("--start")
    parser.add_argument("--context")
    parser.add_argument("--steel", type=int, default=3103, help="Steel API port (aitools: 3103)")
    parser.add_argument("--authorize", action="store_true", help="allow submit, pay, delete and send")
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--max-dollars", type=float, help="cap agent model spend; a call OpenRouter reports without a cost then ends the run budget_exceeded")
    parser.add_argument("--max-seconds", type=float, default=600)
    parser.add_argument("--background", action="store_true", help="work in a background tab")
    parser.add_argument("--no-gate", action="store_true", help="plain fastbrowse, for comparison")
    parser.add_argument("--no-retry", action="store_true")
    parser.add_argument("--json", action="store_true", help="print the full result and the gate's record")
    parser.add_argument("--trace", type=Path, help="append one JSON line per attempt, then fastbrowse's own trace")
    parser.add_argument("--bot-check-above", type=float, help="Jev confidence to call a wall a bot check (fastbrowse: 0.70)")
    parser.add_argument("--login-above", type=float, help="Jev confidence to call a page a sign-in wall (fastbrowse: 0.70)")
    args = parser.parse_args()
    result, record = asyncio.run(run(
        args.task, start=args.start, context=args.context, steel_port=args.steel, authorize=args.authorize,
        max_steps=args.max_steps, max_dollars=args.max_dollars, max_seconds=args.max_seconds, foreground=not args.background,
        use_gate=not args.no_gate, retry_blocked=not args.no_retry, trace=args.trace,
        bot_check_above=args.bot_check_above, login_above=args.login_above,
    ))
    if args.json:
        print(json.dumps({"result": result.model_dump(mode="json"), **record}, indent=1))
    else:
        last = record["attempts"][-1]
        print(f"status: {result.status}  attempts: {len(record['attempts'])}  seconds: {last['seconds']}  agent: ${last['agent_dollars']}")
        if last["gate"]:
            print(f"captchas: {json.dumps(last['gate'])}")
        if result.error:
            print(f"error: {result.error}")
        print(result.answer or "")
    sys.exit(0 if result.status == "complete" else 1)


if __name__ == "__main__":
    main()
