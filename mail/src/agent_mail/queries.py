"""Read-side queries: list mailboxes, full-text search, regex grep, read a message or thread."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any

from .config import Config
from .directory import Directory
from .send import Refused
from .store import Store, now_ms

SYSTEM_HIDE = {"CATEGORY_PERSONAL", "IMPORTANT"}
_QUOTE_MARKERS = [
    re.compile(r"^\s*On .{0,300}?wrote:\s*$", re.MULTILINE | re.DOTALL),  # Gmail / Apple Mail
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*From:\s.+$\n^\s*(Sent|Date):\s", re.MULTILINE),  # Outlook
    re.compile(r"^_{10,}\s*$", re.MULTILINE),  # Outlook separator line
]
_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_REL = re.compile(r"^\s*-?\s*(\d+)\s*(m|min|minutes?|h|hours?|d|days?|w|weeks?)\s*(ago)?\s*$", re.IGNORECASE)
GREP_FIELDS = {"subject": "subject", "body": "body_text", "from": "from_text", "to": "to_text",
               "headers": "headers_json"}


def iso(ms: int | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ms / 1000)) if ms else None


def to_ms(value: str) -> int:
    """YYYY-MM-DD, an ISO time, or relative: '30m', '2h', '3 days ago', '1w'."""
    v = value.strip()
    if m := _REL.match(v):
        n, unit = int(m.group(1)), m.group(2).lower()[0]
        secs = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
        return now_ms() - n * secs * 1000
    try:
        d = datetime.fromisoformat(v)
    except ValueError as e:
        raise Refused(f"bad date {value!r}; use YYYY-MM-DD, an ISO time, or relative like '30m', '2h', '3d'") from e
    if d.tzinfo is None:
        d = d.astimezone()
    return int(d.timestamp() * 1000)


def strip_quoted(text: str) -> str:
    """Keep only the newly written part of a reply."""
    cut = len(text)
    for rx in _QUOTE_MARKERS:
        m = rx.search(text)
        if m:
            cut = min(cut, m.start())
    lines = [ln for ln in text[:cut].splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def links_in(text: str, limit: int = 60) -> list[str]:
    return list(dict.fromkeys(u.rstrip(".,;:!?") for u in _URL.findall(text or "")))[:limit]


def _fts_quote(q: str) -> str:
    return " ".join('"' + t.replace('"', "") + '"' for t in re.findall(r"[\w@.+'-]+", q))


def mailboxes_arg(cfg: Config, mailbox: str | list[str] | None) -> list[str]:
    if not mailbox:
        return sorted(cfg.mailboxes)
    names = [mailbox] if isinstance(mailbox, str) else list(mailbox)
    out = []
    for n in names:
        n = n.strip().lower()
        if n not in cfg.mailboxes:
            raise Refused(f"{n} is not a mirrored mailbox (mirrored: {', '.join(sorted(cfg.mailboxes))}); for an "
                          f"alias, filter with recipient= instead")
        out.append(n)
    return out


def labels_of(store: Store, mailbox: str, raw: str) -> list[str]:
    names = store.label_names(mailbox)
    return [names.get(x, x) for x in raw.strip(",").split(",") if x and x not in SYSTEM_HIDE]


def summary(store: Store, r: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {
        "mailbox": r["mailbox"],
        "message_id": r["gmail_id"],
        "thread_id": r["thread_id"],
        "date": iso(r["internal_ts"]),
        "from": r["from_text"],
        "to": r["to_text"],
        "subject": r["subject"],
        "snippet": (r["snippet"] or "")[:200],
        "dir": r["direction"],
    }
    if ",UNREAD," in r["label_ids"]:
        out["unread"] = True
    if r["has_attachments"]:
        out["attachments"] = True
    labs = [x for x in labels_of(store, r["mailbox"], r["label_ids"]) if x not in ("UNREAD",)]
    if labs:
        out["labels"] = labs
    return out


def list_mailboxes(cfg: Config, store: Store, directory: Directory) -> dict[str, Any]:
    out = []
    for addr, mb in sorted(cfg.mailboxes.items()):
        st = store.mailbox_state(addr)
        c = store.q1("SELECT COUNT(*) n, MAX(internal_ts) last, "
                     "SUM(label_ids LIKE '%,INBOX,%' AND label_ids LIKE '%,UNREAD,%') unread FROM messages "
                     "WHERE mailbox=?", (addr,))
        entry: dict[str, Any] = {
            "mailbox": addr,
            "display_name": mb.display_name,
            "addresses": directory.addresses_of(addr),
            "can_send": mb.send,
            "messages": c["n"] if c else 0,
            "unread_in_inbox": int(c["unread"] or 0) if c else 0,
            "newest": iso(c["last"]) if c else None,
            "synced": iso(st["last_sync_at"]),
        }
        if st["last_error"]:
            entry["error"] = st["last_error"]
        if not st["history_id"]:
            entry["note"] = "first sync still running; search results for this mailbox are partial"
        out.append(entry)
    return {"mailboxes": out, "now": iso(now_ms())}


def _filters(cfg: Config, store: Store, *, mailbox: str | list[str] | None = None, thread_id: str | None = None,
             sender: str | None = None, recipient: str | None = None, subject: str | None = None,
             after: str | int | None = None, before: str | None = None, label: str | None = None,
             unread: bool | None = None, direction: str | None = None, has_attachment: bool | None = None,
             include_spam_trash: bool = False) -> tuple[list[str], list[Any]]:
    mbs = mailboxes_arg(cfg, mailbox)
    where = [f"m.mailbox IN ({','.join('?' for _ in mbs)})"]
    args: list[Any] = list(mbs)
    if thread_id:
        where.append("m.thread_id=?")
        args.append(thread_id)
    if sender:
        where.append("(m.from_addr LIKE ? OR m.from_name LIKE ?)")
        args += [f"%{sender.lower()}%", f"%{sender}%"]
    if recipient:
        where.append("m.to_text LIKE ?")
        args.append(f"%{recipient.lower()}%")
    if subject:
        where.append("m.subject LIKE ?")
        args.append(f"%{subject}%")
    if after:
        where.append("m.internal_ts>=?")
        args.append(after if isinstance(after, int) else to_ms(after))
    if before:
        where.append("m.internal_ts<?")
        args.append(to_ms(before))
    if label:
        ids = store.label_ids_named(None if not mailbox or not isinstance(mailbox, str) else mailbox.strip().lower(),
                                    label)
        where.append("(" + " OR ".join("m.label_ids LIKE ?" for _ in ids) + ")")
        args += [f"%,{i},%" for i in ids]
    if unread is not None:
        where.append("m.label_ids " + ("" if unread else "NOT ") + "LIKE '%,UNREAD,%'")
    if direction in ("in", "out"):
        where.append("m.direction=?")
        args.append(direction)
    if has_attachment is not None:
        where.append("m.has_attachments=?")
        args.append(int(has_attachment))
    if not include_spam_trash and (label or "").upper() not in ("SPAM", "TRASH"):
        where.append("m.label_ids NOT LIKE '%,SPAM,%' AND m.label_ids NOT LIKE '%,TRASH,%'")
    return where, args


def search(cfg: Config, store: Store, query: str | None = None, *, limit: int = 20, offset: int = 0,
           **filters: Any) -> dict[str, Any]:
    where, args = _filters(cfg, store, **filters)
    limit = max(1, min(int(limit), 200))
    order = " ORDER BY m.internal_ts DESC, m.pk DESC LIMIT ? OFFSET ?"

    def run(fts: str | None) -> list[sqlite3.Row]:
        w, a, sql = list(where), list(args), "SELECT m.* FROM messages m "
        if fts:
            sql += "JOIN messages_fts f ON f.rowid=m.pk "
            w.append("messages_fts MATCH ?")
            a.append(fts)
        return store.q(sql + "WHERE " + " AND ".join(w) + order, a + [limit + 1, offset])

    note = None
    if query and query.strip():
        try:
            rows = run(query)
        except sqlite3.OperationalError:
            q2 = _fts_quote(query)
            rows = run(q2) if q2 else []
            note = f"the query isn't valid FTS5 syntax, so it ran as plain words: {q2}"
    else:
        rows = run(None)
    more = len(rows) > limit
    out: dict[str, Any] = {"results": [summary(store, r) for r in rows[:limit]]}
    if more:
        out["next_offset"] = offset + limit
    if note:
        out["note"] = note
    return out


def grep(cfg: Config, store: Store, pattern: str, *, fields: list[str] | None = None, ignore_case: bool = True,
         limit: int = 20, offset: int = 0, context: int = 80, max_matches_per_message: int = 5,
         **filters: Any) -> dict[str, Any]:
    """Regular expression over subject, body, from, to and (optionally) the raw headers; newest first."""
    fields = fields or ["subject", "body", "from", "to"]
    if bad := [f for f in fields if f not in GREP_FIELDS]:
        raise Refused(f"unknown field(s) {bad}; choose from {sorted(GREP_FIELDS)}")
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE if ignore_case else re.MULTILINE)
    except re.error as e:
        raise Refused(f"bad regular expression: {e}") from e
    where, args = _filters(cfg, store, **filters)
    cols = [GREP_FIELDS[f] for f in fields]
    fn = "regexp_i" if ignore_case else "regexp"
    where.append("(" + " OR ".join(f"{fn}(?, m.{c})" for c in cols) + ")")
    args += [pattern] * len(cols)
    limit = max(1, min(int(limit), 100))
    rows = store.q("SELECT m.* FROM messages m WHERE " + " AND ".join(where)
                   + " ORDER BY m.internal_ts DESC, m.pk DESC LIMIT ? OFFSET ?", args + [limit + 1, offset])
    results = []
    for r in rows[:limit]:
        hits = []
        for f, c in zip(fields, cols):
            text = r[c] or ""
            for m in rx.finditer(text):
                s, e = max(0, m.start() - context), min(len(text), m.end() + context)
                hits.append({"field": f, "match": m.group(0)[:300],
                             "context": ("…" if s else "") + text[s:e].replace("\r", "") + ("…" if e < len(text) else "")})
                if len(hits) >= max_matches_per_message:
                    break
            if len(hits) >= max_matches_per_message:
                break
        results.append({**summary(store, r), "matches": hits})
    out: dict[str, Any] = {"results": results}
    if len(rows) > limit:
        out["next_offset"] = offset + limit
    return out


def find_message(store: Store, mailbox: str | None, message_id: str) -> sqlite3.Row:
    rows = [r for r in store.messages_by_id(message_id) if not mailbox or r["mailbox"] == mailbox.strip().lower()]
    if not rows:
        raise Refused(f"message {message_id} not found" + (f" in {mailbox}" if mailbox else "")
                      + " (if it just arrived, sync_now first)")
    if len(rows) > 1:
        raise Refused(f"message id {message_id} is in several mailboxes ({', '.join(r['mailbox'] for r in rows)}); "
                      f"pass mailbox")
    return rows[0]


def message_detail(store: Store, m: sqlite3.Row, *, full_quotes: bool = True, headers: bool = False,
                   max_chars: int = 20000) -> dict[str, Any]:
    body = m["body_text"] or ""
    if not full_quotes:
        body = strip_quoted(body) or body
    entry: dict[str, Any] = {
        "mailbox": m["mailbox"],
        "message_id": m["gmail_id"],
        "thread_id": m["thread_id"],
        "date": iso(m["internal_ts"]),
        "from": m["from_text"],
        "to": json.loads(m["to_addrs"]),
        "subject": m["subject"],
        "dir": m["direction"],
        "labels": labels_of(store, m["mailbox"], m["label_ids"]),
    }
    for k, col in (("cc", "cc_addrs"), ("bcc", "bcc_addrs")):
        if v := json.loads(m[col] or "[]"):
            entry[k] = v
    if m["reply_to"]:
        entry["reply_to"] = m["reply_to"]
    if len(body) > max_chars:
        entry["body"] = body[:max_chars]
        entry["body_truncated"] = f"{len(body)} chars; raise max_chars for the rest"
    else:
        entry["body"] = body
    if links := links_in(m["body_text"] or ""):
        entry["links"] = links
    if atts := store.attachments_of(m["mailbox"], m["gmail_id"]):
        entry["attachments"] = [{"part_id": a["part_id"], "filename": a["filename"], "mime_type": a["mime_type"],
                                 "size": a["size"]} for a in atts]
    if headers:
        entry["headers"] = json.loads(m["headers_json"] or "{}")
        entry["rfc_message_id"] = m["rfc_message_id"]
    return entry


def read_thread(store: Store, mailbox: str, thread_id: str, *, full_quotes: bool = False,
                max_chars: int = 6000) -> dict[str, Any]:
    mb = mailbox.strip().lower()
    msgs = store.thread_messages(mb, thread_id)
    if not msgs:
        raise Refused(f"thread {thread_id} not found in {mb}")
    return {"mailbox": mb, "thread_id": thread_id,
            "messages": [message_detail(store, m, full_quotes=full_quotes, max_chars=max_chars) for m in msgs]}


def since_default(minutes: float) -> int:
    return int((datetime.now() - timedelta(minutes=minutes)).timestamp() * 1000)
