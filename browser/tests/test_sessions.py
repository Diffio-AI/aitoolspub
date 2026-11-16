"""Several sessions and several server processes sharing one browser."""

import asyncio
import sys
import time
from pathlib import Path

from browse.navigator import Navigator
from browse.sessions import Sessions
from browse.tools import Tools

FAKE = f"{sys.executable} {Path(__file__).with_name('fake_navigator.py')}"


async def _opener(t: Tools, sid: str) -> str | None:
    s = t.sessions.get(sid)
    cdp = await s.page.context.new_cdp_session(s.page)
    try:
        return (await cdp.send("Target.getTargetInfo"))["targetInfo"].get("openerId")
    finally:
        await cdp.detach()


async def test_sessions_open_during_a_navigator_run_without_waiting(cdp, site, monkeypatch):
    """Two 'processes' on one browser: a session opened during another's navigator run doesn't wait for it, and
    it has an opener (the launcher), which is what keeps fastbrowse from adopting it into that run."""
    monkeypatch.setenv("FAKE_NAV_SECONDS", "3")
    a = Tools(Sessions(cdp, None), Navigator(cdp, cmd=FAKE), None)
    b = Tools(Sessions(cdp, None), None, None)
    try:
        sa = (await a.open(site + "/dir/"))["session"]
        nav = asyncio.create_task(a.navigate(sa, "anything"))
        await asyncio.sleep(0.5)
        t0 = time.monotonic()
        sb = (await b.open(site + "/dir/makers"))["session"]
        assert time.monotonic() - t0 < 2.0  # didn't wait for the run
        assert not nav.done()
        assert await _opener(b, sb)  # opened by the launcher, so no navigator adopts it
        assert await _opener(a, sa)
        await nav
    finally:
        await a.sessions.shutdown()
        await b.sessions.shutdown()


async def test_navigator_runs_in_different_sessions_overlap(cdp, site, monkeypatch):
    monkeypatch.setenv("FAKE_NAV_SECONDS", "2")
    a = Tools(Sessions(cdp, None), Navigator(cdp, cmd=FAKE), None)
    b = Tools(Sessions(cdp, None), Navigator(cdp, cmd=FAKE), None)
    try:
        sa = (await a.open(site + "/dir/"))["session"]
        sb = (await b.open(site + "/dir/makers"))["session"]
        t0 = time.monotonic()
        await asyncio.gather(a.navigate(sa, "one"), b.navigate(sb, "two"))
        assert time.monotonic() - t0 < 3.5  # 2s each, run side by side
    finally:
        await a.sessions.shutdown()
        await b.sessions.shutdown()


async def test_concurrent_opens_get_their_own_windows(cdp, site):
    procs = [Tools(Sessions(cdp, None), None, None) for _ in range(3)]
    try:
        opened = await asyncio.gather(*[t.open(f"{site}/dir/?n={i}") for i, t in enumerate(procs) for _ in range(2)])
        pages = [t.sessions.get(o["session"]).page for t in procs for o in opened if o["session"] in t.sessions.items]
        assert len(pages) == 6 and len({id(p) for p in pages}) == 6
        assert all(await asyncio.gather(*[p.evaluate("document.visibilityState === 'visible'") for p in pages]))
    finally:
        for t in procs:
            await t.sessions.shutdown()


async def test_navigator_ending_in_someone_elses_tab_is_not_followed(cdp, site, monkeypatch):
    t = Tools(Sessions(cdp, None), Navigator(cdp, cmd=FAKE), None)
    other = Tools(Sessions(cdp, None), None, None)
    try:
        theirs = await other.open(site + "/wizard/submit")
        mine = await t.open(site + "/dir/")
        monkeypatch.setenv("FAKE_FINAL_URL", theirs["url"])
        monkeypatch.setenv("FAKE_NAV_SECONDS", "0.1")
        r = await t.navigate(mine["session"], "anything")
        assert r["now"]["url"].endswith("/dir/") and "stays on its own tab" in r["note"]
        assert other.sessions.get(theirs["session"]).page.url.endswith("/wizard/submit")
        assert t.sessions.get(mine["session"]).page is not other.sessions.get(theirs["session"]).page
    finally:
        await t.sessions.shutdown()
        await other.sessions.shutdown()


