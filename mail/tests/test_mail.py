from __future__ import annotations

import asyncio
import json
from email.message import EmailMessage
from pathlib import Path

import pytest
from conftest import A, A_ALIAS, B, EXT, Env
from starlette.testclient import TestClient

from agent_mail import queries
from agent_mail.mcp_app import build_asgi
from agent_mail.send import Refused

HDRS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def test_sync_search_grep_read(env: Env) -> None:
    box = env.world.box(A)
    box.deliver(EXT, "Your verification code", "Hi Pat,\n\nYour code is 482913. It expires in 10 minutes.")
    box.deliver("news@shop.test", "Weekly deals", "Big savings on everything. Unsubscribe anytime.")
    env.world.box(B).deliver(EXT, "Invoice 2026-11", "Invoice attached. Total due: $120.00")
    await env.sync()

    res = queries.search(env.cfg, env.store, "verification")
    assert [r["subject"] for r in res["results"]] == ["Your verification code"]
    newest = queries.search(env.cfg, env.store, None, limit=2)["results"]
    assert len(newest) == 2 and newest[0]["subject"] == "Invoice 2026-11"
    assert queries.search(env.cfg, env.store, None, mailbox=B)["results"][0]["mailbox"] == B
    assert queries.search(env.cfg, env.store, "unbalanced \"quote")["note"]  # bad FTS falls back to words

    g = queries.grep(env.cfg, env.store, r"\b\d{6}\b")
    assert len(g["results"]) == 1 and g["results"][0]["matches"][0]["match"] == "482913"
    assert "code is 482913" in g["results"][0]["matches"][0]["context"]
    assert queries.grep(env.cfg, env.store, r"total due: \$\d+", ignore_case=True)["results"]
    assert not queries.grep(env.cfg, env.store, r"TOTAL DUE", ignore_case=False)["results"]
    with pytest.raises(Refused):
        queries.grep(env.cfg, env.store, "(unclosed")

    mid = res["results"][0]["message_id"]
    m = queries.message_detail(env.store, queries.find_message(env.store, None, mid), headers=True)
    assert "482913" in m["body"] and m["headers"]["subject"] == "Your verification code"
    assert queries.list_mailboxes(env.cfg, env.store, env.app.directory)["mailboxes"][0]["unread_in_inbox"] == 2


async def test_relative_dates_and_filters(env: Env) -> None:
    env.world.box(A).deliver(EXT, "Fresh", "just now")
    await env.sync()
    assert queries.search(env.cfg, env.store, None, after="5m")["results"]
    assert not queries.search(env.cfg, env.store, None, before="1 day ago")["results"]
    with pytest.raises(Refused):
        queries.to_ms("next tuesday")


async def test_send_reply_forward(env: Env) -> None:
    await env.sync()
    sent = await env.app.sender.send_new("t", A_ALIAS, [B], "Hello B", "first message", bcc=[EXT])
    assert sent["from"] == A_ALIAS and sent["bcc"] == [EXT]
    raw = env.world.sent_raw[-1][1]
    assert "Hello" in raw["From"] and raw["Bcc"] == EXT
    await env.sync()
    got = queries.search(env.cfg, env.store, None, mailbox=B, direction="in")["results"][0]
    assert got["subject"] == "Hello B"

    r = await env.app.sender.reply("t", "thanks!", message_id=got["message_id"], mailbox=B)
    assert r["to"] == [A_ALIAS] and r["from"] == B
    out = env.world.sent_raw[-1][1]
    assert out["Subject"] == "Re: Hello B" and out["In-Reply-To"]

    f = await env.app.sender.forward("t", got["message_id"], [EXT], mailbox=B, note="FYI")
    fwd = env.world.sent_raw[-1][1]
    assert f["status"] == "sent" and fwd["Subject"] == "Fwd: Hello B"
    assert "FYI" in fwd.get_body(("plain",)).get_content() and "first message" in fwd.get_body(("plain",)).get_content()

    with pytest.raises(Refused):
        await env.app.sender.send_new("t", "nobody@else.test", [B], "x", "y")
    with pytest.raises(Refused):
        await env.app.sender.send_new("t", A, ["not an address"], "x", "y")


async def test_send_disabled_mailbox(tmp_path: Path) -> None:
    from conftest import make_config

    raw = make_config(tmp_path)
    raw["mailbox"][1]["send"] = False
    env = Env(tmp_path, raw)
    with pytest.raises(Refused, match="turned off"):
        await env.app.sender.send_new("t", B, [A], "x", "y")


async def test_attachments_roundtrip(env: Env, tmp_path: Path) -> None:
    f = tmp_path / "report.txt"
    f.write_text("quarterly numbers")
    await env.app.sender.send_new("t", A, [B], "Report", "see attached", attachments=[{"path": str(f)}])
    await env.sync()
    got = queries.search(env.cfg, env.store, None, mailbox=B, has_attachment=True)["results"]
    assert got and got[0]["attachments"]
    m = queries.message_detail(env.store, queries.find_message(env.store, B, got[0]["message_id"]))
    part = m["attachments"][0]["part_id"]
    assert await env.app.syncer.attachment_bytes(B, got[0]["message_id"], part) == b"quarterly numbers"
    with pytest.raises(Refused):  # the service's own state can't be mailed out
        await env.app.sender.send_new("t", A, [B], "x", "y", attachments=[{"path": str(env.cfg.db_path)}])


