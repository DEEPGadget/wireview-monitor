#!/usr/bin/env bash
# Stop wvd (backend API + web dashboard) started by scripts/up.sh.
# Without a PID file it falls back to whatever wvd listens on WVD_PORT (8765).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/run/wvd.pid"
PORT="${WVD_PORT:-8765}"

pid=""
if [[ -f "$PID_FILE" ]]; then
    pid=$(cat "$PID_FILE")
    kill -0 "$pid" 2>/dev/null || pid=""
fi
if [[ -z "$pid" ]]; then
    pid=$(ss -ltnp 2>/dev/null | grep ":$PORT " | grep -o 'pid=[0-9]*' | head -n1 | cut -d= -f2 || true)
    if [[ -n "$pid" ]] && ! tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q 'wvd'; then
        echo "error: port $PORT belongs to pid $pid, which is not wvd. Not touching it." >&2
        exit 1
    fi
fi
if [[ -z "$pid" ]]; then
    rm -f "$PID_FILE"
    echo "wvd is not running."
    exit 0
fi

# SIGTERM lets wvd flush buffered samples to SQLite and release the port.
kill "$pid"
for _ in $(seq 1 50); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.2
done
if kill -0 "$pid" 2>/dev/null; then
    echo "wvd (pid $pid) did not stop in 10 s, killing it."
    kill -9 "$pid"
fi
rm -f "$PID_FILE"
echo "wvd stopped (pid $pid)."
