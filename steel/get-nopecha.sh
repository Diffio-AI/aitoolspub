#!/usr/bin/env bash
# Fetch the pinned NopeCHA automation build (closed source, so not kept in this repo) into extensions/, checking
# its sha256. Then set CAPTCHA_EXTENSION=nopecha-<version> in .env and run `docker compose up -d --force-recreate`.
#
# Usage: ./get-nopecha.sh [version sha256]     (default: the pinned 0.6.1)
set -euo pipefail
cd "$(dirname "$0")"
V="${1:-0.6.1}"
SHA="${2:-92ebe154bd34433b4a36e6b2df33006fa6acb19a1b81031a0acb5b72be5e025a}"
DEST="extensions/nopecha-$V"
if [ -f "$DEST/manifest.json" ]; then echo "$DEST is already there"; exit 0; fi
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
curl -fsSL -o "$TMP/n.zip" "https://github.com/NopeCHALLC/nopecha-extension/releases/download/$V/chromium_automation.zip"
echo "$SHA  $TMP/n.zip" | sha256sum -c -
mkdir -p "$DEST" && unzip -q "$TMP/n.zip" -d "$DEST"
grep -q '"nopecha"' "$DEST/manifest.json" || { echo "no settings block in manifest.json: wrong build?" >&2; exit 1; }
echo "ok: $DEST (set CAPTCHA_EXTENSION=nopecha-$V in .env)"
