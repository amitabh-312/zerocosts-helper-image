#!/bin/sh
# ZeroCosts helper container entrypoint.
set -e

: "${RF_SITE:?RF_SITE is not set. Start the helper with the launcher from the course website (it passes your site address).}"
PORT="${RF_PORT:-8765}"
WS="${RF_WORKSPACE:-/workspace}"

# Writable places for whatever user id we were started with (the launcher passes your own uid on Linux).
[ -w "${HOME:-/tmp}" ] || export HOME=/tmp
export CARGO_HOME="$WS/.cargo"                 # crates.io downloads are cached in your folder, so they survive restarts
export XDG_CONFIG_HOME="$WS/.rustforge"        # the pairing code is remembered here
export PATH="$CARGO_HOME/bin:$PATH"
mkdir -p "$CARGO_HOME" "$XDG_CONFIG_HOME" 2>/dev/null || { echo "Cannot write to $WS. If you are on Linux, start with the launcher script (it passes --user)."; exit 1; }

echo "ZeroCosts helper (Docker) — $(rustc --version 2>/dev/null)"

# Is the debugger allowed to trace? (needs ptrace; some Docker setups block it)
if [ "${RF_SKIP_SELFTEST:-0}" != "1" ]; then
  /app/selftest.sh || true
fi

# The course's server projects (web server, chat, REST API, Redis clone) listen on 127.0.0.1 INSIDE the container.
# Forward each port from the container's network address to its loopback, so http://127.0.0.1:PORT works from your browser
# (the launcher publishes the same ports to your machine's loopback only).
IP="$(hostname -i 2>/dev/null | awk '{print $1}')"
if [ -n "$IP" ] && command -v socat >/dev/null 2>&1; then
  for P in ${RF_FORWARD_PORTS:-3000 6380 7878 8080}; do
    socat "TCP-LISTEN:$P,bind=$IP,fork,reuseaddr" "TCP:127.0.0.1:$P" >/dev/null 2>&1 &
  done
fi

exec python3 /app/serve.py --host 0.0.0.0 --port "$PORT" --public-port "$PORT" --workspace "$WS" --site "$RF_SITE" "$@"
