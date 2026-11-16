# Common setup steps (for the agent)

Each tool's guide links here for the steps they all share. Read this whole file before you start.

## 1. Ask the user first

Ask these together, in one message, before installing anything:

1. **Where the repo should live.** If they already have a clone, use it. Otherwise suggest `~/git/aitoolspub`.
   Every path below is relative to that directory; this guide calls it `$AITOOLS`.
2. **Which agents to add the tool to:** Claude Code, Codex, or others (Cursor, Windsurf, Claude Desktop, ...).
   Check which are installed (`command -v claude codex`) and suggest those.
3. The tool-specific questions in its own guide (keys, options).

Ask for API keys in that same message, and tell the user where each one will be stored. Never echo a key back
in output, commit it, or put it in an agent's config file.

## 2. Get the repo

```sh
git clone https://github.com/Diffio-AI/aitoolspub.git "$AITOOLS"   # skip if it's already there
cd "$AITOOLS" && git pull --ff-only                                # if it was already there
```

The tools are Python projects run with [uv](https://docs.astral.sh/uv/). If `uv` is missing, install it
(`curl -LsSf https://astral.sh/uv/install.sh | sh`) and use the full path (`~/.local/bin/uv`) if the shell
doesn't pick it up.

## 3. Keys

Every key is a one-line file in `$AITOOLS/keys/` (git-ignored). Create the directory with `mkdir -m 700 -p keys`
and write each key with mode 600, without printing it:

```sh
umask 077; printf '%s\n' "$KEY_VALUE" > "$AITOOLS/keys/openrouter.key"
```

When the user pastes a key into the chat, write it from there. Never put it in a command line that is logged or
shared. Most tools also accept the same key from an environment variable; files are preferred because every agent
then finds them.

## 4. Register the MCP server with each agent

Each guide gives the server as either a **stdio command** (an absolute path to a program) or an **HTTP URL plus
an Authorization header**. Use absolute paths: agents start servers from other working directories.

### Claude Code

```sh
# stdio
claude mcp add --scope user <name> -- "$AITOOLS/<path to command>"
# HTTP; headersHelper runs a script that prints the header, so the token stays out of Claude's config
claude mcp add-json --scope user <name> '{"type":"http","url":"<url>","headersHelper":"<script>"}'
```

If the user runs Claude Code with several config directories (`CLAUDE_CONFIG_DIR`, e.g. `~/.claude1`), repeat the
command with `CLAUDE_CONFIG_DIR` set for each one. Check with `claude mcp list`: the server should say `Connected`.

To stop Claude asking permission for every call, offer to add `"mcp__<name>__*"` to `permissions.allow` in
`~/.claude/settings.json`. Ask first: for email that means sending without confirmation.

### Codex

Add to `~/.codex/config.toml` (keep it mode 600; it may hold a token):

```toml
[mcp_servers.<name>]
command = "<absolute path to command>"   # stdio
startup_timeout_sec = 30
tool_timeout_sec = 900

[mcp_servers.<name>]
url = "<url>"                            # HTTP
http_headers = { Authorization = "Bearer <token>" }
tool_timeout_sec = 900
```

Check with `codex mcp list`.

### Other agents

Most take the same JSON as Claude Code (`{"command": ...}` or `{"type": "http", "url": ..., "headers": {...}}`)
in their own MCP settings file. Look up where that agent keeps it, add the server and check that it connects.

## 5. Finish

Restart the agents (MCP servers load when a session starts) and check that one of the tool's read-only tools works
from a new session. Then tell the user, in a few lines, what they can now ask for (each guide ends with a short
"Tell the user" section), where the keys are, and how to check the tool is running.
