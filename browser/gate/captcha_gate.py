"""Captcha gate for fastbrowse: solve captchas in code before the agent's next step.

Pass a `CaptchaGate` as `run_task(check_access=gate)`. fastbrowse awaits it before every observation, read,
screenshot, navigation and action, so holding it holds the agent: Jev never sees an unsolved captcha and cannot
submit a form before the token is in.

Routing:
  reCAPTCHA v2 checkbox (incl. Enterprise), hCaptcha, Turnstile, Cloudflare challenge page
      -> NopeCHA (the extension already in Steel) gets `nopecha_wait` seconds, then 2captcha takes over
         (hCaptcha and the Cloudflare page have no 2captcha fallback: 2captcha needs a proxy for the latter).
  GeeTest v3/v4, MTCaptcha, image-text captchas
      -> 2captcha straight away; NopeCHA does not solve them.
  reCAPTCHA invisible / v3
      -> not held: their tokens are made when the form submits.

The gate keeps its own CDP connection, so it needs the browser-level websocket (Steel: ws://localhost:31NN/).
It only looks at tabs opened after `start()`, which are the run's own tabs and popups.
"""

import asyncio
import itertools
import json
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
import websockets

# Installed in every tab the run opens, before page scripts run. Captcha libraries keep their state in closures, so
# the only way to hand them a token from outside is to catch their objects as they are created: GeeTest's captcha
# object (to answer getValidate() and fire onSuccess), and Turnstile's and hCaptcha's render() params (callbacks).
INIT_JS = r"""
(() => {
  if (window.__cg) return;
  const cg = window.__cg = { geetest: [], turnstile: [], hcaptcha: [] };
  // A setter on window is not enough: a top-level `function initGeetest4` in the library replaces an accessor
  // without calling it. So wrap after each script runs instead. A capture listener on document gets a script's
  // load event right after the script executes and before the script element's own onload, which is where pages
  // usually call these.
  const hooks = [];
  const hook = (name, wrap) => hooks.push([name, wrap]);
  const wrapAll = () => {
    for (const [name, wrap] of hooks) {
      const value = window[name];
      if (value && !value.__cgWrapped) try { window[name] = wrap(value); } catch (e) {}
    }
  };
  document.addEventListener('load', e => { if (e.target && e.target.tagName === 'SCRIPT') wrapAll(); }, true);
  const poll = setInterval(wrapAll, 50);
  setTimeout(() => clearInterval(poll), 60000);
  const wrapGeetest = version => init => {
    const wrapped = function (config, callback) {
      return init.call(this, config, obj => {
        const rec = { version, config, obj, handlers: [], result: null };
        cg.geetest.push(rec);
        try {
          const onSuccess = obj.onSuccess && obj.onSuccess.bind(obj);
          if (onSuccess) obj.onSuccess = h => { rec.handlers.push(h); return onSuccess(h); };
          const getValidate = obj.getValidate && obj.getValidate.bind(obj);
          if (getValidate) obj.getValidate = () => rec.result || getValidate();
        } catch (e) {}
        return callback && callback(obj);
      });
    };
    wrapped.__cgWrapped = true;
    return wrapped;
  };
  const wrapRender = kind => api => {
    if (!api || api.__cgWrapped || typeof api.render !== 'function') return api;
    const render = api.render.bind(api);
    api.render = (container, params) => {
      const id = render(container, params);
      cg[kind].push({ id, params: params || {} });
      return id;
    };
    api.__cgWrapped = true;
    return api;
  };
  hook('initGeetest', wrapGeetest(3));
  hook('initGeetest4', wrapGeetest(4));
  hook('turnstile', wrapRender('turnstile'));
  hook('hcaptcha', wrapRender('hcaptcha'));
  wrapAll();

  // reCAPTCHA and hCaptcha keep their image-challenge frame in the page whenever it is not in use, hidden with
  // visibility and moved off screen. fastbrowse still reads into it and offers Jev "Get an audio challenge" and
  // "Get a new challenge", and Jev then calls the page a bot wall (most of bench round 2's blocks). Only
  // display:none takes a frame out of fastbrowse's view, so mirror the provider's own hiding with it, and undo it
  // the moment the provider shows the challenge (for NopeCHA to solve).
  const CHALLENGE = 'iframe[src*="/recaptcha/api2/bframe"], iframe[src*="/recaptcha/enterprise/bframe"], iframe[src*="hcaptcha.com"][src*="frame=challenge"]';
  let queued = false;
  const tuck = () => {
    queued = false;
    for (const frame of document.querySelectorAll(CHALLENGE)) {
      let box = frame;
      while (box.parentElement && box.parentElement !== document.body) box = box.parentElement;
      const hiddenByProvider = box.style.visibility === 'hidden' || box.style.opacity === '0';
      if (hiddenByProvider && box.style.display !== 'none') { box.style.display = 'none'; box.dataset.cgTucked = '1'; }
      else if (!hiddenByProvider && box.dataset.cgTucked) { box.style.display = ''; delete box.dataset.cgTucked; }
    }
  };
  const schedule = () => { if (!queued) { queued = true; setTimeout(tuck, 0); } };
  const start = () => new MutationObserver(schedule).observe(document.documentElement,
    { subtree: true, childList: true, attributes: true, attributeFilter: ['style'] });
  if (document.documentElement) start(); else document.addEventListener('readystatechange', start, { once: true });
  cg.tuck = tuck;
})();
"""

