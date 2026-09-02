#!/bin/bash
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

#
# Secure Box Firewall Configuration
#
# This script configures UFW (Uncomplicated Firewall) to secure Lager boxes.
# It implements a default-deny policy with specific allow rules for authorized access.
#
# Security Model:
# - Default DENY all incoming connections
# - SSH (port 22) allowed from anywhere for management
# - Lager service ports (the LAGER_PORTS array below) restricted to:
#   - Tailscale VPN (tailscale0)
#   - Corporate VPN (if specified)
#   - Docker bridge (docker0)
#   - Localhost (lo)
# - Deny rules written for Lager service ports on every other interface
#
# Scope of all of the above: these rules govern traffic to the HOST. They do
# not filter the ports the box's containers publish. Docker installs its own
# forwarding rules ahead of the host chain, so a published service port is
# reachable from anywhere that can route to the box, whatever `ufw status`
# reports. See the Security Model section of SECURITY.md -- treat network
# reachability as the boundary, not this script.
#
# Usage:
#   sudo ./secure_box_firewall.sh [--corporate-vpn IFACE]
#
# Options:
#   --corporate-vpn IFACE    Corporate VPN interface (e.g., tun0, enp3s0)
#

set -e

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
CORPORATE_VPN_IFACE=""
BACKUP_DIR="/etc/lager/backups"

# Lager service ports.
#
# Single ports plus the per-slot ranges that back concurrent debug probes:
#   2331:2342  GDB + SWO + telnet (3 ports per slot, 4 slots; shared between the
#              J-Link and OpenOCD backends -- which server answers is decided by
#              the probe occupying the slot)
#   4444:4447  OpenOCD interactive telnet (one port per slot)
#   6666:6669  OpenOCD TCL/RPC (one port per slot; the debug service dispatches
#              every OpenOCD runtime command through these)
#   8081:8090  remote PDB console range
#   9090:9097  RTT telnet (2 channels per slot, 4 slots; J-Link or OpenOCD)
#
# This list must match what box/start_box.sh publishes. That is enforced by
# test/unit/box/test_firewall_port_allowlist.py, which parses both files. A
# comment asking the next reader to keep them in step was the only thing holding
# them together before, and they drifted three times.
#
# Every rule built from this array must name `proto tcp`. ufw's extended syntax
# refuses a range without one -- "Must specify 'tcp' or 'udp' with multiple
# ports" -- and under `set -e` that aborts the install at the first range. TCP is
# the right protocol because start_box.sh publishes all of these with a plain
# `-p`, which docker reads as TCP. The same test file pins both halves of that.
LAGER_PORTS=(2331:2342 4444:4447 5000 6666:6669 8080 8081:8090 8100 8301 8765 9000 9090:9097)

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --corporate-vpn)
            CORPORATE_VPN_IFACE="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: sudo $0 [--corporate-vpn IFACE]"
            echo ""
            echo "Configure UFW firewall for secure box access"
            echo ""
            echo "Options:"
            echo "  --corporate-vpn IFACE    Corporate VPN interface (e.g., tun0, enp3s0)"
            echo "  --help                   Show this help message"
            echo ""
            echo "Lager service ports: ${LAGER_PORTS[*]}"
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option $1${NC}"
            exit 1
            ;;
    esac
done

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}Error: This script must be run as root (use sudo)${NC}"
   exit 1
