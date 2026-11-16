"""Turn Gmail API `format=full` message resources into flat records."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from typing import Any

from .gmail import b64decode

MAX_BODY = 200_000


@dataclass
class AttachmentInfo:
    part_id: str
    filename: str
    mime_type: str
    size: int
    attachment_id: str | None
    inline_data: bytes | None = None


@dataclass
class Parsed:
    gmail_id: str
    thread_id: str
    label_ids: list[str]
    internal_ts: int
    history_id: str
    snippet: str
    headers: dict[str, str]
    rfc_message_id: str
    in_reply_to: str
    references: str
    subject: str
    from_name: str
    from_addr: str
    reply_to: str
    to: list[str]
    cc: list[str]
    delivered_to: list[str]
    body_text: str
    bcc: list[str] = field(default_factory=list)
    attachments: list[AttachmentInfo] = field(default_factory=list)
    content_type: str = ""
    dsn_texts: list[str] = field(default_factory=list)
    dsn_attachment_ids: list[str] = field(default_factory=list)
    links_note: str = ""   # stored after body_text; not part of it, so opt-out and bounce checks don't read it


def _decode_header(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _addrs(value: str) -> list[str]:
    return [a.lower() for _, a in getaddresses([value]) if a and "@" in a]


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "blockquote", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a":
            self._href, self._anchor = dict(attrs).get("href"), []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script", "head") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a" and self._href:
            self.links.append((" ".join("".join(self._anchor).split()), self._href.strip()))
            self._href = None

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)
            if self._href is not None:
                self._anchor.append(data)


def html_to_text(src: str) -> str:
    p = _HTMLText()
    try:
        p.feed(src)
        text = "".join(p.out)
    except Exception:
        text = html.unescape(re.sub(r"<[^>]+>", " ", src))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def html_links(src: str) -> list[tuple[str, str]]:
    """(anchor text, href) for each http(s) link in an HTML body, first occurrence of each href."""
    p = _HTMLText()
    try:
        p.feed(src)
    except Exception:
        pass
    seen: dict[str, str] = {}
    for text, href in p.links:
        if href.lower().startswith(("http://", "https://")) and href not in seen:
            seen[href] = text
    return [(t, h) for h, t in seen.items()]


def _links_note(body_text: str, htmls: list[str], limit: int = 40) -> str:
    """The HTML part's links the text body doesn't show, as a note to store after it. A verification "button" is an
    <a href> that the text version often omits (Softonic's), which left a bot nothing to click."""
    missing = [(t, h) for t, h in html_links("\n".join(htmls)) if h not in body_text][:limit]
    if not missing:
        return ""
    return "\n\n[Links in the HTML version]\n" + "\n".join(f"- {t or '(no text)'}: {h}" for t, h in missing)


def _charset(part: dict[str, Any]) -> str:
    for h in part.get("headers", []):
        if h["name"].lower() == "content-type":
            m = re.search(r'charset="?([\w.-]+)', h["value"], re.IGNORECASE)
            if m:
                return m.group(1)
    return "utf-8"


def _decode_body(part: dict[str, Any]) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    raw = b64decode(data)
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def parse_message(msg: dict[str, Any]) -> Parsed:
    payload = msg.get("payload", {})
    headers: dict[str, str] = {}
    delivered: list[str] = []
    for h in payload.get("headers", []):
        name = h["name"].lower()
        if name == "delivered-to":
            delivered += _addrs(h["value"])
        headers.setdefault(name, _decode_header(h["value"]))

    plain: list[str] = []
    htmls: list[str] = []
    attachments: list[AttachmentInfo] = []
    dsn_texts: list[str] = []
    dsn_att: list[str] = []

    def walk(part: dict[str, Any], inside_rfc822: bool) -> None:
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body", {})
        filename = part.get("filename") or ""
        if mime == "message/delivery-status":
            if body.get("data"):
                dsn_texts.append(_decode_body(part))
            elif body.get("attachmentId"):
                dsn_att.append(body["attachmentId"])
            return
        if filename and (body.get("attachmentId") or body.get("data")):
            attachments.append(
                AttachmentInfo(
                    part_id=part.get("partId", ""),
                    filename=filename,
                    mime_type=mime,
                    size=int(body.get("size", 0)),
                    attachment_id=body.get("attachmentId"),
                    inline_data=b64decode(body["data"]) if body.get("data") else None,
                )
            )
            return
        if not inside_rfc822:
            if mime == "text/plain":
                plain.append(_decode_body(part))
            elif mime == "text/html":
                htmls.append(_decode_body(part))
        for sub in part.get("parts", []) or []:
            walk(sub, inside_rfc822 or mime == "message/rfc822")

    walk(payload, False)
    body_text = "\n".join(t for t in plain if t.strip())
    if not body_text and htmls:
        body_text = html_to_text("\n".join(htmls))
    name, addr = parseaddr(headers.get("from", ""))
    return Parsed(
        gmail_id=msg["id"],
        thread_id=msg["threadId"],
        label_ids=list(msg.get("labelIds", [])),
        internal_ts=int(msg.get("internalDate", 0)),
        history_id=str(msg.get("historyId", "")),
        snippet=html.unescape(msg.get("snippet", "")),
        headers=headers,
        rfc_message_id=headers.get("message-id", "").strip(),
        in_reply_to=headers.get("in-reply-to", "").strip(),
        references=headers.get("references", "").strip(),
        subject=headers.get("subject", ""),
        from_name=_decode_header(name),
        from_addr=addr.lower(),
        reply_to=(parseaddr(headers.get("reply-to", ""))[1] or "").lower(),
        to=_addrs(headers.get("to", "")),
        cc=_addrs(headers.get("cc", "")),
        delivered_to=delivered,
        bcc=_addrs(headers.get("bcc", "")),
        body_text=body_text[:MAX_BODY],
        links_note=_links_note(body_text[:MAX_BODY], htmls) if htmls else "",
        attachments=attachments,
        content_type=headers.get("content-type", ""),
        dsn_texts=dsn_texts,
        dsn_attachment_ids=dsn_att,
    )