# Returns every captcha on the page as {type, key, solved, ...}. `key` identifies the widget within the document.
DETECT_JS = r"""
(() => {
  const out = [];
  const q = (s, root) => [...(root || document).querySelectorAll(s)];
  const filled = s => q(s).some(e => (e.value || '').trim().length > 0);
  const shown = e => {
    const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const param = (src, k) => {
    try { const u = new URL(src, location.href); return u.searchParams.get(k) || new URLSearchParams(u.hash.slice(1)).get(k); }
    catch (e) { return null; }
  };
  const cg = window.__cg || null;

  // Cloudflare's full-page challenge ("Just a moment..."). _cf_chl_opt alone isn't one: some sites keep it on the
  // real page after the challenge has passed, so it counts only on an empty page. Never look the challenge's own
  // elements up (querySelector('#challenge-stage') and the like): the challenge watches for that and never clears
  // while a script probes it (every Cloudflare page failed under the gate, 0 of 42, and passed without it).
  const thin = ((document.body && document.body.innerText) || '').trim().length < 400;
  if (/^(just a moment|attention required)/i.test(document.title) || (window._cf_chl_opt && thin)) {
    out.push({ type: 'cloudflare', key: 'cf-page', solved: false });
    return JSON.stringify(out);
  }

  // reCAPTCHA v2 checkbox. Invisible and v3 widgets have size=invisible anchors and are not held.
  if (cg && cg.tuck) cg.tuck();
  const rcToken = filled('textarea[name="g-recaptcha-response"]');
  // A token the gate injected has expired: bring the hidden widget back so it can be solved again.
  if (!rcToken) for (const el of q('[data-cg-marker="recaptcha"]')) { el.previousElementSibling && (el.previousElementSibling.style.display = ''); el.remove(); }
  const rcSeen = new Set();
  for (const f of q('iframe[src*="/recaptcha/api2/anchor"], iframe[src*="/recaptcha/enterprise/anchor"]')) {
    const sitekey = param(f.src, 'k');
    if (param(f.src, 'size') === 'invisible' || !shown(f) || rcSeen.has(sitekey)) continue;
    rcSeen.add(sitekey);
    const holder = f.closest('[data-sitekey]');
    out.push({
      type: 'recaptcha', key: 'rc:' + sitekey, sitekey,
      enterprise: f.src.includes('/enterprise/'),
      sdata: (holder && holder.getAttribute('data-s')) || null,
      callback: (holder && holder.getAttribute('data-callback')) || null,
      solved: filled('textarea[name="g-recaptcha-response"]'),
    });
  }

  // An invisible reCAPTCHA that answered a submit with an image challenge (HubSpot's forms on share.hsforms.com):
  // hold while the challenge is open, so the submit isn't reported before it went through. The widget then counts
  // as solved once its challenge has closed.
  if (!window.__cgRcChallenged) window.__cgRcChallenged = {};
  for (const f of q('iframe[src*="/recaptcha/api2/bframe"], iframe[src*="/recaptcha/enterprise/bframe"]')) {
    const sitekey = param(f.src, 'k');
    if (!sitekey || rcSeen.has(sitekey)) continue;
    const open = shown(f);
    if (!open && !window.__cgRcChallenged[sitekey]) continue;
    window.__cgRcChallenged[sitekey] = true;
    rcSeen.add(sitekey);
    out.push({ type: 'recaptcha', key: 'rc-challenge:' + sitekey, sitekey, invisible: true,
               enterprise: f.src.includes('/enterprise/'), solved: !open });
  }

  // hCaptcha checkbox. An invisible hCaptcha (claude.ai) has a 0x0 checkbox frame and shows only its challenge
  // frame, when the site runs it: then that open challenge is the captcha.
  const hcSolved = () => filled('textarea[name="h-captcha-response"]') || q('iframe[data-hcaptcha-response]').some(i => i.getAttribute('data-hcaptcha-response'));
  let hcFound = false;
  for (const f of q('iframe[src*="hcaptcha.com"]')) {
    if (!/frame=checkbox/.test(f.src) || !shown(f)) continue;
    const sitekey = param(f.src, 'sitekey');
    out.push({ type: 'hcaptcha', key: 'hc:' + sitekey, sitekey, solved: hcSolved() });
    hcFound = true;
    break;
  }
  if (!hcFound) {
    for (const f of q('iframe[src*="hcaptcha.com"][src*="frame=challenge"]')) {
      if (!shown(f)) continue;
      const sitekey = param(f.src, 'sitekey');
      out.push({ type: 'hcaptcha', key: 'hc-challenge:' + sitekey, sitekey, invisible: true, solved: hcSolved() });
      break;
    }
  }

  // Turnstile: the response input sits in light DOM even when the iframe is in a closed shadow root.
  const tsInputs = q('input[name="cf-turnstile-response"]');
  if (tsInputs.length || q('.cf-turnstile').length) {
    const holder = q('.cf-turnstile[data-sitekey]')[0];
    const rendered = cg && cg.turnstile[0];
    const sitekey = (holder && holder.getAttribute('data-sitekey')) || (rendered && rendered.params.sitekey) || null;
    out.push({
      type: 'turnstile', key: 'ts:' + sitekey, sitekey,
      action: (holder && holder.getAttribute('data-action')) || (rendered && rendered.params.action) || null,
      cdata: (holder && holder.getAttribute('data-cdata')) || (rendered && rendered.params.cData) || null,
      solved: filled('input[name="cf-turnstile-response"]'),
    });
  }

  // GeeTest v3/v4, from the objects INIT_JS caught. Without them a token cannot be handed back, so report that.
  if (cg) {
    cg.geetest.forEach((rec, i) => {
      let solved = !!rec.result;
      if (!solved) try { solved = !!rec.obj.getValidate(); } catch (e) {}
      const c = rec.config || {};
      out.push({
        type: rec.version === 4 ? 'geetest4' : 'geetest', key: 'gt:' + i, index: i,
        gt: c.gt || null, challenge: c.challenge || null, captcha_id: c.captchaId || c.captcha_id || null,
        api_server: c.api_server || null, solved,
      });
    });
  }
  if (!(cg && cg.geetest.length)) {
    const v4 = q('script[src*="gcaptcha4.geetest.com/load"]')[0];
    const v3 = q('script[src*="api.geetest.com/get.php"]')[0];
    if (v4 || v3) out.push({ type: v4 ? 'geetest4' : 'geetest', key: 'gt:unhooked', unhooked: true, solved: false });
  }

  // MTCaptcha.
  const mtFrame = q('iframe[src*="mtcaptcha.com"]')[0];
  if (window.mtcaptchaConfig || mtFrame) {
    const sitekey = (window.mtcaptchaConfig && window.mtcaptchaConfig.sitekey) || (mtFrame && param(mtFrame.src, 'sitekey'));
    out.push({ type: 'mtcaptcha', key: 'mt:' + sitekey, sitekey, solved: filled('input[name="mtcaptcha-verifiedtoken"]') });
  }

  // Image-text captcha: a visible text field that asks for a captcha/code, with a small image near it.
  const askRe = /captcha|security code|verification code|text (from|in|on) the image|characters (shown|above|below|in the image)|enter the code/i;
  const fields = q('input:not([type]), input[type=text], input[type=tel], input[type=number]').filter(shown);
  for (const input of fields) {
    const labels = [input.name, input.id, input.placeholder, input.getAttribute('aria-label'), input.autocomplete,
      ...[...(input.labels || [])].map(l => l.textContent)].join(' ');
    if (!askRe.test(labels)) continue;
    let image = null;
    for (let node = input.parentElement, depth = 0; node && depth < 6 && !image; node = node.parentElement, depth++) {
      const candidates = q('img, canvas', node).filter(img => {
        const r = img.getBoundingClientRect(), about = [img.src, img.alt, img.className, img.id].join(' ');
        return shown(img) && r.width >= 40 && r.width <= 500 && r.height >= 15 && r.height <= 200
          && !/\.svg(\?|$)|logo|icon|avatar/i.test(about);
      });
      // Named like a captcha, or else shaped like one: a wide strip of distorted text.
      image = candidates.find(img => /captcha|code|verif/i.test([img.src, img.alt, img.className, img.id].join(' ')))
        || candidates.find(img => { const r = img.getBoundingClientRect(); return r.width / r.height >= 1.8 && r.width / r.height <= 8; })
        || null;
    }
    if (!image) continue;
    if (!input.dataset.cgId) input.dataset.cgId = 'cg' + Math.random().toString(36).slice(2, 8);
    image.dataset.cgImage = input.dataset.cgId;
    out.push({ type: 'image', key: 'img:' + (image.currentSrc || image.src || input.dataset.cgId), field: input.dataset.cgId,
               solved: (input.value || '').trim().length > 0 });
  }

  // A captcha library still loading on a fresh page: its widget is not there yet, but the agent must not act
  // (and submit) before it is. reCAPTCHA v3 (api.js?render=<sitekey>) never renders a widget, so it does not count.
  if (!out.length && performance.now() < 10000) {
    const loading = q('script[src]').map(s => s.src).find(src =>
      /geetest\.com|mtcaptcha\.com|hcaptcha\.com\/1\/api|challenges\.cloudflare\.com\/turnstile/.test(src) ||
      (/\/recaptcha\/(api|enterprise)\.js/.test(src) && !/[?&]render=(?!explicit)/.test(src)));
    if (loading) out.push({ type: 'loading', key: 'loading', script: loading.split('?')[0], solved: false });
  }
  return JSON.stringify(out);
})()
"""

