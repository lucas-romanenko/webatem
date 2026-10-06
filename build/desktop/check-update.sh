#!/usr/bin/env bash
# CI only (.github/workflows/desktop.yml): prove a one-click update end to
# end on this OS, with the build the job just made.
#
#   check-update.sh <installed app executable> <release asset> <data dir> <port>
#
# <release asset> is a file named as the release names it (the app asks the
# feed for that name).
#
# Serves a local copy of a release (tag v99.0.0, the asset under its real
# name, its real SHA-256) the way GitHub's API does, starts the installed
# app on <port>, runs `webatem --update --yes` against that feed, and then
# requires all of this:
#   * the copy that was running is stopped,
#   * the app on disk was replaced (Linux: a new inode for the binary; macOS:
#     a new bundle; Windows: the installer's log says it succeeded),
#   * the new one came back by itself (--resume) and serves the page.
# Linux and macOS stop the new one at the end; Windows leaves it running for
# the uninstall-while-running check that follows.
set -u

APP=$1 ASSET=$2 DATA=$3 PORT=$4
PY=${PY:-python3}
NAME=$(basename "$ASSET")
FEED_PORT=8790
feed="${RUNNER_TEMP:-/tmp}/update-feed"
OS=$(uname -s)
case "$OS" in MINGW*|MSYS*|CYGWIN*) OS=Windows ;; esac
# Windows: the step turns Git Bash's path conversion off (installer flags),
# so every path the app sees must already be in Windows form. <data dir>
# arrives that way (cygpath -m in the workflow); the feed folder is ours.
[ "$OS" = Windows ] && feed=$(cygpath -m "$feed")

fail() { echo "::error::$*"; echo "--- update log ---"; cat "$DATA/updates/update.log" 2>/dev/null; cat "$DATA/updates/install.log" 2>/dev/null | tail -30; echo "--- app log ---"; cat "$DATA/app.log" 2>/dev/null | tail -30; exit 1; }
serving() { curl -fsS -o "$DATA/page.html" "http://127.0.0.1:$PORT/atem/" 2>/dev/null; }
alive() {
  if [ "$OS" = Windows ]; then tasklist /FI "PID eq $1" 2>/dev/null | grep -qi webatem
  else kill -0 "$1" 2>/dev/null; fi
}
identity() {     # what changes when the app on disk is replaced
  case "$OS" in
    Linux)  stat -c %i "$APP" ;;
    Darwin) stat -f %i "$(cd "$(dirname "$APP")/../.." && pwd)" ;;
    *)      echo n/a ;;
  esac
}

mkdir -p "$DATA" "$feed"
cp "$ASSET" "$feed/$NAME"
"$PY" - "$feed" "$NAME" "$FEED_PORT" <<'EOF'
import hashlib, json, os, sys
feed, name, port = sys.argv[1:]
path = os.path.join(feed, name)
sha = hashlib.sha256(open(path, 'rb').read()).hexdigest()
json.dump({'tag_name': 'v99.0.0', 'html_url': 'https://example.invalid/release',
           'assets': [{'name': name, 'browser_download_url': f'http://127.0.0.1:{port}/{name}',
                       'digest': 'sha256:' + sha, 'size': os.path.getsize(path)}]},
          open(os.path.join(feed, 'latest.json'), 'w'))
EOF
"$PY" -m http.server "$FEED_PORT" --bind 127.0.0.1 --directory "$feed" >/dev/null 2>&1 &
FEED_PID=$!
trap 'kill $FEED_PID 2>/dev/null' EXIT
for i in $(seq 1 20); do curl -fsS -o "$DATA/feed.json" "http://127.0.0.1:$FEED_PORT/latest.json" 2>/dev/null && break; sleep 0.5; done
[ -s "$DATA/feed.json" ] || fail "the local release feed never answered on 127.0.0.1:$FEED_PORT"
# Python's urllib follows the system's proxy settings (on macOS, the network
# preferences), curl does not: the first macOS run timed out on this feed
# while curl reached it. Say what this machine sets, then keep loopback
# direct, as any proxy configuration should.
"$PY" -c "import urllib.request; print('proxies this machine sets:', urllib.request.getproxies() or 'none')"
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost

export WEBATEM_NO_TRAY=1 WEBATEM_NO_BROWSER=1 HOST=127.0.0.1 PORT DATA_DIR="$DATA" \
       WEBATEM_UPDATE_FEED="http://127.0.0.1:$FEED_PORT/latest.json"

"$APP" > "$DATA/app.log" 2>&1 &
for i in $(seq 1 60); do serving && break; sleep 2; done
serving || fail "the installed app never served /atem/ before the update"
OLD=$(cat "$DATA/webatem.pid" 2>/dev/null) || fail "the running app wrote no PID file"
BEFORE=$(identity)
echo "running as PID $OLD; updating to the served v99.0.0"

"$APP" --update --yes > "$DATA/cli.log" 2>&1
echo "--- webatem --update ---"; cat "$DATA/cli.log"

for i in $(seq 1 60); do alive "$OLD" || break; sleep 1; done
alive "$OLD" && fail "the copy that was running (PID $OLD) was not stopped"

ok=
for i in $(seq 1 90); do
  NEW=$(cat "$DATA/webatem.pid" 2>/dev/null || true)
  if [ -n "$NEW" ] && [ "$NEW" != "$OLD" ] && alive "$NEW" && serving; then ok=1; break; fi
  sleep 2
done
[ -n "$ok" ] || fail "the updated app did not come back and serve /atem/"

case "$OS" in
  Windows) grep -q "Installation process succeeded" "$DATA/updates/install.log" 2>/dev/null \
             || fail "the installer's log does not say the update succeeded" ;;
  *)       [ "$(identity)" != "$BEFORE" ] || fail "the app on disk was not replaced" ;;
esac
echo "updated in place: PID $OLD stopped, the app replaced, PID $NEW serving again"

if [ "$OS" != Windows ]; then
  kill "$NEW" 2>/dev/null
  for i in $(seq 1 20); do alive "$NEW" || break; sleep 0.5; done
fi
exit 0
