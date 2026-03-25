#!/bin/bash
# Startup script for box python container
# Starts all box services including:
# - Python execution service (port 5000) - replaces controller container
# - Hardware invocation service (port 8080) - device method proxy
# - Debug service (port 8765) - embedded debugging
# - Box HTTP+WebSocket server (port 9000) - hardware control (UART, supply, etc.)

# Function to restart a service if it dies
restart_service() {
    local service_name="$1"
    local service_cmd="$2"
    local log_file="$3"

    while true; do
        echo "$(date): Starting $service_name..." >> "$log_file"
        eval "$service_cmd" >> "$log_file" 2>&1
        echo "$(date): $service_name died! Restarting in 2 seconds..." >> "$log_file"
        sleep 2
    done
}

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

# Start HTTP server for direct hardware access in background with auto-restart
echo "Starting Lager Box HTTP+WebSocket server on port 9000..."
restart_service "HTTP server" "python3 /app/lager/lager/box_http_server.py" "/tmp/lager-http-server.log" &

# Start oscilloscope streaming daemon if available (PicoScope support)
# Ports: 8082 (commands), 8083 (browser streaming), 8084 (database streaming), 8085 (WebSocket CLI)
if [ -x /usr/local/bin/oscilloscope-daemon ]; then
    echo "Starting Oscilloscope streaming daemon on ports 8082-8085..."
    # Set LD_LIBRARY_PATH for PicoScope SDK (mounted from host)
    export LD_LIBRARY_PATH="/opt/picoscope/lib:$LD_LIBRARY_PATH"
    # Run from /opt/oscilloscope where certs are mounted
    mkdir -p /opt/oscilloscope
    ln -sf /opt/oscilloscope/certs /opt/oscilloscope/certs 2>/dev/null || true
    restart_service "oscilloscope daemon" "cd /opt/oscilloscope && /usr/local/bin/oscilloscope-daemon" "/tmp/oscilloscope-daemon.log" &

    # Start simple HTTP server to serve oscilloscope UI (port 8081)
    echo "Starting Oscilloscope UI HTTP server on port 8081..."
    restart_service "oscilloscope UI" "cd /app/lager && python3 -m http.server 8081" "/tmp/oscilloscope-ui.log" &
else
    echo "Oscilloscope daemon not available (PicoScope streaming disabled)"
fi

# Give services a moment to start
sleep 2

# Show status
echo "Services started with auto-restart:"
echo "  - Python Execution Service: port 5000 (log: /tmp/lager-python-service.log)"
echo "  - Hardware Invocation Service: port 8080 (log: /tmp/lager-hardware-service.log)"
echo "  - Debug service: port 8765 (log: /tmp/lager-debug-service.log)"
echo "  - Box HTTP+WebSocket: port 9000 (log: /tmp/lager-http-server.log)"
if [ -x /usr/local/bin/oscilloscope-daemon ]; then
    echo "  - Oscilloscope Daemon: ports 8082-8085 (log: /tmp/oscilloscope-daemon.log)"
    echo "  - Oscilloscope UI: port 8081 (log: /tmp/oscilloscope-ui.log)"
fi
echo ""
echo "Container ready! Controller container is NO LONGER NEEDED."

# Keep container running
tail -f /dev/null
