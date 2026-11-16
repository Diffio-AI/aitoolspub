"""Copy sign-ins from your own Chrome into the shared agent browser.

Sites that put the Steel browser through captchas or refuse its sign-ins ("try a different browser") usually let
your everyday Chrome straight in. Sign in there, then copy that site's cookies over: the agent browser is then
signed in as you, in every agent session, and stays signed in across restarts (cookies are kept in its profile).

    agent-browser-import-cookies --list-profiles
    agent-browser-import-cookies --profile "Claude Personal" --domain claude.ai --dry-run
    agent-browser-import-cookies --profile "Claude Personal" --domain claude.ai
    agent-browser-import-cookies --profile "Person 1" --domain claude.ai --to production

A whole profile can't just be copied into the container: Chrome encrypts cookie values with a key from your login
keyring (GNOME keyring here), which the container's Chromium doesn't have. So this reads the profile's cookie
database (a copy; Chrome can keep running), decrypts the values with the keyring's "Chrome Safe Storage" secret,
and sets them in the agent browser over CDP. Values are never printed.

Only the domains you name are copied (`--all` copies everything, which hands every agent every account the profile
is signed into). Sign-ins that keep their token in localStorage rather than cookies don't carry over.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import websockets
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

CHROME_DIR = Path.home() / ".config" / "google-chrome"
AGENT_PROFILES = Path(__file__).resolve().parents[3] / "steel" / "profiles.json"
EPOCH_DELTA = 11644473600  # seconds from 1601-01-01 (Chrome's epoch) to 1970-01-01
SAME_SITE = {0: "None", 1: "Lax", 2: "Strict"}
PRIORITY = {0: "Low", 1: "Medium", 2: "High"}


def profiles(chrome_dir: Path) -> list[dict]:
    state = json.loads((chrome_dir / "Local State").read_text())
    cache = state.get("profile", {}).get("info_cache", {})
    return [{"dir": d, "name": v.get("name", ""), "account": v.get("user_name", "")} for d, v in sorted(cache.items())]


def profile_dir(chrome_dir: Path, wanted: str) -> Path:
    for p in profiles(chrome_dir):
        if wanted in (p["dir"], p["name"], p["account"]):
            return chrome_dir / p["dir"]
    sys.exit(f"no Chrome profile called {wanted!r}; --list-profiles shows them")


def _key(password: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1)
    return kdf.derive(password)


def keyring_password() -> bytes | None:
    """Chrome's "Chrome Safe Storage" secret from the Secret Service (GNOME keyring / KWallet bridge)."""
    import secretstorage

    bus = secretstorage.dbus_init()
    for item in secretstorage.search_items(bus, {"application": "chrome"}):
        if item.is_locked():
            item.unlock()
        return item.get_secret()
    return None


class Decryptor:
    def __init__(self, db_version: int):
        self.strip_hash = db_version >= 24  # newer databases prefix the value with sha256(host_key)
        self.v10 = _key(b"peanuts")  # the fixed key Chrome uses without a keyring (and for old v10 values)
        pw = keyring_password()
        self.v11 = _key(pw) if pw else None

    def __call__(self, host: str, blob: bytes, plain: str) -> str | None:
        if not blob:
            return plain
        prefix, data = blob[:3], blob[3:]
        key = {b"v10": self.v10, b"v11": self.v11}.get(prefix)
        if key is None:
            return None
        dec = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        raw = dec.update(data) + dec.finalize()
        unpad = padding.PKCS7(128).unpadder()
        try:
            raw = unpad.update(raw) + unpad.finalize()
        except ValueError:
            return None  # wrong key
        if self.strip_hash:
            if raw[:32] != hashlib.sha256(host.encode()).digest():
                return None
            raw = raw[32:]
        try:
            return raw.decode()
        except UnicodeDecodeError:
            return None


def matches(host: str, domains: list[str]) -> bool:
    h = host.lstrip(".").lower()
    return any(h == d or h.endswith("." + d) for d in domains)


