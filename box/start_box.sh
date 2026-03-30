#!/bin/bash

# Script to build and start box Docker containers WITHOUT the controller container
# The Python container now includes all necessary services:
# - Python Execution Service (port 5000) - replaces controller's /python endpoint
# - Debug Service (port 8765) - embedded debugging
# - UART HTTP+WebSocket Server (port 9000) - serial communication
#
# Usage: ./start_box.sh
# Run this script from the box directory after copying code to the box device

set -e

# Ensure standard paths are available (needed when run via SSH or cron)
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Verify Docker is available
if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker is not installed or not in PATH"
    echo ""
    echo "Please install Docker first:"
    echo "  sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2"
    echo "  sudo usermod -aG docker \$USER"
    echo "  # Log out and back in, then try again"
    exit 1
fi

echo "========================================"
echo "Building and starting Lager box"
echo "(Controller container is NO LONGER NEEDED)"
echo "========================================"
echo ""

# Check if docker network exists, create if not
if ! docker network inspect lagernet >/dev/null 2>&1; then
    echo "Creating docker network 'lagernet'..."
    docker network create lagernet
    echo ""
fi

# Check for J-Link installation (searches for any version)
echo "Checking for J-Link GDB Server..."

# Use current user's home directory (works for any username)
BASE_DIR="$HOME"

THIRD_PARTY_DIR="$BASE_DIR/third_party"

# Search for any J-Link installation (version-agnostic)
JLINK_FOUND=false
if [ -d "$THIRD_PARTY_DIR" ]; then
    # Look for any directory matching JLink* pattern
    for dir in "$THIRD_PARTY_DIR"/JLink*; do
        if [ -d "$dir" ] && [ -f "$dir/JLinkGDBServerCLExe" ]; then
            echo "[OK] J-Link found at $dir"
            JLINK_FOUND=true
            break
        fi
    done
fi

if [ "$JLINK_FOUND" = false ]; then
    echo "[WARNING] J-Link not found (optional - pyOCD is used by default)"
    echo ""
    echo "  J-Link was already installed by the deployment script if available."
    echo "  Debug commands will use pyOCD (open source, already installed)."
    echo ""
    echo "  To verify J-Link installation manually:"
    echo "    ls -la $THIRD_PARTY_DIR/JLink*"
    echo ""
fi
echo ""

# 1. Build Lager box container
echo "[1/1] Building Lager Box container..."
cd "${SCRIPT_DIR}/lager"
docker build -f docker/box.Dockerfile -t lager .
echo "Lager Box container built successfully!"
echo ""

echo "========================================"
echo "Container built successfully!"
echo "========================================"
echo ""
echo "Container image created:"
echo "  - lager (includes Python Execution, Debug, and UART services)"
echo ""

echo "========================================"
echo "Starting container..."
echo "========================================"
echo ""

# Start Lager box container in background
echo "[1/1] Starting Lager Box container..."
cd "${SCRIPT_DIR}/lager"

# Check if /etc/lager directory exists
if [ ! -d /etc/lager ]; then
    echo "[WARNING] /etc/lager directory does not exist!"
    echo "  Please run the deployment script first:"
    echo "    ./deployment/setup_and_deploy_box.sh <box-ip>"
    echo ""
    echo "  Or create it manually with:"
    echo "    sudo mkdir -p /etc/lager"
    echo "    sudo chown -R \$(whoami):\$(id -gn) /etc/lager"
    echo ""
    exit 1
fi

# Initialize saved_nets.json if it doesn't exist (no sudo needed since we own /etc/lager)
if [ ! -f /etc/lager/saved_nets.json ]; then
    echo "Initializing /etc/lager/saved_nets.json..."
    echo "[]" > /etc/lager/saved_nets.json
    chmod 666 /etc/lager/saved_nets.json
fi

