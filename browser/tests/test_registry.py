"""The registry of each server's tabs, and the janitor that closes abandoned and idle ones."""

import asyncio
import json
import os
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from browse import janitor
from browse.registry import load_all, proc_start
from browse.sessions import Sessions
from browse.tools import Tools


def _entries(d):
    return [e for _, e in load_all(d)]


async def _until(cond, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.1)
    return cond()


async def test_close_takes_the_sessions_popups_and_its_registry_entry(cdp, site, tmp_path):
    t = Tools(Sessions(cdp, None, registry=tmp_path), None, None)
    try:
        sid = (await t.open(site + "/dir/"))["session"]
        s = t.sessions.get(sid)
        await s.page.evaluate("u => { window.open(u) }", site + "/dir/makers")
        assert await _until(lambda: sum(len(e["targets"]) for e in _entries(tmp_path)) == 2)
        roles = sorted(i["role"] for e in _entries(tmp_path) for i in e["targets"].values())
        assert roles == ["main", "popup"]
        pages = list(s.pages)
        assert len(pages) == 2
        await t.close(sid)
        assert all(p.is_closed() for p in pages)
        assert _entries(tmp_path) == []  # nothing left: the file is gone
    finally:
        await t.sessions.shutdown()


async def test_janitor_closes_a_dead_servers_tabs_and_idle_sessions(cdp, site, tmp_path, monkeypatch):
    t = Tools(Sessions(cdp, None, registry=tmp_path), None, None)
    try:
        keep = (await t.open(site + "/dir/"))["session"]
        idle = (await t.open(site + "/dir/makers"))["session"]
        held = (await t.open(site + "/dir/"))["session"]
        orphan = (await t.open(site + "/dir/makers"))["session"]
        [path] = [p for p, _ in load_all(tmp_path)]
        entry = json.loads(path.read_text())
        by_session = {i["session"]: tid for tid, i in entry["targets"].items()}
        now = time.time()
        entry["targets"][by_session[idle]]["last_used"] = now - 7200
        entry["targets"][by_session[held]]["last_used"] = now - 7200
        entry["targets"][by_session[held]]["hold_until"] = now + 600
        # The orphan's tab belongs to a server that has exited: a pid with no process.
        dead = {**entry, "pid": 2**22 - 7, "pid_start": 1,
                "targets": {by_session[orphan]: entry["targets"].pop(by_session[orphan])}}
        path.write_text(json.dumps(entry))
        (tmp_path / "9-dead.json").write_text(json.dumps(dead))
        assert proc_start(os.getpid()) == entry["pid_start"]

        port = urlsplit(cdp).port
        a = SimpleNamespace(idle=3600, stale=7200, recycle_gb=0, dry_run=False, only=None)
        await janitor.sweep({"name": "test", "api": port, "devtools": port}, load_all(tmp_path), {}, None, a)

        assert await _until(lambda: t.sessions.items[idle].page.is_closed()
                            and t.sessions.items[orphan].page.is_closed())
        assert not t.sessions.items[keep].page.is_closed()
        assert not t.sessions.items[held].page.is_closed()
        assert not (tmp_path / "9-dead.json").exists()
        try:
            t.sessions.get(idle)
            raise AssertionError("an idle-closed session should say so")
        except KeyError as e:
            assert "unused for an hour" in str(e)
    finally:
        await t.sessions.shutdown()
