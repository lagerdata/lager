#!/bin/bash
# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0


# setup_and_deploy_box.sh
# One-command setup and deployment for Lager boxes
#
# This script handles the complete box setup process:
#   1. SSH key configuration (passwordless access)
#   2. Sudo configuration (passwordless udev management)
#   3. Box code deployment (git sparse-checkout via HTTPS)
#   4. J-Link installation (optional, if available)
#   5. Docker container startup
#   6. Post-deployment verification
#
# Uses HTTPS for git operations (no authentication needed for public repo).
#
# Usage: ./setup_and_deploy_box.sh <box-ip> [OPTIONS]
#
# Options:
#   --user <username>     Box username (default: lagerdata)
#   --branch <branch>     Git branch to checkout (default: main)
#   --skip-jlink          Skip J-Link installation even if available
#   --skip-verify         Skip post-deployment verification
#   --help                Show this help message
#
# Examples:
#   ./setup_and_deploy_box.sh <BOX_IP>
#   ./setup_and_deploy_box.sh <BOX_IP> --branch staging
#   ./setup_and_deploy_box.sh <BOX_IP> --user pi
#   ./setup_and_deploy_box.sh <BOX_IP> --skip-jlink

set -e

# Color definitions
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Default values
BOX_USER="lagerdata"
SKIP_JLINK=false
SKIP_VERIFY=false
BOX_IP=""
VPN_INTERFACE=""
GIT_BRANCH="main"

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Default values for firewall
SKIP_FIREWALL=false
SKIP_ADD_BOX=false
CORPORATE_VPN=""

# Parse arguments
show_help() {
    echo "Usage: $0 <box-ip> [OPTIONS]"
    echo ""
    echo "One-command setup and deployment for Lager boxes"
    echo ""
    echo "Arguments:"
    echo "  box-ip                IP address of the box (required)"
    echo ""
    echo "Options:"
    echo "  --user <username>     Box username (default: lagerdata)"
    echo "  --branch <branch>     Git branch to checkout (default: main)"
    echo "  --vpn <interface>     VPN interface to bind to (e.g., tun0, ppp0)"
    echo "                        If not specified, auto-detects Tailscale/WireGuard"
    echo "  --corporate-vpn <iface> Corporate VPN interface for firewall (e.g., tun0)"
    echo "  --skip-firewall       Skip firewall configuration"
    echo "  --install-jlink       Interactively download and install J-Link (requires license acceptance)"
    echo "  --skip-jlink          Skip J-Link installation even if available"
    echo "  --skip-verify         Skip post-deployment verification"
    echo "  --skip-add-box        Skip prompt to add box to .lager config"
    echo "  --help                Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 <BOX_IP>"
    echo "  $0 <BOX_IP> --branch staging"
    echo "  $0 <BOX_IP> --user pi"
    echo "  $0 <BOX_IP> --vpn tun0"
    echo "  $0 <BOX_IP> --corporate-vpn tun0"
    echo "  $0 <BOX_IP> --skip-jlink"
    echo "  $0 <BOX_IP> --skip-firewall"
    echo ""
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --user)
            BOX_USER="$2"
            shift 2
            ;;
        --sparse)
            # Accepted for backwards compatibility (sparse checkout is now the only mode)
            shift
            ;;
        --branch)
            GIT_BRANCH="$2"
            shift 2
            ;;
        --vpn)
            VPN_INTERFACE="$2"
            shift 2
            ;;
        --corporate-vpn)
            CORPORATE_VPN="$2"
            shift 2
            ;;
        --skip-firewall)
            SKIP_FIREWALL=true
            shift
            ;;
        --install-jlink)
            # Note: J-Link is now installed automatically if not present
            # This flag is kept for backwards compatibility but has no effect
            shift
            ;;
        --skip-jlink)
            SKIP_JLINK=true
            shift
            ;;
        --skip-verify)
            SKIP_VERIFY=true
            shift
            ;;
        --skip-add-box)
            SKIP_ADD_BOX=true
            shift
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        *)
            if [ -z "$BOX_IP" ]; then
                BOX_IP="$1"
            else
                echo -e "${RED}Error: Unknown argument '$1'${NC}"
                echo ""
                show_help
                exit 1
            fi
            shift
            ;;
    esac
done

if [ -z "$BOX_IP" ]; then
    echo -e "${RED}Error: No box IP provided${NC}"
    echo ""
    show_help
    exit 1
fi

# Print header
echo ""
echo -e "${BOLD}=========================================${NC}"
echo -e "${BOLD}  Lager Box Setup & Deployment${NC}"
echo -e "${BOLD}=========================================${NC}"
echo ""
echo -e "${BLUE}Box:${NC} ${BOX_USER}@${BOX_IP}"
echo -e "${BLUE}Method:${NC}  sparse-checkout (git over HTTPS)"
echo -e "${BLUE}Branch:${NC}  ${GIT_BRANCH}"
echo -e "${BLUE}Time:${NC}    $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# Step counter
TOTAL_STEPS=7
CURRENT_STEP=0

print_step() {
    CURRENT_STEP=$((CURRENT_STEP + 1))
    echo ""
    echo -e "${BOLD}${BLUE}[${CURRENT_STEP}/${TOTAL_STEPS}] $1${NC}"
    echo "----------------------------------------"
}

print_success() {
    echo -e "${GREEN}[OK] $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}[WARN] $1${NC}"
}

print_error() {
    echo -e "${RED}[ERROR] $1${NC}"
}

print_info() {
    echo -e "${BLUE}[INFO] $1${NC}"
}

# SSH connection multiplexing configuration
# Reuses a single TCP connection for all SSH commands,
# preventing connection reset issues over VPN
EXISTING_CM=$(ssh -G "${BOX_USER}@${BOX_IP}" 2>/dev/null | awk '/^controlmaster /{print $2}')
if [ "$EXISTING_CM" = "auto" ] || [ "$EXISTING_CM" = "yes" ] || [ "$EXISTING_CM" = "autoask" ]; then
    # Existing SSH config already provides ControlMaster — just add keepalive options
    SSH_OPTS="-o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ConnectTimeout=10"
    SCP_OPTS=""
    LAGER_OWN_MASTER=false