# Sync SSH keys from /etc/lager/authorized_keys.d/ into ~/.ssh/authorized_keys.
# Runs as lagerdata (no sudo needed). After the first successful Stout install,
# systemd units handle this automatically; this is the bootstrap path for first install.
_sync_authorized_keys() {
    local keys_dir="/etc/lager/authorized_keys.d"
    [ -d "$keys_dir" ] || return 0
    local auth_keys="$HOME/.ssh/authorized_keys"
    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
    touch "$auth_keys" && chmod 600 "$auth_keys"
    local added=0
    for f in "$keys_dir"/*.pub; do
        [ -f "$f" ] || continue
        local key
        key=$(cat "$f")
        grep -qxF "$key" "$auth_keys" || { echo "$key" >> "$auth_keys"; added=$((added + 1)); }
    done
    [ "$added" -gt 0 ] && echo "  Synced $added SSH key(s) from authorized_keys.d"
    return 0
}

echo "Syncing SSH authorized keys..."
_sync_authorized_keys

# Background poller: catches keys written to authorized_keys.d while the box is running
# (e.g., during a Stout install). Exits on next start_box.sh run via PID file cleanup.
_SSH_SYNC_PID_FILE="/tmp/lager-ssh-sync.pid"
if [ -f "$_SSH_SYNC_PID_FILE" ]; then
    _old_pid=$(cat "$_SSH_SYNC_PID_FILE" 2>/dev/null || true)
    [ -n "$_old_pid" ] && kill "$_old_pid" 2>/dev/null || true
    rm -f "$_SSH_SYNC_PID_FILE"
fi
(
    while true; do
        sleep 5
        _sync_authorized_keys 2>/dev/null
    done
) > /dev/null 2>&1 &
echo "$!" > "$_SSH_SYNC_PID_FILE"
disown "$!"
echo ""

# Check for JLink directory
# Look for J-Link using the THIRD_PARTY_DIR variable (works for any user)
JL_MOUNT_PYTHON=""

if [ -d "$THIRD_PARTY_DIR" ]; then
    JL_DIR=$(find "$THIRD_PARTY_DIR" -maxdepth 1 -type d -name 'JLink*' 2>/dev/null | head -1)
    if [ -n "$JL_DIR" ] && [ -f "$JL_DIR/JLinkGDBServerCLExe" ]; then
        JL_MOUNT_PYTHON="-v $JL_DIR:/home/www-data/third_party/jlink"
        echo "  J-Link mount: $JL_DIR"
    fi
fi

# Warn if J-Link not found
if [ -z "$JL_MOUNT_PYTHON" ]; then
    echo "  [WARNING] J-Link not found - debug commands will not work"
    echo "    Expected location: $THIRD_PARTY_DIR/JLink_*"
fi
echo ""

# Check for customer binaries directory
CUSTOMER_BINARIES_MOUNT=""
CUSTOMER_BIN_DIR="$THIRD_PARTY_DIR/customer-binaries"

if [ -d "$CUSTOMER_BIN_DIR" ]; then
    CUSTOMER_BINARIES_MOUNT="-v $CUSTOMER_BIN_DIR:/home/www-data/customer-binaries"
    echo "Customer binaries directory found:"
    echo "  Host: $CUSTOMER_BIN_DIR"
    echo "  Container: /home/www-data/customer-binaries"

    # List binaries if directory is not empty
    if [ "$(ls -A $CUSTOMER_BIN_DIR 2>/dev/null)" ]; then
        echo "  Binaries available:"
        ls -1 "$CUSTOMER_BIN_DIR" | sed 's/^/    - /'
    else
        echo "  (directory is empty)"
    fi
    echo ""
fi

# Check for oscilloscope daemon (PicoScope streaming support)
OSCILLOSCOPE_MOUNT=""
OSCILLOSCOPE_DAEMON="$THIRD_PARTY_DIR/oscilloscope-daemon"
OSCILLOSCOPE_CERTS="$THIRD_PARTY_DIR/oscilloscope-certs"

if [ -f "$OSCILLOSCOPE_DAEMON" ]; then
    OSCILLOSCOPE_MOUNT="-v $OSCILLOSCOPE_DAEMON:/usr/local/bin/oscilloscope-daemon:ro"
    echo "Oscilloscope daemon found:"
    echo "  Host: $OSCILLOSCOPE_DAEMON"
    echo "  Container: /usr/local/bin/oscilloscope-daemon"

    # Mount certs if available
    if [ -d "$OSCILLOSCOPE_CERTS" ]; then
        OSCILLOSCOPE_MOUNT="$OSCILLOSCOPE_MOUNT -v $OSCILLOSCOPE_CERTS:/opt/oscilloscope/certs:ro"
        echo "  Certs: $OSCILLOSCOPE_CERTS -> /opt/oscilloscope/certs"
    fi
    echo ""
else
    echo "Oscilloscope daemon not found (PicoScope streaming disabled)"
    echo "  Expected: $OSCILLOSCOPE_DAEMON"
    echo "  To enable: Build and copy oscilloscope-daemon (box/oscilloscope-daemon/)"
    echo ""
fi

# Get environment variables that will be passed to Python scripts
[[ -f "$HOME/.env" ]] && source "$HOME/.env" || true

# Auto-detect PIGPIO address (may not exist, default to standard)
# Docker-internal network default for pigpio container; auto-detected at runtime
PIGPIO_ADDR=$(docker inspect -f '{{ .NetworkSettings.Networks.lagernet.IPAddress }}' pigpio 2>/dev/null | tr -d '\n' || echo "172.18.0.2")

# Auto-detect Docker interface
DOCKER_IFACE=$(ip -4 addr show docker0 2>/dev/null | grep 'inet ' | awk '{print $2}' | cut -d'/' -f1)
if [ -z "$DOCKER_IFACE" ]; then
    DOCKER_IFACE=$(/sbin/ifconfig docker0 2>/dev/null | grep -Po 'inet\W+\K\d+\.\d+\.\d+\.\d+' || echo "172.17.0.1")
fi

# Auto-detect Tailscale/VPN interface for logging
VPN_INFO=""
if command -v tailscale &> /dev/null; then
    VPN_IP=$(tailscale ip -4 2>/dev/null || echo "")
    if [ -n "$VPN_IP" ]; then
        VPN_INFO="Tailscale: $VPN_IP"
    fi
fi

echo "Network configuration:"
echo "  Docker Interface: $DOCKER_IFACE"
if [ -n "$VPN_INFO" ]; then
    echo "  VPN: $VPN_INFO"
fi
echo "  PIGPIO Address: $PIGPIO_ADDR"
echo ""

# Stop existing container if running
if docker ps -a --format '{{.Names}}' | grep -q '^lager$'; then
    echo "Stopping existing lager container..."
    docker stop lager 2>/dev/null || true
    docker rm lager 2>/dev/null || true
fi

# Start the Lager container with ALL necessary ports exposed
# Port 5000: Python Execution Service (replaces controller)
# Port 8765: Debug Service
# Port 9000: UART HTTP+WebSocket Server
# Port 8081-8090: Remote debugging (PDB, etc.)
# Port 2331: J-Link GDB Server
# Port 9090: Additional service ports
docker run -d \
    --network lagernet \
    --privileged \
    -v /tmp:/tmp \
    -v /dev:/dev \
    -v /sys/bus/usb:/sys/bus/usb:ro \
    -v /sys/devices:/sys/devices:ro \
    -v /var/run/dbus:/var/run/dbus \
    -v /etc/lager:/etc/lager \
    -v /etc/hostname:/host/etc/hostname:ro \
    -v /opt/SEGGER:/opt/SEGGER:ro \
    -v /opt/picoscope/lib:/opt/picoscope/lib:ro \
    ${JL_MOUNT_PYTHON} \
    ${CUSTOMER_BINARIES_MOUNT} \
    ${OSCILLOSCOPE_MOUNT} \
    -p 5000:5000 \
    -p 8301:5000 \
    -p 8080:8080 \
    -p 8081-8090:8081-8090 \
    -p 8765:8765 \
    -p 9000:9000 \
    -p 2331:2331 \
    -p 9090:9090 \
    --env "PIGPIO_ADDR=$PIGPIO_ADDR" \
    --env "LAGER_HOST=$DOCKER_IFACE" \
    --env "PYTHONBREAKPOINT=remote_pdb.set_trace" \
    --env "LOCAL_ADDRESS=172.18.0.10" \
    --env "REMOTE_PDB_HOST=0.0.0.0" \
    --env "REMOTE_PDB_PORT=5555" \
    --log-driver json-file \
    --log-opt max-size=10m \
    --log-opt max-file=3 \
    --name lager \
    --restart always \
    lager

echo "Lager Box container started"
echo ""

echo "========================================"
echo "Box started successfully!"
echo "========================================"
echo ""
echo "Services running:"
echo "  - Python Execution Service: port 5000 (and 8301 for backwards compatibility)"
echo "  - Debug Service: port 8765"
echo "  - UART HTTP+WebSocket: port 9000"
echo "  - Remote PDB: ports 8081-8090"
echo "  - J-Link GDB Server: port 2331"
echo ""
echo "IMPORTANT: The controller container is NO LONGER NEEDED!"
echo "  All functionality has been moved to the lager container."
echo ""
docker ps --filter "name=lager"
