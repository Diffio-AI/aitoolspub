# agent-mail

An always-on local service that mirrors every configured Google Workspace mailbox, in full, into one SQLite
database, and gives any Claude Code session email tools over MCP: list mailboxes, search, regex grep, read, wait
for new mail, send, reply, forward, label/archive/trash, attachments.

```
Gmail ──watch──▶ Pub/Sub topic ──pull──▶ agent-mail (agent-mail-pull) ──▶ SQLite (WAL, FTS5) + attachments/
  ▲
  └──────── messages.send ◀──────── agent-mail ◀── MCP over HTTP, 127.0.0.1:8766/mcp ◀── agents
```

- **Sync.** The first sync mirrors all mail (`backfill_days = 0`), spam and trash included, in batches of 500, so
  it is searchable while it runs; about 10k messages took under two minutes. After that, sync is incremental
  through `history.list`: new messages, label changes and deletions. Each Pub/Sub notification triggers a sync, a
  catch-up sync runs every 15 minutes, and an expired historyId triggers a full resync.
- **Push.** A pull subscription, so no public endpoint is needed. Gmail keeps one watch per mailbox: if another
  service already watches these mailboxes, share its topic (both renew the same watch) with a subscription of
  agent-mail's own. Delivery to the mirror takes a few seconds.
- **Storage** is `state/` (mode 0700): `mail.db` (messages with all headers, FTS index, labels, aliases, send log),
  `attachments/` (attachments up to 10 MB; larger ones are fetched on demand), `tokens.json` (token hashes).
- **Logs:** `journalctl --user -u agent-mail -f`.

## Setup

See [`../setup/email.md`](../setup/email.md). In short: a Google Cloud service account with domain-wide delegation
(`gmail.modify` scope) over a Workspace domain, its key referenced by `service.key_file` (never copied or logged),
optionally `scripts/setup_gcp.sh` for push, `config.toml` from `config.example.toml`, the systemd user unit in
`systemd/`, and a token per agent (`agent-mail token issue <name> --out FILE`). Claude Code connects with
`scripts/mcp-headers.sh` as its `headersHelper`, which reads the token file when it connects, so the token never
sits in Claude's config.

## Mailboxes

`config.toml` lists the primary mailboxes; send-as aliases are discovered from Gmail. To add one, add a
`[[mailbox]]` (delegation covers the whole domain) and run `systemctl --user restart agent-mail`.
`send = false` makes a mailbox read-only.

## MCP tools

| Tool | What it does |
|---|---|
| `list_mailboxes` | Every mailbox with its addresses (aliases too), whether it can send, message and unread-in-inbox counts, newest message, last sync, errors. |
| `search` | Newest first. `query` is FTS5 over subject/body/from/to (words, `"phrases"`, `prefix*`, AND/OR/NOT, `subject:x`); no query = latest mail. Filters: `mailbox` (one or a list), `sender`, `recipient`, `subject`, `after`, `before`, `label`, `unread`, `direction`, `has_attachment`, `thread_id`, `include_spam_trash`, `limit`, `offset`. |
| `grep` | A Python regex over `subject`, `body`, `from`, `to` and optionally `headers`, with each match in context. Same filters. |
| `read_message` | Headers (all of them with `headers=true`), body, every link (the HTML version's buttons too), labels, attachments. |
| `read_thread` | Every message in a thread, oldest first, quoted history stripped unless `full_quotes`. |
| `wait_for` | Blocks (up to 15 min) until mail matching the filters arrives, or already arrived since `since` (default 10 minutes ago). It syncs every 10s while waiting and returns the newest match in full. Made for verification codes, magic links and replies. |
| `send` | `from_addr`, `to`, `subject`, `body`, optional `cc`, `bcc`, `html`, `attachments`, `reply_to`. |
| `reply` | To a `message_id` (or the latest in `mailbox` + `thread_id`); sets threadId, In-Reply-To and References. `reply_all`, `cc`, `bcc`, `html`, `attachments`. |
| `forward` | A message's text and attachments, with an optional note. |
| `modify` | `mark_read`, `star`, `archive`, `add_labels` (created if new), `remove_labels`, `trash`, `untrash`, on `message_ids` or a whole `thread_id`. |
| `get_attachment` | Text as text, images as images, others as base64 (up to 1.5 MB), or `save_to` a file. |
| `sync_now` | Sync immediately instead of waiting for push. |

Dates take `YYYY-MM-DD`, ISO times or relative values (`30m`, `2h`, `3d`, `1w`). Every send is logged in
`send_log` with the client token's name. There is no policy layer: any session with the token can send as any
configured mailbox, so sends are real mail to real people.

## CLI

`.venv/bin/agent-mail [--config config.toml] <command>`: `serve`, `status`, `sync [mailbox...] [--full]`,
`token issue|revoke|list [name] [--out FILE]`, `search [query] [--mailbox M]`.

## Tests

```bash
~/.local/bin/uv run pytest -q
```

A fake Gmail (`tests/fakes.py`) mimics Gmail's threading and Message-ID rewriting. The tests cover sync, label
changes and deletions, FTS search and its fallback, grep, relative dates, send/reply/forward (aliases, bcc),
attachments both ways, HTML-only links, `wait_for`, label changes and trash, and bearer auth plus every tool over
HTTP.

Checked live: a `claude -p` session sent mail from one configured mailbox to another. It arrived through push in
about 2 seconds, `wait_for` returned it, and the session confirmed the code and trashed both copies.
