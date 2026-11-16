# Set up the email tool (for the agent)

Steps for the agent doing the install. Read [common.md](common.md) first: it covers where the repo goes,
how keys are stored and how to register an MCP server with each agent.

**What gets installed:** `agent-mail` (`mail/`), an always-on local service (a systemd user unit) that mirrors
the chosen Google Workspace mailboxes, in full, into SQLite. Gmail push keeps it within seconds of the real
mailbox. It serves MCP over HTTP at `http://127.0.0.1:8766/mcp`, behind a bearer token per agent.

**Supported auth: only a Google Cloud service account with domain-wide delegation.** That needs a Google
Workspace domain whose admin console the user can reach. Personal `@gmail.com` accounts and OAuth sign-in are not
supported. If that's what the user has, say so and stop; they can ask an agent to add OAuth support to `mail/`
(`mail/src/agent_mail/gmail.py` builds the credentials).

## 1. Ask the user

Along with the questions in common.md, ask:

1. **Which mailboxes** to mirror (primary Workspace addresses; send-as aliases are found automatically), and
   whether agents may **send** from each one (otherwise read-only, `send = false`).
2. **How much history:** all mail (default) or only the last N days.
3. **Google Cloud project:** an existing one or a new one, and whether `gcloud` is installed and signed in as
   someone who can create service accounts and Pub/Sub topics in it. Without `gcloud`, walk the user through the
   same steps in the Cloud console.
4. **Gmail push** (recommended): new mail arrives in seconds. Without it a sync runs every 15 minutes (and
   `wait_for` syncs every 10 seconds while it waits). Warn: Gmail keeps one push watch per mailbox, so if another
   service already watches these mailboxes, use that service's Pub/Sub topic (step 3) so they don't replace
   each other's watch.

## 2. Service account and delegation

With `gcloud` (PROJECT is the user's project id):

```sh
gcloud services enable gmail.googleapis.com pubsub.googleapis.com --project "$PROJECT"
gcloud iam service-accounts create agent-mail --display-name "agent-mail" --project "$PROJECT"
SA="agent-mail@$PROJECT.iam.gserviceaccount.com"
mkdir -m 700 -p "$AITOOLS/keys"
(umask 077; gcloud iam service-accounts keys create "$AITOOLS/keys/gmail-service-account.json" --iam-account "$SA")
```

If key creation fails with `iam.disableServiceAccountKeyCreation`, the organisation blocks service account keys
(the default for newer Google Cloud organisations). An org policy admin has to allow keys for this project: IAM &
Admin → Organization Policies → "Disable service account key creation" → override for the project. Explain this
to the user; don't work around it.

Then **the user** grants domain-wide delegation in the Workspace admin console (it needs a super admin). Give them
the exact values:

- https://admin.google.com → Security → Access and data control → API controls → Manage Domain Wide Delegation →
  Add new
- **Client ID:** the `client_id` field of `keys/gmail-service-account.json` (a long number; print only that field)
- **OAuth scopes:** `https://www.googleapis.com/auth/gmail.modify`

It can take a few minutes to take effect. If the browser tool is installed and signed in to that admin account,
you can offer to do it there.

## 3. Gmail push (optional)

```sh
cd "$AITOOLS/mail"
SA="$SA" GCLOUD="$(command -v gcloud)" scripts/setup_gcp.sh "$PROJECT" [topic] [subscription]
```

It creates the topic (default `gmail-agent-mail`, or the existing one the user named) and a pull subscription
`agent-mail-pull`, and grants Gmail permission to publish and the service account permission to pull. It's
idempotent. Without push, leave `[pubsub] project` empty in the config.

## 4. Configure and install

```sh
cd "$AITOOLS/mail" && uv sync
cp -n config.example.toml config.toml        # git-ignored
```

Edit `config.toml`: `key_file = "../keys/gmail-service-account.json"` (relative to the config file), the
`[pubsub]` project/topic/subscription (or an empty project), `backfill_days`, and one `[[mailbox]]` per address
with its `display_name` (and `send = false` where the user wants read-only).

Run it as a systemd user service:

```sh
mkdir -p ~/.config/systemd/user
sed "s|__AITOOLS_DIR__|$AITOOLS|g" systemd/agent-mail.service > ~/.config/systemd/user/agent-mail.service
systemctl --user daemon-reload && systemctl --user enable --now agent-mail
loginctl enable-linger "$USER"     # optional: keep it running while the user is logged out (may need sudo)
journalctl --user -u agent-mail -f # first sync: about 10k messages in under 2 minutes
.venv/bin/agent-mail status        # each mailbox: counts, last sync, errors
```

`unauthorized_client` or `Client is unauthorized to retrieve access tokens` in the log means the delegation
(step 2) isn't in effect yet, or the client ID or scope is wrong.

## 5. Register with the agents

Issue one token per agent; each is shown once and only its hash is stored:

```sh
mkdir -m 700 -p ~/.config/aitools
.venv/bin/agent-mail token issue claude-code --out ~/.config/aitools/mail.token
.venv/bin/agent-mail token issue codex --out ~/.config/aitools/mail-codex.token
```

- **Claude Code**, under the name **`email`**:
  `claude mcp add-json --scope user email '{"type":"http","url":"http://127.0.0.1:8766/mcp","headersHelper":"'"$AITOOLS"'/mail/scripts/mcp-headers.sh"}'`.
  The helper reads `~/.config/aitools/mail.token` whenever Claude connects, so the token isn't in Claude's config.
- **Codex:** an `[mcp_servers.email]` HTTP entry whose `http_headers` holds `Authorization = "Bearer <token>"`,
  read from `mail-codex.token` (write it into `config.toml` without printing it).
- **Others:** an HTTP server with that header; issue each its own token.

`agent-mail token list` / `token revoke <name>` manage them.

## 6. Check it

From a new agent session: `list_mailboxes` (every mailbox, with counts and a recent sync), then `search` with no
query (the latest mail). To check sending and push together, if the user agrees: send a message from one
configured mailbox to another, then `wait_for` it.

## 7. Tell the user

- **Using it:** tell any agent to "use the email tool" to search, read, reply to or send mail, or to wait for a
  verification email during a signup. It sees every configured mailbox, spam and trash included.
- **It's real email:** sends go out through Gmail as the real mailbox. There's no approval step beyond the
  agent's own permission prompts, so don't add the email tools to auto-allow unless the user wants that.
- **Adding a mailbox:** add a `[[mailbox]]` to `mail/config.toml` (delegation covers the whole domain) and run
  `systemctl --user restart agent-mail`.
- **Running:** `systemctl --user status agent-mail`; logs: `journalctl --user -u agent-mail -f`.