# Hand a 2captcha answer to the page. Each is a function of (captcha, solution) evaluated in the page.
INJECT_JS = {
    "recaptcha": r"""(c, s) => {
      const token = s.gRecaptchaResponse || s.token;
      for (const t of document.querySelectorAll('textarea[name="g-recaptcha-response"]')) { t.value = token; t.innerHTML = token; }
      const callbacks = c.callback ? [c.callback] : [];
      const walk = (o, depth) => {
        if (!o || typeof o !== 'object' || depth > 5 || o instanceof Node) return;
        for (const [k, v] of Object.entries(o)) { if (k === 'callback') callbacks.push(v); else walk(v, depth + 1); }
      };
      try { for (const client of Object.values(window.___grecaptcha_cfg?.clients || {})) walk(client, 0); } catch (e) {}
      for (const api of [window.grecaptcha, window.grecaptcha && window.grecaptcha.enterprise]) if (api) try { api.getResponse = () => token; } catch (e) {}
      let called = 0;
      for (let cb of callbacks) { if (typeof cb === 'string') cb = window[cb]; if (typeof cb === 'function') try { cb(token); called++; } catch (e) {} }
      return called;
    }""",
    "turnstile": r"""(c, s) => {
      for (const i of document.querySelectorAll('input[name="cf-turnstile-response"]')) i.value = s.token;
      let called = 0;
      for (const r of (window.__cg?.turnstile || [])) { let cb = r.params.callback; if (typeof cb === 'string') cb = window[cb]; if (typeof cb === 'function') try { cb(s.token); called++; } catch (e) {} }
      if (window.turnstile) try { window.turnstile.getResponse = () => s.token; } catch (e) {}
      return called;
    }""",
    "mtcaptcha": r"""(c, s) => {
      for (const i of document.querySelectorAll('input[name="mtcaptcha-verifiedtoken"]')) i.value = s.token;
      if (window.mtcaptcha) try { window.mtcaptcha.getVerifiedToken = () => s.token; } catch (e) {}
      let cb = window.mtcaptchaConfig && window.mtcaptchaConfig['verified-callback'];
      if (typeof cb === 'string') cb = window[cb];
      if (typeof cb === 'function') try { cb({ verifiedToken: s.token, isVerified: true }); return 1; } catch (e) {}
      return 0;
    }""",
    "geetest": r"""(c, s) => {
      const rec = window.__cg.geetest[c.index];
      rec.result = rec.version === 4 ? s
        : { geetest_challenge: s.challenge, geetest_validate: s.validate, geetest_seccode: s.seccode };
      for (const [k, v] of Object.entries(rec.result)) for (const i of document.querySelectorAll(`input[name="${k}"]`)) i.value = v;
      let called = 0;
      for (const h of rec.handlers) try { h(); called++; } catch (e) {}
      return called;
    }""",
    "image": r"""(c, s) => {
      const input = document.querySelector(`[data-cg-id="${c.field}"]`);
      if (!input) return -1;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      input.focus();
      setter.call(input, s.text);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return 1;
    }""",
}
INJECT_JS["geetest4"] = INJECT_JS["geetest"]

