#!/usr/bin/env bash
# Start wvd (backend API + web dashboard) in the background.
#
#   scripts/up.sh                      real device, 0.0.0.0:8765
#   scripts/up.sh --simulate load      no hardware
#   WVD_TOKEN=secret scripts/up.sh     require a token
#
# Any arguments are passed to wvd (see `wvd --help`). WVD_PORT/WVD_HOST and the
# other WVD_* variables work too. PID and log go to run/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT/run"
PID_FILE="$RUN_DIR/wvd.pid"
LOG_FILE="$RUN_DIR/wvd.log"
VENV="$ROOT/.venv"
PORT="${WVD_PORT:-8765}"
# --port on the command line wins over WVD_PORT for the health check below.
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
    case "${args[i]}" in
        --port) PORT="${args[i+1]:-$PORT}" ;;
        --port=*) PORT="${args[i]#--port=}" ;;
    esac
done
HEALTH_URL="http://127.0.0.1:$PORT/api/v1/health"

mkdir -p "$RUN_DIR"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "wvd is already running (pid $(cat "$PID_FILE")). Stop it with scripts/down.sh."
    exit 0
fi
rm -f "$PID_FILE"

if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    echo "error: port $PORT is already in use (another wvd not started by this script?)." >&2
    echo "       scripts/down.sh stops a wvd on that port; otherwise pick --port." >&2
    exit 1
fi

if [[ ! -x "$VENV/bin/wvd" ]]; then
    echo "setting up $VENV …"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q -e "$ROOT[server]"
fi

echo "===== up.sh $(date "+%F %T") wvd $*" >>"$LOG_FILE"
log_start=$(wc -l <"$LOG_FILE")
nohup "$VENV/bin/wvd" "$@" >>"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"
pid=$(cat "$PID_FILE")

# Wait for the HTTP server, then up to a few seconds for the device.
health=""
for _ in $(seq 1 50); do
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$PID_FILE"
        echo "error: wvd exited during startup. Last log lines:" >&2
        tail -n +"$((log_start + 1))" "$LOG_FILE" | tail -n 15 >&2
        exit 1
    fi
    health=$(curl -fsS --max-time 1 "$HEALTH_URL" 2>/dev/null || true)
    [[ "$health" == *'"connected":true'* ]] && break
    sleep 0.2
done

if [[ -z "$health" ]]; then
    echo "error: wvd (pid $pid) is not answering on $HEALTH_URL. See $LOG_FILE" >&2
    exit 1
fi

echo "wvd started (pid $pid), log: $LOG_FILE"
echo "  dashboard  http://127.0.0.1:$PORT/"
# Addresses other machines can use: global IPv4, minus Docker bridges.
ip -4 -o addr show scope global 2>/dev/null \
    | awk '$2 !~ /^(docker|br-|veth)/ { sub(/\/.*/, "", $4); print $4 }' \
    | while read -r ip; do echo "             http://$ip:$PORT/"; done
echo "  API docs   http://127.0.0.1:$PORT/docs"
if [[ "$health" != *'"connected":true'* ]]; then
    echo "warning: no device connected yet. Is the WireView plugged in, and the GUI / wireviewd stopped?"
    echo "         wvd keeps retrying; check with: $VENV/bin/wvctl --url http://127.0.0.1:$PORT health"
fi
