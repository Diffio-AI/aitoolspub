"""Sign the agents' own Google account in to an agent browser profile, on demand, with no person involved.

The account's email, password and authenticator secret live in keys/google-account.json (mode 600, git-ignored):
{"email": ..., "password": ..., "totp_secret": ..., "profile": "testing"}.
This reads them and drives Google's sign-in in its own window: email, password, and when Google asks for a second
step, a code from the authenticator secret (it steps past "Google prompt"/passkey challenges via "Try another way").
Nothing secret is printed; only the steps taken and the outcome.

    agent-browser-google-login              # signed in already? say so; otherwise sign in (the file's profile)
    agent-browser-google-login --check      # only report whether it's signed in
    agent-browser-google-login --code       # print the current 6-digit authenticator code, for a site that asks
    agent-browser-google-login --to production --account keys/other.json

Once the profile is signed in to Google, "Sign in with Google" on any site in that profile is a click on the account.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import struct
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from playwright.async_api import Page, async_playwright

from .import_cookies import agent_cdp

DEFAULT_ACCOUNT = Path(__file__).resolve().parents[3] / "keys" / "google-account.json"
DONE_URL = "https://myaccount.google.com/"


def totp(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 code (SHA-1, 30 s), the kind Google Authenticator shows."""
    s = secret.replace(" ", "").upper()
    key = base64.b32decode(s + "=" * (-len(s) % 8))
    h = hmac.new(key, struct.pack(">Q", int((time.time() if at is None else at) // step)), hashlib.sha1).digest()
    o = h[-1] & 15
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


def fresh_code(secret: str) -> str:
    """A code with at least 5 s left in its window (waits for the next one otherwise)."""
    left = 30 - time.time() % 30
    if left < 5:
        time.sleep(left + 0.5)
    return totp(secret)


def load_account(path: Path) -> dict:
    try:
        acc = json.loads(path.read_text())
    except FileNotFoundError:
        sys.exit(f"no account file {path}")
    if not acc.get("email") or not acc.get("password"):
        sys.exit(f"{path} needs email and password")
    return acc


async def visible(page: Page, selector: str) -> bool:
    try:
        return await page.locator(selector).first.is_visible()
    except Exception:  # noqa: BLE001 - mid-navigation
        return False


async def signed_in(page: Page, email: str) -> bool:
    try:
        await page.goto(f"{DONE_URL}?authuser={quote(email)}", wait_until="domcontentloaded", timeout=45000)
    except Exception:  # noqa: BLE001, S110 - a slow load still lands somewhere we can check
        pass
    await asyncio.sleep(2)
    if urlparse(page.url).hostname != "myaccount.google.com":
        return False
    return email.lower() in (await page.evaluate("document.body.innerText")).lower()


async def sign_in(page: Page, acc: dict, log, timeout: float = 150) -> bool:
    email, password, secret = acc["email"], acc["password"], acc.get("totp_secret")
    url = (f"https://accounts.google.com/v3/signin/identifier?continue={quote(DONE_URL, safe='')}"
           f"&login_hint={quote(email)}&flowName=GlifWebSignIn")
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    start, last, tried_other, typed = time.monotonic(), None, 0, set()
    while time.monotonic() - start < timeout:
        await asyncio.sleep(1.5)
        host, path = urlparse(page.url).hostname, urlparse(page.url).path
        if host == "myaccount.google.com":
            return True
        step = None
        if await visible(page, "#identifierId"):
            step = "email"
            if step not in typed or last != step:
                await page.fill("#identifierId", email)
                await page.keyboard.press("Enter")
        elif await visible(page, "input[name=Passwd]"):
            step = "password"
            if last != step:
                await page.fill("input[name=Passwd]", password)
                await page.keyboard.press("Enter")
        elif await visible(page, "input[name=totpPin], #totpPin"):
            step = "authenticator code"
            if not secret:
                log("Google asks for an authenticator code and the account file has no totp_secret")
                return False
            if last != step:
                await page.fill("input[name=totpPin], #totpPin", fresh_code(secret))
                await page.keyboard.press("Enter")
        elif await visible(page, f"[data-identifier='{email}' i], [data-email='{email}' i]"):
            step = "account chooser"
            if last != step:
                await page.locator(f"[data-identifier='{email}' i], [data-email='{email}' i]").first.click()
        elif "/challenge/selection" in path:
            step = "choose a second step"
            option = page.get_by_text("Google Authenticator", exact=False).or_(
                page.get_by_text("authenticator app", exact=False))
            if secret and await option.count():
                await option.first.click()
            else:
                log("Google offers no authenticator option here: " + await body(page))
                return False
        elif "/challenge/" in path and "/challenge/recaptcha" not in path:
            step = f"challenge {path.split('/challenge/')[-1]}"
            other = page.get_by_role("button", name="Try another way")
            if secret and tried_other < 3 and await other.count():
                tried_other += 1
                await other.first.click()
            elif last == step and time.monotonic() - start > 60:
                log(f"stuck on {step}: " + await body(page))
                return False
        elif "/speedbump" in path or "/signin/rejected" in path:
            log(f"Google stopped the sign-in ({path}): " + await body(page))
            return False
        if step and step != last:
            log(step)
            typed.add(step)
        last = step or last
    log("timed out on " + page.url.split("?")[0] + ": " + await body(page))
    return False


async def body(page: Page) -> str:
    try:
        return (await page.evaluate("document.body.innerText"))[:400].replace("\n", " / ")
    except Exception:  # noqa: BLE001
        return ""


async def run(cdp_url: str, acc: dict, check_only: bool) -> int:
    def log(m: str) -> None:
        print(f"  {m}", flush=True)

    async with async_playwright() as pw:
        b = await pw.chromium.connect_over_cdp(cdp_url)
        page = await b.contexts[0].new_page()
        try:
            if await signed_in(page, acc["email"]):
                print(f"{acc['email']} is signed in to Google in this profile")
                return 0
            if check_only:
                print(f"{acc['email']} is not signed in to Google in this profile")
                return 1
            print(f"signing {acc['email']} in to Google")
            ok = await sign_in(page, acc, log) and await signed_in(page, acc["email"])
            print("signed in" if ok else "sign-in did not finish")
            return 0 if ok else 1
        finally:
            await page.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT, help=f"account file (default {DEFAULT_ACCOUNT})")
    ap.add_argument("--to", metavar="AGENT_PROFILE", help="agent browser profile (default: the account file's "
                    "'profile', else the default one)")
    ap.add_argument("--cdp", help="the browser's CDP websocket, instead of --to")
    ap.add_argument("--check", action="store_true", help="only report whether the account is signed in")
    ap.add_argument("--code", action="store_true", help="print the current authenticator code and exit")
    a = ap.parse_args()
    acc = load_account(a.account)
    if a.code:
        if not acc.get("totp_secret"):
            sys.exit(f"{a.account} has no totp_secret")
        print(fresh_code(acc["totp_secret"]))
        return
    sys.exit(asyncio.run(run(a.cdp or agent_cdp(a.to or acc.get("profile")), acc, a.check)))


if __name__ == "__main__":
    main()
