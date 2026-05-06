#!/bin/bash
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0


# convert_to_sparse_checkout.sh
# Convert existing box deployment to use sparse checkout (box code only)
#
# Usage: ./convert_to_sparse_checkout.sh <box-ip> [branch] [username]
#
# Examples:
#   ./convert_to_sparse_checkout.sh <BOX_IP>              # Uses main branch
#   ./convert_to_sparse_checkout.sh <BOX_IP> staging      # Uses staging branch
#   ./convert_to_sparse_checkout.sh <BOX_IP> main lagerdata

set -e

# Color definitions
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Parse arguments
BOX_IP=$1
BRANCH=${2:-main}
BOX_USER=${3:-lagerdata}

if [ -z "$BOX_IP" ]; then
    echo -e "${RED}Error: Box IP not provided${NC}"
    echo ""
    echo "Usage: $0 <box-ip> [branch] [username]"
    echo ""
    echo "Arguments:"
    echo "  box-ip        IP address of the box (required)"
    echo "  branch        Git branch to checkout (default: main)"
    echo "  username      Box username (default: lagerdata)"
    echo ""
    echo "Examples:"
    echo "  $0 <BOX_IP>                    # Uses main branch"
    echo "  $0 <BOX_IP> staging            # Uses staging branch"
    echo "  $0 <BOX_IP> main lagerdata     # Explicit branch and user"
    exit 1
fi

# Print header
echo ""
echo -e "${BOLD}=========================================${NC}"
echo -e "${BOLD}  Convert to Sparse Checkout${NC}"
echo -e "${BOLD}=========================================${NC}"
echo ""
echo -e "${BLUE}Box:${NC} ${BOX_USER}@${BOX_IP}"
echo -e "${BLUE}Branch:${NC}  ${BRANCH}"
echo ""

# Pre-flight checks
echo -e "${BOLD}Pre-flight checks...${NC}"
echo ""

# Check if git is installed on box
echo -n "Checking for git... "
if ssh "${BOX_USER}@${BOX_IP}" "command -v git &> /dev/null"; then
    echo -e "${GREEN}[OK] installed${NC}"
else
    echo -e "${RED}[FAIL] not found${NC}"
    echo ""
    echo -e "${YELLOW}Git is not installed on the box.${NC}"
    echo "Please install it first:"
    echo "  ssh ${BOX_USER}@${BOX_IP}"
    echo "  sudo apt update && sudo apt install -y git"
    echo ""
    echo "Or run the bootstrap script from the setup guide."
    exit 1
fi

# Check if docker is installed on box
echo -n "Checking for docker... "
if ssh "${BOX_USER}@${BOX_IP}" "command -v docker &> /dev/null"; then
    echo -e "${GREEN}[OK] installed${NC}"
else
    echo -e "${RED}[FAIL] not found${NC}"
    echo ""
    echo -e "${YELLOW}Docker is not installed on the box.${NC}"
    echo "Please install it first:"
    echo "  ssh ${BOX_USER}@${BOX_IP}"
    echo "  sudo apt update && sudo apt install -y docker.io"
    echo "  sudo systemctl enable docker"
    echo "  sudo systemctl start docker"
    echo ""
    echo "Or run the bootstrap script from the setup guide."
    exit 1
fi

# Check if /etc/lager exists
echo -n "Checking for /etc/lager... "
if ssh "${BOX_USER}@${BOX_IP}" "test -d /etc/lager" 2>/dev/null; then
    echo -e "${GREEN}[OK] exists${NC}"
else
    echo -e "${YELLOW}[WARNING] creating${NC}"
    ssh "${BOX_USER}@${BOX_IP}" "sudo mkdir -p /etc/lager && sudo chown -R 33:33 /etc/lager && sudo chmod 755 /etc/lager"
    echo "  Created /etc/lager (owned by www-data UID 33)"
fi

# Ensure saved_nets.json exists
echo -n "Checking for saved_nets.json... "
if ssh "${BOX_USER}@${BOX_IP}" "test -f /etc/lager/saved_nets.json" 2>/dev/null; then
    echo -e "${GREEN}[OK] exists${NC}"
    # Ensure correct ownership
    ssh "${BOX_USER}@${BOX_IP}" "sudo chown 33:33 /etc/lager/saved_nets.json && sudo chmod 644 /etc/lager/saved_nets.json" 2>/dev/null || true
