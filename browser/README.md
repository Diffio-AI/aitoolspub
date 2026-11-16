# browser: a shared browser MCP for Claude Code

A stdio MCP server that gives any Claude Code session a real browser: the Steel Chrome in `../steel` (headful, real
GPU optional, NopeCHA + 2captcha for captchas, optional).

- **One browser, shared sign-ins.** Every Claude Code session starts its own server process, and they all attach to
  the same Chrome and its default (persistent) profile. Cookies, localStorage and logins are shared: sign into a
  site once, by hand in the live viewer or in any agent's session, and every agent is signed in.
- **A window per session.** `open()` gives each session its own window, so any number of agents (and any number
  of sessions per agent) work side by side without stealing each other's tabs, and every session's page is a
  foreground page (background tabs are throttled).
- **Two ways to drive it.** The *navigator* (an open model through fastbrowse) browses: finds pages, follows menus,
  quotes what it reads, and, with `allow_actions=true`, carries out a task itself (clicks, types the task's text,
  submits). The *direct tools* are deterministic: `goto`, `read`, `screenshot`, `evaluate`, and the form tools,
  where code types Claude's text verbatim, reads every field back and refuses a submit when the page doesn't hold
  what Claude planned.

## Setup

See [`../setup/browser.md`](../setup/browser.md). In short:

```sh
cd steel && cp .env.example .env && docker compose up -d   # the shared Chromes, one per profile
cd ../browser && uv sync
claude mcp add --scope user browser -- "$PWD/.venv/bin/agent-browser-mcp"   # + --no-captchas / --no-navigator
```

| Flag | Meaning |
|---|---|
| `--profiles` | The browser profiles: JSON `{name: {"cdp": url, "about": text, "default": true}}` (default `aitools/steel/profiles.json`). |
| `--cdp` | One browser's CDP websocket instead of the profiles file (or `$AGENT_BROWSER_CDP_URL`); no profile choice. |
| `--state` | Session packets (one JSONL audit log per session, by date). Default `aitools/state/browser`; `none` for none. |
| `--assets` | Optional directory that file fields may name files in by relative path. Absolute paths always work. |
| `--keys` | Directory with `openrouter.key` (the navigator) and `2captcha.key` (default `keys/` at the repo root). |
| `--agent` | A name recorded in packets. |
| `--no-navigator` | Direct tools only, no LLM browsing. |
| `--no-captchas` | Don't solve captchas: no captcha gate on page loads, presses or navigator runs. |

## Tools

| Tool | What it does |
|---|---|
| `open(url, goal?, allow_actions?, profile?)` | New session (window) in a browser profile (default: the default one). With `goal`, the navigator carries on from there. Returns where it landed: title, headings, field count, main links on a page without a form, the survey on a page with fields. |
| `navigate(session, task, allow_actions?)` | A navigator task from the current page: one goal per call, phrased as a page to reach or a fact to quote. With `allow_actions` it acts: click, choose, type text the task gives it, submit, confirm. |
| `goto`, `read`, `screenshot` | Load a URL, read visible text and links (`link_text=`, `href_contains=` for a link's href), look at the page. No model. |
| `evaluate(session, script)` | Run JavaScript in the page and get the JSON result (localStorage, an element's state, a fetch with the site's cookies). |
| `survey`, `fill`, `check`, `press` | Deterministic forms: every field with a stable id; Claude's text typed verbatim and read back; presses refused on a mismatch unless given `override_reason`. |
| `save_value(session, path)` | Write a token or field value the page shows to a file without printing it (tokens are masked in all output). |
| `handoff(session)` | A live link to that session's window for a person: sign in, 2FA, a check the browser can't pass. Pauses automatic captcha solving on that site (15 min, or until `handoff(session, done=True)`) so the person can do the captcha. |
| `wait_for(session, condition\|selector\|text, timeout)` | Wait up to 15 minutes for a JavaScript condition, a visible selector or some text; keeps polling across reloads. |
| `record(session, seconds, every, watch_selectors)`, `record_status(session, stop?, wait?)` | A background timeline of a page that changes on its own: every change to headings, status/alert text, progress bars, buttons (and disabled state), media elements, dialogs, URL, title and watched selectors, timestamped in `timeline.jsonl`, plus a screenshot every `every` seconds in `shots/` (full-page optional). Up to an hour; other tools keep working meanwhile. |
| `assets`, `log_outcome`, `sessions`, `close` | Bookkeeping. |

Fill entries: `"exact text"`, `true/false`, `{"text": [long, shorter, ...]}` (first that fits), `{"choose": [ranked]}`,
`{"choose_all": [...]}`, `{"tags": [...]}`, `{"file": "/abs/path"}`, `{"keep": true}`, `{"clear": true}`. Files on
this machine upload into the container's Chrome fine: Playwright sends their bytes. Over 45 MB (Playwright refuses
files over 50 MB for a browser in another container), `forms.set_large_files` streams them into the page in 4 MB
chunks and attaches them through a DataTransfer with input/change events; a 57 MB MP3 takes about 13 s.
`evaluate(save_to=path)` writes a result of any size to a file. Output is masked only for provider API-token
patterns (`redact.py`); query strings, base64 and JWTs come through as they are.

