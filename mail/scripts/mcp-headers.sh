#!/bin/sh
# Claude Code MCP headersHelper: prints the Authorization header for agent-mail.
# Usage: "headersHelper": "<aitools>/mail/scripts/mcp-headers.sh [token-file]"
# Default token file: ${AGENT_MAIL_TOKEN_FILE:-~/.config/aitools/mail.token}
set -eu
file="${1:-${AGENT_MAIL_TOKEN_FILE:-$HOME/.config/aitools/mail.token}}"
token=$(tr -d '[:space:]' < "$file")
printf '{"Authorization":"Bearer %s"}\n' "$token"
