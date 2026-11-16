"""A reader/writer lock per browser, shared by every process that drives it.

The navigator (fastbrowse) attaches to a session's tab by a title token, and it lets any tab opened *without an
opener* during its run join that run. Sessions therefore open their windows through a launcher page
(`window.open`), so every session tab has an opener and no navigator adopts it; see sessions.py.

What is left for the lock:
- navigator runs hold it **shared**: any number of them run at once, each on its own session's window;
- opening a tab with no opener (the launcher itself, once per browser start, or the fallback when the launcher
  can't open a window) holds it **exclusive**, so it can't land inside someone's navigator run.

`AGENT_BROWSER_SERIAL_NAVIGATOR=1` makes navigator runs exclusive again (one at a time per browser), the old
behaviour, in case a fastbrowse version ever adopts opener tabs too.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

LOCK_DIR = Path(os.environ.get("AGENT_BROWSER_LOCK_DIR", Path.home() / ".cache" / "agent-browser" / "locks"))
SERIAL_NAVIGATOR = os.environ.get("AGENT_BROWSER_SERIAL_NAVIGATOR", "") not in ("", "0")


@asynccontextmanager
async def browser_lock(cdp_url: str, local: asyncio.Lock | None = None, *, shared: bool = False):
    """Held across processes (flock). `local` serializes exclusive holders inside this process too."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCK_DIR / (hashlib.sha1(cdp_url.encode()).hexdigest()[:16] + ".lock")
    if shared and SERIAL_NAVIGATOR:
        shared = False
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    if local is not None and not shared:
        await local.acquire()
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            await asyncio.to_thread(fcntl.flock, fd, mode)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    finally:
        if local is not None and not shared:
            local.release()
