#!/bin/bash
# setup_and_deploy_box_mac.sh — install the native macOS Lager box.
#
# This is the macOS analogue of setup_and_deploy_box.sh. It runs over SSH from
# a developer's machine (or locally) and installs everything needed to turn
# an idle Apple Silicon MacBook into a Lager Box:
#
#   - Creates the dedicated `lagerdata` macOS user
#   - Installs Homebrew, Python 3.12, libusb, hidapi
#   - Sets up the box state directory at /Library/Application Support/Lager/
#   - Clones the open-source-lager repo
#   - Creates a Python venv and installs the box's Python deps
#   - Installs Tier-2 vendor SDKs:
#       * LabJack LJM (.pkg from labjack.com)
#       * SEGGER J-Link (.pkg from segger.com)
#       * TotalPhase Aardvark (.zip from totalphase.com — login required)
#       * Nordic nrfutil (binary from GitHub releases)
#   - Drops the launchd LaunchDaemon at /Library/LaunchDaemons/com.lager.box.plist
#   - Bootstraps the daemon so the box services come up immediately and on every boot
#
# The script must be run by an admin macOS user with sudo. It will prompt
# once for the macOS password and reuse the cached credential for subsequent
# privileged commands. After the script completes, the box runs as `lagerdata`
# (NOT as the admin user that ran the installer).
#
# Tier-1 unsupported instruments on macOS (no vendor support, will not work):
#   - MCC USB-202 (uldaq is Linux-only)
#   - Picoscope 2000 (Pico has no macOS PicoSDK download)
#
# Usage:
#   ./setup_and_deploy_box_mac.sh [--repo-branch BRANCH] [--skip-vendor]
#
#   --repo-branch BRANCH   Branch of open-source-lager to check out (default: mac-box)
#   --skip-vendor          Skip vendor SDK installs (LJM, J-Link, Aardvark, nrfutil)
#                          for Tier-3-only deployments

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LAGER_USER="lagerdata"
LAGER_GROUP="staff"
LAGER_FULLNAME="Lager Box"

LAGER_STATE_DIR="/Library/Application Support/Lager"
LAGER_REPO_DIR="${LAGER_STATE_DIR}/repo"
LAGER_VENV="${LAGER_STATE_DIR}/venv"
LAGER_BIN_DIR="${LAGER_STATE_DIR}/bin"
LAGER_LOG_DIR="/Library/Logs/Lager"

REPO_URL="https://github.com/lagerdata/lager.git"
REPO_BRANCH="mac-box"

PLIST_LABEL="com.lager.box"
PLIST_DEST="/Library/LaunchDaemons/${PLIST_LABEL}.plist"

NRFUTIL_URL="https://github.com/NordicSemiconductor/pc-nrfutil/releases/download/v6.1.7/nrfutil-mac"
LJM_INSTALLER_URL="https://files.labjack.com/installers/LJM/macOS/LabJack-LJM_2024-06-10.zip"
JLINK_INSTALLER_URL="https://www.segger.com/downloads/jlink/JLink_MacOSX_arm64.pkg"

SKIP_VENDOR=0
AARDVARK_ZIP=""

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --repo-branch)
            REPO_BRANCH="$2"
            shift 2
            ;;
        --skip-vendor)
            SKIP_VENDOR=1
            shift
            ;;
        --repo-url)
            REPO_URL="$2"
            shift 2
            ;;
        --aardvark-zip)
            AARDVARK_ZIP="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '2,50p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { printf '\033[1;34m[lager-mac-install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[lager-mac-install] WARNING:\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[lager-mac-install] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

require_macos() {
    if [ "$(uname -s)" != "Darwin" ]; then
        fail "This script must be run on macOS (uname -s = $(uname -s))"
    fi
    local major
    major=$(sw_vers -productVersion | cut -d. -f1)
    if [ "$major" -lt 12 ]; then
        fail "macOS 12 (Monterey) or newer required. Detected: $(sw_vers -productVersion)"
    fi
    if [ "$(uname -m)" != "arm64" ]; then
        warn "Detected $(uname -m) — Apple Silicon (arm64) is the supported architecture."
    fi
}

