#!/bin/sh
# Wrapper around Steel's own entrypoint (/app/api/entrypoint.sh).
#  1. Clears stale Chrome profile locks (the container's hostname/PIDs change
#     between runs, and a leftover SingletonLock makes Chrome refuse the profile).
#  2. Starts Xvfb when CHROME_HEADLESS=false. The Steel image ships Xvfb but
#     never starts it, so headful Chrome fails to launch without this.
#  3. Loads the captcha-solver extension (CAPTCHA_EXTENSION, a directory under
#     /extensions). The pinned copy stays read-only; a copy in /run/steel-ext
#     gets the API key from CAPTCHA_KEY_FILE written into it, then the load
#     flags are appended to CHROME_ARGS.
#  4. On `docker stop`, closes Chrome over CDP (Browser.close) before killing
#     the API, so cookies and localStorage are flushed to the profile on disk.
set -u

PROFILE="${CHROME_USER_DATA_DIR:-/tmp/steel-chrome}"
mkdir -p "$PROFILE"
rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonSocket" "$PROFILE/SingletonCookie"

if [ "${CHROME_HEADLESS:-true}" = "false" ]; then
  rm -f /tmp/.X10-lock /tmp/.X11-unix/X10
  Xvfb :10 -screen 0 "${XVFB_SCREEN:-1920x1080x24}" -nolisten tcp >/dev/null 2>&1 &
  i=0
  while [ ! -e /tmp/.X11-unix/X10 ] && [ $i -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
fi

if [ -n "${CAPTCHA_EXTENSION:-}" ]; then
  SRC="/extensions/$CAPTCHA_EXTENSION"
  EXT="/run/steel-ext/$CAPTCHA_EXTENSION"
  if [ -f "$SRC/manifest.json" ]; then
    rm -rf /run/steel-ext && mkdir -p /run/steel-ext && cp -a "$SRC" "$EXT"
    KEY=""
    [ -r "${CAPTCHA_KEY_FILE:-}" ] && KEY=$(tr -d ' \r\n' < "$CAPTCHA_KEY_FILE")
    # NopeCHA's automation build reads its settings from a "nopecha" block in
    # manifest.json. mouse_visualization off: it draws a cursor into the page.
    # Other extensions would need their own key step here.
    KEY="$KEY" node -e '
      const fs = require("fs"), f = process.argv[1], m = JSON.parse(fs.readFileSync(f));
      if (m.nopecha) {
        m.nopecha.key = process.env.KEY;
        m.nopecha.mouse_visualization = false;
        fs.writeFileSync(f, JSON.stringify(m, null, 2));
      }
    ' "$EXT/manifest.json"
    chmod -R go-rwx /run/steel-ext
    [ -n "$KEY" ] && echo "[steel-init] $CAPTCHA_EXTENSION loaded with API key" \
                  || echo "[steel-init] $CAPTCHA_EXTENSION loaded WITHOUT API key (free tier)"
    # --silent-debugger-extension-api hides the "started debugging this browser"
    # bar that NopeCHA's debugger-based clicks would otherwise show.
    export CHROME_ARGS="${CHROME_ARGS:-} --load-extension=$EXT --disable-extensions-except=$EXT --silent-debugger-extension-api"
  else
    echo "[steel-init] CAPTCHA_EXTENSION=$CAPTCHA_EXTENSION not found in /extensions, skipping"
  fi
fi

/app/api/entrypoint.sh "$@" &
APP=$!

chrome_running() { pgrep -f -- '--remote-debugging-port=9222' >/dev/null 2>&1; }

shutdown() {
  echo "[steel-init] stopping: closing Chrome cleanly"
  # Freeze the API so it does not relaunch Chrome when it sees the disconnect.
  kill -STOP "$APP" 2>/dev/null
  node -e '
    fetch("http://127.0.0.1:9222/json/version").then(r => r.json()).then(v => {
      const ws = new WebSocket(v.webSocketDebuggerUrl);
      ws.onopen = () => ws.send(JSON.stringify({ id: 1, method: "Browser.close" }));
      ws.onclose = ws.onerror = () => process.exit(0);
      setTimeout(() => process.exit(0), 5000);
    }).catch(() => process.exit(0));
  '
  i=0
  while chrome_running && [ $i -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
  if chrome_running; then
    echo "[steel-init] Chrome did not exit in time, sending SIGTERM"
    pkill -TERM -f -- '--remote-debugging-port=9222'
    sleep 2
  fi
  kill -KILL "$APP" 2>/dev/null
  exit 0
}
trap shutdown TERM INT

wait "$APP"
