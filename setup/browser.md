# Set up the browser tool (for the agent)

Steps for the agent doing the install. Read [common.md](common.md) first: it covers where the repo goes,
how keys are stored and how to register an MCP server with each agent.

**What gets installed:** two self-hosted [Steel](https://github.com/steel-dev/steel-browser) Chromes in Docker
(`steel/`), one per browser profile, and a stdio MCP server (`browser/`) that every agent session starts for
itself. All sessions share each profile's Chrome, so a site signed into once is signed in for every agent. Each
session works in its own window.

| Profile | Port | For |
|---|---|---|
| `testing` (default) | 3103 | everyday agent work and test accounts |
| `production` | 3104 | the user's own accounts, used only when a task needs them |

Ports are bound to 127.0.0.1 only. The profiles' data lives in `steel/profiles/<name>/`.

## 1. Ask the user

Along with the questions in common.md, ask:

1. **The navigator?** (recommended) An open model that browses on the agent's behalf: finds pages, follows menus,
   quotes what it read, and can carry out a task when allowed. Needs an **OpenRouter API key**
   (https://openrouter.ai/keys) and costs about $0.005-0.03 per run. Without it the agent still has every direct
   tool (go to a URL, read, screenshot, fill forms, run JavaScript).
2. **Captcha solving?** Without it nothing else is needed, and when a captcha shows up the agent hands the user a
   live link to solve it. With it:
   - a **NopeCHA key** (https://nopecha.com/pricing, about $5/month for 2,000 solves a day). The NopeCHA extension
     solves reCAPTCHA, hCaptcha, Turnstile and Cloudflare's "Just a moment..." page inside the browser.
   - optionally a **2captcha key** (https://2captcha.com, pay per solve, about $1-3 per 1,000), for GeeTest,
     MTCaptcha and image captchas, and as a backup for reCAPTCHA and Turnstile.
3. **Timezone** for Chrome: suggest the machine's own (`timedatectl show -p Timezone --value`, or
   `readlink /etc/localtime`). It should match where the IP is.

Don't ask about the GPU. If `nvidia-smi` works and Docker has the NVIDIA runtime
(`docker info | grep -i nvidia`), offer it as an option (real WebGL looks more like a desktop). Otherwise skip it.

## 2. Requirements

Docker with Compose v2 (`docker compose version`), and the user able to run `docker` without sudo. If Docker is
missing, tell the user what's needed and stop; installing Docker needs their go-ahead and usually sudo.

## 3. Start the browsers

```sh
cd "$AITOOLS/steel"
cp -n .env.example .env          # then set TZ_NAME (and the lines below) in .env
mkdir -m 700 -p secrets
```

With captcha solving:

```sh
./get-nopecha.sh                                   # downloads the pinned extension and checks its sha256
umask 077; printf '%s\n' "$NOPECHA_KEY" > secrets/nopecha_api_key
# in .env: CAPTCHA_EXTENSION=nopecha-0.6.1
```

With the GPU: uncomment `COMPOSE_FILE=docker-compose.yml:gpu.yml` in `.env` (and `GPU_DEVICE` if it isn't GPU 0).

Then:

```sh
docker compose up -d
docker compose ps                # wait until both say (healthy); the first pull takes a few minutes
docker compose logs steel-testing | grep steel-init   # with captchas: "...loaded with API key"
```

The containers restart on their own after a reboot (`restart: unless-stopped`).

## 4. Install the MCP server

```sh
cd "$AITOOLS/browser" && uv sync
```

Keys go in `$AITOOLS/keys/` (see common.md): `openrouter.key` for the navigator and `2captcha.key` for 2captcha.

The stdio command is `$AITOOLS/browser/.venv/bin/agent-browser-mcp`, plus these flags:

- `--no-captchas` if the user chose no captcha solving,
- `--no-navigator` if there is no OpenRouter key.

Register it under the name **`browser`** with each agent the user chose (common.md, step 4). In Codex, keep
`tool_timeout_sec = 900`: navigator runs and waits take minutes.

## 5. Optional: the tab janitor

Agents that crash leave windows open. A systemd user timer closes abandoned and idle tabs every 5 minutes, and
restarts a profile's Chrome when it's idle and using too much memory. Recommended:

```sh
mkdir -p ~/.config/systemd/user
sed "s|__AITOOLS_DIR__|$AITOOLS|g" "$AITOOLS/browser/systemd/agent-browser-janitor.service" \
  > ~/.config/systemd/user/agent-browser-janitor.service
cp "$AITOOLS/browser/systemd/agent-browser-janitor.timer" ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now agent-browser-janitor.timer
```

## 6. Check it

From a new agent session: `open("https://example.com")`, then `read` (it should quote "Example Domain"), then
`close`. With the navigator: `open("https://en.wikipedia.org", goal="open the article on the Python programming
language and quote its first sentence")`.

## 7. Tell the user

Say it in about this many words:

- **Using it:** tell any agent to "use the browser tool". It opens its own window in the shared Chrome. The
  `production` profile is for your own accounts; say "use the production profile" when a task needs them.
- **Signing in:** a site signed into once stays signed in for every agent in that profile. Ways to do it:
  - ask the agent for a live link to its session (its `handoff` tool) and sign in there yourself;
  - open the live view of every window: http://localhost:3103/v1/sessions/debug?interactive=true (3104 for
    `production`). From another machine, tunnel first: `ssh -L 3103:localhost:3103 -L 3104:localhost:3104 <host>`;
  - copy a sign-in from your own Chrome on this machine (Linux with GNOME keyring):
    `$AITOOLS/browser/.venv/bin/agent-browser-import-cookies --profile "<your Chrome profile>" --domain <site>`
    (`--to production` for the other profile).
- **New profiles:** ask the agent to add one, for example a profile per client. It's a service in
  `steel/docker-compose.yml` plus an entry in `steel/profiles.json`.
- **Captchas:** solved on their own if enabled; otherwise the agent sends you a live link.
- **Running:** `docker compose -f $AITOOLS/steel/docker-compose.yml ps`.
