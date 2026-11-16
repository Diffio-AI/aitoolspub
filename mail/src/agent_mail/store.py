"""SQLite mirror of every configured mailbox (WAL + FTS5), plus the service's own state.

One connection, used only from the event-loop thread (Gmail calls run in worker threads and hand results back).
"""

from __future__ import annotations

import functools
import json
import re
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  pk INTEGER PRIMARY KEY,
  mailbox TEXT NOT NULL,
  gmail_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  rfc_message_id TEXT,
  in_reply_to TEXT,
  references_hdr TEXT,
  from_addr TEXT,
  from_name TEXT,
  from_text TEXT,
  to_text TEXT,
  to_addrs TEXT,
  cc_addrs TEXT,
  bcc_addrs TEXT,
  delivered_to TEXT,
  reply_to TEXT,
  subject TEXT,
  snippet TEXT,
  body_text TEXT,
  headers_json TEXT,
  internal_ts INTEGER,
  label_ids TEXT,          -- ",INBOX,UNREAD," so LIKE '%,X,%' matches a label
  direction TEXT,          -- in | out (out = has the SENT label)
  has_attachments INTEGER DEFAULT 0,
  synced_at INTEGER,
  UNIQUE(mailbox, gmail_id)
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(mailbox, thread_id);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(internal_ts);
CREATE INDEX IF NOT EXISTS messages_synced ON messages(synced_at);
CREATE INDEX IF NOT EXISTS messages_from ON messages(from_addr);
CREATE INDEX IF NOT EXISTS messages_rfcid ON messages(rfc_message_id);
CREATE INDEX IF NOT EXISTS messages_gmail ON messages(gmail_id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  subject, body_text, from_text, to_text,
  content='messages', content_rowid='pk', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, subject, body_text, from_text, to_text)
  VALUES (new.pk, new.subject, new.body_text, new.from_text, new.to_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, subject, body_text, from_text, to_text)
  VALUES ('delete', old.pk, old.subject, old.body_text, old.from_text, old.to_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF subject, body_text, from_text, to_text ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, subject, body_text, from_text, to_text)
  VALUES ('delete', old.pk, old.subject, old.body_text, old.from_text, old.to_text);
  INSERT INTO messages_fts(rowid, subject, body_text, from_text, to_text)
  VALUES (new.pk, new.subject, new.body_text, new.from_text, new.to_text);
END;

CREATE TABLE IF NOT EXISTS attachments(
  pk INTEGER PRIMARY KEY,
  mailbox TEXT, gmail_id TEXT, part_id TEXT,
  filename TEXT, mime_type TEXT, size INTEGER, path TEXT,
  UNIQUE(mailbox, gmail_id, part_id)
);

CREATE TABLE IF NOT EXISTS mailbox_state(
  address TEXT PRIMARY KEY,
  history_id TEXT,
  last_sync_at INTEGER,
  last_full_sync_at INTEGER,
  watch_expiration_ms INTEGER,
  backfilled_days INTEGER,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS labels(
  mailbox TEXT, label_id TEXT, name TEXT, PRIMARY KEY(mailbox, label_id)
);

CREATE TABLE IF NOT EXISTS aliases(
  mailbox TEXT, address TEXT PRIMARY KEY, display_name TEXT, source TEXT
);

CREATE TABLE IF NOT EXISTS send_log(
  pk INTEGER PRIMARY KEY,
  created_at INTEGER,
  client TEXT, mailbox TEXT, from_addr TEXT,
  to_addrs TEXT, cc_addrs TEXT, bcc_addrs TEXT, subject TEXT, body TEXT,
  kind TEXT,               -- new | reply | forward
  thread_id TEXT,
  status TEXT,             -- pending | sent | error
  reason TEXT,
  rfc_message_id TEXT, gmail_id TEXT
);
CREATE INDEX IF NOT EXISTS send_log_ts ON send_log(created_at);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def labels_str(labels: Iterable[str]) -> str:
    labels = list(labels)
    return "," + ",".join(labels) + "," if labels else ","


@functools.lru_cache(maxsize=64)
def _compiled(pattern: str, flags: int) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, flags)
    except re.error:
        return None


def _regexp(pattern: str, value: Any, flags: int = re.MULTILINE) -> int:
    rx = _compiled(pattern, flags)
    return int(rx is not None and value is not None and rx.search(str(value)) is not None)


