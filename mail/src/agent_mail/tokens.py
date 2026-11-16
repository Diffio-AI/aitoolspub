"""Bearer tokens, one per client name, all with full access. The service stores only SHA-256 hashes
(state_dir/tokens.json)."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenStore:
    def __init__(self, path: Path):
        self.path = path
        self._mtime = -1.0
        self._by_hash: dict[str, tuple[str, str]] = {}

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"clients": {}}

    def _refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            mtime = 0.0
        if mtime != self._mtime:
            data = self._load()
            self._by_hash = {h: ("client", n) for n, h in data.get("clients", {}).items()}
            self._mtime = mtime

    def identify(self, token: str) -> tuple[str, str] | None:
        """Return (kind, name) for a valid token."""
        self._refresh()
        return self._by_hash.get(_hash(token))

    def issue(self, kind: str, name: str, out: Path | None) -> str:
        token = f"amt_{secrets.token_urlsafe(32)}"
        data = self._load()
        data.setdefault(kind + "s", {})[name] = _hash(token)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        if out:
            out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token + "\n")
        return token

    def revoke(self, kind: str, name: str) -> bool:
        data = self._load()
        if data.get(kind + "s", {}).pop(name, None) is None:
            return False
        self.path.write_text(json.dumps(data, indent=2))
        return True

    def names(self) -> list[str]:
        return sorted(self._load().get("clients", {}))