**Captchas** are solved by the gate (`gate/`, NopeCHA in Chrome with 2captcha behind it) without Claude's
involvement: inside navigator runs, after every page load and around every press. A result's `captchas` says what
was solved; a press refuses while one is unsolved (override with `override_reason`).

The navigator is any command that speaks the fastbrowse CLI (`task --cdp-url --target-match --context --max-steps
--json [--authorize]`); `BROWSE_NAVIGATOR_CMD` swaps it. The default is `gate/fastbrowse_cli.py` (the captcha gate,
bot-check at 0.97, 3 barren reads, one retry). Its models come from `FASTBROWSE_LLM_*`; unset, MiniMax M3.1 Flash
does every purpose, and OpenRouter calls go through `gate/openrouter_route.py`, which picks providers that cite
evidence correctly (see `gate/README.md`). A navigator run costs about $0.005-0.03.

## Signing in

Some sites refuse this browser's sign-ins outright (claude.ai: "Couldn't complete sign-in. You can try signing in
from a different browser", after its hCaptcha), and captchas come up here far more than in your own Chrome: it is
a fresh profile in an automated, container Chromium. Two ways round it:

- **Copy the sign-in from your own Chrome.** Sign in there, then
  `.venv/bin/agent-browser-import-cookies --profile "<profile name>" --domain <site>` (`--list-profiles` lists them,
  `--dry-run` shows cookie names only). It reads a copy of that profile's cookie database, decrypts it with the
  "Chrome Safe Storage" key from your GNOME keyring (that key is why a profile can't just be copied into the
  container), and sets the cookies in the agent browser over CDP. Values are never printed, and only the domains you
  name are copied. Logins kept in localStorage instead of cookies don't carry over.
- **`handoff`**, for a person to sign in by hand in the live viewer (paste works there: see `steel/README.md`,
  "Live viewer"; or `.venv/bin/agent-browser-type <window>` types what you paste into a hidden terminal prompt into
  the window's focused field). It pauses NopeCHA on that site (it otherwise
  solves the captcha before the person can): `src/browse/solver_pause.py` adds the host to NopeCHA's in-memory
  `disabled_hosts` in its service worker and re-applies it every 4s, since Chrome restarts an idle worker from its
  manifest. The pause is browser-wide for that host.

### A Google account for the agents

To give agents their own Google account for "Sign in with Google", put its email, password and authenticator
secret in `keys/google-account.json` (`{"email": ..., "password": ..., "totp_secret": ..., "profile": "testing"}`,
mode 600). `.venv/bin/agent-browser-google-login` then signs it in to the profile on demand: it checks first
(`--check` only checks), then goes through email, password and, when Google asks, an authenticator code, stepping
past "Google prompt" and passkey challenges with "Try another way". Nothing secret is printed. `--code` prints the
current 6-digit code for a site that asks for it, and `--to` picks another profile. Use a dedicated account, not
a personal one.