fi

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Configuring Box Firewall${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Install UFW if not present
if ! command -v ufw &> /dev/null; then
    echo -e "${YELLOW}Installing UFW...${NC}"
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get update -qq
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get install -y ufw
    echo -e "${GREEN}[OK] UFW installed${NC}"
fi

# Backup functionality disabled to save disk space
# Previous versions created backups at /etc/lager/backups/ufw-backup-*.txt
# To re-enable, uncomment the lines below:
# mkdir -p "$BACKUP_DIR"
# BACKUP_FILE="$BACKUP_DIR/ufw-backup-$(date +%Y%m%d-%H%M%S).txt"
# echo -e "${YELLOW}Backing up current firewall rules to $BACKUP_FILE${NC}"
# ufw status numbered > "$BACKUP_FILE" 2>/dev/null || echo "No existing rules" > "$BACKUP_FILE"

# From the disable below until the enable at the end of this script the box has
# no firewall at all -- `ufw --force reset` on the next line removes every rule,
# the SSH one included. A failure inside that window used to leave it that way
# and say nothing more specific than "Deployment failed!", so a box could come
# out of a failed install wide open with no line anywhere saying so.
#
# This is not a recovery. It puts the box somewhere safer to fail -- default-deny
# with SSH reachable, so it can still be fixed -- says exactly what is and is not
# configured, and keeps the failing exit code.
restore_minimal_policy() {
    local rc=$?
    trap - EXIT
    [ "$rc" -eq 0 ] && return 0

    echo "" >&2
    echo -e "${RED}[FAIL] Firewall configuration aborted (exit $rc) with the firewall down.${NC}" >&2
    echo -e "${YELLOW}Restoring a minimal policy: deny incoming, SSH allowed.${NC}" >&2
    ufw allow 22/tcp comment "SSH access" || true
    ufw --force enable || true
    echo -e "${RED}[FAIL] Lager service ports are NOT allowed through.${NC}" >&2
    echo -e "${RED}       The box is reachable over SSH and its services are blocked${NC}" >&2
    echo -e "${RED}       until this script completes successfully.${NC}" >&2
    exit "$rc"
}
trap restore_minimal_policy EXIT

# Disable UFW temporarily to avoid lockout
echo -e "${YELLOW}Temporarily disabling firewall for configuration...${NC}"
ufw --force disable

# Reset to default configuration
echo -e "${YELLOW}Resetting firewall to defaults...${NC}"
ufw --force reset

# Set default policies
echo -e "${BLUE}Setting default policies (deny incoming, allow outgoing)...${NC}"
ufw default deny incoming
ufw default allow outgoing

# Allow SSH from anywhere (critical - prevents lockout)
echo -e "${GREEN}Allowing SSH (port 22) from anywhere${NC}"
ufw allow 22/tcp comment "SSH access"

# Detect Tailscale interface
TAILSCALE_IFACE=""
if command -v tailscale &> /dev/null && tailscale status &> /dev/null; then
    TAILSCALE_IFACE="tailscale0"
    echo -e "${GREEN}[OK] Detected Tailscale interface: $TAILSCALE_IFACE${NC}"
else
    echo -e "${YELLOW}[WARNING] Tailscale not detected${NC}"
fi

# Verify corporate VPN interface exists if specified
if [ -n "$CORPORATE_VPN_IFACE" ]; then
    if ip link show "$CORPORATE_VPN_IFACE" &> /dev/null; then
        CORPORATE_IP=$(ip addr show "$CORPORATE_VPN_IFACE" | grep "inet " | awk '{print $2}' | cut -d'/' -f1)
        echo -e "${GREEN}[OK] Detected corporate VPN interface: $CORPORATE_VPN_IFACE ($CORPORATE_IP)${NC}"
    else
        echo -e "${RED}[FAIL] Corporate VPN interface $CORPORATE_VPN_IFACE not found${NC}"
        echo -e "${YELLOW}Available interfaces:${NC}"
        ip addr show | grep -E "^[0-9]+:" | awk '{print "  " $2}' | sed 's/:$//'
        exit 1
    fi
fi

echo ""
echo -e "${BLUE}Configuring Lager service access (ports: ${LAGER_PORTS[*]})${NC}"
echo ""

# Allow from localhost
echo -e "${GREEN}Allowing Lager services from localhost (lo)${NC}"
for PORT in "${LAGER_PORTS[@]}"; do
    ufw allow in on lo to any port "$PORT" proto tcp comment "Lager service (localhost)"
done

# Allow from Docker bridge
echo -e "${GREEN}Allowing Lager services from Docker (docker0)${NC}"
for PORT in "${LAGER_PORTS[@]}"; do
    ufw allow in on docker0 to any port "$PORT" proto tcp comment "Lager service (Docker)"
done

# Allow from Tailscale VPN
if [ -n "$TAILSCALE_IFACE" ]; then
    echo -e "${GREEN}Allowing Lager services from Tailscale VPN ($TAILSCALE_IFACE)${NC}"
    for PORT in "${LAGER_PORTS[@]}"; do
        ufw allow in on "$TAILSCALE_IFACE" to any port "$PORT" proto tcp comment "Lager service (Tailscale)"
    done
fi

# Allow from corporate VPN
if [ -n "$CORPORATE_VPN_IFACE" ]; then
    echo -e "${GREEN}Allowing Lager services from corporate VPN ($CORPORATE_VPN_IFACE)${NC}"
    for PORT in "${LAGER_PORTS[@]}"; do
        ufw allow in on "$CORPORATE_VPN_IFACE" to any port "$PORT" proto tcp comment "Lager service (Corporate VPN)"
    done
fi

# Explicitly deny Lager service ports from other interfaces
echo -e "${YELLOW}Writing deny rules for Lager services on other interfaces${NC}"
for PORT in "${LAGER_PORTS[@]}"; do
    ufw deny "$PORT"/tcp comment "Lager service (block external)"
done

# Enable UFW
echo -e "${BLUE}Enabling firewall...${NC}"
ufw --force enable

# The rules are in place and the firewall is up: the window the trap guards is
# closed. Release it so a later failure -- `ufw status verbose` below, say --
# does not re-run the restore path and report a configured box as unfirewalled.
trap - EXIT

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Firewall Configuration Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${BLUE}Current firewall status:${NC}"
ufw status verbose

echo ""
echo -e "${GREEN}[OK] Default DENY policy for incoming traffic${NC}"
echo -e "${GREEN}[OK] SSH (22) allowed from anywhere${NC}"
echo -e "${GREEN}[OK] Lager services accessible from:${NC}"
echo -e "  - Localhost (lo)"
echo -e "  - Docker (docker0)"
if [ -n "$TAILSCALE_IFACE" ]; then
    echo -e "  - Tailscale VPN ($TAILSCALE_IFACE)"
fi
if [ -n "$CORPORATE_VPN_IFACE" ]; then
    echo -e "  - Corporate VPN ($CORPORATE_VPN_IFACE)"
fi
echo -e "${GREEN}[OK] Host firewall configured for Lager services${NC}"
echo ""
echo -e "${YELLOW}Note: these rules govern traffic to the host. They do not filter${NC}"
echo -e "${YELLOW}ports the box's containers publish -- Docker's forwarding rules run${NC}"
echo -e "${YELLOW}ahead of the host chain. See the Security Model section of${NC}"
echo -e "${YELLOW}SECURITY.md: treat network reachability as the boundary.${NC}"
echo ""
