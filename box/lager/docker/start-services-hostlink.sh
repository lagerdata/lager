#!/bin/bash
# Startup script for a Box container whose hardware lives in a native host
# agent (Docker Desktop or OrbStack on macOS).
#
# A Linux Box does not use this file; it runs start-services.sh, which is
# unchanged. This is that script with the hardware-owning services removed:
#
#   - Hardware Invocation Service (port 8080) is replaced by the hostlink
#     relay on the same port, which forwards to the agent on the host.
#   - The debug service and the oscilloscope daemon are not started. They
#     need USB, and this container has none. Until they are served by the
#     agent, their ports are closed here.
#
# The supervisor functions below are copied from start-services.sh on
# purpose. Sharing them would mean editing the script every Linux Box boots
# with; a copy that can drift is the cheaper risk. Keep the two in step when
# either changes.

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
        # Copy and truncate rather than rename, because the writer is still
        # running and holds an append fd on this inode. A rename would leave
        # it appending to the renamed file while the fresh one stayed empty,
        # so the next rotation would rename an empty file over it and unlink
        # the inode the writer still has open -- the disk then fills with a
        # file that has no name, which `ls` and `du` cannot show you. The
        # writers here all redirect with `>>`, so O_APPEND puts their next
        # write at the new end of the truncated file.
        cp -f "$log_file" "${log_file}.1" 2>/dev/null
        : > "$log_file"
    fi
}

# Function to restart a service if it dies.
#
# A TERM is passed on to the service, which then gets to exit cleanly. Left
# to the default, this loop dies on TERM while the service runs on until the
# container's init exits and the kernel SIGKILLs it. For the oscilloscope
# daemon that means a PicoScope left mid-transfer, and the next open of it
# blocks inside the driver for minutes.
restart_service() {
    local service_name="$1"
    local service_cmd="$2"
    local log_file="$3"
    local child=""
    local stopping=0

    trap 'stopping=1; [ -n "$child" ] && kill -TERM "$child" 2>/dev/null' TERM

    while [ "$stopping" = 0 ]; do
        rotate_log "$log_file"
        echo "$(date): Starting $service_name..." >> "$log_file"
        # exec, so $child is the service itself and the TERM reaches it
        # rather than a shell wrapped around it.
        eval "exec $service_cmd" >> "$log_file" 2>&1 &
        child=$!
        # A trapped signal ends `wait` early, while the service is still
        # shutting down.
        while kill -0 "$child" 2>/dev/null; do
            wait "$child"
        done
        child=""
        [ "$stopping" = 0 ] || break
        echo "$(date): $service_name died! Restarting in 2 seconds..." >> "$log_file"
        sleep 2
    done
    echo "$(date): $service_name stopped" >> "$log_file"
}

SERVICE_PIDS=()

start_service() {
    restart_service "$@" &
    SERVICE_PIDS+=("$!")
}

# Every service has to be gone before this script exits: tini exits with it,
# and the kernel then kills whatever is left in the container.
stop_services() {
    trap '' TERM INT
    echo "Stopping services..."
    kill -TERM "${SERVICE_PIDS[@]}" 2>/dev/null
    wait "${SERVICE_PIDS[@]}" 2>/dev/null
    exit 0
}
trap stop_services TERM INT

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

# Start Python execution service (port 5000)
echo "Starting Lager Python Execution Service on port 5000..."
# Every service below, and every script they run, sees this: it is what
# switches the debug net to the host (lager/nets/debug_net.py).
export LAGER_HOSTLINK=1
# Makes serial.Serial("/dev/ttyUSB0") reach the host's port in every Python
# process started from here, user scripts included (see the file itself).
export PYTHONPATH="/app/lager/lager/hostlink/site${PYTHONPATH:+:$PYTHONPATH}"

start_service "python execution" "python3 -m lager.python.service" "/tmp/lager-python-service.log"

# Hostlink relay (port 8080), in place of the hardware invocation service.
# Everything in this container that calls localhost:8080 reaches the host
# agent through it.
echo "Starting Lager hostlink relay on port 8080..."
start_service "hostlink relay" "python3 -m lager.hostlink.relay --port 8080" "/tmp/lager-hardware-service.log"

# The debug service and the oscilloscope daemon also run on the host. They
# carry streams, so they are forwarded as TCP on the ports they hold on a
# Linux Box (the host-side numbers are in lager/hostlink/config.py).
start_service "debug forward" "python3 -m lager.hostlink.tcp_forward --listen 8765 --to 48765 --add-token" "/tmp/lager-debug-service.log"
start_service "scope forward" "python3 -m lager.hostlink.tcp_forward --listen 8085 --to 48085" "/tmp/lager-scope-forward.log"

LAGER_DISABLE_UART_SERVICE_LOWER=$(echo "${LAGER_DISABLE_UART_SERVICE:-}" | tr '[:upper:]' '[:lower:]')
case "$LAGER_DISABLE_UART_SERVICE_LOWER" in
    1|true|yes)
        UART_SERVICE_DISABLED=1
        echo "Skipping Lager Box HTTP+WebSocket server (LAGER_DISABLE_UART_SERVICE=${LAGER_DISABLE_UART_SERVICE})"
        ;;
    *)
        UART_SERVICE_DISABLED=0
        echo "Starting Lager Box HTTP+WebSocket server on port 9000..."
        start_service "HTTP server" "python3 /app/lager/lager/box_http_server.py" "/tmp/lager-http-server.log"
        ;;
esac

# Start MCP server for AI agent integration (port 8100)
echo "Starting Lager MCP server on port 8100..."
start_service "MCP server" "python3 -m lager.mcp" "/tmp/lager-mcp-server.log"

sleep 2

echo "Services started with auto-restart (hardware served by the host agent):"
echo "  - Python Execution Service: port 5000 (log: /tmp/lager-python-service.log)"
echo "  - Hostlink relay: port 8080 (log: /tmp/lager-hardware-service.log)"
if [ "$UART_SERVICE_DISABLED" = "1" ]; then
    echo "  - Box HTTP+WebSocket: DISABLED (LAGER_DISABLE_UART_SERVICE set)"
else
    echo "  - Box HTTP+WebSocket: port 9000 (log: /tmp/lager-http-server.log)"
fi
echo "  - MCP Server (AI): port 8100 (log: /tmp/lager-mcp-server.log)"
echo ""
echo "Container ready."

# Keep container running. `wait` rather than a foreground command, because
# bash runs the TERM trap only once the foreground command returns.
wait
