"""Outbound mail: new messages, replies and forwards, from any configured mailbox (or its send-as aliases)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from .config import Config
from .directory import Directory, Identity
from .gmail import GmailError, MailboxAPI
from .store import Store
from .sync import Syncer

log = logging.getLogger(__name__)
_EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # Gmail's API send limit is ~25 MB after base64 encoding


class AttachmentSpec(TypedDict):
    path: str  # absolute path of a file readable by the service
    filename: NotRequired[str]  # default: the file's name
    inline: NotRequired[bool]  # inline part of the HTML body (needs `html`), shown via <img src="cid:...">
    content_id: NotRequired[str]  # inline only; default: the filename


@dataclass(frozen=True)
class _Attachment:
    data: bytes
    maintype: str
    subtype: str
    filename: str
    inline: bool = False
    cid: str = ""


class Refused(Exception):
    """A request the service can't carry out; the message goes back to the caller."""


def _iso(ms: int | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ms / 1000)) if ms else None


def norm_recipients(values: list[str] | str | None) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [v for v in re.split(r"[,;]", values) if v.strip()]
    out = []
    for v in values:
        addr = parseaddr(v)[1].strip().lower()
        if not _EMAIL_RE.match(addr):
            raise Refused(f"invalid email address: {v!r}")
        if addr not in out:
            out.append(addr)
    return out


