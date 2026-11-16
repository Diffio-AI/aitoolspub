"""Watching a page over time: wait for a condition, and record a timeline of what the page shows.

For pages that change on their own for minutes (a long upload processing, a recording restoring, a build running):
- `wait_for` polls a JavaScript condition, a selector or a piece of text for up to 15 minutes and returns when it
  holds, so an agent doesn't have to loop short calls;
- a recording polls the page's visible state every half second or so and writes every change, timestamped, to
  `timeline.jsonl`, plus a screenshot every N seconds to `shots/`. It runs in the background: the session's other
  tools keep working, and the recording carries on across navigations and reloads (it is polled from outside the
  page, so nothing in the page has to survive).

The state snapshot covers what usually marks a UI state: headings, status and alert regions, progress bars and their
values, buttons and whether they're enabled, media elements, dialogs, the URL and title, plus any CSS selectors the
caller names (their text, or a value for inputs).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PWError, Page

STATE_JS = """(watch) => {
  const vis = e => { if (!e || !e.isConnected) return false; const r = e.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false; const s = getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  const txt = (e, n = 160) => (e.innerText || e.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, n);
  const out = {url: location.href, title: document.title};
  out.headings = [...document.querySelectorAll('h1,h2,h3,[role=heading]')].filter(vis).map(e => txt(e, 120)).filter(Boolean).slice(0, 30);
  out.status = [...document.querySelectorAll('[role=status],[role=alert],[aria-live]:not([aria-live=off]),[role=log]')]
    .filter(vis).map(e => txt(e, 240)).filter(Boolean).slice(0, 20);
  out.progress = [...document.querySelectorAll('progress,[role=progressbar],[role=meter]')].filter(vis).map(e => {
    const v = e.tagName === 'PROGRESS' ? (e.hasAttribute('value') ? e.value : null) : e.getAttribute('aria-valuenow');
    const max = e.tagName === 'PROGRESS' ? e.max : e.getAttribute('aria-valuemax');
    const label = e.getAttribute('aria-label') || e.getAttribute('aria-valuetext') || (e.labels && e.labels[0] && txt(e.labels[0], 60)) || '';
    return `${label || e.id || e.className.toString().slice(0, 40) || e.tagName.toLowerCase()}: ${v === null ? 'indeterminate' : v + (max ? '/' + max : '')}`; }).slice(0, 20);
  out.buttons = [...document.querySelectorAll('button,[role=button],input[type=submit],input[type=button]')].filter(vis).map(e => {
    const t = txt(e, 60) || e.value || e.getAttribute('aria-label') || e.title || '';
    const off = e.disabled || e.getAttribute('aria-disabled') === 'true';
    return t ? (off ? t + ' [disabled]' : t) : null; }).filter(Boolean).slice(0, 60);
  out.media = [...document.querySelectorAll('audio,video')].map(m => `${m.tagName.toLowerCase()} ${(m.currentSrc || m.src || '').slice(0, 120)} ` +
    `ready=${m.readyState} ${m.paused ? 'paused' : 'playing'} duration=${isFinite(m.duration) ? Math.round(m.duration) : m.duration}${m.error ? ' error=' + m.error.code : ''}`).slice(0, 10);
  out.dialogs = [...document.querySelectorAll('dialog[open],[role=dialog],[role=alertdialog]')].filter(vis).map(e => txt(e, 200)).filter(Boolean);
  if (watch && watch.length) {
    out.watch = {};
    for (const sel of watch) {
      let els = []; try { els = [...document.querySelectorAll(sel)]; } catch (e) { out.watch[sel] = 'bad selector'; continue; }
      out.watch[sel] = els.slice(0, 10).map(e => (vis(e) ? '' : '(hidden) ') + ('value' in e && e.tagName !== 'BUTTON' && e.tagName !== 'LI' ? String(e.value).slice(0, 160) : txt(e, 240)));
    }
  }
  return out;
}"""


def diff(old: dict | None, new: dict) -> dict:
    """What changed between two snapshots: scalars as [old, new], lists as added/removed."""
    if old is None:
        return {"initial": new}
    out: dict[str, Any] = {}
    for k in set(old) | set(new):
        a, b = old.get(k), new.get(k)
        if a == b:
            continue
        if isinstance(a, list) or isinstance(b, list):
            a, b = a or [], b or []
            ch = {}
            added = [x for x in b if x not in a]
            removed = [x for x in a if x not in b]
            if added:
                ch["added"] = added
            if removed:
                ch["removed"] = removed
            if not ch:
                ch["order"] = b
            out[k] = ch
        elif isinstance(a, dict) or isinstance(b, dict):
            sub = {s: [(a or {}).get(s), (b or {}).get(s)] for s in set(a or {}) | set(b or {})
                   if (a or {}).get(s) != (b or {}).get(s)}
            out[k] = sub
        else:
            out[k] = [a, b]
    return out


async def snapshot(page: Page, watch: list[str] | None) -> dict | None:
    try:
        return await asyncio.wait_for(page.evaluate(STATE_JS, watch or []), 10)
    except (PWError, asyncio.TimeoutError):
        return None  # mid-navigation, or the page is busy: the next poll gets it


CONDITION_JS = """async ([expr, selector, text]) => {
  if (selector) { const e = document.querySelector(selector); if (!e) return null;
    const r = e.getBoundingClientRect(); return (r.width || r.height) ? (e.innerText || e.value || true) : null; }
  if (text) return document.body && document.body.innerText.includes(text) ? text : null;
  let v = (0, eval)(expr);
  if (typeof v === 'function') v = v();
  return await v;
}"""


async def wait_for(page: Page, *, condition: str | None, selector: str | None, text: str | None,
                   timeout: float, poll: float) -> dict:
    """Poll until the condition is truthy (or the selector is visible, or the text appears)."""
    start = time.monotonic()
    last_error = None
    checks = 0
    while True:
        checks += 1
        try:
            v = await asyncio.wait_for(page.evaluate(CONDITION_JS, [condition, selector, text]), max(poll, 10))
            last_error = None
            if v not in (None, False, 0, ""):
                return {"met": True, "seconds": round(time.monotonic() - start, 1), "checks": checks, "value": v,
                        "url": page.url}
        except asyncio.TimeoutError:
            last_error = "a check took over 10s (the page is busy)"
        except PWError as e:
            msg = str(e).splitlines()[0][:300]
            if "has been closed" in msg or "Target closed" in msg:
                raise
            last_error = msg  # a navigation in between, or the condition threw: keep polling, report the error
        if time.monotonic() - start + poll > timeout:
            out = {"met": False, "seconds": round(time.monotonic() - start, 1), "checks": checks, "url": page.url}
            if last_error:
                out["last_error"] = last_error
            return out
        await asyncio.sleep(poll)


@dataclass
class Recording:
    dir: Path
    seconds: float
    every: float | None
    full_page: bool
    watch: list[str]
    interval: float
    started: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    changes: int = 0
    shots: list[str] = field(default_factory=list)
    errors: int = 0
    ended: str | None = None

    def status(self) -> dict:
        running = self.task is not None and not self.task.done()
        out = {"dir": str(self.dir), "timeline": str(self.dir / "timeline.jsonl"), "running": running,
               "elapsed": round(time.time() - self.started, 1), "seconds": self.seconds, "changes": self.changes,
               "screenshots": len(self.shots)}
        if self.shots:
            out["latest_screenshot"] = self.shots[-1]
        if self.ended:
            out["ended"] = self.ended
        return out


async def record(page: Page, rec: Recording, shoot) -> None:
    """Poll the page's state every `rec.interval` seconds, log the diffs, and take a screenshot every `rec.every`."""
    (rec.dir / "shots").mkdir(parents=True, exist_ok=True)
    timeline = rec.dir / "timeline.jsonl"
    prev: dict | None = None
    t0 = time.monotonic()
    next_shot = 0.0
    with timeline.open("a") as f:
        try:
            while (el := time.monotonic() - t0) < rec.seconds:
                if page.is_closed():
                    rec.ended = "the window closed"
                    break
                snap = await snapshot(page, rec.watch)
                if snap is not None:
                    d = diff(prev, snap)
                    if d:
                        f.write(json.dumps({"t": round(el, 2), "at": time.strftime("%H:%M:%S"), **d},
                                           ensure_ascii=False) + "\n")
                        f.flush()
                        rec.changes += 1
                    prev = snap
                else:
                    rec.errors += 1
                if rec.every and el >= next_shot:
                    path = rec.dir / "shots" / f"{el:07.1f}s.jpg"
                    try:
                        path.write_bytes(await shoot())
                        rec.shots.append(str(path))
                    except Exception:  # noqa: BLE001 - a busy page: skip this frame
                        rec.errors += 1
                    next_shot = el + rec.every
                await asyncio.sleep(max(0.0, rec.interval - ((time.monotonic() - t0) - el)))
            else:
                rec.ended = "finished"
        except asyncio.CancelledError:
            rec.ended = "stopped"
            raise
        finally:
            if prev is not None:
                f.write(json.dumps({"t": round(time.monotonic() - t0, 2), "at": time.strftime("%H:%M:%S"),
                                    "final": prev}, ensure_ascii=False) + "\n")