require_sudo() {
    if ! sudo -n true 2>/dev/null; then
        log "Requesting sudo privileges (needed for user creation, vendor SDK installs, and launchd plist)..."
        sudo -v || fail "Could not obtain sudo. Re-run as an admin user."
    fi
    # Refresh sudo credential in the background while we work.
    ( while true; do sudo -n true; sleep 50; kill -0 "$$" || exit; done 2>/dev/null ) &
    SUDO_REFRESH_PID=$!
    trap 'kill $SUDO_REFRESH_PID 2>/dev/null || true' EXIT
}

run_as_lager() {
    sudo -u "$LAGER_USER" -H "$@"
}

# ---------------------------------------------------------------------------
# Phase 1: Preflight
# ---------------------------------------------------------------------------

log "Phase 1/9: preflight checks"
require_macos
require_sudo

# ---------------------------------------------------------------------------
# Phase 2: Create the lagerdata user
# ---------------------------------------------------------------------------

log "Phase 2/9: ensuring '${LAGER_USER}' user exists"
if id "$LAGER_USER" >/dev/null 2>&1; then
    log "  user '${LAGER_USER}' already exists — skipping creation"
else
    log "  creating '${LAGER_USER}' (hidden from loginwindow, but SSH-accessible)"
    LAGER_PASSWORD=$(openssl rand -base64 32)
    sudo sysadminctl -addUser "$LAGER_USER" \
        -fullName "$LAGER_FULLNAME" \
        -password "$LAGER_PASSWORD" \
        -home "/Users/${LAGER_USER}" \
        -shell /bin/bash
    # Hide the account from the macOS login window so the MacBook's login UI
    # doesn't show a "Lager Box" user, but keep the account fully functional
    # for SSH and `sudo -u lagerdata`. Hiding the account does NOT disable
    # `ssh lagerdata@box` with key auth or `lager ssh --box`.
    sudo dscl . -create "/Users/${LAGER_USER}" IsHidden 1 || true
    unset LAGER_PASSWORD
fi

# Ensure the user is in the staff group (needed for chowns and normal shell use).
sudo dseditgroup -o edit -a "$LAGER_USER" -t user staff || true

# --- SSH key inheritance ----------------------------------------------------
# Copy the installing admin's authorized_keys into ~lagerdata/.ssh/authorized_keys
# so that anyone who can already SSH into the MacBook as the admin user can
# then `lager ssh --box mac-box` and land in an interactive lagerdata shell
# without an extra ssh-copy-id step. If the admin doesn't have an
# authorized_keys file (e.g. they use password auth), fall back to their
# public key files.
log "  seeding ~${LAGER_USER}/.ssh/authorized_keys from the admin user"
sudo -u "$LAGER_USER" mkdir -p "/Users/${LAGER_USER}/.ssh"
sudo chmod 0700 "/Users/${LAGER_USER}/.ssh"
sudo chown "${LAGER_USER}:${LAGER_GROUP}" "/Users/${LAGER_USER}/.ssh"

ADMIN_HOME=$(eval echo "~${SUDO_USER:-$USER}")
ADMIN_AUTH_KEYS="${ADMIN_HOME}/.ssh/authorized_keys"
LAGERDATA_AUTH_KEYS="/Users/${LAGER_USER}/.ssh/authorized_keys"

sudo -u "$LAGER_USER" touch "$LAGERDATA_AUTH_KEYS"
sudo chmod 0600 "$LAGERDATA_AUTH_KEYS"

if [ -s "$ADMIN_AUTH_KEYS" ]; then
    log "  copying ${ADMIN_AUTH_KEYS} → ${LAGERDATA_AUTH_KEYS}"
    sudo cp "$ADMIN_AUTH_KEYS" "$LAGERDATA_AUTH_KEYS"
else
    # Fall back to the admin's public key files so at least their own machine
    # can reach lagerdata over SSH.
    for pubkey in "${ADMIN_HOME}/.ssh/id_ed25519.pub" "${ADMIN_HOME}/.ssh/id_rsa.pub" "${ADMIN_HOME}/.ssh/id_ecdsa.pub"; do
        if [ -f "$pubkey" ]; then
            log "  appending $(basename "$pubkey") to ${LAGERDATA_AUTH_KEYS}"
            sudo sh -c "cat '$pubkey' >> '$LAGERDATA_AUTH_KEYS'"
        fi
    done