# After a 2captcha solve the widget still looks unsolved (GeeTest says "Click to verify", MTCaptcha shows its text
# box), and the agent reads that as a bot wall it cannot pass. The page only reads the token, so hide the widget and
# say in its place that it is solved. The note goes once a button is pressed or the form submitted: it sits where pages
# put their own result, and the reader quoted "Captcha solved" as the result message (bench round 9, 5 of 19 passes,
# and a smoke run that reported only it). The empty element stays, since DETECT_JS restores the widget by it.
CONCEAL_JS = r"""(kind) => {
  if (!window.__cgMarkerClear) {
    window.__cgMarkerClear = (e) => {
      if (e.type === 'click' && !(e.target.closest && e.target.closest('button, input[type=submit], input[type=button], [role=button]'))) return;
      for (const m of document.querySelectorAll('[data-cg-marker]')) { m.textContent = ''; m.removeAttribute('role'); }
    };
    document.addEventListener('click', window.__cgMarkerClear, true);
    document.addEventListener('submit', window.__cgMarkerClear, true);
  }
  const selectors = {
    recaptcha: 'iframe[src*="/recaptcha/api2/anchor"], iframe[src*="/recaptcha/enterprise/anchor"]',
    turnstile: '.cf-turnstile',
    mtcaptcha: 'iframe[src*="mtcaptcha.com"]',
    geetest: '.geetest_holder, .geetest_btn, .geetest_radar_btn',
    geetest4: '.geetest_captcha, .geetest_box_wrap',
  };
  let hidden = 0;
  for (const el of document.querySelectorAll(selectors[kind] || 'x-none')) {
    if (el.style.display === 'none') continue;
    el.style.display = 'none';
    hidden++;
    if (el.nextElementSibling && el.nextElementSibling.dataset.cgMarker) continue;
    const marker = document.createElement('div');
    marker.dataset.cgMarker = kind;
    marker.setAttribute('role', 'status');
    marker.textContent = 'Captcha solved';
    el.after(marker);
  }
  return hidden;
}"""