else
    echo -e "${YELLOW}[WARNING] creating${NC}"
    ssh "${BOX_USER}@${BOX_IP}" "echo '[]' | sudo tee /etc/lager/saved_nets.json > /dev/null && sudo chown 33:33 /etc/lager/saved_nets.json && sudo chmod 644 /etc/lager/saved_nets.json"
    echo "  Created saved_nets.json (owned by www-data UID 33)"
fi

echo -e "${GREEN}[OK] All pre-flight checks passed${NC}"
echo ""

# Confirm
read -p "This will stop containers, remove the box directory, and re-clone. Continue? (yes/no): " -r
if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
    echo "Cancelled."
    exit 0
fi

echo ""
echo -e "${BLUE}[1/5] Stopping containers...${NC}"
ssh "${BOX_USER}@${BOX_IP}" "docker stop \$(docker ps -aq) 2>/dev/null || true"
ssh "${BOX_USER}@${BOX_IP}" "docker rm \$(docker ps -aq) 2>/dev/null || true"
echo -e "${GREEN}[OK] Containers stopped${NC}"

echo ""
echo -e "${BLUE}[2/5] Removing old box directory...${NC}"
ssh "${BOX_USER}@${BOX_IP}" "rm -rf ~/box"
echo -e "${GREEN}[OK] Old directory removed${NC}"

echo ""
echo -e "${BLUE}[3/5] Cloning with sparse checkout (box + scripts)...${NC}"
ssh "${BOX_USER}@${BOX_IP}" "
    git clone --filter=blob:none --no-checkout https://github.com/lagerdata/lager.git ~/box && \
    cd ~/box && \
    git sparse-checkout init --cone && \
    git sparse-checkout set box && \
    git checkout ${BRANCH}
"

# Check if box/ directory exists after checkout
echo -n "Checking box/ directory exists... "
if ssh "${BOX_USER}@${BOX_IP}" "test -d ~/box/box" 2>/dev/null; then
    echo -e "${GREEN}[OK] found${NC}"
else
    echo -e "${RED}[FAIL] not found${NC}"
    echo ""
    echo -e "${RED}Error: The 'box/' directory does not exist on branch '${BRANCH}'${NC}"
    echo ""
    echo "This usually means the branch doesn't have the expected directory structure."
    echo "The box code restructuring may be on a different branch."
    echo ""
    echo "Try running with a different branch:"
    echo -e "  ${BLUE}$0 ${BOX_IP} restructure${NC}"
    echo -e "  ${BLUE}$0 ${BOX_IP} staging${NC}"
    echo ""
    echo "Or check what branches have the box/ directory:"
    echo "  git ls-tree -d --name-only origin/restructure | grep box"
    exit 1
fi

# Move box contents to root
ssh "${BOX_USER}@${BOX_IP}" "
    cd ~/box && \
    shopt -s dotglob && \
    mv box/* . && \
    rmdir box
"
echo -e "${GREEN}[OK] Box code cloned (sparse checkout)${NC}"

echo ""
echo -e "${BLUE}[4/5] Starting containers...${NC}"
ssh "${BOX_USER}@${BOX_IP}" "cd ~/box && ./start_box.sh" 2>&1 | grep -E "successfully|started|OK" || true
echo -e "${GREEN}[OK] Containers starting${NC}"

echo ""
echo -e "${BLUE}[5/5] Verifying...${NC}"
sleep 3
RUNNING=$(ssh "${BOX_USER}@${BOX_IP}" "docker ps --format '{{.Names}}' | wc -l")
echo "  Running containers: ${RUNNING}"

if [ "$RUNNING" -ge 1 ]; then
    echo -e "${GREEN}[OK] Verification passed${NC}"
    ssh "${BOX_USER}@${BOX_IP}" "docker ps --format 'table {{.Names}}\t{{.Status}}'"
else
    echo -e "${YELLOW}[WARNING] No containers running (expected at least 1: lager)${NC}"
fi

echo ""
echo -e "${BOLD}${GREEN}=========================================${NC}"
echo -e "${BOLD}${GREEN}  Conversion Complete!${NC}"
echo -e "${BOLD}${GREEN}=========================================${NC}"
echo ""
echo "Box now uses sparse checkout (box code only)"
echo "Repository only contains: box code and scripts"
echo "Hidden from box: CLI source, backend, deployment scripts"
echo ""
echo "Test update command:"
echo -e "  ${BLUE}lager update --box <box-ip> --version ${BRANCH}${NC}"
echo ""
echo "Future updates:"
echo -e "  ${BLUE}lager update --box <box-ip> --version main${NC}"
echo -e "  ${BLUE}lager update --box <box-ip> --version staging${NC}"
echo ""