fi

sudo chown "${LAGER_USER}:${LAGER_GROUP}" "$LAGERDATA_AUTH_KEYS"
sudo chmod 0600 "$LAGERDATA_AUTH_KEYS"

if [ ! -s "$LAGERDATA_AUTH_KEYS" ]; then
    warn "No SSH public keys found for the admin user. You will need to run"
    warn "  ssh-copy-id ${LAGER_USER}@<box-ip>"
    warn "from each dev machine before \`lager ssh --box\` will work."
fi

# Make sure sshd will serve the new user. macOS Remote Login must be enabled.
if ! sudo systemsetup -getremotelogin 2>/dev/null | grep -qi 'On'; then
    log "  enabling Remote Login (SSH) so lagerdata can be reached"
    sudo systemsetup -setremotelogin on || warn "Failed to enable Remote Login — enable manually in System Settings → General → Sharing"
fi

# ---------------------------------------------------------------------------
# Phase 3: State and log directories
# ---------------------------------------------------------------------------

log "Phase 3/9: creating state and log directories"
sudo mkdir -p "$LAGER_STATE_DIR" "$LAGER_BIN_DIR" "$LAGER_LOG_DIR"
sudo chown -R "${LAGER_USER}:${LAGER_GROUP}" "$LAGER_STATE_DIR" "$LAGER_LOG_DIR"
sudo chmod 0755 "$LAGER_STATE_DIR" "$LAGER_LOG_DIR"

# Initialize empty saved_nets.json so the services don't trip on first run.
if [ ! -f "$LAGER_STATE_DIR/saved_nets.json" ]; then
    echo '[]' | sudo tee "$LAGER_STATE_DIR/saved_nets.json" >/dev/null
    sudo chown "${LAGER_USER}:${LAGER_GROUP}" "$LAGER_STATE_DIR/saved_nets.json"
fi

# ---------------------------------------------------------------------------
# Phase 4: Homebrew + system packages
# ---------------------------------------------------------------------------

log "Phase 4/9: Homebrew and system packages"
if ! command -v brew >/dev/null 2>&1; then
    log "  installing Homebrew (non-interactive)"
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# brew is owned by the admin user, not lagerdata. That's fine — we only need
# it to install system libraries; the box services don't shell out to brew.
brew update
brew install python@3.12 libusb hidapi pkg-config wget || true

# Make sure /opt/homebrew/bin and /opt/homebrew/lib are reachable from the
# lagerdata user's environment.
BREW_PREFIX=$(brew --prefix)
log "  Homebrew prefix: $BREW_PREFIX"

# ---------------------------------------------------------------------------
# Phase 5: Clone the open-source-lager repo
# ---------------------------------------------------------------------------

log "Phase 5/9: cloning open-source-lager (branch: ${REPO_BRANCH})"
if [ -d "$LAGER_REPO_DIR/.git" ]; then
    log "  repo already cloned — fetching latest"
    sudo -u "$LAGER_USER" git -C "$LAGER_REPO_DIR" fetch origin
    sudo -u "$LAGER_USER" git -C "$LAGER_REPO_DIR" checkout "$REPO_BRANCH"
    sudo -u "$LAGER_USER" git -C "$LAGER_REPO_DIR" pull --ff-only origin "$REPO_BRANCH"
else
    sudo -u "$LAGER_USER" git clone --branch "$REPO_BRANCH" "$REPO_URL" "$LAGER_REPO_DIR"
fi

# ---------------------------------------------------------------------------
# Phase 6: Python venv and dependencies
# ---------------------------------------------------------------------------

log "Phase 6/9: Python venv and box dependencies"
PYTHON_BIN="${BREW_PREFIX}/opt/python@3.12/bin/python3.12"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN=$(command -v python3.12 || command -v python3)
fi
log "  using python: $PYTHON_BIN"

