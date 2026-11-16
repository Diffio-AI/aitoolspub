"""In-memory Gmail stand-in: messages in Gmail API `format=full` shape, history, send, threading."""

from __future__ import annotations

import base64
import email
import email.policy
import itertools
import time
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid
from typing import Any

from agent_mail.gmail import HistoryExpired

_ids = itertools.count(1000)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def to_gmail_payload(msg: email.message.Message, part_id: str = "") -> dict[str, Any]:
    headers = [{"name": k, "value": str(v)} for k, v in msg.items()]
    mime = msg.get_content_type()
    node: dict[str, Any] = {"partId": part_id, "mimeType": mime, "filename": msg.get_filename() or "",
                            "headers": headers, "body": {"size": 0}}
    if msg.is_multipart():
        node["parts"] = [
            to_gmail_payload(p, f"{part_id}.{i}" if part_id else str(i))
            for i, p in enumerate(msg.get_payload())
        ]
    elif mime == "message/delivery-status":
        # python's email stores delivery-status as a list of header blocks
        payload = msg.get_payload()
        if isinstance(payload, list):
            text = "\n".join(p.as_string() for p in payload)
        else:
            text = str(payload)
        data = text.encode()
        node["body"] = {"size": len(data), "data": b64(data)}
    else:
        data = msg.get_payload(decode=True) or b""
        node["body"] = {"size": len(data), "data": b64(data)}
    return node


class FakeWorld:
    def __init__(self) -> None:
        self.boxes: dict[str, FakeMailbox] = {}
        self.sent_raw: list[tuple[str, EmailMessage]] = []

    def box(self, address: str) -> FakeMailbox:
        if address not in self.boxes:
            self.boxes[address] = FakeMailbox(self, address)
        return self.boxes[address]

    def __call__(self, address: str) -> FakeMailbox:
        return self.box(address)


