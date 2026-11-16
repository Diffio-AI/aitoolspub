"""Mailbox -> SQLite sync: a full backfill, then incremental `history.list` from the stored historyId."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config
from .directory import Directory
from .gmail import GmailError, HistoryExpired, MailboxAPI
from .parse import Parsed, parse_message
from .store import Store, now_ms

log = logging.getLogger(__name__)
FETCH_CONCURRENCY = 6
FETCH_BATCH = 500  # full syncs ingest in batches, so a big backfill is searchable while it runs


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120] or "file"


class Syncer:
    def __init__(self, cfg: Config, store: Store, gmail: Callable[[str], MailboxAPI], directory: Directory,
                 on_new: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.store = store
        self.gmail = gmail
        self.directory = directory
        self.on_new = on_new or (lambda mailbox: None)
        self.locks: dict[str, asyncio.Lock] = {}

    def lock(self, mailbox: str) -> asyncio.Lock:
        return self.locks.setdefault(mailbox, asyncio.Lock())

    async def _call(self, mailbox: str, fn: str, *args: Any) -> Any:
        return await asyncio.to_thread(lambda: getattr(self.gmail(mailbox), fn)(*args))

    # ------------------------------------------------------------------ metadata
    async def refresh_metadata(self, mailbox: str) -> None:
        """Labels and send-as aliases."""
        self.store.replace_labels(mailbox, await self._call(mailbox, "labels"))
        send_as = await self._call(mailbox, "send_as")
        aliases = [
            (s["sendAsEmail"].lower(), s.get("displayName") or "")
            for s in send_as
            if not s.get("isPrimary") and s.get("verificationStatus", "accepted") == "accepted"
        ]
        self.store.replace_aliases(mailbox, aliases, "sendas")
        self.directory.reload()

    # ------------------------------------------------------------------ sync entry
    async def sync(self, mailbox: str, full: bool = False) -> dict[str, int]:
        async with self.lock(mailbox):
            return await self.sync_locked(mailbox, full)

    async def sync_locked(self, mailbox: str, full: bool = False) -> dict[str, int]:
        state = self.store.mailbox_state(mailbox)
        try:
            widened = (state["backfilled_days"] or 0) < self.cfg.backfill_window
            if full or not state["history_id"] or widened:
                stats = await self._full(mailbox)
            else:
                try:
                    stats = await self._incremental(mailbox, state["history_id"])
                except HistoryExpired:
                    log.warning("%s: historyId expired, running full resync", mailbox)
                    stats = await self._full(mailbox)
        except GmailError as e:
            self.store.update_mailbox_state(mailbox, last_error=f"{time.ctime()}: {e}"[:500])
            raise
        self.store.update_mailbox_state(mailbox, last_sync_at=now_ms(), last_error=None)
        return stats

    async def _full(self, mailbox: str) -> dict[str, int]:
        state = self.store.mailbox_state(mailbox)
        profile = await self._call(mailbox, "profile")
        history_id = str(profile["historyId"])
        query = ""
        if self.cfg.backfill_days > 0:
            query = f"after:{int(time.time() - self.cfg.backfill_days * 86400)}"
        # Spam and trash too, so a search can find mail that landed there (include_spam_trash).
        ids = await self._call(mailbox, "list_message_ids", query)
        known = self.store.known_ids(mailbox)
        new = [i for i in ids if i not in known]
        existing = [i for i in ids if i in known]
        log.info("%s: full sync, %d listed, %d new", mailbox, len(ids), len(new))
        added = 0
        for i in range(0, len(new), FETCH_BATCH):
            added += await self._ingest_all(mailbox, await self._fetch(mailbox, new[i:i + FETCH_BATCH], "full"))
            if len(new) > FETCH_BATCH:
                log.info("%s: backfill %d/%d", mailbox, min(i + FETCH_BATCH, len(new)), len(new))
        for i in range(0, len(existing), FETCH_BATCH):
            for m in await self._fetch(mailbox, existing[i:i + FETCH_BATCH], "minimal"):
                self.store.set_labels(mailbox, m["id"], m.get("labelIds", []))
        self.store.update_mailbox_state(
            mailbox, history_id=history_id, last_full_sync_at=now_ms(),
            backfilled_days=max(self.cfg.backfill_window, state["backfilled_days"] or 0),
        )
        return {"listed": len(ids), "added": added}

    async def _incremental(self, mailbox: str, start_id: str) -> dict[str, int]:
        records, latest = await self._call(mailbox, "history", start_id)
        added: list[str] = []
        labels: dict[str, list[str]] = {}
        deleted: set[str] = set()
        for rec in records:
            for a in rec.get("messagesAdded", []):
                added.append(a["message"]["id"])
            for key in ("labelsAdded", "labelsRemoved"):
                for c in rec.get(key, []):
                    labels[c["message"]["id"]] = c["message"].get("labelIds", [])
            for d in rec.get("messagesDeleted", []):
                deleted.add(d["message"]["id"])
        known = self.store.known_ids(mailbox)
        new = [i for i in dict.fromkeys(added) if i not in known and i not in deleted]
        n = await self._ingest_all(mailbox, await self._fetch(mailbox, new, "full"))
        for gid, labs in labels.items():
            if gid not in deleted and self.store.has_message(mailbox, gid):
                self.store.set_labels(mailbox, gid, labs)
        for gid in deleted:
            self.store.delete_message(mailbox, gid)
        self.store.update_mailbox_state(mailbox, history_id=str(latest))
        return {"history": len(records), "added": n}

    async def _fetch(self, mailbox: str, ids: list[str], fmt: str) -> list[dict[str, Any]]:
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(i: str) -> dict[str, Any] | None:
            async with sem:
                return await self._call(mailbox, "get_message", i, fmt)

        out = await asyncio.gather(*(one(i) for i in ids))
        return [m for m in out if m]

    async def _ingest_all(self, mailbox: str, msgs: list[dict[str, Any]]) -> int:
        n = 0
        for m in sorted(msgs, key=lambda m: int(m.get("internalDate", 0))):
            if await self.ingest(mailbox, m):
                n += 1
        if n:
            self.on_new(mailbox)
        return n

    # ------------------------------------------------------------------ one message
    async def ingest(self, mailbox: str, msg: dict[str, Any]) -> bool:
        labels = msg.get("labelIds", [])
        if "DRAFT" in labels or self.store.has_message(mailbox, msg["id"]):
            return False
        p = parse_message(msg)
        self.store.insert_message({
            "mailbox": mailbox,
            "gmail_id": p.gmail_id,
            "thread_id": p.thread_id,
            "rfc_message_id": p.rfc_message_id,
            "in_reply_to": p.in_reply_to,
            "references_hdr": p.references,
            "from_addr": p.from_addr,
            "from_name": p.from_name,
            "from_text": f"{p.from_name} {p.from_addr}".strip(),
            "to_text": " ".join(p.to + p.cc + p.bcc),
            "to_addrs": p.to,
            "cc_addrs": p.cc,
            "bcc_addrs": p.bcc,
            "delivered_to": p.delivered_to,
            "reply_to": p.reply_to,
            "subject": p.subject,
            "snippet": p.snippet,
            "body_text": p.body_text + p.links_note,
            "headers_json": json.dumps(p.headers, ensure_ascii=False),
            "internal_ts": p.internal_ts,
            "label_ids": labels,
            "direction": "out" if "SENT" in labels else "in",
            "has_attachments": int(bool(p.attachments)),
        })
        await self._store_attachments(mailbox, p)
        return True

    async def _store_attachments(self, mailbox: str, p: Parsed) -> None:
        for a in p.attachments:
            path: Path | None = None
            if a.size <= self.cfg.attachment_max_bytes:
                try:
                    data = a.inline_data
                    if data is None and a.attachment_id:
                        data = await self._call(mailbox, "get_attachment", p.gmail_id, a.attachment_id)
                    if data is not None:
                        path = self.attachment_path(mailbox, p.gmail_id, a.part_id, a.filename)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                except (GmailError, KeyError) as e:
                    log.warning("%s: attachment %s/%s not stored: %s", mailbox, p.gmail_id, a.part_id, e)
            self.store.x(
                "INSERT OR REPLACE INTO attachments(mailbox, gmail_id, part_id, filename, mime_type, size, path)"
                " VALUES (?,?,?,?,?,?,?)",
                (mailbox, p.gmail_id, a.part_id, a.filename, a.mime_type, a.size, str(path) if path else None),
            )

    def attachment_path(self, mailbox: str, gmail_id: str, part_id: str, filename: str) -> Path:
        return self.cfg.attachments_dir / _safe(mailbox) / gmail_id / f"{_safe(part_id)}_{_safe(filename)}"

    async def attachment_bytes(self, mailbox: str, gmail_id: str, part_id: str) -> bytes:
        row = self.store.q1("SELECT path FROM attachments WHERE mailbox=? AND gmail_id=? AND part_id=?",
                            (mailbox, gmail_id, part_id))
        if row and row["path"]:
            try:
                return Path(row["path"]).read_bytes()
            except FileNotFoundError:
                pass
        # Over the eager size limit (or the file went missing): fetch it now. attachmentIds aren't stable.
        msg = await self._call(mailbox, "get_message", gmail_id, "full")
        if not msg:
            raise GmailError(404, "message no longer exists")
        for a in parse_message(msg).attachments:
            if a.part_id == part_id:
                if a.inline_data is not None:
                    return a.inline_data
                if a.attachment_id:
                    return await self._call(mailbox, "get_attachment", gmail_id, a.attachment_id)
        raise GmailError(404, "attachment not found")
