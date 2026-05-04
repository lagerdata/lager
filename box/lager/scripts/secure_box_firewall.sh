#!/bin/bash
#
# Secure Box Firewall Configuration
#
# This script configures UFW (Uncomplicated Firewall) to secure Lager boxes.
# It implements a default-deny policy with specific allow rules for authorized access.
#
# Security Model:
# - Default DENY all incoming connections
# - SSH (port 22) allowed from anywhere for management
# - Lager services (ports 5000, 8301, 8765) restricted to:
#   - Tailscale VPN (tailscale0)
#   - Corporate VPN (if specified)
#   - Docker bridge (docker0)
#   - Localhost (lo)
# - External access explicitly blocked for Lager service ports
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
            echo "Lager service ports: 5000, 8301, 8765"
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
    apt-get update -qq
    apt-get install -y ufw
    echo -e "${GREEN}[OK] UFW installed${NC}"
fi

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Backup existing firewall rules
BACKUP_FILE="$BACKUP_DIR/ufw-backup-$(date +%Y%m%d-%H%M%S).txt"
echo -e "${YELLOW}Backing up current firewall rules to $BACKUP_FILE${NC}"
ufw status numbered > "$BACKUP_FILE" 2>/dev/null || echo "No existing rules" > "$BACKUP_FILE"

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

# Lager service ports.
# Single ports plus the 2331-2334 (J-Link GDB) and 9090-9097 (J-Link RTT
# telnet) ranges so each concurrent J-Link probe gets its own slot.
LAGER_PORTS=(5000 8301 8765 2331:2334 9090:9097)

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
    ufw allow in on lo to any port "$PORT" comment "Lager service (localhost)"
done

# Allow from Docker bridge
echo -e "${GREEN}Allowing Lager services from Docker (docker0)${NC}"
for PORT in "${LAGER_PORTS[@]}"; do
    ufw allow in on docker0 to any port "$PORT" comment "Lager service (Docker)"
done

# Allow from Tailscale VPN
if [ -n "$TAILSCALE_IFACE" ]; then
    echo -e "${GREEN}Allowing Lager services from Tailscale VPN ($TAILSCALE_IFACE)${NC}"
    for PORT in "${LAGER_PORTS[@]}"; do
        ufw allow in on "$TAILSCALE_IFACE" to any port "$PORT" comment "Lager service (Tailscale)"
    done
fi

# Allow from corporate VPN
if [ -n "$CORPORATE_VPN_IFACE" ]; then
    echo -e "${GREEN}Allowing Lager services from corporate VPN ($CORPORATE_VPN_IFACE)${NC}"
    for PORT in "${LAGER_PORTS[@]}"; do
        ufw allow in on "$CORPORATE_VPN_IFACE" to any port "$PORT" comment "Lager service (Corporate VPN)"
    done
fi

# Explicitly deny Lager service ports from other interfaces
echo -e "${YELLOW}Blocking Lager services from external networks${NC}"
for PORT in "${LAGER_PORTS[@]}"; do
    ufw deny "$PORT"/tcp comment "Lager service (block external)"
done

# Enable UFW
echo -e "${BLUE}Enabling firewall...${NC}"
ufw --force enable

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
echo -e "${GREEN}[OK] External access blocked for Lager services${NC}"
echo ""
echo -e "${YELLOW}Backup saved to: $BACKUP_FILE${NC}"
