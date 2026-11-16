"""The MCP server over stdio, as Claude Code runs it."""

import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = str(Path(sys.executable).with_name("agent-browser-mcp"))


async def test_stdio_server_round_trip(site, cdp, assets, tmp_path):
    params = StdioServerParameters(command=SERVER, args=["--cdp", cdp, "--no-navigator", "--state", str(tmp_path),
                                                         "--assets", str(assets), "--agent", "mcp-test"])
    async with stdio_client(params) as (r, w), ClientSession(r, w) as cs:
        init = await cs.initialize()
        assert "handoff" in (init.instructions or "")
        names = {t.name for t in (await cs.list_tools()).tools}
        assert {"open", "navigate", "goto", "read", "screenshot", "survey", "fill", "check", "press", "assets",
                "log_outcome", "sessions", "close", "handoff", "evaluate"} <= names

        def j(res):
            return json.loads(res.content[0].text)

        o = j(await cs.call_tool("open", {"url": site + "/outcome/live/submit"}))
        sv = j(await cs.call_tool("survey", {"session": o["session"], "page_text": False}))
        by = {f["name"]: f["id"] for f in sv["groups"][0]["fields"]}
        filled = j(await cs.call_tool("fill", {"session": o["session"], "values": {
            by["name"]: "Quillo AI", by["url"]: "https://quillo.example", by["email"]: "sam@example.com"}}))
        assert filled["ready"]
        pressed = j(await cs.call_tool("press", {"session": o["session"], "button": sv["groups"][0]["buttons"][0]["id"]}))
        assert "/outcome/live/p/quillo" in pressed["after"]["page_text"]
        ev = j(await cs.call_tool("evaluate", {"session": o["session"], "script": "() => ({t: document.title, n: 1 + 1})"}))
        assert ev["result"]["n"] == 2
        ho = j(await cs.call_tool("handoff", {"session": o["session"], "reason": "test"}))
        assert ho["session"] == o["session"] and "note" in ho
        shot = await cs.call_tool("screenshot", {"session": o["session"]})
        assert shot.content[0].type == "image"
        assert j(await cs.call_tool("assets", {}))["assets"] == ["logo.png", "shots/app.png"]
        bad = await cs.call_tool("survey", {"session": "nope"})
        assert bad.is_error and "no session" in bad.content[0].text
    assert list(tmp_path.rglob("*.jsonl"))


async def test_profiles_route_sessions_to_their_browser(site, cdp, tmp_path):
    # Two profiles on the test Chrome: the routing (ids, listing, per-profile tools) is what's under test.
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({"testing": {"cdp": cdp, "default": True, "about": "tests"},
                                    "production": {"cdp": cdp, "about": "real accounts"}}))
    params = StdioServerParameters(command=SERVER, args=["--profiles", str(profiles), "--no-navigator",
                                                         "--state", str(tmp_path / "packets")])
    async with stdio_client(params) as (r, w), ClientSession(r, w) as cs:
        init = await cs.initialize()
        assert "- production: real accounts" in init.instructions and "testing (default)" in init.instructions

        def j(res):
            return json.loads(res.content[0].text)

        a = j(await cs.call_tool("open", {"url": site + "/outcome/live/submit"}))
        b = j(await cs.call_tool("open", {"url": site + "/outcome/live/submit", "profile": "production"}))
        assert a["session"].startswith("s") and b["session"].startswith("production-")
        listed = {s["session"]: s.get("profile") for s in j(await cs.call_tool("sessions", {}))["sessions"]}
        assert listed == {a["session"]: "testing", b["session"]: "production"}
        assert j(await cs.call_tool("read", {"session": b["session"], "max_chars": 50}))
        bad = await cs.call_tool("open", {"url": site, "profile": "staging"})
        assert bad.is_error and "production" in bad.content[0].text
        gone = await cs.call_tool("read", {"session": "production-000000"})
        assert gone.is_error and b["session"] in gone.content[0].text
        for s in (a, b):
            assert j(await cs.call_tool("close", {"session": s["session"]}))["closed"] == s["session"]
    packets = [json.loads(line) for f in (tmp_path / "packets").rglob("*.jsonl") for line in f.read_text().splitlines()]
    assert {p["profile"] for p in packets if p["kind"] == "open"} == {"testing", "production"}