async def test_navigator_gets_the_session_tab_token(cdp, site, monkeypatch):
    """The navigator is told to attach by a token in this tab's title, and the title is restored after."""
    t = Tools(Sessions(cdp, None), Navigator(cdp, cmd=FAKE), None)
    try:
        o = await t.open(site + "/dir/")
        monkeypatch.setenv("FAKE_NAV_SECONDS", "0.1")
        await t.navigate(o["session"], "anything")
        assert await t.sessions.get(o["session"]).page.title() == "Soundlist: the directory of audio tools"
    finally:
        await t.sessions.shutdown()


async def test_navigator_on_the_same_page_keeps_the_plan(cdp, site, monkeypatch):
    """A navigator run that stays on the document (closing a modal on Wellfound) keeps what the agent filled
    planned; before, the agent's own text read as prefilled and blocked the next press."""
    from browse import forms
    t = Tools(Sessions(cdp, None), Navigator(cdp, cmd=FAKE), None)
    try:
        o = await t.open(site + "/wizard/submit")
        s = t.sessions.get(o["session"])
        sv = await forms.survey(s.page, s.state, max_text=0)
        ids = {f.get("name"): f["id"] for g in sv["groups"] for f in g["fields"]}
        await t.fill(o["session"], {ids["name"]: "Quillo", ids["url"]: "https://quillo.example"})
        monkeypatch.setenv("FAKE_NAV_SECONDS", "0.1")
        await t.navigate(o["session"], "close the dialog")
        chk = await forms.check(s.page, s.state)
        assert not chk.get("unplanned_text") and chk["fields"][ids["name"]]["status"] == "match"
        monkeypatch.setenv("FAKE_NAV_SECONDS", "1.5")
        nav = asyncio.create_task(t.navigate(o["session"], "submit it"))
        await asyncio.sleep(0.7)
        await s.page.reload()                                  # the page loads a new document mid-run
        await nav
        assert not t.sessions.get(o["session"]).state.plan   # a new document: the plan is gone
    finally:
        await t.sessions.shutdown()


async def test_page_hung_after_the_navigator_comes_back_quickly(cdp, site, monkeypatch):
    """SoftwareSuggest: the navigator gave up on a page that stopped answering, and the calls after it held navigate
    until the server's 720s limit."""
    import browse.tools
    monkeypatch.setattr(browse.tools, "PAGE_ANSWER_LIMIT", 1.0)
    t = Tools(Sessions(cdp, None), Navigator(cdp, cmd=FAKE), None)
    try:
        o = await t.open(site + "/dir/")
        page = t.sessions.get(o["session"]).page
        monkeypatch.setenv("FAKE_NAV_SECONDS", "1")
        nav = asyncio.create_task(t.navigate(o["session"], "anything"))
        await asyncio.sleep(0.3)
        await page.evaluate("setTimeout(() => { const t = Date.now(); while (Date.now() - t < 15000) {} }, 0)")
        t0 = time.monotonic()
        r = await nav
        assert "stopped answering" in r["error"] and time.monotonic() - t0 < 5, r
    finally:
        await t.sessions.shutdown()


async def test_closed_tab_says_open_a_new_session(cdp, site):
    """Steel crashed on a CDP timeout (11-01) and its restart closed every tab; the next goto said only
    'Target page, context or browser has been closed'."""
    import pytest

    t = Tools(Sessions(cdp, None), None, None)
    try:
        sid = (await t.open(site + "/dir/"))["session"]
        await t.sessions.get(sid).page.close()
        with pytest.raises(KeyError, match="open a new session"):
            await t.goto(sid, site + "/dir/makers")
        assert sid not in t.sessions.items
    finally:
        await t.sessions.shutdown()


async def test_screenshot_of_a_page_that_never_settles(cdp, site, monkeypatch):
    """Acrobat's cloud reader (11-01): Playwright's screenshot waits for fonts and a stable frame and gave up; the
    agent saw only 'Error executing tool screenshot'. The raw CDP capture takes the tab as it is."""
    from playwright.async_api import Page
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    async def never(self, **kw):
        raise PlaywrightTimeout("Timeout 20000ms exceeded. waiting for fonts to load...")

    t = Tools(Sessions(cdp, None), None, None)
    try:
        sid = (await t.open(site + "/dir/"))["session"]
        monkeypatch.setattr(Page, "screenshot", never)
        shot = await t.screenshot(sid)
        assert shot[:3] == b"\xff\xd8\xff"  # a JPEG
    finally:
        await t.sessions.shutdown()
