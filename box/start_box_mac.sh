#!/bin/bash
# start_box_mac.sh — native macOS launcher for the Lager box services.
#
# This is the macOS equivalent of the Docker container's start-services.sh.
# It runs the same five Python services directly on the macOS host (no
# container, since Docker Desktop on macOS cannot pass USB through to
# containers). The launchd LaunchDaemon at /Library/LaunchDaemons/com.lager.box.plist
# points its ProgramArguments at this script and runs it as the dedicated
# `lagerdata` user; you can also run it manually for development debugging.
#
# Services started:
#   - Python execution service (port 5000)  — lager.python.service
#   - Hardware invocation service (port 8080) — lager/hardware_service.py
#   - Debug service (port 8765)             — lager.debug.service
#   - Box HTTP+WebSocket server (port 9000) — lager/box_http_server.py
#   - MCP server (port 8100)                — lager.mcp
#
# The PicoScope oscilloscope daemon is NOT started on macOS — it depends on
# libps2000.so which Pico Technology does not ship for macOS (PicoSDK macOS
# downloads page returns 404).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Standard paths plus Homebrew on Apple Silicon.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Applications/SEGGER/JLink:$PATH"

# State directory and venv. setup_and_deploy_box_mac.sh creates these and
# chowns them to the lagerdata user.
export LAGER_STATE_DIR="${LAGER_STATE_DIR:-/Library/Application Support/Lager}"
LAGER_VENV="${LAGER_VENV:-$LAGER_STATE_DIR/venv}"
LAGER_LOG_DIR="${LAGER_LOG_DIR:-/Library/Logs/Lager}"
LAGER_BIN_DIR="${LAGER_BIN_DIR:-$LAGER_STATE_DIR/bin}"
export PATH="$LAGER_BIN_DIR:$PATH"

mkdir -p "$LAGER_STATE_DIR" "$LAGER_LOG_DIR"

# Activate the venv so `python3` resolves to the box's Python.
if [ -f "$LAGER_VENV/bin/activate" ]; then
    # shellcheck disable=SC1091
    . "$LAGER_VENV/bin/activate"
else
    echo "WARNING: venv not found at $LAGER_VENV — falling back to system python3"
fi

# Ignore SIGPIPE so HTTP streaming clients disconnecting mid-response do not
# kill our long-lived servers (matches the Linux container behaviour).
trap '' PIPE

# --- helpers -----------------------------------------------------------------

# Initialize state files on first run so the services don't fail with
# FileNotFoundError on a brand-new box.
init_state_files() {
    [ -f "$LAGER_STATE_DIR/saved_nets.json" ] || echo '[]' > "$LAGER_STATE_DIR/saved_nets.json"
    [ -f "$LAGER_STATE_DIR/version" ]         || echo "mac-box|main" > "$LAGER_STATE_DIR/version"
}

# Restart-on-crash supervisor (mirrors start-services.sh in the Linux container).
restart_service() {
    local service_name="$1"
    local service_cmd="$2"
    local log_file="$3"

    while true; do
        echo "$(date): Starting $service_name..." >> "$log_file"
        eval "$service_cmd" >> "$log_file" 2>&1
        echo "$(date): $service_name exited with $?. Restarting in 2 seconds..." >> "$log_file"
        sleep 2
    done
}

# --- preflight ---------------------------------------------------------------

if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: start_box_mac.sh is only for macOS hosts. Use start_box.sh on Linux." >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH. Run setup_and_deploy_box_mac.sh first." >&2
    exit 1
fi

init_state_files

# Make the box's Python package importable. The repo lives at
# /Library/Application Support/Lager/repo by default, and box/lager/ is the
# `lager` package root.
LAGER_PYTHONPATH_ROOT="${LAGER_REPO_DIR:-$LAGER_STATE_DIR/repo}/box"
if [ -d "$LAGER_PYTHONPATH_ROOT/lager" ]; then
    export PYTHONPATH="$LAGER_PYTHONPATH_ROOT:${PYTHONPATH:-}"
else
    # Fallback for development runs from inside the repo
    if [ -d "$REPO_ROOT/box/lager" ]; then
        export PYTHONPATH="$REPO_ROOT/box:${PYTHONPATH:-}"
        LAGER_PYTHONPATH_ROOT="$REPO_ROOT/box"
    fi
fi
echo "Lager PYTHONPATH: $PYTHONPATH"

# --- launch services ---------------------------------------------------------

LOG="$LAGER_LOG_DIR"

echo "Starting Lager Python Execution Service on port 5000..."
restart_service "python execution" "python3 -m lager.python.service" "$LOG/python-service.log" &

echo "Starting Lager Hardware Invocation Service on port 8080..."
restart_service "hardware service" "python3 \"$LAGER_PYTHONPATH_ROOT/lager/hardware_service.py\"" "$LOG/hardware-service.log" &

echo "Starting Lager debug service on port 8765..."
restart_service "debug service" "python3 -m lager.debug.service" "$LOG/debug-service.log" &

echo "Starting Lager Box HTTP+WebSocket server on port 9000..."
restart_service "HTTP server" "python3 \"$LAGER_PYTHONPATH_ROOT/lager/box_http_server.py\"" "$LOG/http-server.log" &

echo "Starting Lager MCP server on port 8100..."
restart_service "MCP server" "python3 -m lager.mcp" "$LOG/mcp-server.log" &

# PicoScope oscilloscope daemon: not supported on macOS (no libps2000.dylib
# from Pico Technology). Picoscope 2000 is documented as a Tier-1 unsupported
# instrument on the macOS box.

echo ""
echo "Lager box services started (logs in $LAGER_LOG_DIR):"
echo "  - Python Execution:    http://localhost:5000  (python-service.log)"
echo "  - Hardware Invocation: http://localhost:8080  (hardware-service.log)"
echo "  - Debug Service:       http://localhost:8765  (debug-service.log)"
echo "  - Box HTTP/WS:         http://localhost:9000  (http-server.log)"
echo "  - MCP Server (AI):     http://localhost:8100  (mcp-server.log)"
echo ""
echo "Box ready."

# Block forever so launchd KeepAlive sees us as healthy. If any of the
# background restart_service loops dies (which they shouldn't — they restart
# their service forever), we exit and launchd brings us back up.
#
# `wait -n` (wait for any single child) requires bash 4.3+, but macOS ships
# bash 3.2 due to GPLv3 licensing. Plain `wait` (no flags) blocks until ALL
# background jobs exit, which achieves the same goal: if any restart_service
# loop somehow terminates, `wait` eventually returns and we exit so launchd
# can restart the whole wrapper.
wait
echo "$(date): A supervised service exited unexpectedly — terminating so launchd can restart us." >&2
exit 1