def read_cookies(profile: Path, domains: list[str] | None) -> tuple[list[dict], dict]:
    """The profile's cookies for `domains` (all when None), as CDP CookieParam dicts."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "Cookies"
        shutil.copy2(profile / "Cookies", db)
        for extra in ("-journal", "-wal"):
            if (profile / f"Cookies{extra}").exists():
                shutil.copy2(profile / f"Cookies{extra}", Path(tmp) / f"Cookies{extra}")
        con = sqlite3.connect(db)
        version = int(con.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0])
        rows = con.execute("SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly,"
                           " samesite, priority, top_frame_site_key FROM cookies").fetchall()
        con.close()
    decrypt = Decryptor(version)
    out, skipped = [], {"undecryptable": 0, "partitioned": 0, "expired": 0}
    now = time.time()
    for host, name, plain, blob, path, expires, secure, httponly, samesite, priority, partition in rows:
        if domains is not None and not matches(host, domains):
            continue
        if partition:
            skipped["partitioned"] += 1  # third-party-context cookies (CHIPS); not needed for a sign-in
            continue
        exp = expires / 1e6 - EPOCH_DELTA if expires else None
        if exp is not None and exp < now:
            skipped["expired"] += 1
            continue
        value = decrypt(host, blob, plain)
        if value is None:
            skipped["undecryptable"] += 1
            continue
        c: dict = {"name": name, "value": value, "path": path or "/", "secure": bool(secure),
                   "httpOnly": bool(httponly), "priority": PRIORITY.get(priority, "Medium")}
        if host.startswith("."):
            c["domain"] = host
        else:  # host-only cookie: set it through a URL so it stays host-only
            c["url"] = f"{'https' if secure else 'http'}://{host}{path or '/'}"
        if samesite in SAME_SITE:
            c["sameSite"] = SAME_SITE[samesite]
        if exp is not None:
            c["expires"] = exp
        out.append(c)
    return out, skipped


async def set_cookies(cdp_url: str, cookies: list[dict]) -> list[dict]:
    """Set them in the browser's default (persistent) context; return what it then holds for those hosts."""
    async with websockets.connect(cdp_url, max_size=None, open_timeout=10) as ws:
        ids = itertools.count(1)

        async def send(method: str, params: dict | None = None) -> dict:
            i = next(ids)
            await ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
            while True:
                r = json.loads(await asyncio.wait_for(ws.recv(), 20))
                if r.get("id") == i:
                    if "error" in r:
                        raise RuntimeError(f"{method}: {r['error'].get('message')}")
                    return r["result"]

        failed = []
        for c in cookies:  # one at a time, so one bad cookie doesn't sink the rest
            try:
                await send("Storage.setCookies", {"cookies": [c]})
            except RuntimeError as e:
                failed.append({"name": c["name"], "host": c.get("domain") or c.get("url"), "error": str(e)})
        held = (await send("Storage.getCookies"))["cookies"]
        return [{"failed": failed}, {"held": held}]


def agent_cdp(name: str | None) -> str:
    """The CDP websocket of an agent browser profile (steel/profiles.json); the default one when no name."""
    if not AGENT_PROFILES.is_file():
        if name:
            sys.exit(f"no {AGENT_PROFILES}; give --cdp instead")
        return "ws://localhost:3103/"
    profiles = json.loads(AGENT_PROFILES.read_text())
    if name is None:
        name = next((n for n, p in profiles.items() if p.get("default")), next(iter(profiles)))
    if name not in profiles:
        sys.exit(f"no agent browser profile {name!r}; the profiles are {', '.join(profiles)}")
    return profiles[name]["cdp"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chrome-dir", type=Path, default=CHROME_DIR, help=f"Chrome's user data dir (default {CHROME_DIR})")
    ap.add_argument("--profile", help='profile directory, name or account, e.g. "Profile 3" or "Claude Personal"')
    ap.add_argument("--domain", action="append", default=[], help="site to copy (with subdomains); repeatable")
    ap.add_argument("--all", action="store_true", help="copy every cookie in the profile")
    ap.add_argument("--to", metavar="AGENT_PROFILE",
                    help=f"agent browser profile to copy into, from {AGENT_PROFILES} (default: its default profile)")
    ap.add_argument("--cdp", help="the agent browser's CDP websocket, instead of --to")
    ap.add_argument("--dry-run", action="store_true", help="list what would be copied (names only), copy nothing")
    ap.add_argument("--list-profiles", action="store_true")
    a = ap.parse_args()

    if a.list_profiles:
        for p in profiles(a.chrome_dir):
            print(f"{p['dir']:<12} {p['name']:<20} {p['account']}")
        return
    if not a.profile or not (a.domain or a.all):
        ap.error("give --profile and at least one --domain (or --all)")
    cdp = a.cdp or agent_cdp(a.to)
    domains = None if a.all else [d.lower().lstrip(".") for d in a.domain]
    prof = profile_dir(a.chrome_dir, a.profile)
    cookies, skipped = read_cookies(prof, domains)
    hosts = sorted({(c.get("domain") or c["url"].split("/")[2]).lstrip(".") for c in cookies})
    print(f"{prof.name}: {len(cookies)} cookies for {', '.join(hosts) or 'nothing'}"
          + (f" (skipped {', '.join(f'{v} {k}' for k, v in skipped.items() if v)})" if any(skipped.values()) else ""))
    if a.dry_run:
        for c in cookies:
            print(f"  {(c.get('domain') or c['url']):<40} {c['name']}")
        return
    if not cookies:
        sys.exit("nothing to copy: sign into the site in that Chrome profile first")
    failed, held = asyncio.run(set_cookies(cdp, cookies))
    names = {(c["name"], c.get("domain", "").lstrip(".") or c["url"].split("/")[2]) for c in cookies}
    present = sum(1 for h in held["held"] if (h["name"], h["domain"].lstrip(".")) in names)
    print(f"set {len(cookies) - len(failed['failed'])} of {len(cookies)} in the agent browser ({a.to or cdp}); "
          f"it now holds {present}")
    for f in failed["failed"]:
        print(f"  failed: {f['host']} {f['name']}: {f['error']}")


if __name__ == "__main__":
    main()