Either way the sign-in lands in that browser profile (`steel/profiles/<name>`), so every session in the profile
has it and it survives restarts. `--to production` copies into another profile than the default.

## Profiles

Steel runs one Chrome with one user-data-dir, so each browser profile is its own Steel instance (`steel/`), listed
in `steel/profiles.json`. One server process serves them all: `open(url, profile=...)` picks the Chrome, the
session's id carries the profile (`production-1a2b3c`; the default profile's ids are plain `s1a2b3c`), and every
other tool follows the session. Each profile has its own captcha gate and solver pause; the navigator (and its
routing proxy) is shared and is pointed at the session's Chrome per run. Packets record the profile.

To add one: a service in `steel/docker-compose.yml` with its own ports and `./profiles/<name>`, an entry in
`steel/profiles.json`, `docker compose up -d`. Claude sessions started afterwards see it.

## Many agents, one browser

All sessions in a profile share one Chrome; three things keep them apart.

- **A window per session, opened with an opener.** fastbrowse attaches a navigator run to its session's tab by a
  token in the tab's title, and it lets any tab opened *without an opener* during the run join that run, which
  would otherwise force navigator runs to take turns per browser. Here every session window is opened by
  `window.open` from a launcher tab ("agent-browser launcher (keep open)", one per Chrome, found again by title by
  every process), so each has an opener and no navigator adopts it. The new window is identified by a one-off
  `window.name`, because every process sees every new page in the shared context.
- **A reader/writer lock per browser** (`src/browse/locks.py`, `flock` under `~/.cache/agent-browser/locks`).
  Navigator runs hold it shared, so they run concurrently. Only opening a tab with no opener (the launcher itself,
  after a browser restart) holds it exclusively. Set `AGENT_BROWSER_SERIAL_NAVIGATOR=1` to make navigator runs
  take turns again if a fastbrowse update ever changes how it picks up tabs.
- **Captcha gates watch only their own session.** The gate (`gate/captcha_gate.py`) watches the attached tab and
  any tab it opens, tracked by `openerId`, and leaves other agents' windows alone. A session also follows only
  popups its own page opened.

Checked on Steel (2026-11-02): three navigator runs at once in three sessions (Wikipedia, Hacker News, and a form
filled and submitted with `allow_actions`), with a fourth process opening and closing 17 windows during the runs.
Every run stayed in its own window, and the whole set took 60s against about 98s one after another. Two concurrent `claude -p` sessions,
then a third: a cookie set in one was sent by the third.

## Tests

```sh
uv sync && .venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q                              # forms, sessions, MCP; no model; ~5 minutes
AGENT_BROWSER_NAV_TESTS=1 .venv/bin/python -m pytest -q    # + the real navigator (OpenRouter, a few cents)
```

The tests run against **the test site** (`src/browse_testsite`, `browse-testsite --port 8811` to look at it) in a
throwaway headless Chrome, never a real site. It is a set of small fake directory sites, one per pattern: hover
menus, server-only length limits, wizards, tag chips, rich text, comboboxes, hidden file inputs, formless SPAs,
prefilled junk, scripts that change typed text, cross-origin iframe forms, native validation, outcome pages and
planted instructions aimed at AI agents.

## Files

| Path | Contents |
|---|---|
| `src/browse/server.py` | MCP server, tool descriptions, CLI flags |
| `src/browse/tools.py` | the tools, independent of MCP |
| `src/browse/sessions.py` | sessions, the launcher and per-session windows, packets |
| `src/browse/navigator.py` | the navigator subprocess, its base context and `allow_actions` |
| `src/browse/forms.py`, `pagejs.py`, `plan.py` | survey / fill / check / press |
| `src/browse/locks.py` | the per-browser reader/writer lock |
| `gate/` | captcha gate, fastbrowse CLI wrapper, OpenRouter routing proxy |
