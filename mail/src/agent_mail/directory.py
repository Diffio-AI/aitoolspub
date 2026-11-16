"""Address -> mailbox resolution: config aliases plus send-as aliases discovered from Gmail."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .store import Store


@dataclass(frozen=True)
class Identity:
    address: str
    mailbox: str
    display_name: str


class Directory:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self._map: dict[str, Identity] = {}
        self.reload()

    def reload(self) -> None:
        m: dict[str, Identity] = {}
        for row in self.store.discovered_aliases():
            mb = self.cfg.mailboxes.get(row["mailbox"])
            if mb:
                m[row["address"]] = Identity(row["address"], mb.address, row["display_name"] or mb.display_name)
        for mb in self.cfg.mailboxes.values():
            for a in mb.aliases:
                m[a.address] = Identity(a.address, mb.address, a.display_name)
            m[mb.address] = Identity(mb.address, mb.address, mb.display_name)
        self._map = m

    def resolve(self, address: str) -> Identity | None:
        return self._map.get(address.strip().lower())

    def addresses_of(self, mailbox: str) -> list[str]:
        return sorted(a for a, i in self._map.items() if i.mailbox == mailbox)

    def own_addresses(self) -> set[str]:
        return set(self._map)

    def mailbox_of(self, address: str) -> str | None:
        i = self.resolve(address)
        return i.mailbox if i else None