if [ ! -d "$LAGER_VENV" ]; then
    sudo -u "$LAGER_USER" "$PYTHON_BIN" -m venv "$LAGER_VENV"
fi

# Pin pip and install the box's Python deps. The list mirrors box.Dockerfile
# minus uldaq (Linux-only, MCC USB-202) and aardvark_py from PyPI (we install
# TotalPhase's macOS-native build separately below).
sudo -u "$LAGER_USER" "$LAGER_VENV/bin/pip" install --upgrade pip wheel

sudo -u "$LAGER_USER" "$LAGER_VENV/bin/pip" install --no-cache-dir \
    'bleak==0.22.2' \
    'requests==2.31.0' \
    'pyserial==3.5' \
    'beautifulsoup4==4.12.0' \
    'esptool==4.6.2' \
    'pytest==6.2.5' \
    'redis==4.0.2' \
    'PyYAML==6.0.1' \
    'ptyprocess==0.7.0' \
    'pexpect==4.8.0' \
    'pexpect-serial==0.1.0' \
    'pyusb==1.2.1' \
    'hidapi==0.14.0' \
    'simplejson==3.18.0' \
    'labjack-ljm==1.23.0' \
    'pygdbmi==0.11.0.0' \
    'cryptography' \
    'pyvisa-py==0.5.2' \
    'PyVISA==1.11.3' \
    'opencv-python-headless==4.10.0.84' \
    'yoctopuce' \
    'joulescope' \
    'ppk2-api' \
    'pyftdi' \
    'Flask==3.0.0' \
    'flask-socketio==5.3.5' \
    'python-socketio==5.10.0' \
    'websockets==12.0' \
    'rich' \
    'cbor2' \
    'websocket-client>=1.6.0' \
    'mcp>=1.0.0' \
    'psutil>=5.9.0'

# Tier-2 vendor pure-Python pieces — Phidget has macOS support; brainstem
# (Acroname) ships a macOS wheel.
sudo -u "$LAGER_USER" "$LAGER_VENV/bin/pip" install --no-cache-dir \
    'Phidget22==1.19.20240311' \
    'brainstem' || warn "Phidget22 / brainstem install failed; thermocouple and Acroname hubs may not work"

# psycopg2 is only used when the optional Postgres telemetry sink is enabled.
sudo -u "$LAGER_USER" "$LAGER_VENV/bin/pip" install --no-cache-dir 'psycopg2-binary==2.9.9' || true

# Note: `uldaq` (MCC USB-202) and `aardvark_py` (TotalPhase Aardvark) are
# intentionally NOT installed from PyPI on macOS. uldaq has no macOS support;
# aardvark_py is replaced by TotalPhase's macOS-native package below.

# ---------------------------------------------------------------------------
# Phase 7: Tier-2 vendor SDK installs
# ---------------------------------------------------------------------------

if [ "$SKIP_VENDOR" -eq 1 ]; then
    log "Phase 7/9: SKIPPING vendor SDKs (--skip-vendor passed)"
