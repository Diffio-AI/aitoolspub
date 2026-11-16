# Steel browser (self-hosted, shared)

Local [Steel](https://github.com/steel-dev/steel-browser) Chromes that every agent session on this host
drives through the browser MCP (`../browser`). Steel runs one Chrome with one persistent profile, so each browser
profile is its own instance: a site signed into once in a profile is signed in for every agent session that uses
it. Each agent session works in its own window of its profile's Chrome.

| Profile | Instance | API / UI / viewer / CDP | DevTools | Data | For |
|---|---|---|---|---|---|
| `testing` (default) | `steel-testing` | `http://localhost:3103` | `http://localhost:9303` | `./profiles/testing/` | everyday agent work, test accounts |
| `production` | `steel-production` | `http://localhost:3104` | `http://localhost:9304` | `./profiles/production/` | your own primary accounts |

`profiles.json` tells the browser MCP about them (name, CDP URL, what it's for, which is the default); agents choose
with `open(url, profile="production")`. To add a profile: copy a service in `docker-compose.yml` with the next ports
and its own `./profiles/<name>`, add it to `profiles.json` (and to `gpu.yml` if you use it), `docker compose up -d`.

All ports are bound to 127.0.0.1 only.

## Files

- `docker-compose.yml` defines the `steel-testing` and `steel-production` services (compose project `agent-steel`).
- `profiles.json` lists the profiles for the browser MCP and `agent-browser-import-cookies --to`.
- `steel-base.yml` holds the image and Chrome settings: the pinned image, headful mode, fingerprint flags, captcha
  solver, timezone and healthcheck.
- `.env` (from `.env.example`, git-ignored) sets the timezone (`TZ_NAME`), the captcha extension
  (`CAPTCHA_EXTENSION`) and, optionally, `COMPOSE_FILE=docker-compose.yml:gpu.yml` for a real GPU.
- `gpu.yml` gives each instance an NVIDIA GPU for WebGL (see "GPU").
- `steel-init.sh` wraps Steel's entrypoint. It clears stale profile locks, starts Xvfb, loads the captcha-solver
  extension with its API key, and closes Chrome cleanly on stop.
- `chrome-policy.json` is a Chrome managed policy that re-enables the PDF viewer, so `navigator.plugins` is not empty.
- `extensions/nopecha-0.6.1/` is the pinned, unmodified NopeCHA automation build, mounted read-only. It is closed
  source, so it isn't in this repo: `get-nopecha.sh` downloads it and checks its sha256.
- `secrets/` (mode 700, git-ignored) holds `nopecha_api_key`, mounted read-only at `/run/steel-secrets`.
- `profiles/<name>/` is each profile's Chrome user-data-dir, where its logins live (root-owned, git-ignored).

## Start, stop, logs

```sh
cd steel
docker compose up -d            # start (restart: unless-stopped, so it comes back after a reboot)
docker compose restart          # restart; open sessions end, logins stay
docker compose stop             # stop; Chrome is closed cleanly first so cookies are flushed
docker compose logs -f
docker compose ps               # shows health
```

## How agents use it

`../browser`'s MCP server connects to `ws://localhost:3103/` (and 3104 for production). That is Steel's API port, which proxies the websocket
to whichever Chrome is currently running, so the URL stays the same across restarts. (`http://localhost:3103`
returns 404 on `/json/version`, and `http://localhost:9303` makes nginx drop the port from `webSocketDebuggerUrl`.)

The MCP attaches to Chrome's default context, which is the persistent profile. Every agent session opens its own
window from a launcher tab titled "agent-browser launcher (keep open)" (see `../browser/README.md` for why). Leave
that tab alone; it is recreated if closed.

Don't create Steel sessions through its API (`POST /v1/sessions`): Steel keeps only one session, and a new one
silently closes every open page. If you ever do, never pass `persist` or `userDataDir` (a Steel 0.5.3 bug then puts
the profile at `/app/api/user-data-dir`, which is not on the volume).

## Profiles and logins

Chrome's user-data-dir is `/data/profile`, bind-mounted from `./profiles/<name>/`. Cookies, localStorage, IndexedDB
and saved logins survive restarts, `down`/`up` and reboots. On `docker stop`, `steel-init.sh` closes Chrome over
CDP so it flushes cookies to disk; a hard kill or power loss can lose roughly the last 30 seconds of cookie changes.

**Signing in for the agents.** Open the live viewer, sign in by hand, close the viewer tab. Every agent session is
then signed in:

- **All windows:** `http://localhost:3103/v1/sessions/debug?interactive=true` (3104: production), a live view with tabs and a URL bar
  that takes clicks and typing.
- **One agent's window:** the browser MCP's `handoff(session)` tool returns
  `http://localhost:3103/v1/sessions/debug?interactive=true&pageId=<target>` for exactly that window, so an agent
  can ask you to finish a login, a 2FA prompt or a check it can't pass.
- **Steel UI:** `http://localhost:3103/ui` (session details, logs, DevTools).
- **From another machine**, tunnel the ports first: `ssh -L 3103:localhost:3103 -L 3104:localhost:3104 <this-host>`.

The profile files are owned by root (the container runs as root). To back up or reset without sudo:

```sh
docker compose stop
docker run --rm -v "$PWD/profiles:/p" alpine tar -C /p -czf - testing > testing-profile.tgz   # backup
docker run --rm -v "$PWD/profiles:/p" alpine rm -rf /p/testing/*   # reset to a fresh profile
docker compose up -d
```

## Live viewer: paste and copy

Steel's live viewer (`/v1/sessions/debug?interactive=true`) only handles ctrl+v / ctrl+c when it is embedded in
Steel's web app, which supplies the clipboard; opened directly, as handoff links are, paste did nothing. And its paste
sent each character as a key press with the character's code as the key code, so `.` arrived as Delete, `!` as
PageUp, `#` as End, `%` as Left, `-` as Insert, and so on: passwords and emails came out wrong.

`viewer/live-session-streamer.ejs` is the image's template (`viewer/live-session-streamer.orig.ejs`, Steel API
0.5.3) with both fixed: opened directly, ctrl+v reads your clipboard (`navigator.clipboard`, so the viewer must be
on `http://localhost:...`, e.g. through an SSH tunnel; the browser asks once to allow clipboard access) and types it
as text, and ctrl+c copies the page's selection. `steel-base.yml` mounts it over the image's copy. Steel caches
templates, so a change takes effect at the next restart. Re-check the patch when the image is updated: diff the new
image's template against the `.orig` copy.

Without the viewer, `../browser/.venv/bin/agent-browser-type [--to production] <window>` types text you paste into
a hidden terminal prompt into the focused field of the window whose URL or title contains `<window>`.

## Browser setup and stealth

- Chrome is Chromium 156, running **headful on Xvfb** (`CHROME_HEADLESS=false`). Steel's default is `--headless=new`, and the image ships Xvfb but never starts it, so `steel-init.sh` starts it.
- **Steel's fingerprint injection is off** (`SKIP_FINGERPRINT_INJECTION=true`). It produces a random fingerprint on every Chrome launch, which is bad for persistent logins. It is also applied late to pages that Playwright creates, and its values contradict each other (an aarch64 user agent on an x86_64 platform, an 800x600 screen inside a 1920x1080 window). The browser now presents its real and stable Linux Chromium fingerprint.
- **WebGL** runs on SwiftShader by default, or on a real GPU with `gpu.yml` (see the next section).
- Timezone is `TZ_NAME` in `.env` (default UTC). Keep it matching the IP's location.

## GPU (WebGL)

Without a GPU, Chrome renders WebGL with SwiftShader (`--enable-unsafe-swiftshader`). That works everywhere, but
SwiftShader and llvmpipe are among the strongest headless/VM/datacenter signals anti-bot scripts read.

With an NVIDIA card and the nvidia-container-toolkit, set `COMPOSE_FILE=docker-compose.yml:gpu.yml` (and
`GPU_DEVICE` if not GPU 0) in `.env` and run `docker compose up -d`. Chrome then uses about 100-300 MiB of VRAM.

- Xvfb has no GPU GLX, so Chrome's usual ANGLE-on-OpenGL path can't reach the card. `--use-angle=vulkan` makes
  ANGLE render through the NVIDIA Vulkan driver instead. `NVIDIA_DRIVER_CAPABILITIES=graphics,utility` is what
  makes the toolkit mount the Vulkan ICD and EGL libraries. WebGL then reports `ANGLE (NVIDIA, Vulkan ...)` with
  the real card, and chrome://gpu shows everything hardware accelerated.
- `--use-gl=angle --use-angle=gl-egl` gives a more typical Linux string, but EGL finds no usable surface config on
  Xvfb, so compositing falls back to software: worse than either clean option. Plain `--use-angle=gl` lands on
  Mesa llvmpipe, which Chrome blocks for WebGL.
- Don't spoof a different GPU string: WebGL parameters and canvas hashes would then contradict it. Consistency
  matters more than rarity.
- **If the NVIDIA runtime breaks** (for example after a driver update), the containers won't start: take
  `gpu.yml` out of `COMPOSE_FILE`.

## Captcha solver (NopeCHA)

Captchas are solved inside the browser by the NopeCHA extension, so the bot only sees the page continue after a few seconds. NopeCHA auto-opens and solves reCAPTCHA v2 and hCaptcha, clicks Turnstile widgets, and clicks the checkbox on Cloudflare's full-page "Just a moment..." challenge. It uses its `debugger` permission to send real mouse input.

### What you need to do

1. Get an API key at https://nopecha.com/pricing. Starter is about $4.99/month for 2,000 credits a day; one credit is roughly one captcha.
2. Fetch the extension, write the key and turn it on:
   ```sh
   cd steel
   ./get-nopecha.sh
   mkdir -m 700 -p secrets
   printf '%s\n' 'YOUR_NOPECHA_KEY' > secrets/nopecha_api_key && chmod 600 secrets/nopecha_api_key
   # .env: CAPTCHA_EXTENSION=nopecha-0.6.1
   docker compose up -d --force-recreate   # every instance shares the key
   docker compose logs steel-testing | grep steel-init   # "...loaded with API key"
   ```
3. Check it: `curl -s "https://api.nopecha.com/status?key=$(cat secrets/nopecha_api_key)"` should show your plan and credit.

With no key file, or an empty one, the extension runs on NopeCHA's keyless free tier: 100 credits a day per IP, no account. When tested, that tier accepted jobs but never returned answers, so in practice a key is required.

### How it's wired

- `steel-base.yml` takes `CAPTCHA_EXTENSION` (a directory under `extensions/`, e.g. `nopecha-0.6.1`) from `.env` and `CAPTCHA_KEY_FILE: /run/steel-secrets/nopecha_api_key`. New instances get both through `extends`.
- At each start, `steel-init.sh` does the following:
  - Copies the pinned extension to `/run/steel-ext/` inside the container (root-only). The copy is never written back to the host.
  - Writes the key into the copy's `manifest.json` under `nopecha.key`, which is where NopeCHA's automation build reads its settings. It also turns off `mouse_visualization`, which would draw a cursor into the page.
  - Appends `--load-extension`, `--disable-extensions-except` and `--silent-debugger-extension-api` to `CHROME_ARGS`. Both load flags are needed because Puppeteer always adds `--disable-extensions`. The last flag hides the "started debugging this browser" bar. Steel's own `extensions` session option only looks in `/app/api/extensions`, so it isn't used.
- The manifest is re-read on every start. Changing the key file and running `restart` takes effect; this was tested by switching between two dummy keys in the persistent profile.
- The extension ID is fixed by the manifest's public key: `dknlfmjaanfblgfdfebhijalfmhmjjjo`.
- To disable the solver, set `CAPTCHA_EXTENSION=` in `.env` and run `docker compose up -d --force-recreate`.

### Where the key is and isn't

- Not in the image, the repo, or any agent config. On the host it is only in `secrets/`; in the container it is in `/run/steel-ext/`. The extension also stores it in its own storage inside the profile.
- Web pages can't read it: NopeCHA exposes no web-accessible resources, and a page's `fetch()` of its `manifest.json` fails.
- **Anything driving the browser can read it**, because navigating to `chrome-extension://dknlfmjaanfblgfdfebhijalfmhmjjjo/manifest.json` shows it. That includes Claude through Playwright MCP, for example if a malicious page talked it into doing so. Chrome's `URLBlocklist` policy does not apply to `chrome-extension://` URLs (tested). Every in-browser solver has this exposure, since the key has to live in the browser. The impact is capped because a NopeCHA plan is a fixed daily quota with no overage billing. Rotate the key if you suspect a leak.

### Pinning and updating

`./get-nopecha.sh <version> <sha256>` fetches another version. The pinned build is NopeCHA's **automation** zip, 0.6.1 (GitHub release 2026-06-11): `chromium_automation.zip`, sha256 `92ebe154bd34433b4a36e6b2df33006fa6acb19a1b81031a0acb5b72be5e025a`. The plain `chromium.zip` has no settings block in its manifest, so it can't be configured unattended. Chrome does not auto-update unpacked extensions. To update:

```sh
cd steel
gh release list -R NopeCHALLC/nopecha-extension          # pick a non-pre-release tag
V=0.6.2   # example
mkdir -p extensions/nopecha-$V
curl -fL -o /tmp/nopecha-$V.zip https://github.com/NopeCHALLC/nopecha-extension/releases/download/$V/chromium_automation.zip
sha256sum /tmp/nopecha-$V.zip && unzip -q /tmp/nopecha-$V.zip -d extensions/nopecha-$V
grep -c '"nopecha"' extensions/nopecha-$V/manifest.json  # must be 1 (the settings block)
# set CAPTCHA_EXTENSION=nopecha-$V in .env, then:
docker compose up -d --force-recreate
```

Keep the old directory until the new one is checked. Roll back by changing `CAPTCHA_EXTENSION` back.

### Telling the bot

For fastbrowse, don't: the browser MCP's navigator runs through `../browser/gate/run.py`, which holds the agent in code until NopeCHA (or 2captcha, for what NopeCHA can't solve) has passed the captcha. A prompt line about captchas made fastbrowse's action picker read pages as bot walls: 7 of 11 demo runs ended `blocked` with it. See `../browser/gate/README.md`.

For Claude driving Playwright MCP directly, a prompt line still helps: "If a captcha or a Cloudflare 'Just a moment...' page appears, don't interact with it; wait up to 60 seconds and re-check the page, because the browser solves it automatically." Otherwise Claude tends to give up, or try to solve it itself, as soon as it sees the challenge.

### Test results (no key)

- The extension loads in the persistent profile, and its service worker runs.
- Logins persist: a marker cookie survived every recreate and restart.
- Playwright MCP works normally with it loaded.
- bot.sannysoft.com is still all green, and no DOM elements or globals are injected into ordinary pages.
- **Google reCAPTCHA demo:** NopeCHA clicked the checkbox by itself and sent the image grid to its API, with the configured key in the request. The keyless backend never answered, so the grid stayed. Claude reported "an image captcha came up and blocked the Submit button."
- **nopecha.com/demo/cloudflare** (full-page challenge): without the extension the browser stalls on "Verify you are human". With the extension it queued a Turnstile job, which never came back on the free tier.
- **demo.turnstile.workers.dev** uses Cloudflare's test sitekey (dummy token), so it proves nothing.
- Re-run the reCAPTCHA and Cloudflare demos once a key is in.

### Solver comparison (October 2026)

| | NopeCHA (chosen) | CapSolver | 2Captcha | CapMonster Cloud |
|---|---|---|---|---|
| reCAPTCHA v2 / v3 | v2 auto; v3 unclear | v2 + v3 auto | v2 + v3, auto-solve off by default | v2 + v3 auto |
| hCaptcha | yes, auto | no (not supported) | no (removed in 3.7.x) | listed, unpriced |
| Turnstile widget | yes (click) | yes (token) | yes, off by default | yes |
| CF full-page challenge | **yes**: clicks the checkbox, earns `cf_clearance` on our IP and UA | no: its script exits on challenge pages; API `AntiCloudflareTask` needs a proxy | attempts token injection, unverified | token mode; `cf_clearance` mode needs a proxy and a Windows UA |
| Price | plans: ~$4.99/mo for 2,000/day | $0.8 v2, $1.0 v3, $1.2 Turnstile/CF per 1k | $1-2.99 v2, $1.45 Turnstile per 1k | $0.6 v2, $0.9 v3, $1.3 Turnstile per 1k |
| Free tier | 100/day per IP, no account (not from VPNs; broken on test day) | none | none | small test balance, needs account |
| Unattended key | `manifest.json` → `nopecha.key` (automation build) | `assets/config.js` → `apiKey`, ignored once a key is stored in the profile | `common/config.js` → `apiKey` | `defaultSettings.json` → `clientKey` |
| Key file readable by web pages | no | **yes**: all files web-accessible | no | **yes** |
| Version / MV3 | 0.6.1 (2026-06), MV3 | 1.17.1 (2026-09), MV3 | 3.7.2 on GitHub (archived), 3.7.4 on the Web Store only; MV3 | 1.15.0 (Web Store only), MV3 |
| Permissions / privacy | `<all_urls>`, debugger, scripting, DNR; closed and obfuscated code; Web Store lists "website content" collected | `*://*/*`, webRequest; Google Analytics events | `<all_urls>` | `<all_urls>`, cookies, webRequest; Amplitude events |

Anti-Captcha is not recommended: it strips CSP headers on every site, posts visited URLs to a stats server, and lets any page set its key. Buster is free but audio-only, and needs a click.

NopeCHA was chosen because Cloudflare challenge pages are the main blocker on directory sites, and it is the only extension that handles them without a proxy. It also covers hCaptcha, and its key is not exposed to web pages. The cost is closed-source code with the `debugger` permission. CapSolver is the fallback if NopeCHA proves unreliable. It handles reCAPTCHA and Turnstile widgets but not Cloudflare challenge pages, and its `web_accessible_resources: ["*"]` would need narrowing so pages can't read `config.js`.

Sources:
- NopeCHA:
  - https://github.com/NopeCHALLC/nopecha-extension/releases (builds, changelog)
  - https://developers.nopecha.com/ (free tier, VPN exclusion)
  - https://developers.nopecha.com/guides/extension_advanced/ (manifest settings)
  - https://nopecha.com/pricing
  - https://chromewebstore.google.com/detail/dknlfmjaanfblgfdfebhijalfmhmjjjo
- CapSolver:
  - https://www.capsolver.com/ (pricing)
  - https://docs.capsolver.com/en/guide/api-support/
  - https://docs.capsolver.com/en/guide/extension/settings_for_developers/
  - https://docs.capsolver.com/en/guide/captcha/cloudflare_challenge/
  - https://github.com/capsolver/capsolver-browser-extension/releases
- 2Captcha:
  - https://2captcha.com/pricing
  - https://github.com/rucaptcha/2captcha-solver (archived)
  - https://chromewebstore.google.com/detail/ifibfemgeogfhoebkmokieepdoobkbpo
- CapMonster Cloud: https://capmonster.cloud/en/prices
- Anti-Captcha: https://anti-captcha.com/apidoc/articles/how-to-integrate-the-plugin
- Buster: https://github.com/dessant/buster

Manifests, config files and web-accessible lists were read from the downloaded release zips.

## Updating Steel

Steel only publishes `latest`, so the image is pinned by digest in `steel-base.yml`. To update:

```sh
docker pull ghcr.io/steel-dev/steel-browser:latest
docker image inspect ghcr.io/steel-dev/steel-browser:latest --format '{{index .RepoDigests 0}}'
```

Put the new digest in `steel-base.yml`, run `docker compose up -d`, and check the bot still works.