else
    # No existing multiplexing — set up our own
    CONTROL_DIR="$HOME/.lager_cache/ssh_control"
    mkdir -p "$CONTROL_DIR"
    CONTROL_PATH="${CONTROL_DIR}/deploy-${BOX_IP}"
    SSH_OPTS="-o ControlMaster=auto -o ControlPath=${CONTROL_PATH} -o ControlPersist=10m -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ConnectTimeout=10"
    SCP_OPTS="-o ControlPath=${CONTROL_PATH}"
    LAGER_OWN_MASTER=true
fi

# Wrapper for ssh -t that includes multiplexing and suppresses
# "Shared connection to X closed." noise from pseudo-terminal sessions
ssh_t() {
    local rc=0
    ssh -t $SSH_OPTS "$@" 2> >(grep -v "onnection to .* closed\." >&2) || rc=$?
    return $rc
}

# Pre-flight checks
echo -e "${BOLD}Pre-flight checks...${NC}"
echo ""

# Check if SSH is available
if ! command -v ssh &> /dev/null; then
    print_error "ssh is not installed"
    exit 1
fi
print_success "ssh is installed"

# Check if we can reach the box (basic connectivity)
print_info "Testing basic connectivity to ${BOX_IP}..."
if ! ping -c 1 -W 2 "${BOX_IP}" &> /dev/null; then
    print_warning "Cannot ping ${BOX_IP} - but will continue (ping may be blocked)"
else
    print_success "Box is reachable"
fi

# Check for git on box (required for deployment)
print_info "Checking for git on box..."
if ssh -o BatchMode=yes -o ConnectTimeout=10 "${BOX_USER}@${BOX_IP}" "command -v git &> /dev/null" 2>/dev/null; then
    print_success "git is installed on box"
else
    # Try with password auth
    if ssh -o ConnectTimeout=10 "${BOX_USER}@${BOX_IP}" "command -v git &> /dev/null" 2>/dev/null; then
        print_success "git is installed on box"
    else
        print_error "git is not installed on box"
        echo ""
        echo "Please install git on the box first:"
        echo "  ssh ${BOX_USER}@${BOX_IP}"
        echo "  sudo apt update && sudo apt install -y git"
        exit 1
    fi
fi

# Check for Docker on box (required for running containers)
print_info "Checking for Docker on box..."
if ssh -o BatchMode=yes -o ConnectTimeout=10 "${BOX_USER}@${BOX_IP}" "command -v docker &> /dev/null" 2>/dev/null; then
    print_success "Docker is installed on box"
else
    # Try with password auth
    if ssh -o ConnectTimeout=10 "${BOX_USER}@${BOX_IP}" "command -v docker &> /dev/null" 2>/dev/null; then
        print_success "Docker is installed on box"
    else
        print_warning "Docker is not installed on box - will install it now"
        echo ""
        print_info "Installing Docker on box (this may take a few minutes)..."

        # Install Docker on the box
        ssh_t "${BOX_USER}@${BOX_IP}" "
            sudo apt-get update && \
            sudo apt-get install -y docker.io docker-compose-v2 && \
            sudo systemctl enable docker && \
            sudo systemctl start docker && \
            sudo usermod -aG docker \$USER
        "

        if [ $? -eq 0 ]; then
            print_success "Docker installed successfully"
            echo ""
            print_warning "Docker group membership requires re-login to take effect"
            print_info "The script will continue, but you may need to re-run if Docker commands fail"
            echo ""
        else
            print_error "Failed to install Docker"
            echo ""
            echo "Please install Docker manually on the box:"
            echo "  ssh ${BOX_USER}@${BOX_IP}"
            echo "  sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2"
            echo "  sudo usermod -aG docker \$USER"
            echo "  # Log out and back in, then re-run this script"
            exit 1
        fi
    fi
fi

# Always ensure current user is in docker group (handles multi-user scenario)
print_info "Ensuring ${BOX_USER} is in the docker group..."
ssh_t "${BOX_USER}@${BOX_IP}" "sudo usermod -aG docker \$USER" 2>/dev/null || true

# Verify docker access works for this user
if ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "docker info >/dev/null 2>&1"; then
    print_success "${BOX_USER} has docker access"
else
    print_warning "Docker group membership may require re-login"
    print_info "If Docker commands fail, log out and back in to the box, then re-run install"
fi

# =============================================================================
# STEP 1: SSH Key Setup
# =============================================================================
print_step "Checking SSH Access"

KEY_FILE="$HOME/.ssh/lager_box"
SSH_CONFIG="$HOME/.ssh/config"
NEEDS_SSH_SETUP=false

# Check if we already have passwordless access
print_info "Testing existing SSH configuration..."
if ssh -o BatchMode=yes -o ConnectTimeout=5 "${BOX_USER}@${BOX_IP}" "echo 'test'" &>/dev/null; then
    print_success "Passwordless SSH already configured"
else
    print_warning "Passwordless SSH not configured - will set up now"
    NEEDS_SSH_SETUP=true
fi

