"""The LLM browser agent, run as a subprocess that speaks the fastbrowse CLI.

Any command that accepts fastbrowse's flags (`task --cdp-url URL --target-match TOKEN --context TEXT
--max-steps N --json`) and prints its JSON result can stand in, via BROWSE_NAVIGATOR_CMD. The default is
captcha-gate's `fastbrowse_cli.py` (fastbrowse with the captcha gate and its bench-tuned settings) when it is
there, else plain `fastbrowse`. It runs on the
session's own tab: the tab's title gets a one-off token for --target-match, so the agent attaches to that
tab and no other. Several navigator runs can go at once in one browser, each in its own session window.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# fastbrowse reads its models from the environment; these are the defaults when the caller sets none. MiniMax M3.1
# Flash for every purpose, as in captcha-gate (its bench: 19/22 against 16/22 for GLM-5.3 + GLM-5.3-Flash).
DEFAULT_ENV = {
    "FASTBROWSE_LLM_PROVIDER": "openrouter",
    "FASTBROWSE_LLM_MODEL": "minimax/minimax-m3.1-flash",
}
KEY_FILES = {"TYPESAFE_API_KEY": "typesafe.key", "OPENROUTER_API_KEY": "openrouter.key",
             "TWOCAPTCHA_API_KEY": "2captcha.key"}

GATE_DIR = Path(os.environ.get("AGENT_BROWSER_GATE_DIR") or Path(__file__).resolve().parents[2] / "gate")
KEYS_DIR = Path(os.environ.get("AGENT_BROWSER_KEYS") or Path(__file__).resolve().parents[3] / "keys")
GATED_CLI = GATE_DIR / "fastbrowse_cli.py"
# OpenRouter calls go through gate/openrouter_route.py, which sends each model to the providers it cites
# its evidence correctly on (MiniMax on InferenceNet, OpenRouter's usual pick, doesn't, and runs end `stuck`). One
# proxy per server process; the gated CLI sees FASTBROWSE_LLM_BASE_URL set and uses it instead of starting its own.
ROUTE_SCRIPT = GATE_DIR / "openrouter_route.py"


def _die_with_parent() -> None:
    # The MCP server can be killed without running atexit; the proxy must not outlive it.
    import ctypes
    ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG

BASE_CONTEXT = (
    "You are the hands of another agent, which writes all text and decides what gets submitted. Your job is to "
    "get around websites and read them. Do not type into a site's forms or submit anything unless the task "
    "explicitly gives you the exact text to enter; a search box is fine when the task needs a search. When the "
    "task is to reach a form, keep going until the form's fields are on screen: an explainer, pricing or "
    "'get started' page is a step on the way, not the end. Stop as soon as you are on the page the task asks "
    "for and say where you are."
)
# allow_actions: the caller (Claude) asked the navigator to carry out the task itself, not just reach a page.
ACT_CONTEXT = (
    "You are the hands of another agent, which has asked you to carry out this task on the site yourself: click, "
    "choose, type and submit as the task requires. Type only text the task gives you (or that is plainly implied, "
    "like a search for something the task names); never invent names, messages or account details. Do exactly "
    "what the task asks and nothing more: no extra purchases, deletions, posts or settings changes. If the task "
    "needs information it doesn't give you (a password, a choice it didn't make), stop and say what is missing. "
    "When done, say what you did and what the page shows now."
)


@dataclass
class NavResult:
    status: str
    answer: str | None
    final_url: str | None
    error: str | None
    steps: int
    dollars: float | None
    seconds: float
    wall: Any = None
    captchas: dict | None = None
    raw_tail: str | None = None

    def brief(self, max_answer: int = 2000) -> dict:
        out = {k: v for k, v in self.__dict__.items() if v not in (None, "", [], {}) and k != "raw_tail"}
        if self.answer and len(self.answer) > max_answer:
            out["answer"] = self.answer[:max_answer] + f"… [{len(self.answer)} chars; read() the page for the rest]"
        if self.status in ("error", "no_json") and self.raw_tail:
            out["stderr_tail"] = self.raw_tail
        if self.error and "HTTP 402" in self.error:
            out["note"] = ("The navigator's model account is out of credit, so navigate won't work until it's topped up. "
                           "survey, fill, press, read and goto don't use it: drive the page with those meanwhile.")
        return out


class Navigator:
    def __init__(self, cdp_url: str, *, keys_dir: Path | None = KEYS_DIR, cmd: str | None = None):
        self.cdp_url = cdp_url
        self.keys_dir = keys_dir
        default = [sys.executable, str(GATED_CLI)] if GATED_CLI.is_file() else [str(Path(sys.executable).with_name("fastbrowse"))]
        cmd = cmd or os.environ.get("BROWSE_NAVIGATOR_CMD")
        self.cmd = shlex.split(cmd) if cmd else default
        self._route: subprocess.Popen | None = None
        self._route_url: str | None = None

    def _route_base(self) -> str:
        """The routing proxy's base URL, started on first use (one per server process)."""
        if self._route is None or self._route.poll() is not None:
            self._route = subprocess.Popen([sys.executable, str(ROUTE_SCRIPT)], stdout=subprocess.PIPE, text=True,
                                           preexec_fn=_die_with_parent)
            self._route_url = self._route.stdout.readline().strip()
        return self._route_url

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        for k, v in DEFAULT_ENV.items():
            env.setdefault(k, v)
        for var, fname in KEY_FILES.items():
            if not env.get(var) and self.keys_dir and (self.keys_dir / fname).is_file():
                env[var] = (self.keys_dir / fname).read_text().strip()
        if env["FASTBROWSE_LLM_PROVIDER"] == "openrouter" and not env.get("FASTBROWSE_LLM_BASE_URL") \
                and ROUTE_SCRIPT.is_file():
            env["FASTBROWSE_LLM_PROVIDER"] = "custom"
            env["FASTBROWSE_LLM_BASE_URL"] = self._route_base()
            env["FASTBROWSE_LLM_API_KEY"] = env.get("OPENROUTER_API_KEY", "")
        return env

    def close(self) -> None:
        if self._route and self._route.poll() is None:
            self._route.terminate()

    async def run(self, task: str, *, target_match: str, context: str | None = None, max_steps: int = 30,
                  timeout: float = 630, trace: Path | None = None, act: bool = False,
                  cdp_url: str | None = None) -> NavResult:
        """`cdp_url` overrides the server's browser: one navigator (and one routing proxy) serves every profile."""
        base = ACT_CONTEXT if act else BASE_CONTEXT
        args = [*self.cmd, task, "--cdp-url", cdp_url or self.cdp_url, "--target-match", target_match,
                "--context", base + (f"\n\nFrom the caller: {context}" if context else ""),
                "--max-steps", str(max_steps), "--json"]
        if act:
            args.append("--authorize")
        if trace:
            args += ["--trace", str(trace)]
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        proc = await asyncio.create_subprocess_exec(*args, env=self._env(), stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return NavResult("timeout", None, None, f"navigator ran past {timeout:.0f}s", 0, None, loop.time() - t0)
        secs = round(loop.time() - t0, 1)
        try:
            d = json.loads(out.decode() or "null") or {}
        except json.JSONDecodeError:
            return NavResult("no_json", None, None, "navigator printed no JSON result", 0, None, secs,
                             raw_tail=(err.decode() or out.decode())[-600:])
        lines = (d.get("cost") or {}).get("lines") or []
        dollars = round(sum(x.get("dollars") or 0 for x in lines), 4) if lines else None
        return NavResult(str(d.get("status")), d.get("answer"), d.get("final_url"), d.get("error"),
                         len(d.get("steps") or []), dollars, secs, wall=d.get("wall"),
                         captchas=_captchas(d.get("gate")), raw_tail=err.decode()[-600:])


def _captchas(record: dict | None) -> dict | None:
    """The gated CLI's record, cut to what the caller can use: what was solved or given up on, and retries."""
    attempts = (record or {}).get("attempts") or []
    gate = attempts[-1].get("gate") if attempts else None
    if not gate or not (gate.get("solved") or gate.get("gave_up") or gate.get("unsolved")):
        return None
    out = {k: gate[k] for k in ("solved", "gave_up", "unsolved", "twocaptcha_dollars") if gate.get(k)}
    if len(attempts) > 1:
        out["attempts"] = len(attempts)
    return out