IMAGE_JS = r"""(field) => {
  const img = document.querySelector(`[data-cg-image="${field}"]`);
  if (!img) return null;
  try {
    const canvas = img.tagName === 'CANVAS' ? img : document.createElement('canvas');
    if (img.tagName !== 'CANVAS') {
      canvas.width = img.naturalWidth || img.width; canvas.height = img.naturalHeight || img.height;
      canvas.getContext('2d').drawImage(img, 0, 0);
    }
    return { data: canvas.toDataURL('image/png').split(',')[1] };
  } catch (e) {
    const r = img.getBoundingClientRect();
    return { clip: { x: r.left + scrollX, y: r.top + scrollY, width: r.width, height: r.height, scale: 1 } };
  }
}"""

NOPECHA_FIRST = {"recaptcha", "hcaptcha", "turnstile", "cloudflare"}
NO_FALLBACK = {"hcaptcha", "cloudflare"}
LOADING_WAIT = 8
"""Seconds to hold a fresh page whose captcha library is loading but has not rendered its widget."""

class TwoCaptchaError(RuntimeError):
    pass


class CaptchaUnsolved(RuntimeError):
    """Raised from the hook when a captcha the gate gave up on is still on the page, so the run stops there instead of
    wandering. fastbrowse ends the run with status `error`; `run.py` reports it as `blocked` with `gate.unsolved`."""


class TwoCaptcha:
    """Minimal client for 2captcha's createTask/getTaskResult API."""

    API = "https://api.2captcha.com"

    def __init__(self, key: str, http: httpx.AsyncClient):
        self.key, self.http = key, http

    async def solve(self, task: dict, timeout: float = 180) -> tuple[dict, float]:
        created = (await self.http.post(f"{self.API}/createTask", json={"clientKey": self.key, "task": task})).json()
        if created.get("errorId"):
            raise TwoCaptchaError(created.get("errorCode") or str(created))
        task_id, deadline = created["taskId"], time.monotonic() + timeout
        await asyncio.sleep(5 if task["type"] == "ImageToTextTask" else 10)
        while time.monotonic() < deadline:
            result = (await self.http.post(f"{self.API}/getTaskResult", json={"clientKey": self.key, "taskId": task_id})).json()
            if result.get("errorId"):
                raise TwoCaptchaError(result.get("errorCode") or str(result))
            if result.get("status") == "ready":
                return result["solution"], float(result.get("cost") or 0)
            await asyncio.sleep(4)
        raise TwoCaptchaError(f"no answer within {timeout:.0f}s")


@dataclass
class _Seen:
    first: float = field(default_factory=time.monotonic)
    attempts: int = 0
    done: bool = False
    """Solved, or given up on; either way the gate no longer holds the run for it."""