if [ "$NEEDS_SSH_SETUP" = true ]; then
    echo ""
    echo -e "${BOLD}Setting up SSH access...${NC}"
    echo ""

    # Check if key already exists
    if [ -f "$KEY_FILE" ]; then
        print_success "SSH key already exists at $KEY_FILE"
    else
        print_info "Generating SSH key pair..."
        ssh-keygen -t ed25519 -f "$KEY_FILE" -N "" -C "lager-box-access"
        print_success "SSH key generated"
    fi
    echo ""

    # Copy public key to box
    print_info "Copying public key to box..."
    echo "You will be prompted for your box password ONE TIME:"
    echo ""

    cat "$KEY_FILE.pub" | ssh "$BOX_USER@$BOX_IP" "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"

    if [ $? -eq 0 ]; then
        echo ""
        print_success "Public key copied successfully"
    else
        echo ""
        print_error "Failed to copy public key. Please check your password and try again."
        exit 1
    fi

    # Configure SSH config
    echo ""
    print_info "Configuring SSH client..."

    # Check if entry already exists for this specific IP
    if grep -q "Host $BOX_IP$" "$SSH_CONFIG" 2>/dev/null; then
        print_success "SSH config entry already exists for $BOX_IP"
    else
        # Create SSH config if it doesn't exist
        touch "$SSH_CONFIG"
        chmod 600 "$SSH_CONFIG"

        # Find where to insert (before "Host *" if it exists, otherwise at end)
        if grep -q "^Host \*" "$SSH_CONFIG" 2>/dev/null; then
            # Insert before the "Host *" line to override global settings
            TEMP_FILE=$(mktemp)
            awk '/^Host \*/ && !inserted {
              print "# Lager Box - Auto-configured by setup_and_deploy_box.sh"
              print "# Entry for '"$BOX_IP"' ('"$BOX_USER"')"
              print "# MUST be before Host * to override global settings"
              print "Host '"$BOX_IP"'"
              print "  User '"$BOX_USER"'"
              print "  IdentityFile ~/.ssh/lager_box"
              print "  ProxyCommand none"
              print "  StrictHostKeyChecking no"
              print "  UserKnownHostsFile=/dev/null"
              print ""
              inserted=1
            }
            {print}' "$SSH_CONFIG" > "$TEMP_FILE"
            mv "$TEMP_FILE" "$SSH_CONFIG"
        else
            # Append to end of file
            cat >> "$SSH_CONFIG" << EOF

# Lager Box - Auto-configured by setup_and_deploy_box.sh
# Entry for $BOX_IP ($BOX_USER)
Host $BOX_IP
    User $BOX_USER
    IdentityFile ~/.ssh/lager_box
    ProxyCommand none
    StrictHostKeyChecking no
    UserKnownHostsFile=/dev/null
EOF
        fi

        print_success "SSH config updated"
    fi

    # Test connection
    echo ""
    print_info "Testing passwordless connection..."
    if ssh -o BatchMode=yes -o ConnectTimeout=5 "$BOX_IP" "echo 'Connection successful'" 2>/dev/null; then
        print_success "Passwordless SSH access configured successfully!"
    else
        print_warning "Connection test failed. You may need to manually verify the setup."
    fi
fi

# =============================================================================
# Establish SSH Connection Multiplexing
# =============================================================================
if [ "$LAGER_OWN_MASTER" = true ]; then
    # Clean up any stale control socket from a previous run
    ssh -O exit -o ControlPath="${CONTROL_PATH}" "${BOX_USER}@${BOX_IP}" 2>/dev/null || true

    # Establish master connection (runs in background)
    print_info "Establishing persistent SSH connection..."
    if ssh $SSH_OPTS -fN "${BOX_USER}@${BOX_IP}"; then
        print_success "SSH connection established (multiplexed)"
    else
        print_warning "Could not establish multiplexed SSH — will use individual connections"
    fi

    # Clean up master connection on exit (success or failure)
    cleanup_ssh() {
        ssh -O exit -o ControlPath="${CONTROL_PATH}" "${BOX_USER}@${BOX_IP}" 2>/dev/null || true
    }
    trap cleanup_ssh EXIT
else
    # Warm up the existing multiplexed connection
    print_info "Warming up SSH connection..."
    ssh $SSH_OPTS -o BatchMode=yes "${BOX_USER}@${BOX_IP}" "true" 2>/dev/null || true
    print_success "SSH connection ready (using existing multiplexing)"
fi

# =============================================================================
# STEP 2: Sudo Configuration
# =============================================================================
print_step "Configuring Passwordless Sudo"

# Always create/update sudoers file to ensure it has latest rules
# (Don't skip even if file exists - it might have outdated rules)
print_info "Setting up passwordless sudo (you may be prompted for password once)..."
echo ""

# Create a temporary script on the box to set up sudoers
TEMP_SCRIPT=$(mktemp)
cat > "$TEMP_SCRIPT" << SCRIPT_EOF
#!/bin/bash
echo "Creating sudoers configuration for passwordless udev management..."

