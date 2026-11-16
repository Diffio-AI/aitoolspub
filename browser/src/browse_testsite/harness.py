"""Test harness pieces: the test site on a free port and a throwaway Chromium with a CDP endpoint.

The browser is a real Chrome process (not a Playwright-launched one) so the navigator subprocess can attach to
it over CDP exactly as it attaches to Steel. `BROWSE_TEST_CDP` points tests at another browser instead.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from .app import LOG, build


NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def testsite(host: str = "127.0.0.1", port: int | None = None):
    """Yields the base URL; the app's LOG holds what was submitted."""
    port = port or free_port()
    server = uvicorn.Server(uvicorn.Config(build(), host=host, port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield f"http://{'127.0.0.1' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}"
    finally:
        server.should_exit = True
        t.join(5)


def chrome_binary() -> str:
    if os.environ.get("BROWSE_TEST_CHROME"):
        return os.environ["BROWSE_TEST_CHROME"]
    # Playwright's own Chromium; the sync API can't be used here (tests run inside an event loop).
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache/ms-playwright"))
    found = sorted(cache.glob("chromium-*/chrome-linux64/chrome"), key=lambda p: int(p.parts[-3].split("-")[1]))
    if not found:
        raise RuntimeError("no Playwright Chromium; run `.venv/bin/python -m playwright install chromium`")
    return str(found[-1])


@contextmanager
def chrome(headless: bool = True):
    """Yields a CDP websocket URL for a fresh Chrome with a throwaway profile."""
    if os.environ.get("BROWSE_TEST_CDP"):
        yield os.environ["BROWSE_TEST_CDP"]
        return
    prof = Path(tempfile.mkdtemp(prefix="browse-test-chrome-"))
    port = free_port()
    args = [chrome_binary(), f"--remote-debugging-port={port}", f"--user-data-dir={prof}", "--no-first-run",
            "--no-default-browser-check", "--disable-background-networking", "--window-size=1280,900",
            # Ubuntu blocks Chrome's user-namespace sandbox (AppArmor); Playwright passes this too. Local pages only.
            "--no-sandbox",
            "about:blank"]
    if headless:
        args.insert(1, "--headless=new")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws = None
        for _ in range(100):
            try:
                with NO_PROXY.open(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                    ws = json.load(r)["webSocketDebuggerUrl"]
                    break
            except OSError:
                time.sleep(0.1)
        if not ws:
            raise RuntimeError("chrome did not start")
        yield ws
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(prof, ignore_errors=True)


def submissions(site: str | None = None) -> list[dict]:
    return [e for e in LOG if site is None or e["site"] == site]