class Sender:
    def __init__(self, cfg: Config, store: Store, gmail: Callable[[str], MailboxAPI], directory: Directory,
                 syncer: Syncer):
        self.cfg = cfg
        self.store = store
        self.gmail = gmail
        self.directory = directory
        self.syncer = syncer

    # ------------------------------------------------------------------ helpers
    def identity(self, from_addr: str) -> Identity:
        ident = self.directory.resolve(from_addr)
        if ident is None:
            raise Refused(f"{from_addr.strip().lower()} is not a configured mailbox or send-as alias "
                          f"(see list_mailboxes)")
        if not self.cfg.mailboxes[ident.mailbox].send:
            raise Refused(f"sending from {ident.mailbox} is turned off in the config (send = false)")
        return ident

    def load_attachments(self, specs: list[AttachmentSpec] | None, has_html: bool) -> list[_Attachment]:
        if not specs:
            return []
        secret = [self.cfg.key_file.resolve(), self.cfg.state_dir.resolve()]
        files: list[tuple[Path, dict[str, Any]]] = []
        total = 0
        for spec in specs:
            raw_path = str(spec.get("path") or "") if isinstance(spec, dict) else ""
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                raise Refused(f"attachment path must be absolute: {raw_path!r}")
            path = path.resolve()
            if any(path == s or s in path.parents for s in secret):
                raise Refused(f"attachment {raw_path} is not allowed (the service's own key or state)")
            if not path.is_file():
                raise Refused(f"attachment not found or not a regular file: {raw_path}")
            if spec.get("inline") and not has_html:
                raise Refused(f"inline attachment {raw_path} needs an html body")
            total += path.stat().st_size
            files.append((path, dict(spec)))
        if total > MAX_ATTACHMENT_BYTES:
            raise Refused(f"attachments total {total / 1048576:.1f} MB; the limit is "
                          f"{MAX_ATTACHMENT_BYTES // 1048576} MB (Gmail's API limit is ~25 MB after encoding)")
        out = []
        for path, spec in files:
            filename = str(spec.get("filename") or path.name)
            mime = mimetypes.guess_type(filename)[0] or mimetypes.guess_type(path.name)[0]
            maintype, subtype = (mime or "application/octet-stream").split("/", 1)
            inline = bool(spec.get("inline"))
            cid = str(spec.get("content_id") or filename).strip().strip("<>") if inline else ""
            out.append(_Attachment(path.read_bytes(), maintype, subtype, filename, inline, cid))
        return out

    @staticmethod
    def build(ident: Identity, to: list[str], cc: list[str], bcc: list[str], subject: str, body: str, *,
              in_reply_to: str = "", references: str = "", html: str | None = None,
              attachments: list[_Attachment] | None = None, reply_to: str | None = None,
              headers: dict[str, str] | None = None) -> tuple[EmailMessage, str]:
        """text/plain; with html, multipart/alternative (inline parts in multipart/related under the HTML); with
        regular attachments, all of that inside multipart/mixed."""
        msg = EmailMessage()
        msg["From"] = formataddr((ident.display_name, ident.address)) if ident.display_name else ident.address
        if to:
            msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)  # Gmail delivers to Bcc and strips the header
        if reply_to:
            msg["Reply-To"] = reply_to
        msg["Subject"] = subject
        msg_id = make_msgid(domain=ident.address.rsplit("@", 1)[-1])
        msg["Message-ID"] = msg_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = (references + " " + in_reply_to).strip()
        for k, v in (headers or {}).items():
            msg[k] = v
        msg.set_content(body.rstrip() + "\n")
        atts = attachments or []
        if html:
            msg.add_alternative(html, subtype="html")
            html_part = msg.get_payload()[1]
            for a in atts:
                if a.inline:
                    html_part.add_related(a.data, a.maintype, a.subtype, cid=f"<{a.cid}>", filename=a.filename,
                                          disposition="inline")
        for a in atts:
            if not a.inline:
                msg.add_attachment(a.data, a.maintype, a.subtype, filename=a.filename)
        return msg, msg_id

    async def deliver(self, client: str, ident: Identity, msg: EmailMessage, msg_id: str, *, kind: str,
                      to: list[str], cc: list[str], bcc: list[str], thread_id: str | None) -> dict[str, Any]:
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        text_part = msg.get_body(preferencelist=("plain",)) or msg
        pk = self.store.log_send(
            client=client, mailbox=ident.mailbox, from_addr=ident.address, to_addrs=to, cc_addrs=cc, bcc_addrs=bcc,
            subject=str(msg["Subject"]), body=text_part.get_content(), kind=kind, thread_id=thread_id,
            status="pending", rfc_message_id=msg_id,
        )
        try:
            res = await asyncio.to_thread(lambda: self.gmail(ident.mailbox).send(raw, thread_id))
        except GmailError as e:
            self.store.update_send(pk, status="error", reason=str(e)[:500])
            raise Refused(f"Gmail rejected the send: {e}") from e
        tid = res.get("threadId") or thread_id or ""
        self.store.update_send(pk, status="sent", gmail_id=res.get("id"), thread_id=tid)
        log.info("sent %s by %s from %s to %s (thread %s)", kind, client, ident.address, to + cc + bcc, tid)
        out: dict[str, Any] = {"status": "sent", "mailbox": ident.mailbox, "from": ident.address, "to": to,
                               "message_id": res.get("id"), "thread_id": tid}
        if cc:
            out["cc"] = cc
        if bcc:
            out["bcc"] = bcc
        return out

    def _find(self, mailbox: str | None, message_id: str) -> Any:
        rows = [r for r in self.store.messages_by_id(message_id) if not mailbox or r["mailbox"] == mailbox]
        if not rows:
            raise Refused(f"message {message_id} not found" + (f" in {mailbox}" if mailbox else ""))
        if len(rows) > 1:
            raise Refused(f"message id {message_id} is in several mailboxes ({', '.join(r['mailbox'] for r in rows)});"
                          f" pass mailbox")
        return rows[0]

    # ------------------------------------------------------------------ public
    async def send_new(self, client: str, from_addr: str, to: list[str] | str, subject: str, body: str,
                       cc: list[str] | str | None = None, bcc: list[str] | str | None = None, html: str | None = None,
                       attachments: list[AttachmentSpec] | None = None, reply_to: str | None = None,
                       headers: dict[str, str] | None = None) -> dict[str, Any]:
        to_l, cc_l, bcc_l = norm_recipients(to), norm_recipients(cc), norm_recipients(bcc)
        if not (to_l or cc_l or bcc_l):
            raise Refused("no recipients")
        ident = self.identity(from_addr)
        atts = self.load_attachments(attachments, bool(html))
        msg, msg_id = self.build(ident, to_l, cc_l, bcc_l, subject, body, html=html, attachments=atts,
                                 reply_to=reply_to, headers=headers)
        return await self.deliver(client, ident, msg, msg_id, kind="new", to=to_l, cc=cc_l, bcc=bcc_l, thread_id=None)

    async def reply(self, client: str, body: str, *, mailbox: str | None = None, thread_id: str | None = None,
                    message_id: str | None = None, reply_all: bool = False, from_addr: str | None = None,
                    cc: list[str] | str | None = None, bcc: list[str] | str | None = None,
                    html: str | None = None, attachments: list[AttachmentSpec] | None = None) -> dict[str, Any]:
        if message_id:
            target = self._find(mailbox.strip().lower() if mailbox else None, message_id)
            mailbox, thread_id = target["mailbox"], target["thread_id"]
        elif mailbox and thread_id:
            mailbox = mailbox.strip().lower()
            target = None
        else:
            raise Refused("give message_id, or mailbox and thread_id")
        atts = self.load_attachments(attachments, bool(html))
        async with self.syncer.lock(mailbox):
            try:  # a reply sent from Gmail seconds ago should be what this one follows
                await self.syncer.sync_locked(mailbox)
            except GmailError as e:
                log.warning("pre-reply sync of %s failed: %s", mailbox, e)
            msgs = self.store.thread_messages(mailbox, thread_id)
            if not msgs:
                raise Refused(f"thread {thread_id} not found in {mailbox}")
            if target is None:
                target = msgs[-1]
            own = self.directory.own_addresses()
            if target["direction"] == "in":
                to_l = [target["reply_to"] or target["from_addr"]]
                cc_l: list[str] = []
                if reply_all:
                    rest = json.loads(target["to_addrs"]) + json.loads(target["cc_addrs"])
                    cc_l = [a for a in dict.fromkeys(rest) if a not in own and a not in to_l]
            else:  # following up on our own message
                to_l = json.loads(target["to_addrs"])
                cc_l = json.loads(target["cc_addrs"]) if reply_all else []
            to_l = [a for a in to_l if a]
            cc_l = list(dict.fromkeys(cc_l + norm_recipients(cc)))
            if not to_l:
                raise Refused("could not work out who to reply to")
            if from_addr is None:
                if target["direction"] == "out":
                    candidates = [target["from_addr"]]
                else:  # the address the mail was sent to, when it is one of this mailbox's
                    delivered = (json.loads(target["to_addrs"]) + json.loads(target["cc_addrs"])
                                 + json.loads(target["delivered_to"] or "[]"))
                    candidates = [a for a in delivered if self.directory.mailbox_of(a) == mailbox]
                from_addr = (candidates or [mailbox])[0]
            ident = self.identity(from_addr)
            if ident.mailbox != mailbox:
                raise Refused(f"{ident.address} belongs to {ident.mailbox}, not {mailbox}: a reply has to be sent from "
                              f"the mailbox that holds the thread")
            subject = target["subject"] or ""
            if not re.match(r"^\s*re\s*:", subject, re.IGNORECASE):
                subject = f"Re: {subject}".strip()
            msg, msg_id = self.build(ident, to_l, cc_l, norm_recipients(bcc), subject, body,
                                     in_reply_to=target["rfc_message_id"] or "",
                                     references=target["references_hdr"] or "", html=html, attachments=atts)
            return await self.deliver(client, ident, msg, msg_id, kind="reply", to=to_l, cc=cc_l,
                                      bcc=norm_recipients(bcc), thread_id=thread_id)

    async def forward(self, client: str, message_id: str, to: list[str] | str, *, mailbox: str | None = None,
                      note: str = "", from_addr: str | None = None, cc: list[str] | str | None = None,
                      include_attachments: bool = True) -> dict[str, Any]:
        target = self._find(mailbox.strip().lower() if mailbox else None, message_id)
        mb = target["mailbox"]
        ident = self.identity(from_addr or mb)
        to_l, cc_l = norm_recipients(to), norm_recipients(cc)
        if not to_l:
            raise Refused("no recipients")
        head = ["---------- Forwarded message ---------", f"From: {target['from_text']}",
                f"Date: {_iso(target['internal_ts'])}", f"Subject: {target['subject']}",
                f"To: {', '.join(json.loads(target['to_addrs']))}"]
        if cc_orig := json.loads(target["cc_addrs"]):
            head.append(f"Cc: {', '.join(cc_orig)}")
        body = (note.rstrip() + "\n\n" if note.strip() else "") + "\n".join(head) + "\n\n" + (target["body_text"] or "")
        atts: list[_Attachment] = []
        if include_attachments:
            total = 0
            for a in self.store.attachments_of(mb, message_id):
                data = await self.syncer.attachment_bytes(mb, message_id, a["part_id"])
                total += len(data)
                if total > MAX_ATTACHMENT_BYTES:
                    raise Refused("the original's attachments are over 20 MB; forward with include_attachments=false")
                maintype, _, subtype = (a["mime_type"] or "application/octet-stream").partition("/")
                atts.append(_Attachment(data, maintype, subtype or "octet-stream", a["filename"] or "attachment"))
        subject = target["subject"] or ""
        if not re.match(r"^\s*fwd?\s*:", subject, re.IGNORECASE):
            subject = f"Fwd: {subject}".strip()
        msg, msg_id = self.build(ident, to_l, cc_l, [], subject, body, attachments=atts)
        return await self.deliver(client, ident, msg, msg_id, kind="forward", to=to_l, cc=cc_l, bcc=[], thread_id=None)