def _regexp_i(pattern: str, value: Any) -> int:
    return _regexp(pattern, value, re.MULTILINE | re.IGNORECASE)


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.create_function("REGEXP", 2, _regexp, deterministic=True)  # `x REGEXP p` calls regexp(p, x)
        self.db.create_function("regexp_i", 2, _regexp_i, deterministic=True)

    def close(self) -> None:
        self.db.close()

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, tuple(args)).fetchall()

    def q1(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.db.execute(sql, tuple(args)).fetchone()

    def x(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.db.execute(sql, tuple(args))

    # ------------------------------------------------------------ mailbox state
    def mailbox_state(self, address: str) -> sqlite3.Row:
        self.x("INSERT OR IGNORE INTO mailbox_state(address) VALUES (?)", (address,))
        row = self.q1("SELECT * FROM mailbox_state WHERE address=?", (address,))
        assert row is not None
        return row

    def update_mailbox_state(self, address: str, **fields: Any) -> None:
        self.mailbox_state(address)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE mailbox_state SET {cols} WHERE address=?", (*fields.values(), address))

    # ------------------------------------------------------------ messages
    def has_message(self, mailbox: str, gmail_id: str) -> bool:
        return self.q1("SELECT 1 FROM messages WHERE mailbox=? AND gmail_id=?", (mailbox, gmail_id)) is not None

    def known_ids(self, mailbox: str) -> set[str]:
        return {r[0] for r in self.q("SELECT gmail_id FROM messages WHERE mailbox=?", (mailbox,))}

    def insert_message(self, row: dict[str, Any]) -> None:
        row = dict(row)
        row["label_ids"] = labels_str(row.get("label_ids", []))
        for k in ("to_addrs", "cc_addrs", "bcc_addrs", "delivered_to"):
            row[k] = json.dumps(row.get(k, []))
        row["synced_at"] = now_ms()
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.x(f"INSERT OR REPLACE INTO messages({cols}) VALUES ({marks})", row.values())

    def set_labels(self, mailbox: str, gmail_id: str, labels: Iterable[str]) -> None:
        self.x("UPDATE messages SET label_ids=? WHERE mailbox=? AND gmail_id=?",
               (labels_str(labels), mailbox, gmail_id))

    def delete_message(self, mailbox: str, gmail_id: str) -> None:
        self.x("DELETE FROM messages WHERE mailbox=? AND gmail_id=?", (mailbox, gmail_id))
        self.x("DELETE FROM attachments WHERE mailbox=? AND gmail_id=?", (mailbox, gmail_id))

    def message(self, mailbox: str, gmail_id: str) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM messages WHERE mailbox=? AND gmail_id=?", (mailbox, gmail_id))

    def messages_by_id(self, gmail_id: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM messages WHERE gmail_id=?", (gmail_id,))

    def thread_messages(self, mailbox: str, thread_id: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM messages WHERE mailbox=? AND thread_id=? ORDER BY internal_ts, pk",
                      (mailbox, thread_id))

    def attachments_of(self, mailbox: str, gmail_id: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM attachments WHERE mailbox=? AND gmail_id=? ORDER BY part_id", (mailbox, gmail_id))

    # ------------------------------------------------------------ send log
    def log_send(self, **fields: Any) -> int:
        fields.setdefault("created_at", now_ms())
        for k in ("to_addrs", "cc_addrs", "bcc_addrs"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        cols = ", ".join(fields)
        cur = self.x(f"INSERT INTO send_log({cols}) VALUES ({', '.join('?' for _ in fields)})", fields.values())
        return int(cur.lastrowid or 0)

    def update_send(self, pk: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE send_log SET {cols} WHERE pk=?", (*fields.values(), pk))

    # ------------------------------------------------------------ labels / aliases
    def replace_labels(self, mailbox: str, labels: list[dict[str, Any]]) -> None:
        self.x("DELETE FROM labels WHERE mailbox=?", (mailbox,))
        for lab in labels:
            self.x("INSERT INTO labels VALUES (?,?,?)", (mailbox, lab["id"], lab.get("name", lab["id"])))

    def add_label(self, mailbox: str, label_id: str, name: str) -> None:
        self.x("INSERT OR REPLACE INTO labels VALUES (?,?,?)", (mailbox, label_id, name))

    def label_ids_named(self, mailbox: str | None, name: str) -> list[str]:
        if re.fullmatch(r"[A-Z_]+", name):
            return [name]  # system label id (INBOX, UNREAD, STARRED, CATEGORY_PROMOTIONS, ...)
        sql, args = "SELECT label_id FROM labels WHERE lower(name)=lower(?)", [name]
        if mailbox:
            sql += " AND mailbox=?"
            args.append(mailbox)
        return [r[0] for r in self.q(sql, args)] or [name]

    def label_names(self, mailbox: str) -> dict[str, str]:
        return {r[0]: r[1] for r in self.q("SELECT label_id, name FROM labels WHERE mailbox=?", (mailbox,))}

    def replace_aliases(self, mailbox: str, aliases: list[tuple[str, str]], source: str) -> None:
        self.x("DELETE FROM aliases WHERE mailbox=? AND source=?", (mailbox, source))
        for addr, name in aliases:
            self.x("INSERT OR REPLACE INTO aliases VALUES (?,?,?,?)", (mailbox, addr, name, source))

    def discovered_aliases(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM aliases")
