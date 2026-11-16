from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fakes import FakeWorld

from agent_mail.config import parse_config
from agent_mail.service import App

A = "a@ours.test"
A_ALIAS = "hello@alias.test"
B = "b@ours.test"
EXT = "pat@ext.test"


def make_config(tmp: Path, **over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "service": {"key_file": str(tmp / "key.json"), "state_dir": str(tmp / "state")},
        "pubsub": {"project": ""},
        "mailbox": [
            {"address": A, "display_name": "Alice", "aliases": [{"address": A_ALIAS, "display_name": "Hello"}]},
            {"address": B, "display_name": "Bob"},
        ],
    }
    raw.update(over)
    return raw


class Env:
    def __init__(self, tmp: Path, raw: dict[str, Any] | None = None):
        self.world = FakeWorld()
        for addr in (A, B):
            self.world.box(addr)
        self.cfg = parse_config(raw or make_config(tmp))
        self.app = App(self.cfg, gmail=self.world)  # type: ignore[arg-type]

    @property
    def store(self):  # type: ignore[no-untyped-def]
        return self.app.store

    async def sync(self) -> None:
        for mb in self.cfg.mailboxes:
            await self.app.syncer.refresh_metadata(mb)
            await self.app.syncer.sync(mb)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)