else
    log "Phase 7/9: Tier-2 vendor SDK installs"

    TMP_DIR=$(mktemp -d)
    trap 'rm -rf "$TMP_DIR"; kill ${SUDO_REFRESH_PID:-0} 2>/dev/null || true' EXIT

    # ---- LabJack LJM ------------------------------------------------------
    if [ -f /usr/local/lib/libLabJackM.dylib ] || [ -f /Library/LabJack/LJM/Libraries/libLabJackM.dylib ]; then
        log "  LabJack LJM already installed — skipping"
    else
        log "  downloading LabJack LJM macOS installer"
        if curl -fL "$LJM_INSTALLER_URL" -o "$TMP_DIR/ljm.zip"; then
            unzip -q -o "$TMP_DIR/ljm.zip" -d "$TMP_DIR/ljm"
            LJM_PKG=$(find "$TMP_DIR/ljm" -name "*.pkg" -print -quit)
            if [ -n "$LJM_PKG" ]; then
                log "  installing LJM .pkg (sudo)"
                sudo installer -pkg "$LJM_PKG" -target /
            else
                warn "LJM zip didn't contain a .pkg — inspect $TMP_DIR/ljm manually"
            fi
        else
            warn "LJM download failed from $LJM_INSTALLER_URL — install manually from https://labjack.com/support/software/installers/ljm"
        fi
    fi

    # ---- SEGGER J-Link ----------------------------------------------------
    if [ -x /Applications/SEGGER/JLink/JLinkGDBServerCLExe ] || [ -x /Applications/SEGGER/JLink/JLinkExe ]; then
        log "  SEGGER J-Link already installed — skipping"
    else
        log "  downloading SEGGER J-Link macOS installer"
        # SEGGER's downloads usually require accepting an EULA. The direct
        # .pkg URL above works for unauthenticated downloads of the most
        # recent J-Link Software Pack; if it ever changes, fall back to the
        # manual instructions.
        if curl -fL --user-agent "Mozilla/5.0" "$JLINK_INSTALLER_URL" -o "$TMP_DIR/jlink.pkg"; then
            log "  installing J-Link .pkg (sudo)"
            sudo installer -pkg "$TMP_DIR/jlink.pkg" -target /
        else
            warn "J-Link download failed — install manually from https://www.segger.com/downloads/jlink/ and re-run with --skip-vendor"
        fi
    fi

    # ---- TotalPhase Aardvark ----------------------------------------------
    AARDVARK_VENV_DEST="$LAGER_VENV/lib/python3.12/site-packages"
    if [ -f "$AARDVARK_VENV_DEST/aardvark_py.py" ] && [ -f "$AARDVARK_VENV_DEST/aardvark.dylib" ]; then
        log "  TotalPhase Aardvark already vendored — skipping"
    else
        # TotalPhase requires a login to download the Aardvark API zip from:
        #
        #   https://www.totalphase.com/products/aardvark-software-api/
        #     → "Mac ARM 64-bit" (download ID 418)
        #
        # The admin running this script can provide the zip via:
        #   1. --aardvark-zip /path/to/aardvark-api-mac-arm64-v6.00.zip
        #   2. Dropping it as /tmp/aardvark.zip before running the installer.
        #   3. Dropping it in ~/Downloads/ (any filename matching aardvark*.zip).
        #
        # If none of these exist, the script warns but continues — the box
        # will still work for every other instrument; Aardvark I2C/SPI/GPIO
        # just won't be available until the user supplies the zip.
        if [ -z "$AARDVARK_ZIP" ]; then
            for candidate in \
                /tmp/aardvark.zip \
                /tmp/aardvark-api-*.zip \
                "$HOME/Downloads/aardvark-api-mac-arm64"*.zip \
                "$HOME/Downloads/aardvark"*.zip \
                "$HOME/Desktop/aardvark"*.zip \
            ; do
                # shellcheck disable=SC2086
                for match in $candidate; do
                    if [ -f "$match" ]; then
                        AARDVARK_ZIP="$match"
                        break 2
                    fi
                done
            done
        fi

        if [ -n "$AARDVARK_ZIP" ]; then
            log "  vendoring Aardvark from $AARDVARK_ZIP"
            unzip -q -o "$AARDVARK_ZIP" -d "$TMP_DIR/aardvark"
            DYLIB=$(find "$TMP_DIR/aardvark" -name 'aardvark.dylib' -print -quit)
            PYWRAP=$(find "$TMP_DIR/aardvark" -name 'aardvark_py.py' -print -quit)
            if [ -n "$DYLIB" ] && [ -n "$PYWRAP" ]; then
                sudo cp "$DYLIB" "$AARDVARK_VENV_DEST/"
                sudo cp "$PYWRAP" "$AARDVARK_VENV_DEST/"
                sudo chown "${LAGER_USER}:${LAGER_GROUP}" "$AARDVARK_VENV_DEST/aardvark.dylib" "$AARDVARK_VENV_DEST/aardvark_py.py"
                log "  Aardvark vendored successfully"
            else
                warn "Aardvark zip didn't contain expected files (aardvark.dylib + aardvark_py.py)"
            fi
        else
            warn ""
            warn "TotalPhase Aardvark API (I2C/SPI/GPIO) could not be installed."
            warn ""
            warn "The download requires a free TotalPhase account. To install:"
            warn "  1. Go to https://www.totalphase.com/products/aardvark-software-api/"
            warn "  2. Download 'Mac ARM 64-bit' (v6.00 or later)"
            warn "  3. Re-run this installer with:"
            warn "       --aardvark-zip /path/to/aardvark-api-mac-arm64-v6.00.zip"
            warn "     OR drop the zip as /tmp/aardvark.zip before running."
            warn ""
            warn "The box will work for all other instruments without Aardvark."
        fi
    fi

    # ---- Nordic nrfutil ---------------------------------------------------
    NRFUTIL_DEST="$LAGER_BIN_DIR/nrfutil"
    if [ -x "$NRFUTIL_DEST" ]; then
        log "  nrfutil already installed at $NRFUTIL_DEST — skipping"
    else
        log "  downloading nrfutil-mac"
        if curl -fL "$NRFUTIL_URL" -o "$TMP_DIR/nrfutil"; then
            sudo install -o "$LAGER_USER" -g "$LAGER_GROUP" -m 0755 "$TMP_DIR/nrfutil" "$NRFUTIL_DEST"
            log "  installed: $NRFUTIL_DEST"
        else
            warn "nrfutil download failed from $NRFUTIL_URL — Nordic flashing will not work"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Phase 8: Write version file