# Create sudoers file (using actual username: ${BOX_USER})
sudo tee /etc/sudoers.d/lagerdata-udev > /dev/null << 'SUDOERS'
# Allow ${BOX_USER} user to manage udev rules without password
# This enables automated deployment of instrument USB permissions
${BOX_USER} ALL=(ALL) NOPASSWD: /bin/cp /tmp/*.rules /etc/udev/rules.d/
${BOX_USER} ALL=(ALL) NOPASSWD: /bin/chmod 644 /etc/udev/rules.d/*.rules
${BOX_USER} ALL=(ALL) NOPASSWD: /usr/bin/udevadm control --reload-rules
${BOX_USER} ALL=(ALL) NOPASSWD: /usr/bin/udevadm trigger
${BOX_USER} ALL=(ALL) NOPASSWD: /bin/rm -f /tmp/*.rules
# Allow ${BOX_USER} user to manage /etc/lager directory permissions
${BOX_USER} ALL=(ALL) NOPASSWD: /bin/chmod * /etc/lager
${BOX_USER} ALL=(ALL) NOPASSWD: /bin/chmod * /etc/lager/saved_nets.json
${BOX_USER} ALL=(ALL) NOPASSWD: /bin/chmod * /etc/lager/version
${BOX_USER} ALL=(ALL) NOPASSWD: /bin/chown * /etc/lager
${BOX_USER} ALL=(ALL) NOPASSWD: /bin/chown * /etc/lager/saved_nets.json
${BOX_USER} ALL=(ALL) NOPASSWD: /bin/chown * /etc/lager/version
${BOX_USER} ALL=(ALL) NOPASSWD: /usr/bin/chmod * /etc/lager
${BOX_USER} ALL=(ALL) NOPASSWD: /usr/bin/chmod * /etc/lager/saved_nets.json
${BOX_USER} ALL=(ALL) NOPASSWD: /usr/bin/chmod * /etc/lager/version
${BOX_USER} ALL=(ALL) NOPASSWD: /usr/bin/chown * /etc/lager
${BOX_USER} ALL=(ALL) NOPASSWD: /usr/bin/chown * /etc/lager/saved_nets.json
${BOX_USER} ALL=(ALL) NOPASSWD: /usr/bin/chown * /etc/lager/version
${BOX_USER} ALL=(ALL) NOPASSWD: /bin/mkdir -p /etc/lager
${BOX_USER} ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/lager/saved_nets.json
${BOX_USER} ALL=(ALL) NOPASSWD: /bin/rm -f /etc/lager/version
${BOX_USER} ALL=(ALL) NOPASSWD: /bin/mv /tmp/lager_version_tmp /etc/lager/version
# Allow ${BOX_USER} user to enable Docker service for auto-start
${BOX_USER} ALL=(ALL) NOPASSWD: /bin/systemctl enable docker
SUDOERS

# Set correct permissions
sudo chmod 440 /etc/sudoers.d/lagerdata-udev

# Validate sudoers syntax
if sudo visudo -c; then
    echo "[OK] Sudoers configuration created successfully"
else
    echo "[ERROR] Invalid sudoers syntax"
    exit 1
fi
SCRIPT_EOF

# Copy script to box and execute with -t for terminal allocation
scp $SCP_OPTS "$TEMP_SCRIPT" "${BOX_USER}@${BOX_IP}:/tmp/setup_sudo.sh" >/dev/null
ssh_t "${BOX_USER}@${BOX_IP}" "chmod +x /tmp/setup_sudo.sh && /tmp/setup_sudo.sh && rm /tmp/setup_sudo.sh"
rm "$TEMP_SCRIPT"
echo ""

# Verify setup
if ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "test -f /etc/sudoers.d/lagerdata-udev" 2>/dev/null; then
    print_success "Sudo configuration completed"
else
    print_warning "Sudo setup may have failed - deployment may require password"
fi

# Ensure /etc/lager directory exists (always check, even if sudo was already configured)
echo ""
print_info "Ensuring /etc/lager directory exists..."
if ! ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "test -d /etc/lager" 2>/dev/null; then
    print_warning "/etc/lager does not exist - creating it now (may require password)..."

    TEMP_SCRIPT=$(mktemp)
    cat > "$TEMP_SCRIPT" << 'SCRIPT_EOF'
#!/bin/bash
# Create /etc/lager directory for box configuration
# Docker containers run as www-data (UID 33), so set ownership accordingly
if [ ! -d /etc/lager ]; then
    sudo mkdir -p /etc/lager
    sudo chown -R 33:33 /etc/lager
    sudo chmod 755 /etc/lager
    echo "[OK] /etc/lager directory created (owned by www-data UID 33)"
fi

# Initialize saved_nets.json if it doesn't exist
if [ ! -f /etc/lager/saved_nets.json ]; then
    echo "[]" | sudo tee /etc/lager/saved_nets.json > /dev/null
    # Set ownership to www-data (UID 33) so container can write to it
    sudo chown 33:33 /etc/lager/saved_nets.json
    sudo chmod 644 /etc/lager/saved_nets.json
    echo "[OK] Initialized /etc/lager/saved_nets.json (owned by www-data UID 33)"
fi
SCRIPT_EOF

    scp $SCP_OPTS "$TEMP_SCRIPT" "${BOX_USER}@${BOX_IP}:/tmp/setup_lager_dir.sh" >/dev/null
    ssh_t "${BOX_USER}@${BOX_IP}" "chmod +x /tmp/setup_lager_dir.sh && /tmp/setup_lager_dir.sh && rm /tmp/setup_lager_dir.sh"
    rm "$TEMP_SCRIPT"
else
    print_success "/etc/lager directory exists"
fi

# Always ensure correct permissions (even if directory existed before)
print_info "Ensuring correct permissions on /etc/lager..."
ssh_t "${BOX_USER}@${BOX_IP}" "sudo chown -R 33:33 /etc/lager && sudo chmod 755 /etc/lager"
print_success "Permissions set correctly (owned by www-data UID 33)"

# Ensure saved_nets.json exists and is writable
if ! ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "test -f /etc/lager/saved_nets.json" 2>/dev/null; then
    print_info "Initializing /etc/lager/saved_nets.json..."
    ssh_t "${BOX_USER}@${BOX_IP}" "echo '[]' | sudo tee /etc/lager/saved_nets.json > /dev/null && sudo chown 33:33 /etc/lager/saved_nets.json && sudo chmod 644 /etc/lager/saved_nets.json"
    print_success "saved_nets.json initialized (owned by www-data UID 33)"
else
    print_success "/etc/lager/saved_nets.json exists"
    # Ensure proper permissions for Docker container access (www-data UID 33)
    ssh_t "${BOX_USER}@${BOX_IP}" "sudo chown 33:33 /etc/lager/saved_nets.json && sudo chmod 644 /etc/lager/saved_nets.json"
fi

# =============================================================================
# STEP 2.5: Configure Firewall Security
# =============================================================================
print_step "Configuring Box Firewall"

if [ "$SKIP_FIREWALL" = true ]; then
    print_info "Skipping firewall configuration (--skip-firewall flag set)"
else
    print_info "Deploying firewall configuration script to box..."

    # Copy the firewall script to the box (located in ../security/)
    scp $SCP_OPTS "${SCRIPT_DIR}/../security/secure_box_firewall.sh" "${BOX_USER}@${BOX_IP}:/tmp/secure_box_firewall.sh" >/dev/null

    # Build firewall script arguments
    FIREWALL_ARGS=""
    if [ -n "$CORPORATE_VPN" ]; then
        FIREWALL_ARGS="--corporate-vpn $CORPORATE_VPN"
    fi

    print_info "Running firewall configuration on box..."
    echo ""

    # Run the firewall script on the box
    # Note: Cannot capture output because sudo may prompt for password via PTY
    ssh_t "${BOX_USER}@${BOX_IP}" "chmod +x /tmp/secure_box_firewall.sh && sudo /tmp/secure_box_firewall.sh $FIREWALL_ARGS && rm /tmp/secure_box_firewall.sh"

    echo ""
    print_success "Firewall configuration completed"
fi

# =============================================================================
# STEP 3: Deploy Box Code
# =============================================================================
print_step "Deploying Box Code"

BOX_SRC="${SCRIPT_DIR}/../../box/"
UDEV_RULES_DIR="${SCRIPT_DIR}/../../box/udev_rules"

    # ==========================================================================
    # Sparse Checkout Deployment via HTTPS (no authentication needed)
    # ==========================================================================
    print_info "Deploying via git sparse-checkout (branch: ${GIT_BRANCH})..."
    echo ""

    # Verify GitHub HTTPS connectivity (with timeout to prevent hangs)
    print_info "Checking GitHub connectivity..."
    if ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "timeout 30 git ls-remote --exit-code https://github.com/lagerdata/lager.git HEAD" >/dev/null 2>&1; then
        print_success "GitHub is reachable via HTTPS"
    else
        # Re-run without suppression so SSH errors are visible
        print_error "Cannot reach GitHub from the box"
        ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "timeout 30 git ls-remote --exit-code https://github.com/lagerdata/lager.git HEAD" 2>&1 || true
        echo "The box needs internet access to clone from GitHub."
        echo "Check the box's network connectivity and DNS resolution."
        exit 1
    fi

    # Check if ~/box already exists and is a git repo
    HAS_GIT_REPO=$(ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "test -d ~/box/.git && echo 'yes' || echo 'no'" 2>/dev/null) || {
        print_error "SSH connection failed while checking box state"
        echo "  The SSH connection to the box was lost."
        echo "  This may be due to network instability. Please try again."
        exit 1
    }

    # Also check if essential files exist (repo might be corrupted from previous flattening)
    HAS_START_SCRIPT=$(ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "test -f ~/box/start_box.sh && echo 'yes' || echo 'no'" 2>/dev/null) || {
        print_error "SSH connection failed while checking box state"
        exit 1
    }
    HAS_BOX_SUBDIR=$(ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "test -d ~/box/box && echo 'yes' || echo 'no'" 2>/dev/null) || {
        print_error "SSH connection failed while checking box state"
        exit 1
    }

    # If repo exists but is corrupted (no start_box.sh and no box/ subdir), force fresh clone
    if [ "$HAS_GIT_REPO" = "yes" ] && [ "$HAS_START_SCRIPT" = "no" ] && [ "$HAS_BOX_SUBDIR" = "no" ]; then
        print_warning "Existing repository is corrupted (missing essential files)"
        print_info "Removing corrupted repository and doing fresh clone..."
        ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "rm -rf ~/box" 2>/dev/null || true
        HAS_GIT_REPO="no"
    fi

    if [ "$HAS_GIT_REPO" = "yes" ]; then
        print_info "Existing git repository found - will update instead of re-clone"
        echo ""

        # Update existing sparse checkout (discard any local changes)
        # Re-configure sparse checkout to ensure box directory is included
        ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "
            cd ~/box && \
            git sparse-checkout set box && \
            git fetch origin && \
            git reset --hard HEAD && \
            git clean -fd && \
            git checkout ${GIT_BRANCH} && \
            git reset --hard origin/${GIT_BRANCH}
        "

        # After update, check if box/ subdirectory exists and flatten if needed
        if ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "test -d ~/box/box" 2>/dev/null; then
            print_info "Flattening directory structure..."
            ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "
                cd ~/box && \
                shopt -s dotglob && \
                mv box/* . && \
                rmdir box
            "
        fi

        print_success "Existing repository updated to ${GIT_BRANCH}"
    else
        # Remove old box directory if exists (rsync-based or corrupted)
        print_info "Removing old box directory (if exists)..."
        ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "rm -rf ~/box" 2>/dev/null || true

        # Clone with sparse checkout
        print_info "Cloning repository with sparse checkout..."
        echo ""
        ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "
            git clone --filter=blob:none --no-checkout https://github.com/lagerdata/lager.git ~/box && \
            cd ~/box && \
            git sparse-checkout init --cone && \
            git sparse-checkout set box && \
            git checkout ${GIT_BRANCH}
        "

        # Check if box/ directory exists after checkout
        if ! ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "test -d ~/box/box" 2>/dev/null; then
            print_error "The 'box/' directory does not exist on branch '${GIT_BRANCH}'"
            echo ""
            echo "This usually means the branch doesn't have the expected directory structure."
            echo ""
            echo "Try running with a different branch:"
            echo "  $0 ${BOX_IP} --sparse --branch restructure"
            echo "  $0 ${BOX_IP} --sparse --branch main"
            echo ""
            exit 1
        fi

        # Flatten directory structure: move box/* to root
        print_info "Flattening directory structure..."
        ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "
            cd ~/box && \
            shopt -s dotglob && \
            mv box/* . && \
            rmdir box
        "

        print_success "Repository cloned with sparse checkout"
    fi

    echo ""
    print_success "Box code deployed via sparse checkout (branch: ${GIT_BRANCH})"

# Deploy udev rules (applies to both deployment methods)
echo ""
print_info "Deploying udev rules..."
if [ -d "${UDEV_RULES_DIR}" ]; then
    # Deploy all .rules files from udev_rules directory
    RULES_COUNT=$(find "${UDEV_RULES_DIR}" -name "*.rules" 2>/dev/null | wc -l)
    if [ "$RULES_COUNT" -gt 0 ]; then
        echo "Found $RULES_COUNT udev rule(s) to deploy"

        # Copy all rules files to /tmp on box
        scp $SCP_OPTS "${UDEV_RULES_DIR}"/*.rules "${BOX_USER}@${BOX_IP}:/tmp/"

        # Install rules and reload udev
        ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "
            echo 'Installing udev rules...'
            sudo cp /tmp/*.rules /etc/udev/rules.d/
            sudo chmod 644 /etc/udev/rules.d/*.rules
            sudo udevadm control --reload-rules
            sudo udevadm trigger
            echo 'Deployed udev rules:'
            ls -1 /tmp/*.rules | xargs -n1 basename
            rm -f /tmp/*.rules
            echo '[OK] Udev rules deployed and activated'
        "
        print_success "Udev rules deployed successfully"
    else
        print_warning "No .rules files found in ${UDEV_RULES_DIR}"
    fi
else
    print_warning "udev_rules directory not found at ${UDEV_RULES_DIR}"
fi


# =============================================================================
# Helper: Download and install J-Link using SEGGER's debian package
# =============================================================================
download_jlink_on_box() {
    local jlink_version="$1"

    print_info "Installing J-Link software on box..."
    print_warning "Note: J-Link is proprietary software from SEGGER"
    echo ""
    echo "By installing J-Link, you agree to SEGGER's license terms:"
    echo "  https://www.segger.com/products/debug-probes/j-link/tools/terms-of-use/"
    echo ""
    echo "Key points:"
    echo "  • Free to use with genuine SEGGER products"
    echo "  • May only be used with SEGGER J-Link hardware"
    echo "  • Not for use with counterfeit/clone products"
    echo ""

    # Install J-Link using the .deb package directly on box
    # SEGGER provides a stable deb package that can be downloaded without authentication
    ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "BOX_USER=${BOX_USER}" bash << 'REMOTE_SCRIPT'
        set -e

        mkdir -p "/home/${BOX_USER}/third_party"
        cd /tmp

        echo "Downloading J-Link debian package..."

        # Download the .deb package (this URL is stable and doesn't require authentication)
        DEB_URL="https://www.segger.com/downloads/jlink/JLink_Linux_x86_64.deb"

        if command -v wget &> /dev/null; then
            wget --post-data="accept_license_agreement=accepted" -q --show-progress -O JLink.deb "$DEB_URL" 2>&1 || {
                echo "Download failed - trying alternative method..."
                # Try without post data (sometimes it works)
                wget -q --show-progress -O JLink.deb "$DEB_URL" 2>&1
            }
        elif command -v curl &> /dev/null; then
            curl -L -d "accept_license_agreement=accepted" -# -o JLink.deb "$DEB_URL" 2>&1 || {
                echo "Download failed - trying alternative method..."
                curl -L -# -o JLink.deb "$DEB_URL" 2>&1
            }
        else
            echo "Error: Neither wget nor curl is available"
            exit 1
        fi

        if [ ! -f JLink.deb ] || [ ! -s JLink.deb ]; then
            echo "Download failed - file is empty or missing"
            exit 1
        fi

        echo "Extracting J-Link from debian package..."

        # Use dpkg-deb if available (standard on Debian/Ubuntu), otherwise fall back to ar
        if command -v dpkg-deb &> /dev/null; then
            dpkg-deb -x JLink.deb extracted

            if [ -d extracted/opt/SEGGER ]; then
                # Find the J-Link directory (version may vary)
                JLINK_DIR=$(find extracted/opt/SEGGER -maxdepth 1 -type d -name "JLink*" | head -n 1)
                if [ -n "$JLINK_DIR" ]; then
                    mv "$JLINK_DIR" "/home/${BOX_USER}/third_party/"
                    echo "J-Link installed successfully to /home/${BOX_USER}/third_party/$(basename $JLINK_DIR)"
                    cd /tmp
                    rm -rf extracted JLink.deb
                    exit 0
                else
                    echo "Error: Could not find J-Link directory in package"
                    rm -rf extracted JLink.deb
                    exit 1
                fi
            else
                echo "Error: Package extraction failed - opt/SEGGER directory not found"
                rm -rf extracted JLink.deb
                exit 1
            fi
        elif command -v ar &> /dev/null; then
            # Fall back to ar if dpkg-deb is not available
            ar x JLink.deb

            # Debian packages can use either .tar.gz or .tar.xz compression
            # Extract only the opt/SEGGER directory to avoid permission errors
            if [ -f data.tar.xz ]; then
                tar xJf data.tar.xz ./opt/SEGGER 2>&1 | grep -v "Cannot utime\|Cannot change mode" || true
            elif [ -f data.tar.gz ]; then
                tar xzf data.tar.gz ./opt/SEGGER 2>&1 | grep -v "Cannot utime\|Cannot change mode" || true
            else
                echo "Error: Could not find data.tar.gz or data.tar.xz"
                exit 1
            fi

            # Move J-Link to third_party directory
            if [ -d opt/SEGGER ]; then
                # Find the J-Link directory (version may vary)
                JLINK_DIR=$(find opt/SEGGER -maxdepth 1 -type d -name "JLink*" | head -n 1)
                if [ -n "$JLINK_DIR" ]; then
                    mv "$JLINK_DIR" "/home/${BOX_USER}/third_party/"
                    echo "J-Link installed successfully to /home/${BOX_USER}/third_party/$(basename $JLINK_DIR)"
                else
                    echo "Error: Could not find J-Link directory in package"
                    exit 1
                fi
            else
                echo "Error: Package extraction failed - opt/SEGGER directory not found"
                exit 1
            fi

            # Cleanup
            cd /tmp
            rm -f JLink.deb control.tar.* data.tar.* debian-binary
            rm -rf opt etc usr var
        else
            echo "Error: Neither dpkg-deb nor ar is available for extracting .deb package"
            echo "Please install dpkg (standard) or binutils package"
            exit 1
        fi

REMOTE_SCRIPT

    return $?
}

# =============================================================================
# STEP 4: J-Link Installation (Optional)
# =============================================================================
print_step "Installing J-Link (Optional)"

JLINK_VERSION="V794a"

if [ "$SKIP_JLINK" = true ]; then
    print_info "Skipping J-Link installation (--skip-jlink flag set)"
else
    # Check if J-Link is already on box (check for any version, not just hardcoded)
    print_info "Checking if J-Link is already installed on box..."

    # First check if any J-Link executable exists (any version)
    EXISTING_JLINK=$(ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "find /home/${BOX_USER}/third_party -name JLinkGDBServerCLExe 2>/dev/null | head -n 1" || echo "")

    if [ -n "$EXISTING_JLINK" ]; then
        # J-Link already extracted and installed
        INSTALLED_DIR=$(dirname "$EXISTING_JLINK")
        print_success "J-Link already installed on box at: $(basename $INSTALLED_DIR)"
    else
        # Check if any J-Link tarball exists but needs extraction
        EXISTING_TGZ=$(ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "find /home/${BOX_USER}/third_party -name 'JLink_Linux_*.tgz' 2>/dev/null | head -n 1" || echo "")

        if [ -n "$EXISTING_TGZ" ]; then
            # Found tarball - validate and extract it
            print_info "Found J-Link tarball on box: $(basename $EXISTING_TGZ)"
            print_info "Validating tarball..."

            # Validate the tarball is a valid gzip file
            if ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "gzip -t $EXISTING_TGZ 2>/dev/null"; then
                print_info "Extracting J-Link..."
                ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "cd /home/${BOX_USER}/third_party && tar xzf $(basename $EXISTING_TGZ)"

                # Verify extraction
                EXTRACTED_JLINK=$(ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "find /home/${BOX_USER}/third_party -name JLinkGDBServerCLExe 2>/dev/null | head -n 1" || echo "")
                if [ -n "$EXTRACTED_JLINK" ]; then
                    print_success "J-Link extracted successfully"
                else
                    print_warning "J-Link extraction may have failed - will download fresh"
                    # Remove corrupted tarball and download fresh
                    ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "rm -f $EXISTING_TGZ"
                    EXISTING_TGZ=""  # Clear variable to trigger download
                fi
            else
                print_warning "Tarball is corrupted - removing and will download fresh"
                ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "rm -f $EXISTING_TGZ"
                EXISTING_TGZ=""  # Clear variable to trigger download
            fi
        fi

        # Download if no valid tarball exists
        if [ -z "$EXISTING_TGZ" ]; then
            # No J-Link found on box - download it automatically
            print_info "J-Link not found on this box - will download and install"
            echo ""

            # Download and install J-Link on the box using debian package
            if download_jlink_on_box "$JLINK_VERSION"; then
                # Verify installation
                INSTALLED_JLINK=$(ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "find /home/${BOX_USER}/third_party -name JLinkGDBServerCLExe 2>/dev/null | head -n 1" || echo "")
                if [ -n "$INSTALLED_JLINK" ]; then
                    echo ""
                    print_success "J-Link installed successfully on box"
                else
                    print_warning "J-Link installation verification failed"
                    print_info "Continuing with pyOCD (already installed)"
                fi
            else
                print_warning "J-Link download failed"
                echo ""
                print_info "This is OK - debug commands will use pyOCD instead"
                print_info "pyOCD is open-source and works with J-Link hardware"
                echo ""
            fi
        fi
    fi
fi

# =============================================================================
# STEP 4.5: Install pyOCD (Open Source Debug Tool)
# =============================================================================
print_step "Installing pyOCD (Open Source)"

# pyOCD is an open-source alternative to J-Link that supports CMSIS-DAP, ST-Link, and other debug probes
# It's installed automatically via pip

print_info "Checking if pyOCD is already installed..."
if ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "python3 -c 'import pyocd' 2>/dev/null" ; then
    PYOCD_VERSION=$(ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "python3 -c 'import pyocd; print(pyocd.__version__)' 2>/dev/null" || echo "unknown")
    print_success "pyOCD already installed (version: ${PYOCD_VERSION})"
else
    print_info "Installing pyOCD and yoctopuce via pip..."
    echo ""

    # Install pyOCD and yoctopuce using pip3
    ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "pip3 install --user yoctopuce" 2>&1 | grep -v "Defaulting to user installation" || true
    if ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "pip3 install --user 'pyocd>=0.36.0'" 2>&1 | grep -q "Successfully installed\|Requirement already satisfied" ; then
        echo ""
        PYOCD_VERSION=$(ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "python3 -c 'import pyocd; print(pyocd.__version__)' 2>/dev/null" || echo "installed")
        print_success "pyOCD installed successfully (version: ${PYOCD_VERSION})"
        echo ""
        print_info "pyOCD provides debug support for:"
        echo "  - CMSIS-DAP debug probes"
        echo "  - ST-Link debuggers"
        echo "  - J-Link (if hardware is present)"
        echo "  - 70+ ARM Cortex-M microcontrollers"
    else
        print_warning "pyOCD installation encountered issues"
        echo ""
        echo "  You can manually install it later:"
        echo "    ssh ${BOX_USER}@${BOX_IP} 'pip3 install --user pyocd'"
        echo ""
    fi
fi

# =============================================================================
# STEP 5: Start Docker Containers
# =============================================================================
print_step "Starting Docker Containers"

print_info "Ensuring Docker service is enabled (for auto-start on boot)..."
ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "sudo systemctl enable docker >/dev/null 2>&1 || true"
print_success "Docker service enabled"

print_info "Stopping and removing all existing containers..."
# Update restart policy first to prevent auto-restart, then force remove all containers
ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "
    # Disable auto-restart on all containers first
    docker update --restart=no \$(docker ps -aq) 2>/dev/null || true
    # Stop all running containers
    docker stop \$(docker ps -q) 2>/dev/null || true
    # Remove all containers (running and stopped)
    docker rm -f \$(docker ps -aq) 2>/dev/null || true
    # Prune any leftover containers
    docker container prune -f 2>/dev/null || true
    # Wait a moment for Docker to clean up
    sleep 2
" 2>/dev/null || true
print_success "All containers cleaned up"

print_info "Cleaning up Docker images and build cache to free disk space..."
echo ""
ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "
    echo 'Removing unused Docker images...'
    docker image prune -af 2>/dev/null || true
    echo 'Removing Docker build cache...'
    docker builder prune -af 2>/dev/null || true
    echo 'Cleanup complete'
" 2>/dev/null || true
echo ""
print_success "Docker images and build cache cleaned up"

print_info "Checking available disk space..."
ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "df -h / | tail -n 1 | awk '{print \"Available: \" \$4 \" (\" \$5 \" used)\"}'" 2>/dev/null || true
echo ""

# Configure VPN interface if specified
if [ -n "$VPN_INTERFACE" ]; then
    print_info "Configuring VPN interface: $VPN_INTERFACE"
    ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "echo 'LAGER_WG_IFACE=${VPN_INTERFACE}' > /home/${BOX_USER}/.env"
    print_success "VPN interface configured: $VPN_INTERFACE"
    echo ""
fi

print_info "Building and starting containers..."
echo ""
ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "cd ~/box && chmod +x start_box.sh && ./start_box.sh"

echo ""
print_success "Docker containers started successfully"

# =============================================================================
# Post-Deployment Verification
# =============================================================================
if [ "$SKIP_VERIFY" = false ]; then
    echo ""
    echo -e "${BOLD}${BLUE}Post-Deployment Verification${NC}"
    echo "----------------------------------------"

    # Check container status (new setup uses single 'lager' container)
    print_info "Checking container status..."
    echo ""
    ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "docker ps --filter 'name=lager' --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
    echo ""

    # Count running containers
    RUNNING_CONTAINERS=$(ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "docker ps --filter 'name=lager' --format '{{.Names}}' | wc -l")

    if [ "$RUNNING_CONTAINERS" -ge 1 ]; then
        print_success "Lager container is running"
    else
        print_warning "Expected 1 container (lager), but found ${RUNNING_CONTAINERS} running"
    fi

    # Verify restart policies
    echo ""
    print_info "Verifying auto-restart configuration..."
    ssh $SSH_OPTS "${BOX_USER}@${BOX_IP}" "cd ~/box && ./verify_restart_policy.sh" || true

    # Test lager connectivity (if lager CLI is available)
    if command -v lager &> /dev/null; then
        echo ""
        print_info "Testing Lager CLI connectivity..."

        # Try to connect using lager hello
        if timeout 10 lager hello --box "${BOX_IP}" &>/dev/null; then
            print_success "Lager CLI can communicate with box"
        else
            print_warning "Lager CLI connection test failed (you may need to add box to .lager config)"
        fi
    fi
fi

# =============================================================================
# Success Summary
# =============================================================================
echo ""
echo -e "${BOLD}${GREEN}=========================================${NC}"
echo -e "${BOLD}${GREEN}  Deployment Complete!${NC}"
echo -e "${BOLD}${GREEN}=========================================${NC}"
echo ""
echo -e "${GREEN}[OK]${NC} Box is ready to use at: ${BOX_USER}@${BOX_IP}"
echo -e "${GREEN}[OK]${NC} Deployed with sparse checkout (branch: ${GIT_BRANCH})"
echo -e "${GREEN}[OK]${NC} 'lager update' is available for future updates"
echo ""

# Next steps
echo -e "${BOLD}Next Steps:${NC}"
echo ""
echo "1. Add box to your local .lager configuration:"
echo -e "   ${BLUE}cd your-project-directory${NC}"
echo -e "   ${BLUE}lager boxes add --name my-box --ip ${BOX_IP}${NC}"
echo ""
echo "2. Test connectivity:"
echo -e "   ${BLUE}lager hello --box ${BOX_IP}${NC}"
echo ""
echo "3. List available instruments (if connected):"
echo -e "   ${BLUE}lager instruments --box ${BOX_IP}${NC}"
echo ""
echo "4. Create nets for your hardware:"
echo -e "   ${BLUE}lager nets create <net-name> <net-type> <channel> <address> --box ${BOX_IP}${NC}"
echo ""

echo "5. Update box code in the future:"
echo -e "   ${BLUE}lager update --box ${BOX_IP}${NC}"
echo -e "   ${BLUE}lager update --box ${BOX_IP} --version ${GIT_BRANCH}${NC}"
echo ""

# Offer to add to .lager config (unless --skip-add-box was passed)
if [ "$SKIP_ADD_BOX" != "true" ] && [ -f ".lager" ]; then
    echo -e "${YELLOW}Would you like to add this box to .lager in the current directory?${NC}"
    echo -n "Enter box name (or press Enter to skip): "
    read BOX_NAME

    if [ -n "$BOX_NAME" ]; then
        # Check if lager CLI is available
        if command -v lager &> /dev/null; then
            if lager boxes add --name "$BOX_NAME" --ip "${BOX_IP}" 2>/dev/null; then
                print_success "Added '${BOX_NAME}' to .lager configuration"
                echo ""
                echo "You can now use:"
                echo -e "   ${BLUE}lager hello --box ${BOX_NAME}${NC}"
            else
                print_warning "Failed to add to .lager - you may need to add manually"
            fi
        else
            print_warning "Lager CLI not found - add manually to .lager"
        fi
    fi
fi

echo ""
echo -e "${GREEN}Happy testing!${NC}"
echo ""
