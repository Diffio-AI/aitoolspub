"""Thin, thread-safe Gmail + Pub/Sub REST clients using the service account key (domain-wide delegation).

Everything here is blocking; callers run it in a worker thread. The `MailboxAPI`
protocol is what sync/send code depends on, so tests can swap in a fake.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

log = logging.getLogger(__name__)

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
PUBSUB_SCOPE = "https://www.googleapis.com/auth/pubsub"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
PUBSUB = "https://pubsub.googleapis.com/v1"


class GmailError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Gmail API {status}: {message}")
        self.status = status


class HistoryExpired(GmailError):
    pass


class MailboxAPI(Protocol):
    address: str

    def profile(self) -> dict[str, Any]: ...
    def list_message_ids(self, query: str) -> list[str]: ...
    def get_message(self, msg_id: str, fmt: str = "full") -> dict[str, Any] | None: ...
    def history(self, start_history_id: str) -> tuple[list[dict[str, Any]], str]: ...
    def send(self, raw_b64: str, thread_id: str | None) -> dict[str, Any]: ...
    def watch(self, topic: str) -> dict[str, Any]: ...
    def get_attachment(self, msg_id: str, attachment_id: str) -> bytes: ...
    def labels(self) -> list[dict[str, Any]]: ...
    def create_label(self, name: str) -> dict[str, Any]: ...
    def send_as(self) -> list[dict[str, Any]]: ...
    def trash(self, msg_id: str) -> None: ...
    def untrash(self, msg_id: str) -> None: ...
    def batch_modify(self, msg_ids: list[str], add: list[str], remove: list[str]) -> None: ...


def _b64decode(data: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class _Rest:
    def __init__(self, session: AuthorizedSession):
        self._session = session

    def call(self, method: str, url: str, *, ok404: bool = False, **kw: Any) -> Any:
        delay = 1.0
        for attempt in range(6):
            try:
                r = self._session.request(method, url, timeout=kw.pop("timeout", 60), **kw)
            except RefreshError as e:  # delegation not granted / bad subject: permanent
                raise GmailError(401, f"auth refresh failed: {str(e)[:200]}") from e
            except Exception as e:  # network errors
                if attempt == 5:
                    raise
                log.warning("%s %s failed (%s); retrying", method, url.split("?")[0], e)
                time.sleep(delay + random.random())
                delay *= 2
                continue
            if r.status_code == 404 and ok404:
                return None
            if r.status_code in (429, 500, 502, 503, 504) or (
                r.status_code == 403 and "rateLimitExceeded" in r.text
            ):
                if attempt == 5:
                    raise GmailError(r.status_code, r.text[:300])
                time.sleep(delay + random.random())
                delay *= 2
                continue
            if r.status_code >= 400:
                raise GmailError(r.status_code, r.text[:300])
            return r.json() if r.content else {}
        raise AssertionError("unreachable")


class GmailREST(_Rest):
    """Gmail API acting as one mailbox (domain-wide delegation subject)."""

    def __init__(self, key_file: Path, address: str):
        creds = service_account.Credentials.from_service_account_file(
            str(key_file), scopes=[GMAIL_SCOPE], subject=address
        )
        super().__init__(AuthorizedSession(creds))
        self.address = address

    def profile(self) -> dict[str, Any]:
        return self.call("GET", f"{GMAIL}/profile")

    def list_message_ids(self, query: str) -> list[str]:
        ids: list[str] = []
        token = None
        while True:
            params: dict[str, Any] = {"q": query, "maxResults": 500, "includeSpamTrash": "true"}
            if token:
                params["pageToken"] = token
            r = self.call("GET", f"{GMAIL}/messages", params=params)
            ids += [m["id"] for m in r.get("messages", [])]
            token = r.get("nextPageToken")
            if not token:
                return ids

    def get_message(self, msg_id: str, fmt: str = "full") -> dict[str, Any] | None:
        return self.call("GET", f"{GMAIL}/messages/{msg_id}", params={"format": fmt}, ok404=True)

    def history(self, start_history_id: str) -> tuple[list[dict[str, Any]], str]:
        out: list[dict[str, Any]] = []
        token = None
        latest = start_history_id
        while True:
            params: dict[str, Any] = {
                "startHistoryId": start_history_id,
                "maxResults": 500,
                "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
            }
            if token:
                params["pageToken"] = token
            r = self.call("GET", f"{GMAIL}/history", params=params, ok404=True)
            if r is None:
                raise HistoryExpired(404, f"historyId {start_history_id} expired")
            out += r.get("history", [])
            latest = r.get("historyId", latest)
            token = r.get("nextPageToken")
            if not token:
                return out, latest

    def send(self, raw_b64: str, thread_id: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"raw": raw_b64}
        if thread_id:
            body["threadId"] = thread_id
        return self.call("POST", f"{GMAIL}/messages/send", json=body)

    def watch(self, topic: str) -> dict[str, Any]:
        return self.call("POST", f"{GMAIL}/watch", json={"topicName": topic})

    def get_attachment(self, msg_id: str, attachment_id: str) -> bytes:
        r = self.call("GET", f"{GMAIL}/messages/{msg_id}/attachments/{attachment_id}")
        return _b64decode(r.get("data", ""))

    def labels(self) -> list[dict[str, Any]]:
        return self.call("GET", f"{GMAIL}/labels").get("labels", [])

    def create_label(self, name: str) -> dict[str, Any]:
        return self.call(
            "POST",
            f"{GMAIL}/labels",
            json={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
        )

    def send_as(self) -> list[dict[str, Any]]:
        return self.call("GET", f"{GMAIL}/settings/sendAs").get("sendAs", [])

    def trash(self, msg_id: str) -> None:
        self.call("POST", f"{GMAIL}/messages/{msg_id}/trash")

    def untrash(self, msg_id: str) -> None:
        self.call("POST", f"{GMAIL}/messages/{msg_id}/untrash")

    def batch_modify(self, msg_ids: list[str], add: list[str], remove: list[str]) -> None:
        for i in range(0, len(msg_ids), 1000):
            self.call("POST", f"{GMAIL}/messages/batchModify",
                      json={"ids": msg_ids[i:i + 1000], "addLabelIds": add, "removeLabelIds": remove})


class GmailFactory:
    """Caches one client per (thread, mailbox); AuthorizedSession isn't shared across threads."""

    def __init__(self, key_file: Path):
        self.key_file = key_file
        self._local = threading.local()

    def __call__(self, address: str) -> MailboxAPI:
        cache = getattr(self._local, "clients", None)
        if cache is None:
            cache = self._local.clients = {}
        if address not in cache:
            cache[address] = GmailREST(self.key_file, address)
        return cache[address]


class PubSubREST(_Rest):
    def __init__(self, key_file: Path):
        creds = service_account.Credentials.from_service_account_file(str(key_file), scopes=[PUBSUB_SCOPE])
        super().__init__(AuthorizedSession(creds))

    def pull(self, subscription: str, max_messages: int = 100) -> list[dict[str, Any]]:
        r = self.call(
            "POST", f"{PUBSUB}/{subscription}:pull", json={"maxMessages": max_messages}, timeout=120
        )
        return r.get("receivedMessages", [])

    def ack(self, subscription: str, ack_ids: list[str]) -> None:
        if ack_ids:
            self.call("POST", f"{PUBSUB}/{subscription}:acknowledge", json={"ackIds": ack_ids})


b64decode = _b64decode
