"""Deterministic form work on a Playwright page: survey, fill, check. No model is involved here.

Field ids are stamped into the DOM by the survey (`data-bb`); fields in child frames carry a frame prefix
(`F1.f3`). Filling types only the plan's own strings and reads every value back; `check` is the gate a
press of a submit button goes through.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Frame, Page

from . import pagejs
from .plan import Entry

TEXT_KINDS = {"text", "textarea", "richtext", "combobox", "tags"}
# Captcha widget frames say nothing about the page (a press solves the captcha first); Kaggle's hidden reCAPTCHA
# challenge frame was listed with its inline script as its text.
CAPTCHA_FRAME = re.compile(r"recaptcha/(api2|enterprise)/(anchor|bframe)|hcaptcha\.com/|challenges\.cloudflare\.com/")


def js_len(s: str) -> int:
    """String length as a page's maxlength counts it (UTF-16 code units)."""
    return len(s.encode("utf-16-le")) // 2


def norm_text(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


_TYPOGRAPHY = str.maketrans({"\u2013": "-", "\u2014": "-", "\u2010": "-", "\u2011": "-", "\u2212": "-",
                             "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2026": "...",
                             "\u00a0": " "})


def loose_text(s: str) -> str:
    """Text as a site may echo it back: WordPress's texturize turned "- a closet test" into "– a closet test" when
    Jane Friedman's Gravity Form came back after a rejected captcha, so the agent's own pitch read as the site's."""
    return " ".join(norm_text(s).translate(_TYPOGRAPHY).split())


def paragraphs(s: str) -> list[str]:
    return [re.sub(r"[ \t ]+", " ", p).strip() for p in re.split(r"\n+", norm_text(s)) if p.strip()]


def match_option(options: list[dict], wanted: str) -> dict | None:
    """Exact label, exact value, case-insensitive label/value, then a unique prefix/contains match."""
    w = wanted.strip()
    lw = w.casefold()
    for test in (lambda o: o["label"] == w, lambda o: o["value"] == w,
                 lambda o: o["label"].casefold() == lw, lambda o: str(o["value"]).casefold() == lw):
        hit = [o for o in options if test(o)]
        if hit:
            return hit[0]
    for test in (lambda o: o["label"].casefold().startswith(lw), lambda o: lw in o["label"].casefold()):
        hit = [o for o in options if test(o)]
        if len(hit) == 1:
            return hit[0]
    return None


@dataclass
class FormState:
    """What the session knows about the page's fields since the last survey."""

    frames: dict[str, Frame] = field(default_factory=dict)  # prefix -> frame
    fields: dict[str, dict] = field(default_factory=dict)  # id -> field (with "_frame")
    baseline: dict[str, Any] = field(default_factory=dict)  # id -> value when first seen
    chosen: dict[str, Any] = field(default_factory=dict)  # id -> what fill decided to put there
    plan: dict[str, Entry] = field(default_factory=dict)  # id -> entry, merged across fill calls
    # name or label -> text you typed on the previous document. A form that posts and comes back with an error
    # (Contact Form 7 on a rejected captcha) shows your own text again; that is yours, not the site's.
    carried: dict[str, str] = field(default_factory=dict)

    def carry(self) -> dict[str, str]:
        out = {}
        for fid, entry in self.plan.items():
            v, f = self.chosen.get(fid), self.fields.get(fid) or {}
            if entry.action == "text" and isinstance(v, str) and v and (f.get("name") or f.get("label")):
                out[f.get("name") or f.get("label")] = v
        return out


def _prefix_for(state: FormState, frame: Frame, page: Page) -> str:
    if frame is page.main_frame:
        state.frames[""] = frame
        return ""
    for p, f in state.frames.items():
        if f is frame:
            return p
    p = f"F{len([k for k in state.frames if k])+1}."
    state.frames[p] = frame
    return p


async def survey(page: Page, state: FormState, *, max_text: int = 3000, validity: bool = False) -> dict:
    """Survey the main frame and every child frame that has fields. Updates `state.fields`."""
    merged: dict[str, Any] | None = None
    seen: dict[str, dict] = {}
    for frame in page.frames:
        if frame.is_detached():
            continue
        prefix = _prefix_for(state, frame, page)
        # The iframe covering the page (a consent wall) is surveyed as the overlay: its buttons are what to press.
        cover = bool(prefix and merged and "frame" in (merged.get("overlay") or {}) and await _is_cover(frame))
        try:
            # A frame that never answers (a stuck ad iframe) is skipped rather than hanging the survey; the main
            # frame gets longer.
            res = await asyncio.wait_for(frame.evaluate(pagejs.SURVEY, {"prefix": prefix, "validity": validity,
                                                                        "maxText": max_text if not prefix else 0,
                                                                        "asOverlay": cover}),
                                         20 if not prefix else 8)
        except Exception:  # noqa: BLE001 - cross-origin frames mid-navigation, about:blank, timeouts, etc.
            continue
        if cover and res.get("overlay"):
            merged["overlay"] = {"frame": prefix, "url": res["url"], **res["overlay"]}
        for g in res["groups"]:
            for f in g["fields"]:
                seen[f["id"]] = {**f, "_frame": prefix, "_group": g["group"]}
        if not prefix:
            merged = res  # page.frames starts with the main frame
        elif (merged is not None and frame.parent_frame is page.main_frame and res["url"] != "about:blank"
              and not (CAPTCHA_FRAME.search(res["url"]) and not res["groups"])):
            # Every top-level child frame is listed with its text: an embedded form's result page lives there.
            info: dict[str, Any] = {"prefix": prefix, "url": res["url"]}
            if max_text:
                try:
                    info["text"] = await asyncio.wait_for(frame.evaluate(
                        "() => (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').trim().slice(0, 600)"), 8)
                except Exception:  # noqa: BLE001
                    pass
            if res["groups"] or info.get("text"):
                merged.setdefault("frames", []).append(info)
            merged["groups"] += res["groups"]
            merged["errors"] += res["errors"]
            if res.get("notices"):
                merged["notices"] = merged.get("notices", []) + res["notices"]
    merged = merged or {"url": page.url, "title": "", "headings": [], "step": "", "errors": [], "groups": [], "text": ""}
    for fid, f in seen.items():
        state.fields[fid] = f
        state.baseline.setdefault(fid, f.get("value"))
    return merged


async def _is_cover(frame: Frame) -> bool:
    try:
        el = await asyncio.wait_for(frame.frame_element(), 5)
        return await el.get_attribute("data-bb-cover") == "1"
    except Exception:  # noqa: BLE001 - detached mid-survey
        return False


def _loc(state: FormState, fid: str, opt: int | None = None):
    f = state.fields[fid]
    frame = state.frames[f["_frame"]]
    sel = f'[data-bb="{fid}"]' + (f'[data-bb-opt="{opt}"]' if opt is not None else "")
    return frame.locator(sel).first


async def _relocate(page: Page, state: FormState, fid: str) -> bool:
    """The element was re-rendered and lost its stamp: find it again by kind, name and label, and restamp it."""
    old = state.fields[fid]
    fresh = FormState(frames=state.frames)
    await survey(page, fresh, max_text=0)
    for nid, nf in fresh.fields.items():
        if nid == fid:
            return True
        if (nf["kind"], nf.get("name"), nf.get("label")) == (old["kind"], old.get("name"), old.get("label")) \
                and nf["_frame"] == old["_frame"]:
            frame = state.frames[nf["_frame"]]
            await frame.evaluate("([a, b]) => document.querySelectorAll(`[data-bb=\"${a}\"]`).forEach(e => e.dataset.bb = b)",
                                 [nid, fid])
            state.fields[fid] = {**nf, "id": fid}
            return True
    return False


async def _read_field(page: Page, state: FormState, fid: str) -> dict | None:
    """The field as it is right now (a one-field survey)."""
    f = state.fields[fid]
    frame = state.frames[f["_frame"]]
    res = await frame.evaluate(pagejs.SURVEY, {"prefix": f["_frame"], "maxText": 0})
    for g in res["groups"]:
        for nf in g["fields"]:
            if nf["id"] == fid:
                if nf["kind"] == "tags":
                    chips = await frame.evaluate(pagejs.TAG_CHIPS, fid)
                    nf["_chips"] = chips
                state.fields[fid] = {**nf, "_frame": f["_frame"], "_group": g["group"]}
                return state.fields[fid]
    return None


def _typed(f: dict) -> str:
    """The text in a field's own input: for a tags field that's `typed`, since its `value` is the chips."""
    v = f.get("typed", "") if f["kind"] == "tags" else f.get("value")
    return v if isinstance(v, str) else ""


def _compare(f: dict, entry: Entry, chosen: Any) -> tuple[str, str]:
    """(status, note) for one planned field, from its current state."""
    kind, val = f["kind"], f.get("value")
    a = entry.action
    if a == "keep":
        if f.get("required") and val in ("", [], False, None):
            # Keeping an empty required field doesn't finish it (CMSWire's "How did you hear" read back ready).
            return "mismatch", "required and empty: keep only leaves it as it is"
        return "match", ""
    if a == "clear":
        empty = val in ("", [], False, None)
        return ("match", "") if empty else ("mismatch", "still has a value")
    if a == "text":
        if chosen is None:
            return "error", "nothing typed"
        if kind == "richtext":
            if paragraphs(val or "") == paragraphs(chosen):
                return "match", ""
            if " ".join(paragraphs(val or "")) == " ".join(paragraphs(chosen)):
                return "mismatch", "the editor lost the line breaks"
            return "mismatch", ""
        if norm_text(_typed(f)) == norm_text(chosen):
            return "match", ""
        # A phone box formats as you type (Fillout turned 7196456403 into "(719) 645-6403"): the same digits match.
        digits = lambda t: re.sub(r"\D", "", str(t or ""))  # noqa: E731
        if (f.get("input_type") == "tel" or re.search(r"phone|mobile|\btel", f.get("label") or "", re.I)) \
                and len(digits(chosen)) >= 7 and digits(_typed(f)).endswith(digits(chosen)):
            return "match", "the page formatted the number"
        return "mismatch", ""
    if a == "check":
        return ("match", "") if bool(val) == entry.check else ("mismatch", "")
    if a == "choose":
        if chosen is None:
            return "error", "no option matched"
        if kind in ("select", "radio"):
            return ("match", "") if val == chosen["label"] else ("mismatch", "")
        if isinstance(val, list):  # a multi-select combobox: its picks are chips
            return ("match", "") if chosen["label"].casefold() in {norm_text(v).casefold() for v in val} \
                else ("mismatch", "")
        if norm_text(val or "") == chosen["label"]:
            return "match", ""
        # Some comboboxes (downshift on Wellfound) clear the input once an option is picked and show the pick
        # beside it, where it reads as part of the field's label.
        if not val and chosen["label"].casefold() in (f.get("label") or "").casefold():
            return "match", ""
        # The site may store a pick in its own shorter form: "Colorado Springs, CO, US" became "Colorado Springs, US"
        # on F6S. Every word of what the field holds coming from the picked suggestion is the same pick.
        words = lambda t: set(re.findall(r"\w+", t.casefold()))  # noqa: E731
        if val and isinstance(val, str) and words(val) <= words(chosen["label"]):
            return "match", ""
        return "mismatch", ""  # combobox
    if a == "choose_all":
        want = sorted(o["label"] for o in (chosen or []))
        if kind == "combobox":  # chips: every pick is there (removing the site's own chips is up to you)
            have = {norm_text(v).casefold() for v in (val if isinstance(val, list) else [])}
            return ("match", "") if want and all(w.casefold() in have for w in want) else ("mismatch", "")
        return ("match", "") if sorted(val or []) == want else ("mismatch", "")
    if a == "file":
        if not val and isinstance(chosen, dict) and chosen.get("uploaded"):
            return "match", ""  # sent on change, input cleared by the site
        want = sorted(Path(p).name for p in entry.file or [])
        return ("match", "") if sorted(val or []) == want else ("mismatch", "")
    if a == "tags":
        chips = (f.get("_chips") or {})
        got = chips.get("chips") or []
        hidden = ",".join(chips.get("hidden") or [])
        if not got and not hidden:
            return "unverifiable", "no chips or hidden value found to read the tags back from"
        missing = [t for t in entry.tags or [] if t.casefold() not in {g.casefold() for g in got}
                   and t.casefold() not in hidden.casefold()]
        extra = [g for g in got if g.casefold() not in {t.casefold() for t in entry.tags or []}]
        if missing or extra:
            return "mismatch", (f"missing {missing}" if missing else "") + (f" extra {extra}" if extra else "")
        return "match", ""
    return "error", f"unknown action {a}"


def _actual(f: dict) -> Any:
    if f["kind"] == "tags":
        return (f.get("_chips") or {}).get("chips", f.get("value"))
    return f.get("value")


def _click_blocker(error: str) -> str | None:
    """What Playwright's call log says stopped a click. Softonic's assistant panel lay over its description editor
    (11-02) and the note was only "Timeout 30000ms exceeded", which doesn't say the panel has to be closed first."""
    over = re.findall(r"- (<.*?>(?:…</\w+>)?)(?: from .*? subtree)? intercepts pointer events", error)
    if over:
        return f"{over[-1]} covers the field: close or dismiss it (a panel, dialog or banner), then fill again"
    state = re.findall(r"- (element is not (?:visible|enabled|stable|editable)[^\n]*)", error)
    return state[-1] if state else None


async def _empty(loc) -> None:
    """Clear an input. Playwright's fill("") presses Delete, and react-select takes Delete in an empty input as
    "remove the last pick": on Softonic (11-02) choose_all cleared the box before each value and once more at the end,
    so every chip it made was taken away again. An input that is already empty is left alone."""
    try:
        if not await loc.input_value():
            return
    except Exception:  # noqa: BLE001 - not an input (a contenteditable), or it detached
        pass
    await loc.fill("")


async def _type_text(page: Page, loc, f: dict, text: str, *, slow: bool) -> None:
    if f["kind"] == "richtext":
        await loc.click(timeout=10000)
        await page.keyboard.press("ControlOrMeta+a")
        await page.keyboard.press("Backspace")
        for i, line in enumerate(norm_text(text).split("\n")):
            if i:
                await page.keyboard.press("Enter")
            if line:
                await page.keyboard.insert_text(line)
        return
    if slow:
        await _empty(loc)
        await loc.press_sequentially(text, delay=5)
    else:
        await loc.fill(text)


async def _fill_text(page: Page, state: FormState, fid: str, entry: Entry) -> dict:
    """Type the first variant that fits. A page that silently truncates reveals a limit: move to a variant
    that fits it. A page that rewrites the text (capitalizing, stripping characters) is reported as is."""
    f = state.fields[fid]
    variants = entry.text or []
    limit, discovered = f.get("maxlength"), None
    skip: set[str] = set()
    while True:
        caps = [x for x in (limit, discovered) if x]
        cap = min(caps) if caps else None
        pick = next((v for v in variants if v not in skip and (cap is None or js_len(v) <= cap)), None)
        if pick is None:
            state.chosen.pop(fid, None)
            return {"status": "too_long", "limit": cap, "lengths": [js_len(v) for v in variants],
                    "note": f"no variant fits {cap} characters" + (" (limit found by truncation)" if discovered else "")}
        loc = _loc(state, fid)
        state.chosen[fid] = pick
        cur, note = f, ""
        for slow in ((False,) if f["kind"] == "richtext" else (False, True)):
            # Controlled inputs sometimes drop a programmatic fill, so the second try types key by key.
            await _type_text(page, loc, f, pick, slow=slow)
            cur = await _read_field(page, state, fid) or f
            status, note = _compare(cur, entry, pick)
            if status == "match":
                out: dict[str, Any] = {"status": "filled"}
                if len(variants) > 1:
                    out["typed_variant"] = variants.index(pick)
                if discovered:
                    out["note"] = f"page truncates at {discovered} characters"
                if f["kind"] in ("text", "combobox") and f.get("input_type") in (None, "search") and not f.get("secret"):
                    # Manta's category accepts only a picked suggestion; typed text fails on submit.
                    seen = await _suggestions(state.frames[f["_frame"]], timeout=0.6)
                    if seen:
                        out["suggestions"] = seen[:10]
                        out["note"] = ("a suggestion list opened as this was typed; if the field needs a pick from it, "
                                       "fill it again with {\"choose\": [...]}")
                return out
            actual = norm_text(_typed(cur))
            if f["kind"] != "richtext" and actual and pick.startswith(actual) and js_len(actual) < js_len(pick):
                discovered = js_len(actual)
                skip.add(pick)
                await _empty(loc)
                break
        else:
            return {"status": "mismatch", "expected": pick, "actual": _typed(cur),
                    "note": note or "the page changed the text as it was typed"}


# react-select's options are divs with no role (Wellfound): ids like react-select-2-option-0, classes like x__option.
OPTION_SEL = ("[role=option], [role=listbox] li, .autocomplete li, .suggestions li, [class*='suggestion'], "
              "[id^='react-select'][id*='-option-'], [class*='__option'], .ui-menu-item")


async def _suggestions(frame: Frame, timeout: float = 1.5) -> list[str]:
    """The visible autocomplete suggestions, once any appear (or none after `timeout`)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            # Leaves only: [class*='__option'] also matches select2's results <ul> ("select2-results__options"),
            # whose text is every option at once (137k characters on SoftwareSuggest).
            texts = await frame.locator(OPTION_SEL).evaluate_all(
                """(els, sel) => { const out = [];
                    for (const e of els) {
                        if (out.length >= 50) break;
                        if (e.offsetParent === null || e.querySelector(sel)) continue;
                        const t = e.innerText.replace(/\\s+/g, ' ').trim().slice(0, 150);
                        if (t) out.push(t);
                    }
                    return out; }""", OPTION_SEL)
        except Exception:  # noqa: BLE001 - suggestion list redrawn under us
            texts = []
        if texts or asyncio.get_running_loop().time() >= deadline:
            return list(dict.fromkeys(texts))
        await asyncio.sleep(0.15)


async def _focused_search(frame: Frame):
    """The text box an opened dropdown focused for searching its options, if it did."""
    focused = await frame.evaluate_handle(
        "() => { const a = document.activeElement;"
        " return a && a.matches('input:not([type=hidden]):not([type=checkbox]):not([type=radio])')"
        " && a.offsetParent !== null ? a : null; }")
    el = focused.as_element()
    if el is None:
        await focused.dispose()
    return el


async def _click_option(frame: Frame, text: str, timeout: float = 1.5) -> bool:
    """Click a visible autocomplete suggestion whose text is exactly `text` (case-insensitive)."""
    deadline = asyncio.get_running_loop().time() + timeout
    want = " ".join(text.split()).casefold()
    while asyncio.get_running_loop().time() < deadline:
        # The index of the first visible leaf option with this text; a list filtered to one option has a container
        # with the same text (select2's results <ul>), and a click on the <ul> picks nothing.
        try:
            i = await frame.locator(OPTION_SEL).evaluate_all(
                """(els, [sel, want]) => els.findIndex(e => e.offsetParent !== null && !e.querySelector(sel)
                    && e.innerText.replace(/\\s+/g, ' ').trim().toLowerCase() === want)""", [OPTION_SEL, want])
            if i >= 0:
                await frame.locator(OPTION_SEL).nth(i).click()
                return True
        except Exception:  # noqa: BLE001 - suggestion list redrawn under us
            pass
        await asyncio.sleep(0.15)
    return False


async def fill(page: Page, state: FormState, plan: dict[str, Entry], assets: Path | None) -> dict:
    """Fill each planned field and read it back. Returns per-field results."""
    results: dict[str, dict] = {}
    navigated = False
    for fid, entry in plan.items():
        if navigated:
            results[fid] = {"status": "skipped", "note": "not filled: the page loaded a new page before this field"}
            continue
        if fid not in state.fields:
            results[fid] = {"status": "error", "note": "unknown field id; survey first"}
            continue
        loc = _loc(state, fid)
        if await loc.count() == 0 and not await _relocate(page, state, fid):
            results[fid] = {"status": "error", "note": "field is gone from the page (re-survey)"}
            continue
        f = state.fields[fid]
        state.plan[fid] = entry
        url = page.url
        try:
            results[fid] = await _fill_one(page, state, fid, f, entry, assets)
        except Exception as e:  # noqa: BLE001 - one bad field shouldn't sink the rest
            msg = str(e).splitlines()[0]
            if "context was destroyed" in msg or "navigation" in msg.lower():
                # A control that loads a new page when it changes (a country or category picker): the choice
                # went through and the page moved on.
                results[fid] = {"status": "navigated", "note": "this choice loaded a new page"}
                navigated = True
                continue
            why = _click_blocker(str(e))
            results[fid] = {"status": "error", "note": f"{type(e).__name__}: {msg[:200]}" + (f"; {why}" if why else "")}
        if page.url != url:
            results[fid] = {**results[fid], "status": "navigated", "note": "this choice loaded a new page"}
            navigated = True
    return results


async def _fill_one(page: Page, state: FormState, fid: str, f: dict, entry: Entry, assets: Path | None) -> dict:
    a, kind = entry.action, f["kind"]
    loc = _loc(state, fid)
    frame = state.frames[f["_frame"]]
    if f.get("disabled") and a not in ("keep",):
        return {"status": "error", "note": "field is disabled or read-only"}
    if a == "keep":
        state.chosen[fid] = f.get("value")
        return {"status": "kept", "value": f.get("value")}
    if a == "text":
        if kind not in TEXT_KINDS:
            return {"status": "error", "note": f"'text' doesn't apply to a {kind} field"}
        return await _fill_text(page, state, fid, entry)
    if a == "clear":
        if kind == "richtext":
            await _type_text(page, loc, f, "", slow=False)
        elif kind in ("text", "textarea", "combobox", "tags"):
            await loc.fill("")
        elif kind == "checkbox":
            await loc.set_checked(False, force=True)
        elif kind == "select":
            await loc.select_option(index=0)
        elif kind == "file":
            await loc.set_input_files([])
        cur = await _read_field(page, state, fid) or f
        s, note = _compare(cur, entry, None)
        return {"status": "filled" if s == "match" else s, **({"note": note} if note else {})}
    if a == "check":
        if kind != "checkbox":
            return {"status": "error", "note": f"'check' doesn't apply to a {kind} field"}
        try:
            await loc.set_checked(bool(entry.check), timeout=3000)
        except Exception:  # noqa: BLE001 - visually hidden input behind a styled label
            if bool(f.get("value")) != bool(entry.check):
                await loc.evaluate("e => e.click()")
    elif a in ("choose", "choose_all"):
        opts = f.get("options") or []
        # A plain text input can carry an autocomplete whose pick sets a hidden field (Hotfrog's Category sets
        # cat2), so typed text alone fails the submit: choose picks from its suggestions like a combobox's.
        typeable = await loc.evaluate(
            "e => e.matches('input, textarea') || e.isContentEditable") if kind == "combobox" else True
        if kind == "combobox" and a == "choose" and not typeable:
            # A combobox that is a button or div (F6S's founding year): open it with a click, pick from its list.
            await loc.click()
            seen = await _suggestions(frame)
            hit = next((h for w in entry.choose or []
                        if (h := match_option([{"label": t, "value": t} for t in seen], w))), None)
            # A long list (select2, chosen) shows only its first options and focuses a search box in the dropdown:
            # SoftwareSuggest's 13,758 cities never reached Colorado Springs, so search for each choice.
            search = None if hit else await _focused_search(frame)
            for want in [] if search is None else entry.choose or []:
                for query in dict.fromkeys([want, want.split(",")[0].strip()]):
                    await _empty(search)
                    await search.type(query, delay=20)
                    await asyncio.sleep(0.3)
                    found = await _suggestions(frame)
                    seen += [t for t in found if t not in seen]
                    if hit := match_option([{"label": t, "value": t} for t in found], want):
                        break
                if hit:
                    break
            if not hit or not await _click_option(frame, hit["label"]):
                await page.keyboard.press("Escape")
                state.chosen.pop(fid, None)
                return {"status": "no_match", "note": "none of the choices is in this list; choose one of `offered`",
                        "tried": entry.choose, "offered": seen[:60]}
            state.chosen[fid] = {"label": hit["label"]}
        elif kind == "combobox" and a == "choose_all" and typeable:
            # A multi-select combobox (Softonic's "Compatible with"): type and pick each value in turn; each becomes
            # a chip. Values already picked are left as they are.
            have = {norm_text(v).casefold(): v for v in (f.get("value") if isinstance(f.get("value"), list) else [])}
            picked, missing, offered = [], [], []
            for want in entry.choose_all or []:
                if want.casefold() in have:
                    picked.append(have[want.casefold()])
                    continue
                for _ in range(2):
                    await _empty(loc)
                    await loc.press_sequentially(want, delay=20)
                    seen = await _suggestions(frame)
                    hit = match_option([{"label": t, "value": t} for t in seen], want)
                    # The last pick's blur handler can clear the box while this one is typed (react-select's
                    # menu closes on blur): type it again rather than call it not offered.
                    try:
                        if hit or await loc.input_value() == want:
                            break
                    except Exception:  # noqa: BLE001 - an input that detached while the menu redrew
                        break
                offered += [t for t in seen if t not in offered]
                if hit and await _click_option(frame, hit["label"]):
                    picked.append(hit["label"])
                    await asyncio.sleep(0.2)
                else:
                    missing.append(want)
            try:
                await _empty(loc)
            except Exception:  # noqa: BLE001 - some inputs detach while the menu closes
                pass
            state.chosen[fid] = [{"label": p} for p in picked]
            if missing:
                return {"status": "no_match", "note": f"not offered: {missing}; the others were picked",
                        "picked": picked, "offered": offered[:40]}
        elif kind in ("combobox", "text") and a == "choose" and f.get("multi") and any(
                w.casefold() in {norm_text(v).casefold() for v in f.get("value") or []} for w in entry.choose or []):
            # Already one of this multi-select's chips: picking it again would do nothing (or remove it).
            state.chosen[fid] = {"label": next(v for w in entry.choose or [] for v in f["value"]
                                               if norm_text(v).casefold() == w.casefold())}
        elif kind in ("combobox", "text") and a == "choose":
            offered: list[str] = []
            for want in entry.choose or []:
                hit = None
                # A place search often offers nothing for "Colorado Springs, CO" but "Colorado Springs, Colorado"
                # for "Colorado Springs" (Wellfound), so a choice with a comma is also searched by its first part.
                for query in dict.fromkeys([want, want.split(",")[0].strip()]):
                    await _empty(loc)
                    await loc.press_sequentially(query, delay=20)
                    seen = await _suggestions(frame)
                    offered += [t for t in seen if t not in offered]
                    hit = match_option([{"label": t, "value": t} for t in seen], want)
                    if hit and await _click_option(frame, hit["label"]):
                        break
                    hit = None
                if hit:
                    state.chosen[fid] = {"label": hit["label"]}
                    break
                hit = match_option(opts, want) if opts else None
                if hit:
                    await loc.fill(hit["value"])
                    state.chosen[fid] = {"label": hit["value"]}
                    break
            else:
                # Nothing typed brought up a match: a select-style combobox lists everything with an empty query.
                await _empty(loc)
                await loc.press_sequentially(" ", delay=20)
                await loc.press("Backspace")
                offered += [t for t in await _suggestions(frame, timeout=1.0) if t not in offered]
                await _empty(loc)
                state.chosen.pop(fid, None)
                if kind == "text" and not offered:
                    return {"status": "no_match", "tried": entry.choose, "offered": [],
                            "note": "no suggestion list appeared: this looks like a plain text field, so type the value"}
                return {"status": "no_match", "note": "no suggestion matched any choice; choose one of `offered`",
                        "tried": entry.choose, "offered": offered[:40]}
        elif kind in ("select", "multiselect", "radio", "checkboxes"):
            if f.get("more_options"):
                fresh = await frame.evaluate(
                    "(id) => [...document.querySelector(`[data-bb=\"${id}\"]`).options].map(o => ({value: o.value, label: (o.label||o.textContent).replace(/\\s+/g,' ').trim()}))",
                    fid)
                opts = fresh
            wants = entry.choose if a == "choose" else entry.choose_all
            if a == "choose":
                hit = next((h for w in wants or [] if (h := match_option(opts, w))), None)
                if not hit:
                    state.chosen.pop(fid, None)
                    return {"status": "no_match", "note": "none of the choices is an option here",
                            "options": [o["label"] for o in opts[:80]]}
                picks = [hit]
            else:
                picks, missing = [], []
                for w in wants or []:
                    h = match_option(opts, w)
                    (picks.append(h) if h else missing.append(w))
                if missing:
                    state.chosen.pop(fid, None)
                    return {"status": "no_match", "note": f"not options here: {missing}",
                            "options": [o["label"] for o in opts[:80]]}
            state.chosen[fid] = picks[0] if a == "choose" else picks
            if kind in ("select", "multiselect"):
                await loc.select_option(value=[p["value"] for p in picks])
            else:
                for i, o in enumerate(opts):
                    on = any(o is p for p in picks)
                    el = _loc(state, fid, i)
                    if kind == "radio" and not on:
                        continue
                    try:
                        await el.set_checked(on, timeout=3000)
                    except Exception:  # noqa: BLE001 - hidden input behind a label
                        if await el.evaluate("e => e.checked") != on:
                            await el.evaluate("e => e.click()")
        else:
            note = "this combobox takes one value; use choose" if kind == "combobox" else ""
            return {"status": "error", "note": note or f"'{a}' doesn't apply to a {kind} field"}
    elif a == "file":
        if kind != "file":
            return {"status": "error", "note": f"'file' doesn't apply to a {kind} field"}
        paths = []
        for p in entry.file or []:
            # An absolute path is any file on this machine; a relative one is inside the assets directory.
            if Path(p).expanduser().is_absolute():
                full = Path(p).expanduser().resolve()
                if not full.is_file():
                    return {"status": "error", "note": f"no such file: {p}"}
            elif assets is None:
                return {"status": "error", "note": f"give an absolute path ({p!r} is relative and this server has "
                                                  f"no assets directory)"}
            else:
                full = (assets / p).resolve()
                if not full.is_relative_to(assets.resolve()) or not full.is_file():
                    return {"status": "error", "note": f"not an asset: {p}"}
            paths.append(str(full))
        if f.get("uploader") == "dropzone":
            # Click the zone and answer the file chooser it opens, then wait for the upload to finish.
            zone = frame.locator(f'[data-bb-dz="{fid}"]').first
            async with page.expect_file_chooser(timeout=10000) as fc:
                await zone.click()
            await (await fc.value).set_files(paths)
            try:
                await frame.wait_for_function(
                    "([z, n]) => { const e = document.querySelector(`[data-bb-dz=\"${z}\"]`);"
                    " return e && e.querySelectorAll('.dz-complete, .dz-success, .dz-error').length >= n; }",
                    arg=[fid, len(paths)], timeout=60000)
            except Exception:
                pass
            err = await zone.evaluate("z => [...z.querySelectorAll('.dz-error-message')].map(e => e.innerText.trim())"
                                      ".filter(Boolean).join(' ')")
            if err:
                return {"status": "error", "note": f"the uploader refused it: {err[:200]}"}
        else:
            # Many sites upload on change and then clear the input (Manta's logo), so an empty readback alone
            # doesn't mean it failed: watch for the upload request.
            sent: list = []

            def on_request(r):
                if r.method in ("POST", "PUT") and ("multipart" in (r.headers.get("content-type") or "")
                                                    or len(r.post_data_buffer or b"") > 1000):
                    sent.append(r.url)
            page.on("request", on_request)
            try:
                if any(Path(x).stat().st_size > LARGE_FILE for x in paths):
                    await set_large_files(loc, paths)
                else:
                    await loc.set_input_files(paths)
                await asyncio.sleep(2.5)
            finally:
                page.remove_listener("request", on_request)
            cur = await _read_field(page, state, fid) or f
            if not cur.get("value") and sent:
                state.chosen[fid] = {"uploaded": sent[-1]}
                return {"status": "uploaded", "note": "the site sent the file and cleared the input; check the page "
                        "(a preview or the saved profile) to confirm it took"}
            if not cur.get("value"):
                return {"status": "mismatch", "actual": [], "note": "the input is empty and no upload went out; the "
                        "site may want its own upload button (press it, then fill this field again)"}
    elif a == "tags":
        if kind not in ("tags", "combobox", "text"):
            return {"status": "error", "note": f"'tags' doesn't apply to a {kind} field"}
        for t in entry.tags or []:
            l2 = _loc(state, fid)
            await l2.click()
            await l2.press_sequentially(t, delay=20)
            if not await _click_option(frame, t, timeout=1.0):
                await l2.press("Enter")
            await asyncio.sleep(0.1)
        state.chosen[fid] = entry.tags
    cur = await _read_field(page, state, fid) or f
    status, note = _compare(cur, entry, state.chosen.get(fid))
    out: dict[str, Any] = {"status": "filled" if status == "match" else status}
    if note:
        out["note"] = note
    if status == "mismatch":
        out["actual"] = _actual(cur)
    if a == "choose" and status == "match" and state.chosen.get(fid):
        out["chose"] = state.chosen[fid]["label"]
    return out


async def check(page: Page, state: FormState, *, group: str | None = None, planned_forms: bool = False,
                validity: bool = True) -> dict:
    """Re-read every field and compare to the plan. `ok` is what a submit needs.

    Problems: a planned field that doesn't hold what was planned; text in a field nobody planned (prefilled
    or typed by someone else); a required field nobody planned; the browser's own validation messages.
    `group` checks one form; `planned_forms` the forms holding planned fields (a newsletter box beside the
    form you filled isn't yours to finish).
    """
    snap = await survey(page, state, max_text=0, validity=validity)
    groups = {group} if group else None
    if planned_forms and not group:
        groups = {state.fields[fid]["_group"] for fid in state.plan if fid in state.fields} or None
    fields: dict[str, dict] = {}
    unplanned_text, unplanned_required, invalid, errors, defaults, carried = [], [], [], [], [], []
    present = set()
    # Text you typed into fields that are gone now: a client-side route change (Softonic's Save) re-renders the form
    # with new ids and your text in it, under a different label.
    shown_ids = {f["id"] for g in snap["groups"] for f in g["fields"]}
    typed_gone = {loose_text(v) for fid, e in state.plan.items() if e.action == "text" and fid not in shown_ids
                  and isinstance(v := state.chosen.get(fid), str) and v}
    typed_gone |= {loose_text(v) for v in state.carried.values() if v}
    for g in snap["groups"]:
        if groups and g["group"] not in groups:
            continue
        for f in g["fields"]:
            fid = f["id"]
            present.add(fid)
            if f["kind"] == "tags":
                f = {**f, "_chips": await state.frames[state.fields[fid]["_frame"]].evaluate(pagejs.TAG_CHIPS, fid)}
            entry = state.plan.get(fid)
            if entry is not None:
                s, note = _compare(f, entry, state.chosen.get(fid))
                fields[fid] = {"status": s, **({"note": note} if note else {}),
                               **({"expected": _expected(entry, state.chosen.get(fid)), "actual": _actual(f)}
                                  if s == "mismatch" else {})}
            elif f.get("captcha"):
                pass  # an image-captcha field: the captcha gate types its answer, nobody plans it
            else:
                v = _actual(f)
                mine = state.carried.get(f.get("name") or f.get("label") or "")
                if f["kind"] in TEXT_KINDS and v and ((mine and loose_text(str(v)) == loose_text(mine))
                                                     or loose_text(str(v)) in typed_gone):
                    carried.append({"id": fid, "label": f.get("label")})
                elif f["kind"] in TEXT_KINDS and v and not f.get("disabled") and not f.get("secret") \
                        and not f.get("shown"):  # a div combobox's text is its current pick or prompt: a default
                    unplanned_text.append({"id": fid, "label": f.get("label"), "value": v,
                                           "prefilled": v == state.baseline.get(fid)})
                elif f.get("required") and v in ("", [], False, None):
                    unplanned_required.append({"id": fid, "label": f.get("label"), "kind": f["kind"]})
                elif (f["kind"] not in TEXT_KINDS or f.get("shown")) and v not in ("", [], False, None):
                    defaults.append({"id": fid, "label": f.get("label"), "value": v})
            if f.get("invalid"):
                invalid.append({"id": fid, "label": f.get("label"), "message": f["invalid"]})
            if f.get("error"):
                errors.append({"id": fid, "label": f.get("label"), "error": f["error"]})
    missing = [fid for fid in state.plan if fid not in present
               and (not groups or state.fields.get(fid, {}).get("_group") in groups)]
    bad = [fid for fid, r in fields.items() if r["status"] in ("mismatch", "error")]
    # Blocking: planned fields that differ, text nobody planned, required fields nobody planned, the browser's
    # own validation. Informational: fields no longer shown (a wizard's earlier step), error text on the page
    # (may be stale until the next submit), site defaults left as they are (selects, checkboxes).
    return {
        "ok": not bad and not unplanned_text and not unplanned_required and not invalid,
        "fields": fields,
        **({"unplanned_text": unplanned_text} if unplanned_text else {}),
        **({"unplanned_required": unplanned_required} if unplanned_required else {}),
        **({"invalid": invalid} if invalid else {}),
        **({"field_errors": errors} if errors else {}),
        **({"page_errors": snap["errors"]} if snap["errors"] else {}),
        **({"site_defaults": defaults} if defaults else {}),
        **({"yours_from_last_page": carried} if carried else {}),
        **({"planned_not_shown": missing} if missing else {}),
    }


def _expected(entry: Entry, chosen: Any) -> Any:
    if entry.action == "text":
        return chosen
    if entry.action == "choose":
        return (chosen or {}).get("label")
    if entry.action == "choose_all":
        return [o["label"] for o in chosen or []]
    return getattr(entry, entry.action)


LARGE_FILE = 45 * 1024 * 1024  # Playwright won't send files over 50 MB to a browser in another container
CHUNK = 4 * 1024 * 1024

ADD_CHUNK = """(el, [i, b64]) => {
  const bin = atob(b64), bytes = new Uint8Array(bin.length);
  for (let k = 0; k < bin.length; k++) bytes[k] = bin.charCodeAt(k);
  (el.__bbFiles ||= [])[i] = (el.__bbFiles[i] || []).concat([bytes]);
}"""
ATTACH = """(el, files) => {
  const dt = new DataTransfer();
  files.forEach((f, i) => dt.items.add(new File(el.__bbFiles[i], f.name, {type: f.type, lastModified: f.lastModified})));
  delete el.__bbFiles;
  el.files = dt.files;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return [...el.files].map(f => f.size);
}"""


async def set_large_files(loc, paths: list[str]) -> None:
    """Put files of any size into a file input: their bytes go to the page in chunks and are attached as File
    objects through a DataTransfer, with input and change events, as a real file pick would fire."""
    import base64
    import mimetypes

    await loc.evaluate("el => { el.__bbFiles = [] }")
    meta = []
    for i, path in enumerate(paths):
        pth = Path(path)
        with pth.open("rb") as fh:
            while chunk := fh.read(CHUNK):
                await loc.evaluate(ADD_CHUNK, [i, base64.b64encode(chunk).decode()])
        meta.append({"name": pth.name, "type": mimetypes.guess_type(pth.name)[0] or "application/octet-stream",
                     "lastModified": int(pth.stat().st_mtime * 1000)})
    sizes = await loc.evaluate(ATTACH, meta)
    want = [Path(x).stat().st_size for x in paths]
    if sizes != want:
        raise RuntimeError(f"the page holds {sizes} bytes, the files have {want}")
