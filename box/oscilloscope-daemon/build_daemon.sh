#!/bin/bash
#
# Build script for the oscilloscope-daemon.
#
# Boxes do not need this: start_box.sh builds the daemon from the box's own
# checkout whenever its Rust sources change, so `lager install` and
# `lager update` keep it current. This script is for building by hand.
#
# The daemon loads the PicoTech drivers at runtime with dlopen (see
# daemon/src/oscilloscope/pico/loader.rs), so no PicoScope library is linked
# and no scope has to be attached. One binary serves every supported series.
#
# The FFI bindings ARE generated at build time, from the PicoTech headers.
# Those are not in this repository -- PicoTech's licence does not allow it --
# so daemon/build.rs looks for them in two places, in order:
#   - picoscope/include/<family>/ at the repo root (unpack the SDK there)
#   - /opt/picoscope/include/<family>/ (where the PicoTech packages put them)
#
# Requirements:
#   - Rust toolchain
#   - clang / libclang-dev (bindgen needs it to parse the headers)
#   - the PicoTech headers, as above
#
# Usage:
#   ./build_daemon.sh              # Build release binary
#   ./build_daemon.sh --install    # Build and stage into box/lager/docker/
#
# At RUNTIME the box needs the PicoTech shared libraries, installed by
# `lager install` (see cli/deployment/scripts/setup_and_deploy_box.sh) and
# mounted into the container at /opt/picoscope/lib.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo "Building Oscilloscope Daemon"
echo "========================================"
echo ""

if ! command -v cargo &> /dev/null; then
    echo "ERROR: Rust toolchain not found!"
    echo ""
    echo "Install Rust with:"
    echo "  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
    echo "  source \$HOME/.cargo/env"
    exit 1
fi

# bindgen parses the headers through libclang, so this is a hard requirement
# even though nothing links against the SDK.
if ! command -v clang &> /dev/null; then
    echo "ERROR: clang not found (bindgen needs libclang to parse headers)"
    echo "Install with: sudo apt-get install -y libclang-dev"
    exit 1
fi

# Checked here so a missing SDK is one clear line rather than a panic from
# deep inside the build script.
REPO_HEADERS="$SCRIPT_DIR/../../picoscope/include"
SDK_HEADERS="/opt/picoscope/include"
if [ ! -f "$REPO_HEADERS/libps2000/ps2000.h" ] && [ ! -f "$SDK_HEADERS/libps2000/ps2000.h" ]; then
    echo "ERROR: PicoTech headers not found"
    echo "  Looked in: $REPO_HEADERS/<family>/"
    echo "        and: $SDK_HEADERS/<family>/"
    echo "Install the PicoTech packages, or unpack the PicoScope SDK into"
    echo "picoscope/include/ at the repo root."
    exit 1
fi

echo "Building daemon (release mode)..."
echo ""

cargo build --release --package daemon

BINARY="$SCRIPT_DIR/target/release/daemon"
if [ ! -f "$BINARY" ]; then
    echo "Build reported success but $BINARY is missing"
    exit 1
fi

echo ""
echo "Build successful!"
echo "Binary: $BINARY"

if [ "$1" == "--install" ]; then
    echo ""
    echo "Staging into box docker directory..."
    cp "$BINARY" "$SCRIPT_DIR/../lager/docker/oscilloscope-daemon"
    echo "Installed to: $SCRIPT_DIR/../lager/docker/oscilloscope-daemon"
fi

echo ""
echo "========================================"
echo "Build complete!"
echo "========================================"
echo ""
echo "To deploy to a box by hand:"
echo "  scp target/release/daemon lagerdata@<box-ip>:/home/lagerdata/third_party/.oscilloscope-daemon.new"
echo "  ssh lagerdata@<box-ip> 'mv -f ~/third_party/.oscilloscope-daemon.new ~/third_party/oscilloscope-daemon && docker restart lager'"
echo ""
echo "Copy to a new name, then rename: the running container executes the old"
echo "file, so writing over it in place fails with 'Text file busy'. The"
echo "restart is required because the daemon is bind-mounted as a single file,"
echo "and the container keeps the old inode until it starts again."
echo ""
echo "On a box with the PicoTech headers, the next 'lager update' rebuilds the"
echo "daemon from the box's checkout and replaces a binary installed this way."
echo ""
