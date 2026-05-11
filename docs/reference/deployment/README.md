# Box Deployment Scripts

Scripts for deploying, securing, and maintaining Lager boxes.

## Directory Structure

Deployment scripts are packaged with the CLI and live in `cli/deployment/`:

```
cli/deployment/
├── scripts/                     # Main deployment scripts
│   ├── setup_and_deploy_box.sh  # Primary box setup and deployment
│   ├── setup_ssh_key.sh         # SSH key setup for box access
│   └── convert_to_sparse_checkout.sh  # Fix directory structure issues
└── security/                    # Security configuration
    └── secure_box_firewall.sh   # UFW firewall setup
```

Cloud-init configs and additional docs live alongside this README in `docs/reference/deployment/`:

```
docs/reference/deployment/
├── README.md                    # This file
├── deploy_process.txt           # Quick setup guide for new boxes
└── cloud-init/                  # Cloud-init configurations
    ├── user-data                # Autoinstall configuration for new boxes
    └── user-data.example        # Template with placeholder values
```

## When to Use Each Script

| Script | Use When... |
|--------|-------------|
| `scripts/setup_and_deploy_box.sh` | Setting up a **new box** or doing a **full redeploy** from your local machine |
| `scripts/convert_to_sparse_checkout.sh` | `lager update` fails with "Not found" errors or box has nested directory structure |
| `security/secure_box_firewall.sh` | Configuring firewall on a box (usually called by `setup_and_deploy_box.sh`) |

## Deployment Workflows

### New Box Setup (First Time)

Use `setup_and_deploy_box.sh` for initial box setup:

```bash
# Using the CLI (recommended):
lager install --ip <box-ip>

# Or run the script directly:
cli/deployment/scripts/setup_and_deploy_box.sh <box-ip>
```

This script handles everything:
- SSH key setup (passwordless access)
- Sudo configuration
- Code deployment via git sparse-checkout (HTTPS, no authentication needed)
- Firewall configuration
- pyOCD and J-Link installation
- Docker container startup

You'll be prompted for the box password once during initial setup.

### Subsequent Updates

After initial setup, use the `lager update` CLI command:

```bash
# Update box to latest code on a branch
lager update --box <box> --version <branch> --yes

# Examples
lager update --box my-box --version main --yes
lager update --box <BOX_IP> --version staging --yes
```

`lager update` works out of the box -- no authentication is needed since the repository is public and uses HTTPS.

---

## Script Reference

### `scripts/setup_and_deploy_box.sh` - Primary Deployment

**When to use:**
- Setting up a brand new box
- Full redeploy after major changes
- When you have the full lager repo locally

**Usage:**
```bash
./scripts/setup_and_deploy_box.sh <box-ip> [OPTIONS]
```

**Options:**
| Option | Description |
|--------|-------------|
| `--user <username>` | Box username (default: lagerdata) |
| `--version <ref>` | Lager git tag or branch to deploy (default: main) |
| `--vpn <iface>` | VPN interface to bind services to (auto-detects Tailscale/WireGuard) |
| `--corporate-vpn <iface>` | Corporate VPN interface for firewall (e.g., tun0) |
| `--skip-firewall` | Skip firewall configuration |
| `--skip-jlink` | Skip J-Link installation |
| `--jlink-version <ver>` | Pin J-Link to a specific SEGGER version (default: latest) |
| `--skip-verify` | Skip post-deployment verification |
| `--skip-add-box` | Skip the prompt to add the box to the local `.lager` config |

**Examples:**
```bash
# Standard deployment
./scripts/setup_and_deploy_box.sh <BOX_IP>

# With corporate VPN firewall rules
./scripts/setup_and_deploy_box.sh <BOX_IP> --corporate-vpn tun0

# Different username
./scripts/setup_and_deploy_box.sh <BOX_IP> --user pi
```

**What it does:**
1. Configures SSH keys for passwordless access
2. Sets up passwordless sudo for deployment commands
3. Configures UFW firewall (restricts Lager ports to VPN/localhost)
4. Clones box code via git sparse-checkout (HTTPS)
5. Deploys udev rules for USB instruments
6. Installs pyOCD (open source debug tool)
7. Installs J-Link (if available)
8. Builds and starts Docker containers

---

### `scripts/convert_to_sparse_checkout.sh` - Fix Directory Structure

**When to use:**
- `lager update` returns "Not found" errors
- `lager binaries` commands fail with "Not found"
- Box has nested `~/box/box/` directory structure

**Usage:**
```bash
./scripts/convert_to_sparse_checkout.sh <box-ip> [branch]
```