class CaptchaGate:
    """A fastbrowse `check_access` hook that holds the run while a captcha in one of its tabs is unsolved."""

    def __init__(
        self,
        cdp_url: str,
        twocaptcha_key: str | None = None,
        *,
        nopecha_wait: float = 30,
        cloudflare_wait: float = 60,
        max_hold: float = 240,
        max_solves: int = 10,
        max_dollars: float = 0.10,
        stop_when_unsolved: bool = True,
        attach_match: str | None = None,
        force_2captcha: bool = False,
        log=None,
    ):
        self.cdp_url, self.twocaptcha_key = cdp_url, twocaptcha_key
        self.nopecha_wait, self.cloudflare_wait, self.max_hold = nopecha_wait, cloudflare_wait, max_hold
        self.max_solves, self.max_dollars = max_solves, max_dollars
        # For a site that rejected the extension's answer: 2captcha solves at once, even a captcha NopeCHA already
        # marked solved, and its token replaces NopeCHA's (American Heritage refused NopeCHA's reCAPTCHA twice).
        self.force_2captcha = force_2captcha
        self._log = log or (lambda msg: print(f"[captcha-gate] {msg}", file=sys.stderr, flush=True))
        self.events: list[dict] = []
        """What the gate did, for the run's record: holds, solves (by whom, how long, cost) and give-ups."""
        self.solves = 0
        self.dollars = 0.0
        self.stop_when_unsolved = stop_when_unsolved
        self.attach_match = attach_match
        """A run that attaches to an open tab (fastbrowse's `target_match`) works in a tab that predates the gate:
        tabs whose title or URL contains this are watched like the run's own. With it, the gate watches *only*
        that tab and the tabs it opens (by opener, transitively): other agents work in other windows of the same
        browser at the same time, and their captchas are theirs."""
        self._mine: set[str] = set()
        self.unsolved: list[dict] = []
        """Captchas the gate gave up on that were still on the page at its last scan."""
        self._gave_up: set[str] = set()
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._sessions: dict[str, str] = {}
        self._preexisting: set[str] = set()
        self._reloaded: set[str] = set()
        self._seen: dict[str, _Seen] = {}
        self._lock = asyncio.Lock()
        self._clean_at = 0.0
        self._started = time.monotonic()

    # ---- lifecycle -------------------------------------------------------------------------------------------

    async def __aenter__(self) -> "CaptchaGate":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        self._ws = await websockets.connect(self.cdp_url, max_size=None)
        self._tasks: set[asyncio.Task] = set()
        self._reader_task = asyncio.create_task(self._reader())
        self._http = httpx.AsyncClient(timeout=30)
        self._solver = TwoCaptcha(self.twocaptcha_key, self._http) if self.twocaptcha_key else None
        targets = (await self._send("Target.getTargets"))["targetInfos"]
        self._preexisting = {t["targetId"] for t in targets if t["type"] == "page"
                             and not (self.attach_match and (self.attach_match in t["title"] or self.attach_match in t["url"]))}
        if self.attach_match:
            self._mine = {t["targetId"] for t in targets if t["type"] == "page"
                          and (self.attach_match in t["title"] or self.attach_match in t["url"])}
        await self._send("Target.setDiscoverTargets", {"discover": True})

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._reader_task.cancel()
        await self._ws.close()
        await self._http.aclose()

    # ---- CDP plumbing ----------------------------------------------------------------------------------------

    async def _reader(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "id" in msg:
                    future = self._pending.pop(msg["id"], None)
                    if future and not future.done():
                        future.set_result(msg)
                elif msg.get("method") == "Target.targetCreated":
                    info = msg["params"]["targetInfo"]
                    if info["type"] == "page" and (not self.attach_match or info.get("openerId") in self._mine):
                        if self.attach_match:
                            self._mine.add(info["targetId"])
                        # Install the hooks as early as possible; a check_access call before navigation repeats it.
                        task = asyncio.create_task(self._quiet_session(info["targetId"]))
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
                elif msg.get("method") == "Target.targetDestroyed":
                    self._sessions.pop(msg["params"]["targetId"], None)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.add_done_callback(lambda f: f.cancelled() or f.exception())  # nobody may await it now
                    future.set_exception(ConnectionError("CDP connection closed"))

    async def _send(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 15) -> dict:
        i = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[i] = future
        message = {"id": i, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        await self._ws.send(json.dumps(message))
        try:
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(i, None)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result", {})

    async def _session(self, target_id: str) -> str:
        session = self._sessions.get(target_id)
        if session:
            return session
        session = (await self._send("Target.attachToTarget", {"targetId": target_id, "flatten": True}))["sessionId"]
        self._sessions[target_id] = session
        # Chromium 156 ignores scripts added for new documents on a session that has not enabled the Page domain.
        await self._send("Page.enable", {}, session)
        await self._send("Page.addScriptToEvaluateOnNewDocument", {"source": INIT_JS}, session)
        await self._send("Runtime.evaluate", {"expression": INIT_JS}, session)
        return session

    async def _quiet_session(self, target_id: str) -> None:
        try:
            await self._session(target_id)
        except (RuntimeError, asyncio.TimeoutError, ConnectionError, websockets.ConnectionClosed):
            self._sessions.pop(target_id, None)

    async def _eval(self, session: str, expression: str, timeout: float = 10):
        result = await self._send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}, session, timeout)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("text", "page script failed"))
        return result.get("result", {}).get("value")

    async def _call(self, session: str, function: str, *args):
        return await self._eval(session, f"({function})(...{json.dumps(list(args))})")

    # ---- the hook --------------------------------------------------------------------------------------------

    async def __call__(self) -> None:
        # fastbrowse calls the hook several times at once before each step; one scan answers them all.
        if time.monotonic() - self._clean_at < 0.3:
            return
        async with self._lock:
            if time.monotonic() - self._clean_at < 0.3:
                return
            began, announced = time.monotonic(), set()
            while True:
                open_captchas = await self._scan()
                if not open_captchas:
                    self._clean_at = time.monotonic()
                    if announced:
                        self._record("released", seconds=round(time.monotonic() - began, 1))
                    self._raise_if_unsolved()
                    return
                if time.monotonic() - began > self.max_hold:
                    self._record("hold_limit", seconds=round(time.monotonic() - began, 1))
                    for _, captcha, key in open_captchas:
                        self._seen[key].done = True
                        self._gave_up.add(key)
                    self.unsolved = [{"type": c["type"], "url": c["url"]} for _, c, _ in open_captchas]
                    self._raise_if_unsolved()
                    return
                for target, captcha, key in open_captchas:
                    if key not in announced and captcha["type"] != "loading":
                        announced.add(key)
                        self._record("holding", type=captcha["type"], url=captcha["url"])
                    await self._handle(target, captcha, key)
                await asyncio.sleep(1.5)

    async def _scan(self) -> list[tuple[str, dict, str]]:
        """Unsolved captchas the gate is still responsible for, in the run's tabs."""
        targets = (await self._send("Target.getTargets"))["targetInfos"]
        if self.attach_match:
            grew = True
            while grew:  # the attached tab, and whatever it (or a tab it opened) opened since
                grew = False
                for t in targets:
                    if t["type"] == "page" and t["targetId"] not in self._mine and (
                            t.get("openerId") in self._mine or self.attach_match in t["title"]
                            or self.attach_match in t["url"]):
                        self._mine.add(t["targetId"])
                        grew = True
        found, still_there = [], []
        for t in targets:
            if t["type"] != "page" or t["targetId"] in self._preexisting or not t["url"].startswith("http"):
                continue
            if self.attach_match and t["targetId"] not in self._mine:
                continue
            try:
                session = await self._session(t["targetId"])
                captchas = json.loads(await self._eval(session, DETECT_JS) or "[]")
            except (RuntimeError, asyncio.TimeoutError):
                # The tab navigated or closed mid-scan; the next scan sees its new document.
                self._sessions.pop(t["targetId"], None)
                continue
            for c in captchas:
                c["url"] = t["url"]
                key = f"{t['targetId']}|{t['url']}|{c['key']}"
                first_sight = key not in self._seen
                seen = self._seen.setdefault(key, _Seen())
                if c["solved"] and not seen.done and self.force_2captcha and not seen.attempts \
                        and c["type"] not in NO_FALLBACK and self._solver:
                    found.append((t["targetId"], c, key))
                    continue
                if c["solved"] and not seen.done:
                    seen.done = True
                    # Solved between two scans with no 2captcha job out: the extension did it.
                    if not first_sight and not seen.attempts:
                        self._record("solved", type=c["type"], by="nopecha", url=c["url"], seconds=round(time.monotonic() - seen.first, 1))
                if not seen.done:
                    found.append((t["targetId"], c, key))
                elif key in self._gave_up and not c["solved"]:
                    still_there.append({"type": c["type"], "url": c["url"]})
        self.unsolved = still_there
        return found

    async def _handle(self, target: str, c: dict, key: str) -> None:
        seen, kind = self._seen[key], c["type"]
        c["_key"] = key
        waited = time.monotonic() - seen.first
        if kind == "loading":
            if waited >= LOADING_WAIT:
                seen.done = True
            return
        if kind in NOPECHA_FIRST:
            limit = self.cloudflare_wait if kind == "cloudflare" else self.nopecha_wait
            if self.force_2captcha and kind not in NO_FALLBACK:
                limit = 0
            if waited < limit:
                return
            if kind == "cloudflare" and target not in self._reloaded:
                # A Cloudflare check NopeCHA hasn't cleared in time often passes on a fresh load, and the clearance
                # cookie a late solve set carries over (Softpedia: given up at 60s, open on the next visit).
                self._reloaded.add(target)
                self._record("reload", type=kind, reason="Cloudflare check not cleared in time; trying a fresh load")
                await self._send("Page.reload", {}, await self._session(target))
                del self._seen[key]
                await asyncio.sleep(6)
                return
            if kind in NO_FALLBACK or not self._solver or not c.get("sitekey"):
                return self._give_up(seen, c, f"NopeCHA did not solve it in {limit:.0f}s and 2captcha has no fallback for it")
        if c.get("unhooked"):
            # GeeTest loaded before the hooks were in; a reload with them installed lets the token be handed back.
            if target not in self._reloaded:
                self._reloaded.add(target)
                self._record("reload", type=kind, reason="captcha object created before the gate's hooks")
                await self._send("Page.reload", {}, await self._session(target))
                del self._seen[key]
                # Keep holding until the widget is rebuilt, or the next scan finds a page with no captcha yet.
                await asyncio.sleep(6)
                return
            return self._give_up(seen, c, "captcha object not reachable for injection")
        if not self._solver:
            return self._give_up(seen, c, "no 2captcha key")
        if seen.attempts >= 2:
            return self._give_up(seen, c, "2captcha failed twice")
        if self.solves >= self.max_solves or self.dollars >= self.max_dollars:
            return self._give_up(seen, c, f"2captcha budget spent ({self.solves} solves, ${self.dollars:.4f})")
        seen.attempts += 1
        await self._solve_2captcha(target, c, seen)

    def _raise_if_unsolved(self) -> None:
        if self.stop_when_unsolved and self.unsolved:
            # Stop the clean-scan shortcut from swallowing the next call too: the run must see this every time.
            self._clean_at = 0.0
            kinds = ", ".join(sorted({u["type"] for u in self.unsolved}))
            raise CaptchaUnsolved(f"unsolved captcha ({kinds}) at {self.unsolved[0]['url']}")

    def _give_up(self, seen: _Seen, c: dict, reason: str) -> None:
        seen.done = True
        self._gave_up.add(c["_key"])
        self._record("gave_up", type=c["type"], url=c["url"], reason=reason)

    async def _solve_2captcha(self, target: str, c: dict, seen: _Seen) -> None:
        session = await self._session(target)
        kind, url = c["type"], c["url"]
        user_agent = await self._eval(session, "navigator.userAgent")
        if kind == "recaptcha":
            task = {"type": "RecaptchaV2EnterpriseTaskProxyless" if c["enterprise"] else "RecaptchaV2TaskProxyless",
                    "websiteURL": url, "websiteKey": c["sitekey"], "userAgent": user_agent}
            if c.get("sdata"):
                task["enterprisePayload"] = {"s": c["sdata"]}
            if c.get("invisible"):
                task["isInvisible"] = True
        elif kind == "turnstile":
            task = {"type": "TurnstileTaskProxyless", "websiteURL": url, "websiteKey": c["sitekey"]}
            if c.get("action"):
                task["action"] = c["action"]
            if c.get("cdata"):
                task["data"] = c["cdata"]
        elif kind == "mtcaptcha":
            task = {"type": "MtCaptchaTaskProxyless", "websiteURL": url, "websiteKey": c["sitekey"]}
        elif kind == "geetest":
            task = {"type": "GeeTestTaskProxyless", "websiteURL": url, "gt": c["gt"], "challenge": c["challenge"], "userAgent": user_agent}
            if c.get("api_server"):
                task["geetestApiServerSubdomain"] = c["api_server"]
        elif kind == "geetest4":
            task = {"type": "GeeTestTaskProxyless", "websiteURL": url, "version": 4, "initParameters": {"captcha_id": c["captcha_id"]}}
        elif kind == "image":
            shot = await self._call(session, IMAGE_JS, c["field"])
            if shot and shot.get("clip"):
                shot = {"data": (await self._send("Page.captureScreenshot", {"format": "png", "clip": shot["clip"], "captureBeyondViewport": True}, session))["data"]}
            if not shot or not shot.get("data"):
                return self._give_up(seen, c, "could not capture the captcha image")
            task = {"type": "ImageToTextTask", "body": shot["data"]}
        else:
            return self._give_up(seen, c, "no 2captcha route")
        began = time.monotonic()
        try:
            solution, cost = await self._solver.solve(task)
        except (TwoCaptchaError, httpx.HTTPError) as exc:
            self._record("2captcha_error", type=kind, error=str(exc)[:200], attempt=seen.attempts)
            return
        self.solves += 1
        self.dollars += cost
        try:
            session = await self._session(target)
            called = await self._call(session, INJECT_JS[kind], c, solution)
            if kind != "image":
                await self._call(session, CONCEAL_JS, kind)
        except (RuntimeError, asyncio.TimeoutError) as exc:
            self._record("inject_error", type=kind, error=str(exc)[:200])
            return
        seen.done = True
        self._record("solved", type=kind, by="2captcha", url=url, seconds=round(time.monotonic() - began, 1),
                     waited=round(began - seen.first, 1), cost=cost, callbacks=called,
                     **({"text": solution.get("text")} if kind == "image" else {}))

    def _record(self, event: str, **details) -> None:
        entry = {"at": round(time.monotonic() - self._started, 1), "event": event, **details}
        if "url" in entry:
            entry["url"] = urlsplit(entry["url"])._replace(query="", fragment="").geturl()
        self.events.append(entry)
        self._log(json.dumps(entry))

    def summary(self) -> dict:
        solved = [e for e in self.events if e["event"] == "solved"]
        return {
            "solved": [{k: e[k] for k in ("type", "by", "seconds", "cost") if k in e} for e in solved],
            "gave_up": [{k: e[k] for k in ("type", "reason") if k in e} for e in self.events if e["event"] == "gave_up"],
            "unsolved": self.unsolved,
            "twocaptcha_solves": self.solves,
            "twocaptcha_dollars": round(self.dollars, 5),
        }

    def solved_on(self, origin: str) -> bool:
        """Whether the gate solved a captcha on `origin`, so a `blocked` there is fastbrowse misreading a solved page."""
        return any(e["event"] == "solved" and e.get("url", "").startswith(origin) for e in self.events)

