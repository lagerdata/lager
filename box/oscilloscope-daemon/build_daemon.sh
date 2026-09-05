#!/bin/bash
#
# Build script for the oscilloscope-daemon.
#
# The daemon loads PicoTech drivers at runtime with dlopen (see
# daemon/src/oscilloscope/pico/loader.rs) and generates its FFI bindings from
# headers vendored in picoscope/include, so NEITHER the PicoScope SDK nor a
# scope has to be present to build. One binary serves every supported series.
#
# That is a change from earlier versions, which linked libps2000 at build time
    10|# behind a `ps2000` Cargo feature. Both are gone: there is no feature to
# select and no link-time SDK dependency.
#
# Requirements:
#   - Rust toolchain
#   - clang / libclang-dev (bindgen needs it to parse the vendored headers)
#
# Usage:
#   ./build_daemon.sh              # Build release binary
#   ./build_daemon.sh --install    # Build and stage into box/lager/docker/
#
    20|# At RUNTIME the box needs the PicoTech shared libraries, installed by
# `lager install` (see cli/deployment/scripts/setup_and_deploy_box.sh) and
# mounted into the container at /opt/picoscope/lib.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
    30|echo "Building Oscilloscope Daemon"
echo "========================================"
echo ""

if ! command -v cargo &> /dev/null; then
    echo "ERROR: Rust toolchain not found!"
    echo ""
    echo "Install Rust with:"
    echo "  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
    echo "  source \$HOME/.cargo/env"
    40|    exit 1
fi

# bindgen parses the vendored headers through libclang, so this is a hard
# requirement even though nothing links against the SDK.
if ! command -v clang &> /dev/null; then
    echo "ERROR: clang not found (bindgen needs libclang to parse headers)"
    echo "Install with: sudo apt-get install -y libclang-dev"
    exit 1
fi
    50|
echo "Building daemon (release mode)..."
echo ""

cargo build --release --package daemon

BINARY="$SCRIPT_DIR/target/release/daemon"
if [ ! -f "$BINARY" ]; then
    echo "Build reported success but $BINARY is missing"
    exit 1
    60|fi

echo ""
echo "Build successful!"
echo "Binary: $BINARY"

if [ "$1" == "--install" ]; then
    echo ""
    echo "Staging into box docker directory..."
    cp "$BINARY" "$SCRIPT_DIR/../lager/docker/oscilloscope-daemon"
    70|    echo "Installed to: $SCRIPT_DIR/../lager/docker/oscilloscope-daemon"
fi

echo ""
echo "========================================"
echo "Build complete!"
echo "========================================"
echo ""
echo "To deploy to a box:"
echo "  scp target/release/daemon lagerdata@<box-ip>:/home/lagerdata/third_party/oscilloscope-daemon"
    80|echo "  ssh lagerdata@<box-ip> 'docker restart lager'"
echo ""
echo "The container restart is required: the daemon is bind-mounted as a"
echo "single file, and Docker keeps serving the old inode until the mount is"
echo "re-resolved."
echo ""
