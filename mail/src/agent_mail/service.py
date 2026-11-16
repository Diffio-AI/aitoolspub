"""Wiring + background loops: Pub/Sub pull, per-mailbox sync workers, catch-up, watch renewal; waiting for mail."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from . import queries
from .config import Config
from .directory import Directory
from .gmail import GmailError, GmailFactory, MailboxAPI, PubSubREST
from .send import Refused, Sender
from .store import Store, labels_str, now_ms
from .sync import Syncer
from .tokens import TokenStore

log = logging.getLogger(__name__)
WAIT_RESYNC_SECONDS = 10.0  # while someone waits for mail, sync this often even without a push


class App:
    def __init__(self, cfg: Config, gmail: Callable[[str], MailboxAPI] | None = None):
        self.cfg = cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(cfg.state_dir, 0o700)
        self.store = Store(cfg.db_path)
        self.gmail = gmail or GmailFactory(cfg.key_file)
        self.directory = Directory(cfg, self.store)
        self.syncer = Syncer(cfg, self.store, self.gmail, self.directory, on_new=self._mail_arrived)
        self.sender = Sender(cfg, self.store, self.gmail, self.directory, self.syncer)
        self.tokens = TokenStore(cfg.tokens_path)
        self._dirty: dict[str, asyncio.Event] = {}
        self._synced: dict[str, asyncio.Event] = {}
        self._arrived = asyncio.Condition()
        self._tasks: list[asyncio.Task[Any]] = []

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        for mb in self.cfg.mailboxes:
            ev = self._dirty[mb] = asyncio.Event()
            ev.set()
            self._synced[mb] = asyncio.Event()
            self._tasks.append(asyncio.create_task(self._sync_worker(mb, ev), name=f"sync-{mb}"))
        self._tasks.append(asyncio.create_task(self._catchup_loop(), name="catchup"))
        self._tasks.append(asyncio.create_task(self._watch_loop(), name="watch"))
        if self.cfg.pubsub_project:
            self._tasks.append(asyncio.create_task(self._pubsub_loop(), name="pubsub"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def request_sync(self, mailbox: str) -> None:
        if ev := self._dirty.get(mailbox):
            ev.set()

    def _mail_arrived(self, mailbox: str) -> None:
        async def notify() -> None:
            async with self._arrived:
                self._arrived.notify_all()
        try:
            asyncio.get_running_loop().create_task(notify())
        except RuntimeError:
            pass

    # ------------------------------------------------------------------ loops
    async def _sync_worker(self, mailbox: str, dirty: asyncio.Event) -> None:
        meta_at = 0.0
        backoff = 30.0
        while True:
            await dirty.wait()
            dirty.clear()
            try:
                if time.time() - meta_at > 86400:
                    await self.syncer.refresh_metadata(mailbox)
                    meta_at = time.time()
                stats = await self.syncer.sync(mailbox)
                if stats.get("added"):
                    log.info("%s: synced %s", mailbox, stats)
                exp = self.store.mailbox_state(mailbox)["watch_expiration_ms"] or 0
                if self.cfg.pubsub_project and exp < now_ms() + 3600_000:
                    await self.watch_one(mailbox)  # e.g. delegation was just granted
                backoff = 30.0
                self._synced[mailbox].set()
            except asyncio.CancelledError:
                raise
            except GmailError as e:
                self.store.update_mailbox_state(mailbox, last_error=f"{time.ctime()}: {e}"[:500])
                log.error("%s: sync failed: %s (retry in %.0fs)", mailbox, e, backoff)
                self._synced[mailbox].set()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 3600)
                dirty.set()
            except Exception:
                log.exception("%s: sync crashed", mailbox)
                self._synced[mailbox].set()
                await asyncio.sleep(60)
                dirty.set()

    async def _catchup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.catchup_minutes * 60)
            for mb in self.cfg.mailboxes:
                self.request_sync(mb)

    async def _watch_loop(self) -> None:
        if not self.cfg.pubsub_project:
            return
        while True:
            for mb in self.cfg.mailboxes:
                await self.watch_one(mb)
            await asyncio.sleep(self.cfg.watch_renew_hours * 3600)

    async def watch_one(self, mb: str) -> None:
        try:
            r = await asyncio.to_thread(lambda: self.gmail(mb).watch(self.cfg.topic_path))
            self.store.update_mailbox_state(mb, watch_expiration_ms=int(r.get("expiration", 0)))
            log.info("%s: watch ok until %s", mb, time.ctime(int(r.get("expiration", 0)) / 1000))
        except GmailError as e:
            self.store.update_mailbox_state(mb, last_error=f"{time.ctime()}: watch: {e}"[:500])
            log.error("%s: watch failed: %s", mb, e)

    async def _pubsub_loop(self) -> None:
        client = PubSubREST(self.cfg.key_file)
        sub = self.cfg.subscription_path
        backoff = 5.0
        while True:
            try:
                t0 = time.time()
                received = await asyncio.to_thread(client.pull, sub)
                backoff = 5.0
                acks = []
                for rm in received:
                    acks.append(rm["ackId"])
                    try:
                        data = json.loads(base64.b64decode(rm["message"].get("data", "")))
                        addr = str(data.get("emailAddress", "")).lower()
                    except Exception:
                        continue
                    if addr in self.cfg.mailboxes:
                        self.request_sync(addr)
                await asyncio.to_thread(client.ack, sub, acks)
                if not received and time.time() - t0 < 5:
                    await asyncio.sleep(5)  # pull returned early with nothing; don't spin
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("pubsub pull failed: %s (retry in %.0fs)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    # ------------------------------------------------------------------ on demand
    async def sync_now(self, mailboxes: list[str], full: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for mb in mailboxes:
            try:
                out[mb] = await self.syncer.sync(mb, full=full)
            except GmailError as e:
                out[mb] = {"error": str(e)[:300]}
        return out

    async def wait_for(self, *, timeout: float, since_ms: int, query: str | None, filters: dict[str, Any],
                       limit: int = 5) -> dict[str, Any]:
        """Block until mail matching the filters (received at or after `since_ms`) is in the mirror, syncing the
        mailboxes involved right away and every few seconds meanwhile (a push can lag)."""
        mbs = queries.mailboxes_arg(self.cfg, filters.get("mailbox"))
        filters = {**filters, "after": since_ms}
        started = time.monotonic()
        deadline = started + timeout
        next_sync = 0.0
        while True:
            res = queries.search(self.cfg, self.store, query, limit=limit, **filters)
            if res["results"]:
                newest = res["results"][0]
                m = queries.find_message(self.store, newest["mailbox"], newest["message_id"])
                return {"found": True, "waited_seconds": round(time.monotonic() - started, 1),
                        "matches": res["results"], "message": queries.message_detail(self.store, m)}
            now = time.monotonic()
            if now >= deadline:
                return {"found": False, "waited_seconds": round(timeout), "note": "nothing matching arrived; the "
                        "mirror was synced while waiting. Check the filters (search without them), or wait longer."}
            if now >= next_sync:
                for mb in mbs:
                    self.request_sync(mb)
                next_sync = now + WAIT_RESYNC_SECONDS
            try:
                async with self._arrived:
                    await asyncio.wait_for(self._arrived.wait(), min(next_sync, deadline) - now)
            except TimeoutError:
                pass

    async def modify(self, mailbox: str, message_ids: list[str], *, add: list[str], remove: list[str],
                     trash: bool = False, untrash: bool = False) -> dict[str, Any]:
        """Labels by name or system id (INBOX, UNREAD, STARRED, IMPORTANT, SPAM); unknown user labels are created."""
        mb = mailbox.strip().lower()
        if mb not in self.cfg.mailboxes:
            raise Refused(f"{mb} is not a mirrored mailbox")
        rows = [self.store.message(mb, i) for i in message_ids]
        if missing := [i for i, r in zip(message_ids, rows) if r is None]:
            raise Refused(f"not in {mb}: {', '.join(missing)}")
        api = self.gmail(mb)

        async def label_id(name: str, create: bool) -> str:
            ids = self.store.label_ids_named(mb, name)
            if ids != [name] or name.isupper() or not create:
                return ids[0]
            known = {lab.get("name"): lab["id"] for lab in await asyncio.to_thread(api.labels)}
            if name in known:
                self.store.add_label(mb, known[name], name)
                return known[name]
            lab = await asyncio.to_thread(api.create_label, name)
            self.store.add_label(mb, lab["id"], name)
            return str(lab["id"])

        add_ids = [await label_id(n, True) for n in add]
        remove_ids = [await label_id(n, False) for n in remove]
        try:
            if add_ids or remove_ids:
                await asyncio.to_thread(api.batch_modify, message_ids, add_ids, remove_ids)
            for i in message_ids:
                if trash:
                    await asyncio.to_thread(api.trash, i)
                if untrash:
                    await asyncio.to_thread(api.untrash, i)
        except GmailError as e:
            raise Refused(f"Gmail refused the change: {e}") from e
        for r in rows:  # mirror it now; the next sync confirms
            labs = [x for x in r["label_ids"].strip(",").split(",") if x]
            labs = [x for x in labs if x not in remove_ids] + [x for x in add_ids if x not in labs]
            if trash:
                labs = [x for x in labs if x != "INBOX"] + (["TRASH"] if "TRASH" not in labs else [])
            if untrash:
                labs = [x for x in labs if x != "TRASH"]
            self.store.x("UPDATE messages SET label_ids=? WHERE pk=?", (labels_str(labs), r["pk"]))
        self.request_sync(mb)
        return {"mailbox": mb, "changed": message_ids, "added": add_ids, "removed": remove_ids,
                **({"trashed": True} if trash else {}), **({"untrashed": True} if untrash else {})}


def status(app: App) -> dict[str, Any]:
    out = {}
    for mb in app.cfg.mailboxes:
        st = app.store.mailbox_state(mb)
        n = app.store.q1("SELECT COUNT(*) FROM messages WHERE mailbox=?", (mb,))
        out[mb] = {
            "messages": n[0] if n else 0,
            "history_id": st["history_id"],
            "last_sync": time.ctime(st["last_sync_at"] / 1000) if st["last_sync_at"] else None,
            "watch_expires": time.ctime(st["watch_expiration_ms"] / 1000) if st["watch_expiration_ms"] else None,
            "error": st["last_error"],
        }
    sends = app.store.q1("SELECT COUNT(*) n FROM send_log WHERE status='sent'")
    return {"mailboxes": out, "sent_via_service": sends["n"] if sends else 0, "now": time.ctime(now_ms() / 1000)}