async def test_html_links_are_kept(env: Env) -> None:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "noreply@app.test", A, "Confirm your email"
    m.set_content("Confirm your account by clicking the button.")
    m.add_alternative('<p>Hi</p><a href="https://app.test/verify?token=abc123">Confirm</a>', subtype="html")
    env.world.box(A).add(m, ["INBOX", "UNREAD"])
    await env.sync()
    got = queries.search(env.cfg, env.store, "confirm")["results"][0]
    d = queries.message_detail(env.store, queries.find_message(env.store, A, got["message_id"]))
    assert "https://app.test/verify?token=abc123" in d["links"]
    assert queries.grep(env.cfg, env.store, r"token=\w+")["results"]


async def test_wait_for_sees_new_mail(env: Env) -> None:
    await env.sync()
    await env.app.start()
    try:
        async def later() -> None:
            await asyncio.sleep(0.5)
            env.world.box(A).deliver("robot@signup.test", "Welcome aboard", "Your magic link: https://signup.test/m/1")
            env.app.request_sync(A)  # what a Pub/Sub push does

        task = asyncio.create_task(later())
        res = await env.app.wait_for(timeout=10, since_ms=queries.since_default(1), query=None,
                                     filters={"sender": "signup.test", "mailbox": None})
        await task
        assert res["found"] and res["message"]["links"] == ["https://signup.test/m/1"]
        none = await env.app.wait_for(timeout=1, since_ms=queries.since_default(1), query=None,
                                      filters={"sender": "never.test", "mailbox": None})
        assert none["found"] is False
    finally:
        await env.app.stop()


async def test_modify_labels_and_trash(env: Env) -> None:
    env.world.box(A).deliver(EXT, "Read me", "x")
    await env.sync()
    mid = queries.search(env.cfg, env.store, None, mailbox=A)["results"][0]["message_id"]
    res = await env.app.modify(A, [mid], add=["STARRED", "Projects/Agent"], remove=["UNREAD", "INBOX"])
    assert "STARRED" in res["added"] and "UNREAD" in res["removed"]
    row = env.store.message(A, mid)
    assert ",STARRED," in row["label_ids"] and ",UNREAD," not in row["label_ids"]
    assert queries.search(env.cfg, env.store, None, label="Projects/Agent")["results"]
    await env.app.modify(A, [mid], add=[], remove=[], trash=True)
    assert not queries.search(env.cfg, env.store, "read")["results"]  # trash is hidden by default
    assert queries.search(env.cfg, env.store, "read", include_spam_trash=True)["results"]
    await env.sync()  # the fake's history agrees with the local update
    assert ",TRASH," in env.store.message(A, mid)["label_ids"]


async def test_history_deletes_and_label_changes(env: Env) -> None:
    rec = env.world.box(A).deliver(EXT, "Temp", "x")
    await env.sync()
    env.world.box(A).relabel(rec["id"], ["INBOX"])
    await env.sync()
    assert ",UNREAD," not in env.store.message(A, rec["id"])["label_ids"]


def rpc(client: TestClient, token: str | None, method: str, params: dict, id_: int = 1):  # type: ignore[no-untyped-def]
    h = dict(HDRS)
    if token:
        h["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", headers=h, content=json.dumps({"jsonrpc": "2.0", "id": id_, "method": method,
                                                               "params": params}))


def test_http_auth_and_tools(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.world.box(A).deliver(EXT, "Hello there", "body text 1234")
    token = env.app.tokens.issue("client", "claude-code", None)
    with TestClient(build_asgi(env.app), base_url="http://127.0.0.1:8766") as c:
        c.portal.call(env.sync)  # type: ignore[attr-defined]
        assert rpc(c, None, "tools/list", {}).status_code == 401
        assert rpc(c, "amt_wrong", "tools/list", {}).status_code == 401
        init = rpc(c, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "t", "version": "0"}})
        assert init.status_code == 200, init.text
        names = {t["name"] for t in rpc(c, token, "tools/list", {}).json()["result"]["tools"]}
        assert {"list_mailboxes", "search", "grep", "read_message", "read_thread", "wait_for", "send", "reply",
                "forward", "modify", "get_attachment", "sync_now"} <= names

        def call(name: str, **args):  # type: ignore[no-untyped-def]
            res = rpc(c, token, "tools/call", {"name": name, "arguments": args}).json()["result"]
            text = res["content"][0]["text"]
            return text if res.get("isError") else json.loads(text)

        assert {m["mailbox"] for m in call("list_mailboxes")["mailboxes"]} == {A, B}
        hit = call("search", query="hello")["results"][0]
        assert hit["subject"] == "Hello there"
        assert call("grep", pattern=r"\d{4}")["results"][0]["matches"][0]["match"] == "1234"
        assert call("read_message", message_id=hit["message_id"])["body"].startswith("body text")
        assert "not a mirrored mailbox" in call("search", mailbox="zzz@x.test")
        sent = call("send", from_addr=A, to=[EXT], subject="s", body="b")
        assert sent["status"] == "sent"
        assert c.get("/healthz").status_code == 200
