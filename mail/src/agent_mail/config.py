"""Service configuration (TOML): where the key and state live, Pub/Sub, and the mailboxes to mirror."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.toml"
ALL_DAYS = 1_000_000  # backfill_days = 0 means all mail; stored as this many days


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Alias:
    address: str
    display_name: str


@dataclass(frozen=True)
class Mailbox:
    address: str
    display_name: str
    aliases: tuple[Alias, ...] = ()
    send: bool = True  # agents may send from this mailbox and its aliases

    def addresses(self) -> set[str]:
        return {self.address, *(a.address for a in self.aliases)}


@dataclass(frozen=True)
class Config:
    key_file: Path
    state_dir: Path
    listen_host: str
    listen_port: int
    backfill_days: int  # 0 = all mail
    catchup_minutes: float
    watch_renew_hours: float
    attachment_max_bytes: int
    pubsub_project: str
    pubsub_topic: str
    pubsub_subscription: str
    mailboxes: dict[str, Mailbox]

    @property
    def backfill_window(self) -> int:
        return self.backfill_days if self.backfill_days > 0 else ALL_DAYS

    @property
    def db_path(self) -> Path:
        return self.state_dir / "mail.db"

    @property
    def attachments_dir(self) -> Path:
        return self.state_dir / "attachments"

    @property
    def tokens_path(self) -> Path:
        return self.state_dir / "tokens.json"

    @property
    def topic_path(self) -> str:
        return f"projects/{self.pubsub_project}/topics/{self.pubsub_topic}"

    @property
    def subscription_path(self) -> str:
        return f"projects/{self.pubsub_project}/subscriptions/{self.pubsub_subscription}"


def _lower(s: str) -> str:
    return s.strip().lower()


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    path = Path(path or os.environ.get("AGENT_MAIL_CONFIG") or DEFAULT_CONFIG)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return parse_config(raw, base=path.parent)


def parse_config(raw: dict[str, Any], base: Path | None = None) -> Config:
    base = base or Path.cwd()
    svc = raw.get("service", {})
    ps = raw.get("pubsub", {})

    def p(value: str) -> Path:
        q = Path(os.path.expanduser(value))
        return q if q.is_absolute() else (base / q).resolve()

    mailboxes: dict[str, Mailbox] = {}
    for m in raw.get("mailbox", []):
        addr = _lower(m["address"])
        name = m.get("display_name", "")
        aliases = []
        for a in m.get("aliases", []):
            if isinstance(a, str):
                aliases.append(Alias(_lower(a), name))
            else:
                aliases.append(Alias(_lower(a["address"]), a.get("display_name", name)))
        mailboxes[addr] = Mailbox(addr, name, tuple(aliases), bool(m.get("send", True)))
    if not mailboxes:
        raise ConfigError("no [[mailbox]] configured")
    if "key_file" not in svc:
        raise ConfigError("service.key_file is required")
    return Config(
        key_file=p(svc["key_file"]),
        state_dir=p(svc.get("state_dir", "state")),
        listen_host=svc.get("listen_host", "127.0.0.1"),
        listen_port=int(svc.get("listen_port", 8766)),
        backfill_days=int(svc.get("backfill_days", 0)),
        catchup_minutes=float(svc.get("catchup_minutes", 15)),
        watch_renew_hours=float(svc.get("watch_renew_hours", 24)),
        attachment_max_bytes=int(svc.get("attachment_max_bytes", 10 * 1024 * 1024)),
        pubsub_project=ps.get("project", ""),
        pubsub_topic=ps.get("topic", "gmail-mail-service"),
        pubsub_subscription=ps.get("subscription", "agent-mail-pull"),
        mailboxes=mailboxes,
    )
