"""Sessions: one browser window each, with its form state and an append-only packet (JSONL) of what happened.

Every session of every agent lives in the same Chrome and its default context, so cookies, logins and site storage
are shared by all of them: sign in once (by hand in the live viewer, or in any session) and every session is signed
in. Each session gets its own *window* rather than a tab, so every session's page is the visible, foreground page of
its window: background tabs are throttled, and two agents can't fight over which tab is in front.

Windows are opened by `window.open` from a launcher page that stays open in the browser (one per Chrome, found again
by its title by every process). That gives each session tab an opener, which keeps a concurrent navigator run from
adopting it (see locks.py).

Every tab a session holds (its window, and any popup its pages open) is listed in this process's registry file
(registry.py), so the janitor can close the tabs of a server that died without closing them, and sessions left idle.
Closing a session closes all of its tabs.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from .forms import FormState
from .locks import browser_lock
from .redact import redact
from .registry import DEFAULT_DIR, Registry

LAUNCHER_TITLE = "agent-browser launcher (keep open)"
LAUNCHER_HTML = (f"<title>{LAUNCHER_TITLE}</title><body style='font:14px sans-serif;padding:2em'>"
                 "<p>The agent browser opens each agent session's window from this tab. Leave it open; it is "
                 "recreated if closed.</p></body>")
WINDOW_FEATURES = "popup,width=1440,height=900"


class Packet:
    """Everything a session saw, planned, typed and got back, for audits and for replaying with another agent."""

    def __init__(self, path: Path | None):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, **data: Any) -> None:
        if not self.path:
            return
        with self.path.open("a") as f:
            f.write(redact(json.dumps({"t": round(time.time(), 3), "kind": kind, **data}, ensure_ascii=False,
                                      default=str)) + "\n")


@dataclass
class Session:
    id: str
    page: Page
    packet: Packet
    purpose: str = ""
    state: FormState = field(default_factory=FormState)
    created: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    nav_runs: int = 0
    pages: list[Page] = field(default_factory=list)  # every tab this session holds: its window and their popups

    def reset_forms(self) -> None:
        """A different document: old stamps and plans no longer describe anything on the page."""
        self.state = FormState(carried=self.state.carry())


class Sessions:
    def __init__(self, cdp_url: str, state_dir: Path | None, *, agent: str = "", profile: str = "",
                 id_prefix: str = "s", registry: Path | None = DEFAULT_DIR):
        self.cdp_url = cdp_url
        self.registry = Registry(cdp_url, agent=agent, directory=registry)
        self.profile = profile  # the browser profile's name (one Chrome each), in packets and listings
        self.id_prefix = id_prefix
        self.state_dir = state_dir
        self.agent = agent
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self._lock = asyncio.Lock()
        self._browser_local = asyncio.Lock()
        self.items: dict[str, Session] = {}

    def browser_lock(self, *, shared: bool = False):
        """Shared for navigator runs, exclusive for opening a tab without an opener (see locks.py)."""
        return browser_lock(self.cdp_url, self._browser_local, shared=shared)

    async def context(self) -> BrowserContext:
        async with self._lock:
            if self._ctx is None or self._browser is None or not self._browser.is_connected():
                if self._pw is None:
                    self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
                ctxs = self._browser.contexts
                self._ctx = ctxs[0] if ctxs else await self._browser.new_context()
            return self._ctx

    async def _launcher(self, ctx: BrowserContext) -> Page:
        """The browser's launcher page: found by its title (another process may have made it), else created under
        the exclusive lock, since it has no opener itself."""
        for p in ctx.pages:
            try:
                if not p.is_closed() and await p.title() == LAUNCHER_TITLE:
                    return p
            except Exception:  # noqa: BLE001 - a page mid-navigation or closing
                continue
        async with self.browser_lock():
            for p in ctx.pages:  # made by another process while this one waited for the lock
                try:
                    if not p.is_closed() and await p.title() == LAUNCHER_TITLE:
                        return p
                except Exception:  # noqa: BLE001
                    continue
            page = await ctx.new_page()
            await page.set_content(LAUNCHER_HTML)
            return page

    async def _new_window(self, ctx: BrowserContext) -> Page:
        # Other processes open windows in the same context at the same time, and each process sees every page
        # event, so the new window is recognised by a one-off window.name rather than by being the next page.
        for attempt in (1, 2):
            launcher = await self._launcher(ctx)
            name = "agentwin" + secrets.token_hex(6)
            found: asyncio.Future[Page] = asyncio.get_running_loop().create_future()

            async def check(p: Page) -> None:
                try:
                    if await p.evaluate("window.name") == name and not found.done():
                        found.set_result(p)
                except Exception:  # noqa: BLE001 - another process's page, closing or navigating
                    pass

            def on_page(p: Page) -> None:
                asyncio.ensure_future(check(p))

            ctx.on("page", on_page)
            try:
                await launcher.evaluate("([f, n]) => { window.open('about:blank', n, f) }", [WINDOW_FEATURES, name])
                page = await asyncio.wait_for(found, 10)
                await page.evaluate("() => { window.name = '' }")
                return page
            except Exception:  # noqa: BLE001 - launcher closed under us, or a popup blocker: try once more
                if attempt == 2:
                    break
            finally:
                ctx.remove_listener("page", on_page)
        # Last resort: a plain tab, opened under the exclusive lock so no navigator run adopts it.
        async with self.browser_lock():
            return await ctx.new_page()

    async def open(self, purpose: str = "") -> Session:
        ctx = await self.context()
        page = await self._new_window(ctx)
        sid = self.id_prefix + secrets.token_hex(3)
        path = None
        if self.state_dir:
            path = self.state_dir / time.strftime("%Y-%m-%d") / f"{time.strftime('%H%M%S')}-{sid}.jsonl"
        s = Session(sid, page, Packet(path), purpose)
        self.items[sid] = s
        await self._track(s, page, "main")
        s.packet.write("open", session=sid, agent=self.agent, purpose=purpose, cdp=self.cdp_url,
                       profile=self.profile or None)
        return s

    async def _track(self, s: Session, page: Page, role: str) -> None:
        """Count a tab as the session's: closed with it, and listed for the janitor. Popups it opens are too."""
        if page not in s.pages:
            s.pages.append(page)
        page.on("popup", lambda p: asyncio.ensure_future(self._track(s, p, "popup")))
        tid = await _target_id(page)
        if s.id not in self.items:  # the session closed while this popup was being looked up
            await _close_quietly(page)
            return
        if tid:
            self.registry.add(tid, s.id, role=role, purpose=s.purpose)
            page.on("close", lambda _p: self.registry.drop(target_id=tid))

    def hold(self, sid: str, seconds: float) -> None:
        """Keep the janitor off this session through a long background job (a recording, a person's handoff)."""
        self.registry.touch(sid, hold=seconds)

    def get(self, sid: str) -> Session:
        try:
            s = self.items[sid]
        except KeyError:
            raise KeyError(f"no session {sid!r}; open one first (open sessions: {', '.join(self.items) or 'none'})")
        if s.page.is_closed():
            # The tab is gone: the site closed it, or the browser restarted under us (Steel's API crashed on a CDP
            # timeout on 11-01 and took every tab with it). The session can't come back; say so up front.
            self.items.pop(sid, None)
            self.registry.drop(session=sid)
            asyncio.ensure_future(_close_all(s.pages))
            s.packet.write("close", reason="tab closed")
            raise KeyError(f"session {sid}'s window was closed (the site or a person closed it, the browser "
                           f"restarted, or it sat unused for an hour and was cleaned up); open a new session. "
                           f"Sign-ins are kept in the browser profile.")
        self.registry.touch(sid)
        return s

    async def close(self, sid: str) -> None:
        s = self.items.pop(sid, None)
        if s:
            s.packet.write("close")
            self.registry.drop(session=sid)
            await _close_all([s.page, *s.pages])

    async def shutdown(self) -> None:
        for sid in list(self.items):
            try:
                await self.close(sid)
            except Exception:  # noqa: BLE001 - browser may already be gone
                pass
        # No browser.close(): over CDP it can quit the shared Chrome. Stopping Playwright just disconnects.
        self.registry.remove_file()
        if self._pw:
            await self._pw.stop()


async def _target_id(page: Page) -> str | None:
    """The tab's CDP target id, which is how the janitor sees it."""
    try:
        cdp = await page.context.new_cdp_session(page)
        try:
            return (await cdp.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
        finally:
            await cdp.detach()
    except Exception:  # noqa: BLE001 - closed already, or a browser that doesn't allow it: just not listed
        return None


async def _close_quietly(page: Page) -> None:
    try:
        if not page.is_closed():
            await page.close()
    except Exception:  # noqa: BLE001 - closed by the site, or the browser is gone
        pass


async def _close_all(pages: list[Page]) -> None:
    for p in dict.fromkeys(pages):
        await _close_quietly(p)