**Examples:**
```bash
# Convert to main branch
./scripts/convert_to_sparse_checkout.sh <BOX_IP> main

# Convert to specific branch
./scripts/convert_to_sparse_checkout.sh <BOX_IP> staging
```

**What it does:**
1. Stops all Docker containers
2. Removes the old `~/box` directory
3. Clones with sparse checkout (box code only)
4. Flattens the directory structure
5. Restarts containers

**Why this is needed:**

Some boxes end up with a nested structure where git pulls code to `~/box/box/` but Docker builds from `~/box/lager/`. This causes the container to run old code. The script fixes this by ensuring `~/box/lager/` contains the git-tracked code directly.

---

### `security/secure_box_firewall.sh` - Firewall Configuration

**When to use:**
- Manually configuring firewall (usually not needed - `setup_and_deploy_box.sh` calls this)
- Reconfiguring firewall after network changes
- Adding corporate VPN interface to allowed list

**Usage:**
```bash
# On the box (requires sudo)
sudo ./secure_box_firewall.sh [--corporate-vpn <iface>]
```

**Example:**
```bash
# Copy to box and run
scp security/secure_box_firewall.sh lagerdata@<BOX_IP>:/tmp/
ssh lagerdata@<BOX_IP> 'sudo /tmp/secure_box_firewall.sh'

# With corporate VPN
ssh lagerdata@<BOX_IP> 'sudo /tmp/secure_box_firewall.sh --corporate-vpn tun0'
```

**What it does:**
- Sets default DENY policy for incoming traffic
- Allows SSH (port 22) from anywhere
- Restricts Lager ports (5000, 8301, 8765) to:
  - Tailscale VPN (tailscale0)
  - Corporate VPN (if specified)
  - Docker bridge (docker0)
  - Localhost (lo)
- Explicitly blocks Lager ports from external networks

---

## Cloud-Init Configuration

The `cloud-init/` directory contains configuration files for automated Ubuntu installation on new boxes.

### `cloud-init/user-data` - Autoinstall Configuration

This file is used with Ubuntu Server's autoinstall feature for automated box provisioning.

**Usage:**
1. Format a USB drive as FAT32 with label "CIDATA"
2. Copy `cloud-init/user-data` to the USB drive
3. Create an empty `meta-data` file on the USB drive
4. Boot the box from Ubuntu installer USB
5. Insert the CIDATA USB when prompted

See `deploy_process.txt` (in this directory) for detailed step-by-step instructions.

---

## Additional Documentation

### `docs/deploy_process.txt` - Quick Setup Guide

A step-by-step guide for setting up new Lager boxes from scratch, including:
- USB drive preparation
- Ubuntu installation
- Tailscale configuration
- Lager deployment
- WiFi setup
- Box renaming

---

## Troubleshooting

### `lager update` returns "Not found" errors

This usually means the box has a nested directory structure. Fix with:

```bash
cli/deployment/scripts/convert_to_sparse_checkout.sh <box-ip> <branch>
lager update --box <box> --version <branch> --yes
```

### Cannot connect to box services

1. Check firewall status:
   ```bash
   ssh lagerdata@<box-ip> 'sudo ufw status verbose'
   ```

2. Verify you're connected via VPN (Tailscale or corporate)

3. Check containers are running:
   ```bash
   ssh lagerdata@<box-ip> "docker ps"
   ```

### SSH connection issues

```bash
# Test passwordless access
ssh -o BatchMode=yes lagerdata@<box-ip> "echo test"

# If it fails, re-run deployment to fix SSH keys
lager install --ip <box-ip>
```

### Container startup failures

```bash
# Check container logs
ssh lagerdata@<box-ip> "docker logs controller"
ssh lagerdata@<box-ip> "docker logs python"

# Check disk space
ssh lagerdata@<box-ip> "df -h"

# Manual restart
ssh lagerdata@<box-ip> "cd ~/box && ./start_box.sh"
```

---

## Requirements

### Local Machine
- `ssh` client installed
- Network access to box (Tailscale or local network)
- lager repository cloned

### Box Device
- Ubuntu/Debian Linux
- Docker installed
- User account with sudo access (default: `lagerdata`)
- Network connectivity (Tailscale recommended)

---

## Quick Reference

```bash
# New box setup (recommended)
lager install --ip <ip>

# Or run deployment script directly
cli/deployment/scripts/setup_and_deploy_box.sh <ip>

# Update existing box
lager update --box <box> --version <branch> --yes

# Configure firewall
scp cli/deployment/security/secure_box_firewall.sh lagerdata@<ip>:/tmp/
ssh lagerdata@<ip> 'sudo /tmp/secure_box_firewall.sh'

# Fix "Not found" errors
cli/deployment/scripts/convert_to_sparse_checkout.sh <ip> <branch>
```
