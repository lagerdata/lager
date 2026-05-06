#!/bin/bash
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

#
# Setup SSH key authentication for a Lager Box
#
# Usage: ./setup_ssh_key.sh <box-name-or-ip> [username]
#
# This script:
# 1. Creates the lager_box SSH key if it doesn't exist
# 2. Copies it to the specified box
# 3. Verifies passwordless SSH works
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

print_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[OK]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }
print_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

KEY_FILE="$HOME/.ssh/lager_box"

# Check arguments
if [ -z "$1" ]; then
    echo -e "${BOLD}Usage:${NC} $0 <box-name-or-ip> [username]"
    echo ""
    echo "Examples:"
    echo "  $0 MY-BOX"
    echo "  $0 <TAILSCALE-IP>"
    echo "  $0 MY-BOX lagerdata"
    echo ""
    exit 1
fi

BOX="$1"
USERNAME="${2:-lagerdata}"

# Try to resolve box name to IP using lager CLI
if [[ ! "$BOX" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    print_info "Resolving box name '$BOX'..."

    # Try to get IP from lager boxes command
    BOX_IP=$(lager boxes 2>/dev/null | grep -i "^$BOX " | awk '{print $2}' || true)

    if [ -z "$BOX_IP" ]; then
        print_warn "Could not resolve '$BOX' from lager boxes. Using as hostname."
        BOX_IP="$BOX"
    else
        print_success "Resolved to $BOX_IP"
    fi
else
    BOX_IP="$BOX"
fi

SSH_TARGET="${USERNAME}@${BOX_IP}"

echo ""
echo -e "${BOLD}Setting up SSH key authentication${NC}"
echo -e "Target: ${SSH_TARGET}"
echo -e "Key:    ${KEY_FILE}"
echo ""

# Step 1: Create SSH key if it doesn't exist
if [ -f "$KEY_FILE" ]; then
    print_success "SSH key already exists at $KEY_FILE"
else
    print_info "Generating new SSH key..."
    mkdir -p "$HOME/.ssh"
    ssh-keygen -t ed25519 -f "$KEY_FILE" -N "" -C "lager-box-access"
    print_success "SSH key created"
fi

# Step 2: Check if key is already on the box
print_info "Checking if key is already installed on box..."
if ssh -i "$KEY_FILE" -o BatchMode=yes -o ConnectTimeout=5 "$SSH_TARGET" "echo test" &>/dev/null; then
    print_success "SSH key already works for $SSH_TARGET"
    echo ""
    echo -e "${GREEN}${BOLD}SSH is already configured!${NC}"
    echo ""
    exit 0
fi

# Step 3: Copy key to box
print_info "Copying SSH key to box (you'll be prompted for password)..."
echo ""

ssh-copy-id -i "$KEY_FILE" "$SSH_TARGET"

# Step 4: Verify it works
echo ""
print_info "Verifying passwordless SSH..."

if ssh -i "$KEY_FILE" -o BatchMode=yes -o ConnectTimeout=5 "$SSH_TARGET" "echo test" &>/dev/null; then
    print_success "SSH key authentication working!"
    echo ""
    echo -e "${GREEN}${BOLD}Setup complete!${NC}"
    echo ""
    echo "You can now SSH without a password:"
    echo "  ssh -i $KEY_FILE $SSH_TARGET"
    echo ""
    echo "Or run lager commands:"
    echo "  lager update --box $BOX --yes"
    echo ""
else
    print_error "SSH key authentication failed"
    echo ""
    echo "Try manually with:"
    echo "  ssh-copy-id -i $KEY_FILE $SSH_TARGET"
    exit 1
fi
