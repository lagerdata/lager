#!/bin/bash
# Startup script for box python container
# Starts all box services including:
# - Python execution service (port 5000) - replaces controller container
# - Hardware invocation service (port 8080) - device method proxy
# - Debug service (port 8765) - embedded debugging
# - Box HTTP+WebSocket server (port 9000) - hardware control (UART, supply, etc.)

# Cap a log file, keeping one previous generation.
#
# These logs live on the box's SD card and nothing else trims them. The
# oscilloscope daemon's readiness poll was measured writing ~990 MB/day, which
# fills the card and takes the whole box down rather than just the daemon.
LOG_MAX_BYTES=${LAGER_LOG_MAX_BYTES:-33554432}  # 32 MiB

rotate_log() {
    local log_file="$1"
    [ -f "$log_file" ] || return 0

    # stat's flags differ between GNU and BSD; the container is GNU but the
    # fallback keeps this script usable when run on a developer machine.
    local size
    size=$(stat -c %s "$log_file" 2>/dev/null || stat -f %z "$log_file" 2>/dev/null || echo 0)

    if [ "$size" -gt "$LOG_MAX_BYTES" ]; then
        mv -f "$log_file" "${log_file}.1"
        : > "$log_file"
    fi
}

# Function to restart a service if it dies
restart_service() {
    local service_name="$1"
    local service_cmd="$2"
    local log_file="$3"

    while true; do
        rotate_log "$log_file"
        echo "$(date): Starting $service_name..." >> "$log_file"
        eval "$service_cmd" >> "$log_file" 2>&1
        echo "$(date): $service_name died! Restarting in 2 seconds..." >> "$log_file"
        sleep 2
    done
}

# Trim logs periodically too, since a service that never dies never re-enters
# the loop above and so would never rotate.
log_janitor() {
    while true; do
        sleep 300
        for log_file in /tmp/lager-*.log /tmp/oscilloscope-*.log; do
            [ -f "$log_file" ] && rotate_log "$log_file"
        done
    done
}
log_janitor &

# Ignore SIGPIPE at shell level to prevent process termination when client disconnects during HTTP streaming
# This is inherited by all Python processes and prevents "Broken pipe" from killing the servers
trap '' PIPE

# Start Python execution service (port 5000) - THIS REPLACES THE CONTROLLER CONTAINER
echo "Starting Lager Python Execution Service on port 5000..."
restart_service "python execution" "python3 -m lager.python.service" "/tmp/lager-python-service.log" &

# Start hardware invocation service (port 8080) - CRITICAL for Device proxy pattern
echo "Starting Lager Hardware Invocation Service on port 8080..."
restart_service "hardware service" "python3 /app/lager/lager/hardware_service.py" "/tmp/lager-hardware-service.log" &

# Start debug service in background with auto-restart
echo "Starting Lager debug service on port 8765..."
restart_service "debug service" "python3 -m lager.debug.service" "/tmp/lager-debug-service.log" &

# Start HTTP server for direct hardware access in background with auto-restart.
# Gated on LAGER_DISABLE_UART_SERVICE so customers can free port 9000 for their
# own broker / service when they're not using lager's UART feature. Without the
# skip, the supervisor would re-spawn box_http_server.py and lose the port race
# every restart. Set the env var via box_config.json's `env` field.
LAGER_DISABLE_UART_SERVICE_LOWER=$(echo "${LAGER_DISABLE_UART_SERVICE:-}" | tr '[:upper:]' '[:lower:]')
case "$LAGER_DISABLE_UART_SERVICE_LOWER" in
    1|true|yes)
        UART_SERVICE_DISABLED=1
        echo "Skipping Lager Box HTTP+WebSocket server (LAGER_DISABLE_UART_SERVICE=${LAGER_DISABLE_UART_SERVICE})"
        ;;
    *)
        UART_SERVICE_DISABLED=0
        echo "Starting Lager Box HTTP+WebSocket server on port 9000..."
        restart_service "HTTP server" "python3 /app/lager/lager/box_http_server.py" "/tmp/lager-http-server.log" &
        ;;
esac

# Start MCP server for AI agent integration (port 8100)
echo "Starting Lager MCP server on port 8100..."
restart_service "MCP server" "python3 -m lager.mcp" "/tmp/lager-mcp-server.log" &

# Start oscilloscope daemon if available (PicoScope support).
#
# One listener now, not four. The daemon previously bound 8082-8084 for
# WebTransport plus 8085 for WebSocket, and served its UI from a bare
# python -m http.server on 8081 -- none of which were published off-box, so
# the only reachable path was through port 9000 anyway. The UI is now served
# by box_http_server on 9000 and captures are relayed over the Unix socket
# below, which is why no certificate directory is needed: the WebTransport
# listeners it existed for are gone.
if [ -x /usr/local/bin/oscilloscope-daemon ]; then
    echo "Starting Oscilloscope daemon (socket + port 8085)..."
    # PicoScope SDK is mounted from the host; the daemon dlopens from here.
    export LAGER_SCOPE_SOCKET="${LAGER_SCOPE_SOCKET:-/tmp/lager-scope.sock}"
    export LAGER_SCOPE_DATA_PORT="${LAGER_SCOPE_DATA_PORT:-8085}"
    export LAGER_SCOPE_LOG="${LAGER_SCOPE_LOG:-info}"
    export LD_LIBRARY_PATH="/opt/picoscope/lib:$LD_LIBRARY_PATH"
    restart_service "oscilloscope daemon" "/usr/local/bin/oscilloscope-daemon" "/tmp/oscilloscope-daemon.log" &
else
    echo "Oscilloscope daemon not available (PicoScope support disabled)"
fi

# Give services a moment to start
sleep 2

# Show status
echo "Services started with auto-restart:"
echo "  - Python Execution Service: port 5000 (log: /tmp/lager-python-service.log)"
echo "  - Hardware Invocation Service: port 8080 (log: /tmp/lager-hardware-service.log)"
echo "  - Debug service: port 8765 (log: /tmp/lager-debug-service.log)"
if [ "$UART_SERVICE_DISABLED" = "1" ]; then
    echo "  - Box HTTP+WebSocket: DISABLED (LAGER_DISABLE_UART_SERVICE set; port 9000 free for customer use)"
else
    echo "  - Box HTTP+WebSocket: port 9000 (log: /tmp/lager-http-server.log)"
fi
echo "  - MCP Server (AI): port 8100 (log: /tmp/lager-mcp-server.log)"
if [ -x /usr/local/bin/oscilloscope-daemon ]; then
    echo "  - Oscilloscope Daemon: ${LAGER_SCOPE_SOCKET} + port ${LAGER_SCOPE_DATA_PORT} (log: /tmp/oscilloscope-daemon.log)"
    echo "  - Oscilloscope UI: port 9000 at /scope"
fi
echo ""
echo "Container ready! Controller container is NO LONGER NEEDED."

# Keep container running
tail -f /dev/null
