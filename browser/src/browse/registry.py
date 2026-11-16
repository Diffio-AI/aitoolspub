"""Which browser tabs belong to which live server process, kept on disk for the janitor (agent-browser-janitor).

Every browser MCP server (agent-browser-mcp) writes one small JSON file per
Chrome it drives: its pid (with the process start time, so a reused pid doesn't count), and for every tab its
sessions hold, including popups those tabs opened, the session id and when it was last used. The janitor closes a
tab whose owner process is gone, or that has sat unused past the idle limit, and leaves the rest alone.

    ~/.local/state/agent-browser/live/<Steel API port>-<pid>.json   (AGENT_BROWSER_REGISTRY overrides the directory)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_DIR = Path(os.environ.get("AGENT_BROWSER_REGISTRY")
                   or Path.home() / ".local" / "state" / "agent-browser" / "live")


def browser_key(cdp_url: str) -> str:
    """The Chrome a CDP URL reaches, as the janitor names it: its port on this host (ws://localhost:3103/ -> 3103)."""
    u = urlsplit(cdp_url)
    return str(u.port or (443 if u.scheme in ("wss", "https") else 80))


def proc_start(pid: int) -> int | None:
    """The process's start time in clock ticks since boot, which tells a live owner from a reused pid."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def owner_alive(entry: dict) -> bool:
    pid = entry.get("pid")
    return isinstance(pid, int) and proc_start(pid) is not None and proc_start(pid) == entry.get("pid_start")


class Registry:
    """This process's tabs in one Chrome. Writes are best-effort: a read-only disk mustn't break a tool call."""

    def __init__(self, cdp_url: str, *, agent: str = "", directory: Path | None = DEFAULT_DIR):
        self.pid = os.getpid()
        self.path = Path(directory) / f"{browser_key(cdp_url)}-{self.pid}.json" if directory else None
        self.head = {"pid": self.pid, "pid_start": proc_start(self.pid), "cdp": cdp_url, "agent": agent,
                     "started": round(time.time())}
        self.targets: dict[str, dict] = {}

    def add(self, target_id: str, session: str, *, role: str = "main", purpose: str = "") -> None:
        now = round(time.time())
        self.targets[target_id] = {"session": session, "role": role, "purpose": purpose[:120], "opened": now,
                                   "last_used": now, "hold_until": 0}
        self._write()

    def touch(self, session: str, *, hold: float | None = None) -> None:
        """The session was just used; `hold` keeps it (seconds from now) through a long background job, 0 ends it."""
        now = time.time()
        changed = False
        for t in self.targets.values():
            if t["session"] == session:
                t["last_used"] = round(now)
                if hold is not None:
                    t["hold_until"] = round(now + hold) if hold > 0 else 0
                changed = True
        if changed:
            self._write()

    def drop(self, session: str | None = None, target_id: str | None = None) -> None:
        before = len(self.targets)
        self.targets = {k: v for k, v in self.targets.items()
                        if not ((session and v["session"] == session) or (target_id and k == target_id))}
        if len(self.targets) != before:
            self._write()

    def remove_file(self) -> None:
        if self.path:
            try:
                self.path.unlink()
            except OSError:
                pass

    def _write(self) -> None:
        if not self.path:
            return
        try:
            if not self.targets:
                self.path.unlink(missing_ok=True)
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({**self.head, "updated": round(time.time()), "targets": self.targets}))
            os.replace(tmp, self.path)
        except OSError:
            pass


def load_all(directory: Path = DEFAULT_DIR) -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(Path(directory).glob("*.json")):
        try:
            out.append((p, json.loads(p.read_text())))
        except (OSError, ValueError):
            continue
    return out