class FakeMailbox:
    def __init__(self, world: FakeWorld, address: str):
        self.world = world
        self.address = address
        self.messages: dict[str, dict[str, Any]] = {}
        self.history_id = 100
        self.history_log: list[dict[str, Any]] = []
        self.min_history = 0
        self.label_list = [{"id": "INBOX", "name": "INBOX"}, {"id": "SENT", "name": "SENT"}]
        self.send_as_list: list[dict[str, Any]] = [{"sendAsEmail": address, "isPrimary": True}]
        self.modify_calls: list[tuple[list[str], list[str], list[str]]] = []

    # ---- helpers for tests
    def _thread_for(self, msg: email.message.Message, thread_id: str | None) -> str:
        if thread_id:
            return thread_id
        refs = set((msg.get("In-Reply-To", "") + " " + msg.get("References", "")).split())
        for m in self.messages.values():
            if m["_rfcid"] in refs:
                return m["threadId"]
        return f"t{next(_ids)}"

    def add(self, msg: email.message.Message, labels: list[str], thread_id: str | None = None) -> dict[str, Any]:
        if not msg.get("Message-ID"):
            msg["Message-ID"] = make_msgid(domain="example.test")
        gid = f"m{next(_ids)}"
        self.history_id += 1
        rec = {
            "id": gid,
            "threadId": self._thread_for(msg, thread_id),
            "labelIds": list(labels),
            "internalDate": str(int(time.time() * 1000)),
            "historyId": str(self.history_id),
            "snippet": "",
            "payload": to_gmail_payload(msg),
            "_rfcid": msg["Message-ID"],
        }
        self.messages[gid] = rec
        self.history_log.append({"id": str(self.history_id),
                                 "messagesAdded": [{"message": {"id": gid, "threadId": rec["threadId"]}}]})
        return rec

    def deliver(self, from_: str, subject: str, body: str, *, in_reply_to: str = "", to: str | None = None,
                headers: dict[str, str] | None = None) -> dict[str, Any]:
        m = EmailMessage()
        m["From"] = from_
        m["To"] = to or self.address
        m["Subject"] = subject
        if in_reply_to:
            m["In-Reply-To"] = in_reply_to
            m["References"] = in_reply_to
        for k, v in (headers or {}).items():
            m[k] = v
        m.set_content(body)
        return self.add(m, ["INBOX", "UNREAD"])

    def outside_send(self, to: str, subject: str, body: str, thread_id: str | None = None,
                     in_reply_to: str = "") -> dict[str, Any]:
        """The human sending from the Gmail UI."""
        m = EmailMessage()
        m["From"] = self.address
        m["To"] = to
        m["Subject"] = subject
        if in_reply_to:
            m["In-Reply-To"] = in_reply_to
        m.set_content(body)
        return self.add(m, ["SENT"], thread_id)

    def relabel(self, gid: str, labels: list[str]) -> None:
        self.messages[gid]["labelIds"] = labels
        self.history_id += 1
        self.history_log.append({"id": str(self.history_id), "labelsAdded": [{
            "message": {"id": gid, "threadId": self.messages[gid]["threadId"], "labelIds": labels},
            "labelIds": labels}]})

    # ---- MailboxAPI
    def profile(self) -> dict[str, Any]:
        return {"emailAddress": self.address, "historyId": str(self.history_id)}

    def list_message_ids(self, query: str) -> list[str]:
        return [g for g, m in self.messages.items() if "DRAFT" not in m["labelIds"]]

    def get_message(self, msg_id: str, fmt: str = "full") -> dict[str, Any] | None:
        m = self.messages.get(msg_id)
        if not m:
            return None
        out = {k: v for k, v in m.items() if not k.startswith("_")}
        if fmt == "minimal":
            out.pop("payload", None)
        return out

    def history(self, start_history_id: str) -> tuple[list[dict[str, Any]], str]:
        if int(start_history_id) < self.min_history:
            raise HistoryExpired(404, "expired")
        return [h for h in self.history_log if int(h["id"]) > int(start_history_id)], str(self.history_id)

    def send(self, raw_b64: str, thread_id: str | None) -> dict[str, Any]:
        raw = base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4))
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        self.world.sent_raw.append((self.address, msg))  # type: ignore[arg-type]
        del msg["Message-ID"]  # like Gmail, assign our own Message-ID
        msg["Message-ID"] = make_msgid(domain="mail.gmail.test")
        raw = msg.as_bytes()
        rec = self.add(msg, ["SENT"], thread_id)
        for _, addr in getaddresses([msg.get("To", ""), msg.get("Cc", "")]):
            if addr in self.world.boxes and addr != self.address:
                copy = email.message_from_bytes(raw, policy=email.policy.default)
                self.world.boxes[addr].add(copy, ["INBOX", "UNREAD"])
        return {"id": rec["id"], "threadId": rec["threadId"]}

    def watch(self, topic: str) -> dict[str, Any]:
        return {"historyId": str(self.history_id), "expiration": str(int(time.time() * 1000) + 7 * 86400000)}

    def get_attachment(self, msg_id: str, attachment_id: str) -> bytes:
        raise KeyError(attachment_id)

    def labels(self) -> list[dict[str, Any]]:
        return list(self.label_list)

    def create_label(self, name: str) -> dict[str, Any]:
        lab = {"id": f"Label_{next(_ids)}", "name": name}
        self.label_list.append(lab)
        return lab

    def send_as(self) -> list[dict[str, Any]]:
        return list(self.send_as_list)

    def trash(self, msg_id: str) -> None:
        labs = self.messages[msg_id]["labelIds"]
        self._set(msg_id, [x for x in labs if x != "INBOX"] + ["TRASH"])

    def untrash(self, msg_id: str) -> None:
        self._set(msg_id, [x for x in self.messages[msg_id]["labelIds"] if x != "TRASH"])

    def batch_modify(self, msg_ids: list[str], add: list[str], remove: list[str]) -> None:
        self.modify_calls.append((list(msg_ids), list(add), list(remove)))
        for i in msg_ids:
            labs = [x for x in self.messages[i]["labelIds"] if x not in remove]
            self._set(i, labs + [x for x in add if x not in labs])

    def _set(self, gid: str, labels: list[str]) -> None:
        self.relabel(gid, labels)