# ---------------------------------------------------------------------------

log "Phase 8/9: writing version file"
GIT_REV=$(sudo -u "$LAGER_USER" git -C "$LAGER_REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)
echo "${GIT_REV}|${REPO_BRANCH}" | sudo tee "$LAGER_STATE_DIR/version" >/dev/null
sudo chown "${LAGER_USER}:${LAGER_GROUP}" "$LAGER_STATE_DIR/version"

# ---------------------------------------------------------------------------
# Phase 9: Install and bootstrap launchd LaunchDaemon
# ---------------------------------------------------------------------------

log "Phase 9/9: launchd LaunchDaemon"
PLIST_SOURCE="$LAGER_REPO_DIR/box/launchd/com.lager.box.plist"
if [ ! -f "$PLIST_SOURCE" ]; then
    fail "plist source not found at $PLIST_SOURCE — repo checkout looks broken"
fi

sudo install -o root -g wheel -m 0644 "$PLIST_SOURCE" "$PLIST_DEST"
log "  installed: $PLIST_DEST"

# Idempotency: bootout an existing daemon before bootstrapping again.
sudo launchctl bootout "system/${PLIST_LABEL}" 2>/dev/null || true
sudo launchctl bootstrap system "$PLIST_DEST"
sudo launchctl enable "system/${PLIST_LABEL}"
sudo launchctl kickstart -k "system/${PLIST_LABEL}"

log "  daemon bootstrapped — checking status"
sleep 2
if sudo launchctl print "system/${PLIST_LABEL}" >/dev/null 2>&1; then
    log "  ✓ ${PLIST_LABEL} is loaded"
else
    warn "${PLIST_LABEL} did not load — check /Library/Logs/Lager/launchd.err.log"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

BOX_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "<unknown>")

cat <<EOF

\033[1;32m==========================================================================
 Lager box install complete
==========================================================================\033[0m

  Box IP:           ${BOX_IP}
  State directory:  ${LAGER_STATE_DIR}
  Logs:             ${LAGER_LOG_DIR}
  Repo:             ${LAGER_REPO_DIR} (branch: ${REPO_BRANCH})
  Daemon:           ${PLIST_LABEL}

Smoke test from your dev machine:

  curl http://${BOX_IP}:9000/health
  curl http://${BOX_IP}:5000/

Then add the box to your CLI config:

  lager boxes add mac-box ${BOX_IP}
  lager hello --box mac-box
  lager instruments --box mac-box

To stop/start/restart the daemon:

  sudo launchctl kickstart system/${PLIST_LABEL}    # start (or restart with -k)
  sudo launchctl bootout system/${PLIST_LABEL}      # stop and unload

To uninstall, run:

  sudo ${LAGER_REPO_DIR}/box/launchd/lager-box-ctl.sh uninstall

EOF
