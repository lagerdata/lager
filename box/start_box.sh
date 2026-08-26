#!/bin/bash

# Script to build and start box Docker containers WITHOUT the controller container
# The Python container now includes all necessary services:
# - Python Execution Service (port 5000) - replaces controller's /python endpoint
# - Debug Service (port 8765) - embedded debugging
# - UART HTTP+WebSocket Server (port 9000) - serial communication
#
# Usage: ./start_box.sh [--no-publish | --publish]
# Run this script from the box directory after copying code to the box device
#
# --no-publish (or LAGER_NO_PUBLISH=1) skips publishing the container's
# service ports on the host: the container is reachable only on the lagernet
# Docker network. Use this when a reverse proxy on the same network owns the
# host ports and forwards traffic to this container.
#
# The chosen mode persists: --no-publish writes a marker file so later runs
# without flags keep the container lagernet-only (an update script that
# doesn't know about the proxy would otherwise republish the ports and
# collide with it). --publish clears the marker and returns to the default.

set -e

NO_PUBLISH="${LAGER_NO_PUBLISH:-}"
EXPLICIT_PUBLISH=""
for arg in "$@"; do
    case "$arg" in
        --no-publish) NO_PUBLISH=1 ;;
        --publish) EXPLICIT_PUBLISH=1 ;;
    esac
done

# --- BEGIN single-instance guard (extracted verbatim by test/unit/box/test_authorized_keys_sync.py) ---
# Only one start_box.sh may run at a time. Concurrent copies race each other:
# competing `docker run`s, and (historically) multiple key-sync loops appending
# to authorized_keys at once. Orphaned copies also accumulate across restarts,
# so boxes have been found running ten-plus instances at once.
#
# The lock is an fd held for this process's lifetime and released by the kernel
# when it exits, so a killed or crashed run can never leave a stale lock behind
# (a PID file can). fd 9 is explicit rather than flock(1)'s own fd because the
# background poller below inherits open descriptors — it closes fd 9 with `9>&-`
# so a long-lived poller cannot pin the lock after this script exits.
#
# Failure to open the lock file is a warning, never fatal: this script's
# standing rule is that environmental problems must not stop a box coming up.
# Overridable only so the unit tests can lock a temp path instead of the real
# one; production always uses the default.
LOCK_FILE="${LAGER_START_BOX_LOCK:-/tmp/lager-start-box.lock}"
if ( : >>"$LOCK_FILE" ) 2>/dev/null; then
    exec 9>>"$LOCK_FILE"
    if command -v flock >/dev/null 2>&1; then
        if ! flock -n 9; then
            echo "ERROR: another start_box.sh is already running (lock: $LOCK_FILE)."
            echo "       Wait for it to finish, or stop it and retry."
            exit 1
        fi
    else
        echo "[WARNING] flock not found — cannot enforce single-instance startup."
    fi
else
    echo "[WARNING] Could not open $LOCK_FILE; single-instance guard disabled."
fi
# --- END single-instance guard ---

NO_PUBLISH_MARKER="/etc/lager/no_publish"
if [ -n "$EXPLICIT_PUBLISH" ]; then
    NO_PUBLISH=""
    rm -f "$NO_PUBLISH_MARKER" 2>/dev/null || true
elif [ -z "$NO_PUBLISH" ] && [ -f "$NO_PUBLISH_MARKER" ]; then
    NO_PUBLISH=1
    echo "Keeping previous --no-publish mode (marker: $NO_PUBLISH_MARKER; pass --publish to publish ports again)"
fi
if [ -n "$NO_PUBLISH" ]; then
    touch "$NO_PUBLISH_MARKER" 2>/dev/null || \
        echo "[WARNING] Could not write $NO_PUBLISH_MARKER — no-publish mode will not survive a plain restart"
fi

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
    echo "[WARNING] J-Link not found (optional - OpenOCD is used otherwise)"
    echo ""
    echo "  J-Link was already installed by the deployment script if available."
    echo "  Without it, SEGGER J-Link probes will not work; OpenOCD ships in"
    echo "  the container and drives ST-Link, CMSIS-DAP and FTDI probes."
    echo ""
    echo "  To verify J-Link installation manually:"
    echo "    ls -la $THIRD_PARTY_DIR/JLink*"
    echo ""
fi
echo ""

# --- BEGIN pre-built image (extracted verbatim by test/unit/box/test_prebuilt_image.py) ---
# A rejected image is ~1 GB; do not leave it parked on the box.
discard_prebuilt_box_image() {
    docker rmi "$1" >/dev/null 2>&1 || true
}

# Pull the pre-built box image named by LAGER_BOX_IMAGE and promote it to the
# local `lager` tag. Returns non-zero for EVERY miss, and the caller then falls
# through to the build below.
#
# That bias is deliberate and load-bearing. The local build is slow but has
# always produced something that runs; the new failure mode a pull introduces
# is a box that comes up FAST AND BROKEN, which on a real box is strictly worse
# than slow. Never trade the second for the first.
#
# The premise that makes a CI-built image interchangeable with a box-built one:
# everything box-specific -- PicoScope, SEGGER, /etc/lager, customer binaries,
# the cargo/npm volumes -- is bind-mounted at `docker run` time below, never
# COPY'd into the image.
use_prebuilt_box_image() {
    local ref="${LAGER_BOX_IMAGE:-}"
    local want="${LAGER_BOX_IMAGE_VERSION:-}"
    local platform cfg rc label tmp

    # Pull by digest, never by tag -- enforced by shape rather than by trust.
    # A tag is mutable: between resolving it and pulling it the tag can move,
    # so two boxes installed minutes apart could run different bytes with
    # nothing on either recording which. A caller holding only a tag has not
    # resolved it, and resolving it is not this script's job.
    case "$ref" in
        *@sha256:*) ;;
        *)
            echo "  LAGER_BOX_IMAGE is not a digest reference (no @sha256:); building instead."
            return 1
            ;;
    esac

    # An image that cannot be checked against anything is not worth pulling.
    if [ -z "$want" ]; then
        echo "  LAGER_BOX_IMAGE_VERSION is unset, so the image cannot be verified; building instead."
        return 1
    fi

    echo "[1/1] Pulling pre-built Lager Box image..."
    echo "      ${ref}"

    # The published manifest is single-platform amd64 and is NOT a multi-arch
    # index, so there is nothing for docker to negotiate against: without an
    # explicit --platform a non-amd64 host pulls the amd64 image anyway, tags
    # it `lager`, and the container dies with `exec format error`. This is
    # defence in depth, not support for another architecture -- a box is
    # documented x86-64 only, and box.Dockerfile fetches LabJack and nrfutil
    # from hardcoded x64 URLs regardless. It makes an unsupported host fail
    # cleanly at the manifest instead of confusingly at container start.
    platform="linux/$(dpkg --print-architecture 2>/dev/null || uname -m)"

    # Pull through a THROWAWAY, empty docker config so the request goes out
    # anonymously. The image is public, and inheriting the box's ambient
    # credentials makes the pull depend on state that has nothing to do with
    # this feature: a box that ever logged in to ghcr.io for something else
    # sends those credentials, GHCR evaluates them against *this* repository
    # rather than falling back to anonymous, and returns `denied: denied` for a
    # package anyone can read. Observed on a real workstation, not theorised.
    #
    # Safe here because nothing on a box uses a docker *context* -- it talks to
    # the default local socket, the same way the `docker build` below does.
    cfg=$(mktemp -d) || return 1
    if command -v timeout >/dev/null 2>&1; then
        timeout 300 docker --config "$cfg" pull --platform "$platform" "$ref" && rc=0 || rc=$?
    else
        docker --config "$cfg" pull --platform "$platform" "$ref" && rc=0 || rc=$?
    fi
    # The exit status is captured before the cleanup so a failed pull is still
    # classifiable, and the directory goes away either way.
    rm -rf "$cfg"
    if [ "$rc" -ne 0 ]; then
        echo "  Pull failed (exit ${rc}); building instead."
        return 1
    fi

    # The verification is the reason this function exists. A local build is
    # provably made from the tree the box just checked out; a pulled image
    # carries no such proof, and the post-deploy check only ever confirms that
    # *a* container is running -- it would report success on a box serving the
    # wrong version entirely. So the image has to claim, in a label the
    # publisher stamps from the tag it actually built, to be what was asked for.
    label=$(docker image inspect \
        --format '{{index .Config.Labels "org.opencontainers.image.version"}}' \
        "$ref" 2>/dev/null) || label=""

    # A Go template indexing a missing key prints the literal `<no value>`.
    # An image carrying no label at all is rejected too: images published
    # before the labelling workflow landed are indistinguishable from an image
    # built from anything at all, and "no evidence" is not "good evidence".
    if [ -z "$label" ] || [ "$label" = "<no value>" ]; then
        echo "  Pulled image carries no version label; discarding it and building instead."
        discard_prebuilt_box_image "$ref"
        return 1
    fi
    if [ "$label" != "$want" ]; then
        echo "  Pulled image claims ${label}, expected ${want}; discarding it and building instead."
        discard_prebuilt_box_image "$ref"
        return 1
    fi

    if ! docker tag "$ref" lager; then
        echo "  Could not tag the pulled image as 'lager'; discarding it and building instead."
        discard_prebuilt_box_image "$ref"
        return 1
    fi

    # Drop the digest reference now that `lager` points at the image. Leaving
    # it attached is what would stop `docker image prune -f` from EVER
    # reclaiming a superseded image: prune removes only images with no
    # references left, so a lingering digest ref parks every old ~1 GB release
    # on the box permanently.
    docker rmi "$ref" >/dev/null 2>&1 || true

    # Record WHERE the running image came from. /etc/lager/build-hash cannot
    # answer this -- it hashes the box's own tree, which reads identically
    # whether the image was built here or pulled. Best-effort: nothing gates on
    # this file, and /etc/lager may not exist yet on a standalone run (the
    # deployment script creates it, and the check below requires it anyway).
    if [ -d /etc/lager ]; then
        if tmp=$(mktemp /etc/lager/.image-source.XXXXXX 2>/dev/null); then
            if printf 'ghcr:%s\n' "${ref##*@}" > "$tmp" 2>/dev/null \
                && chmod 644 "$tmp" 2>/dev/null; then
                mv -f "$tmp" /etc/lager/image-source 2>/dev/null || rm -f "$tmp"
            else
                rm -f "$tmp"
            fi
        fi
    fi

    return 0
}
# --- END pre-built image ---

# 1. Obtain the Lager box container image
# LAGER_SKIP_BUILD lets a caller that already built the image (e.g. `lager
# update`, which builds it in its own step with full error reporting) skip the
# redundant rebuild here and go straight to starting the container. Standalone
# and deployment runs leave it unset, so they still build.
#
# LAGER_BOX_IMAGE names a pre-built image to pull instead of building. It must
# be an immutable digest reference, and LAGER_BOX_IMAGE_VERSION must carry the
# release tag the image is required to claim. `lager install` sets both after
# resolving the tag on the operator's machine. Any miss falls through to the
# build: a slow install that works beats a fast one that does not.
#
# Note the errexit subtlety -- `use_prebuilt_box_image` is called as an `elif`
# condition, which suppresses `set -e` for its whole body. That is exactly what
# lets a failed pull fall through instead of killing the deploy, and it is also
# why every step inside it checks its own status explicitly.
if [ -n "${LAGER_SKIP_BUILD:-}" ]; then
    echo "[1/1] Skipping build (LAGER_SKIP_BUILD set; image already built by caller)"
    IMAGE_SOURCE_DESC="supplied by the caller"
    if ! docker image inspect lager >/dev/null 2>&1; then
        echo "ERROR: LAGER_SKIP_BUILD is set but no 'lager' image exists to start."
        echo "       Re-run without LAGER_SKIP_BUILD to build it."
        exit 1
    fi
elif [ -n "${LAGER_BOX_IMAGE:-}" ] && use_prebuilt_box_image; then
    echo "Pre-built image verified and tagged 'lager'; no build needed."
    IMAGE_SOURCE_DESC="pulled from the registry"
else
    echo "[1/1] Building Lager Box container..."
    cd "${SCRIPT_DIR}/lager"
    # box.Dockerfile uses `# syntax=` + `RUN --mount=type=cache` (build cache for
    # the cargo/pip layers), which require BuildKit. Docker >= 23 enables it by
    # default; force it on so this path also works on 18.09–22 boxes and never
    # falls back to the legacy builder (which errors on `--mount`).
    DOCKER_BUILDKIT=1 docker build -f docker/box.Dockerfile -t lager .
    echo "Lager Box container built successfully!"
    IMAGE_SOURCE_DESC="built on this box"
    # Clear any registry provenance left by an earlier pull. Without this the
    # file outlives the image it describes: a box that pulled once and then
    # rebuilt would keep claiming to run registry bytes it no longer runs,
    # which is worse than recording nothing at all. `lager update` avoids the
    # same trap by always writing one of ghcr:/local:, but it computes a build
    # hash to write and this path has none, so the honest record is no record.
    rm -f /etc/lager/image-source 2>/dev/null || true
fi
echo ""

echo "========================================"
echo "Container image ready"
echo "========================================"
echo ""
# Say where it came from rather than asserting a build. Two of the three
# branches above do not build, and reporting "built successfully" after a pull
# is the kind of small untruth that costs someone an hour during an incident.
echo "Image 'lager' (${IMAGE_SOURCE_DESC}) -- Python Execution, Debug and UART services"
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

# Initialize saved_nets.json if it doesn't exist. No sudo needed: /etc/lager is
# owned by www-data (the container's uid) and group-writable by this user, which
# `lager install` / `lager update` set up. On a box whose /etc/lager predates
# that (owner-only 33:33 755), this and every box_config render below fail —
# run `lager update --box <BOX>` to repair the permissions.
if [ ! -f /etc/lager/saved_nets.json ]; then
    echo "Initializing /etc/lager/saved_nets.json..."
    echo "[]" > /etc/lager/saved_nets.json
    chmod 666 /etc/lager/saved_nets.json
fi

# --- BEGIN secret-file ownership (extracted verbatim by test/unit/box/test_secret_file_ownership.py) ---
# Secrets must not be group/world-readable, and the runtime must still be able
# to read them. Both halves matter, and enforcing only the first is what broke
# a box in the field.
#
# Mode 0600 grants the OWNER alone. Everything that reads these files runs as
# uid 33 inside the container, so 0600 is only safe once uid 33 owns the file.
# On a box where the file had been copied in by hand it was owned by the host
# login user instead — this script runs as that user, so its chmod SUCCEEDED
# and instantly locked the runtime out of its own secrets. The executor caught
# the PermissionError and injected an empty secret set, so the failure was
# silent; the box just stopped having secrets.
#
# The old loop had the diagnostics exactly backwards: it warned when chmod
# failed (the HEALTHY case — the file already belongs to uid 33 and only the
# container can chmod it) and said nothing when chmod succeeded (the case that
# creates the lockout).
#
# Order is chmod-then-chown on purpose. Once the file belongs to uid 33 this
# user can no longer chmod it, so setting the mode while we are still the owner
# reaches the target state in one pass; doing it the other way round leaves the
# mode for the container to fix on its next load.
LAGER_SECRET_FILES="${LAGER_SECRET_FILES:-/etc/lager/org_secrets.json /etc/lager/secret_key}"
# uid 33 is www-data, the user the container runs as. Hardcoded because it is
# baked into the container image, not discovered at runtime.
LAGER_CONTAINER_UID="${LAGER_CONTAINER_UID:-33}"

# `find -uid` / `-perm` rather than `stat`, whose flags differ between GNU and
# BSD; this keeps the block runnable off-box by its unit test.
_owned_by_container_uid() {
    [ -n "$(find "$1" -maxdepth 0 -uid "$LAGER_CONTAINER_UID" 2>/dev/null)" ]
}

_normalize_secret_files() {
    for secret_file in $LAGER_SECRET_FILES; do
        [ -f "$secret_file" ] || continue

        # Succeeds only while this user still owns the file; a no-op once it
        # belongs to uid 33, which is the state we are trying to reach.
        chmod 600 "$secret_file" 2>/dev/null || true

        if ! _owned_by_container_uid "$secret_file"; then
            # sudo first: the box's NOPASSWD grant covers chown, and this is
            # the path that repairs a hand-copied file automatically. `-n` so a
            # box without the grant fails immediately instead of waiting for a
            # password nobody is there to type. Plain chown covers the case
            # where this script is already running as root.
            sudo -n chown "$LAGER_CONTAINER_UID:$LAGER_CONTAINER_UID" "$secret_file" 2>/dev/null \
                || chown "$LAGER_CONTAINER_UID:$LAGER_CONTAINER_UID" "$secret_file" 2>/dev/null \
                || true
        fi

        # Still not ours, and now unreadable by anyone but its owner: the
        # runtime cannot read its own secrets. Loud, because the symptom
        # otherwise is silently-absent secrets rather than an error.
        if ! _owned_by_container_uid "$secret_file" \
            && [ -n "$(find "$secret_file" -maxdepth 0 -perm 600 2>/dev/null)" ]; then
            echo ""
            echo "  ============================================================"
            echo "  WARNING: the container cannot read $secret_file"
            echo "  ============================================================"
            echo "  It is mode 0600 but not owned by uid $LAGER_CONTAINER_UID, which is the user"
            echo "  the container runs as. Secret injection will be EMPTY and any"
            echo "  in-container reader will fail with 'Permission denied'."
            echo ""
            echo "  Fix it with:"
            echo "    sudo chown $LAGER_CONTAINER_UID:$LAGER_CONTAINER_UID $secret_file && sudo chmod 600 $secret_file"
            echo ""
            echo "  \`lager update\` also repairs this automatically."
            echo "  ============================================================"
            echo ""
        fi
    done
}
_normalize_secret_files
# --- END secret-file ownership ---

# --- BEGIN authorized-keys sync (extracted verbatim by test/unit/box/test_authorized_keys_sync.py) ---
# Publish SSH keys from the key directory into ~/.ssh/authorized_keys.
#
# ONE key directory: /etc/lager/authorized_keys.d, durable across reboots. An
# external key manager — e.g. a control plane completing a first-time install
# from inside the container, before it has any SSH access to the box — drops
# <name>.pub files there via the /etc/lager bind mount, and this loop publishes
# them within ~5s. That is the bootstrap path and it must keep working.
#
# MARKER-BLOCK CONVENTION. This loop owns only the lines between the two
# sentinels below, and rewrites that region wholesale on every pass:
#   * deleting a .pub now actually REVOKES the key (the old append-only sync
#     could add keys but never remove them),
#   * a key can never be appended twice, so concurrent passes cannot duplicate
#     lines the way the old grep-then-append race did.
# Every line OUTSIDE the block is preserved byte-for-byte. That is what keeps
# keys installed by `lager ssh-setup` / ssh-copy-id / cloud-init — which never
# create a .pub here — from being revoked by this loop.
#
# Any other system that manages this file must claim its OWN distinct sentinel
# pair. Two managers sharing one pair would each rebuild the other's region from
# its own source and fight on every pass; distinct pairs is what lets them
# coexist, since each preserves everything outside its own block.
_AK_BEGIN="# BEGIN LAGER MANAGED KEYS (managed by start_box.sh — do not edit by hand)"
_AK_END="# END LAGER MANAGED KEYS"

# Overridable only so the unit tests can point it at a temp dir; production
# always uses the default.
LAGER_AUTHORIZED_KEYS_D="${LAGER_AUTHORIZED_KEYS_D:-/etc/lager/authorized_keys.d}"

_sync_authorized_keys() {
    local auth_keys="$HOME/.ssh/authorized_keys"
    local keys_dir="$LAGER_AUTHORIZED_KEYS_D"

    # A MISSING key directory is ambiguous — a transient /etc/lager mount or
    # permissions problem looks identical to "no keys" — so do nothing rather
    # than risk revoking every managed key. An existing but EMPTY directory is
    # unambiguous and does revoke, which is the point of the rebuild.
    [ -d "$keys_dir" ] || return 0

    mkdir -p "$HOME/.ssh" 2>/dev/null && chmod 700 "$HOME/.ssh" 2>/dev/null
    [ -f "$auth_keys" ] || { touch "$auth_keys" && chmod 600 "$auth_keys"; }

    local staged tmp count
    staged=$(mktemp) || return 0
    tmp=$(mktemp "$HOME/.ssh/.authorized_keys.XXXXXX") || { rm -f "$staged"; return 0; }
    chmod 600 "$tmp"

    (
        shopt -s nullglob
        for f in "$keys_dir"/*.pub; do
            [ -f "$f" ] || continue
            # Read line-by-line instead of `cat`: a .pub with no trailing
            # newline (common in generated files) would otherwise run straight
            # into the next key's line and corrupt both entries. The
            # `|| [ -n "$ak_line" ]` clause is what emits that final
            # newline-less line.
            while IFS= read -r ak_line || [ -n "$ak_line" ]; do
                case "$ak_line" in ''|\#*) continue ;; esac
                printf '%s\n' "$ak_line"
            done < "$f"
        done
    ) | awk '!seen[$0]++' > "$staged"

    # Rebuild in two parts:
    #  1. every line outside our block, minus any line that is itself a
    #     currently-staged key. Dropping those adopts copies that a previous
    #     append-only sync left loose in the file, and collapses the duplicate
    #     lines that the old race produced — without touching keys we do not
    #     manage (they are not in the key directory, so they are not dropped).
    #  2. our block, regenerated from the key directory.
    # The staged keys are loaded in BEGIN rather than with the usual two-file
    # `NR == FNR` idiom: when the key directory is empty the staged file is
    # empty too, and `NR == FNR` then stays true for every line of the SECOND
    # file — which swallows the whole of authorized_keys and revokes keys we do
    # not manage. Reading it here keeps the empty case correct.
    if ! awk -v b="$_AK_BEGIN" -v e="$_AK_END" -v staged_file="$staged" '
            BEGIN { while ((getline line < staged_file) > 0) staged[line] = 1 }
            $0 == b { inblock = 1; next }
            $0 == e { inblock = 0; next }
            inblock { next }
            ($0 in staged) { next }
            { print }
        ' "$auth_keys" > "$tmp"; then
        rm -f "$staged" "$tmp"
        return 0
    fi
    if [ -s "$staged" ]; then
        { printf '%s\n' "$_AK_BEGIN"; cat "$staged"; printf '%s\n' "$_AK_END"; } >> "$tmp"
    fi

    # No-op passes must not churn the file (this runs every 5 seconds).
    if cmp -s "$tmp" "$auth_keys"; then
        rm -f "$staged" "$tmp"
        return 0
    fi

    count=$(wc -l < "$staged" | tr -d '[:space:]')
    chmod 600 "$tmp"
    # Rename, never rewrite in place: sshd must only ever see a complete file.
    # Same directory, so this is atomic.
    if mv -f "$tmp" "$auth_keys"; then
        echo "  Rebuilt authorized_keys from $keys_dir ($count managed key(s))"
    else
        rm -f "$tmp"
    fi
    rm -f "$staged"
    return 0
}
# --- END authorized-keys sync ---

echo "Syncing SSH authorized keys..."
_sync_authorized_keys

# Background poller: publishes keys written to the key directory while the box
# is running (e.g. during a control-plane-driven first install, which waits a
# few seconds for exactly this). The single-instance guard at the top of this
# script means only one poller can be started; the PID file additionally stops
# the poller left behind by a PREVIOUS run, which outlives its parent.
#
# `9>&-` closes the inherited single-instance lock fd — without it this
# long-lived child would hold the lock forever and every later start_box.sh
# would refuse to run.
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
) 9>&- > /dev/null 2>&1 &
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

    # Ensure the well-known subtree used by RAM-resident flash loaders
    # exists (see lager/box/lager/debug/da1469x_loader.py). This lets
    # operators just `scp` a `flash_loader.elf` + `.elf.bin` pair into
    # `~/third_party/customer-binaries/openocd/flash-loaders/<family>/`
    # without first mkdir-ing the chain. Default perms (umask) are fine
    # because the container only reads these files; we don't chmod 777
    # like we do for the customer-binaries root (the container writes
    # uploaded binaries there as www-data, hence the wider perms above).
    # Best-effort — a failure here must not stop the container from
    # starting, since custom binaries / flash-loaders are an optional
    # feature.
    mkdir -p "$CUSTOMER_BIN_DIR/openocd/flash-loaders" 2>/dev/null || true

    # List uploaded binaries (files only — skips subdirectories like the
    # `openocd/` flash-loaders tree ensured above, which is intentionally
    # not a user-uploaded executable). `-L` follows symlinks before the
    # `-type f` test so a symlinked binary (e.g. `ln -s /usr/bin/jq jq`)
    # still surfaces, matching the `os.path.isfile()` filtering used by
    # `_handle_binaries_list` (lager/box/lager/python/service.py) and
    # `lager.binaries.runner.list_binaries`.
    binaries_found=$(find -L "$CUSTOMER_BIN_DIR" -mindepth 1 -maxdepth 1 -type f -printf '%f\n' 2>/dev/null | sort)
    if [ -n "$binaries_found" ]; then
        echo "  Binaries available:"
        echo "$binaries_found" | sed 's/^/    - /'
    else
        echo "  (no uploaded binaries)"
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

# Lager Box config (declarative). Optional. No-op when /etc/lager/box_config.json is absent.
# A malformed config is logged but never blocks the container start; the box always comes up.
#
# render_docker_args.py writes a bash-sourceable file declaring three arrays
# (BOX_CONFIG_MOUNTS, BOX_CONFIG_ENV, BOX_CONFIG_HOST_PATHS). Sourcing + array
# expansion preserves values containing whitespace / $ / quotes through to
# docker run — the previous stdout-and-unquoted-expansion path mangled them.
BOX_CONFIG_FILE="/etc/lager/box_config.json"
BOX_CONFIG_ARGS_FILE="/etc/lager/box_config.docker.sh"
PIP_REQS_FILE="/etc/lager/user_requirements.txt"
CARGO_PKGS_FILE="/etc/lager/cargo_packages.txt"
NPM_PKGS_FILE="/etc/lager/npm_packages.txt"
BOX_CONFIG_MOUNTS=()
BOX_CONFIG_ENV=()
BOX_CONFIG_HOST_PATHS=()
# Set by any renderer that fails below. The container still comes up (that is a
# hard requirement of this script), but the script exits 3 at the end so the
# caller can tell "box is up AND config applied" from "box is up but the config
# did NOT apply". See the exit at the bottom of this file.
BOX_CONFIG_RENDER_FAILED=0
if [ -f "$BOX_CONFIG_FILE" ]; then
    echo "Lager Box config detected at $BOX_CONFIG_FILE"
    if ! python3 "${SCRIPT_DIR}/lager/box_config/render_docker_args.py" \
            "$BOX_CONFIG_FILE" "$BOX_CONFIG_ARGS_FILE"; then
        echo "[ERROR] Could not render docker args from box_config.json (see above);"
        echo "        the container will start from the PREVIOUS render if one exists"
        echo "        (stale mounts/volumes/env), or with none at all if it does not."
        BOX_CONFIG_RENDER_FAILED=1
    fi
    # Source whether the renderer succeeded or not — on failure it still
    # writes empty arrays, which is the right "no box-config" state. Skipping
    # the source on failure would leave stale arrays from any earlier run in
    # the same shell.
    if [ -f "$BOX_CONFIG_ARGS_FILE" ]; then
        # shellcheck disable=SC1090
        source "$BOX_CONFIG_ARGS_FILE"
    fi
    if [ ${#BOX_CONFIG_MOUNTS[@]} -gt 0 ]; then
        echo "  Mount/volume args (${#BOX_CONFIG_MOUNTS[@]}): ${BOX_CONFIG_MOUNTS[*]}"
    fi
    if [ ${#BOX_CONFIG_ENV[@]} -gt 0 ]; then
        echo "  Env args (${#BOX_CONFIG_ENV[@]}): ${BOX_CONFIG_ENV[*]}"
    fi
    # Pre-create bind-mount host paths so `docker run` doesn't fail when a
    # path is declared in box_config.json but doesn't exist yet on the host.
    # We can only mkdir as the current user (lagerdata); root-owned parents
    # like /srv still need a one-time `sudo mkdir + chown 33:33` from the
    # operator. Failures here are warnings, never fatal.
    for _p in "${BOX_CONFIG_HOST_PATHS[@]}"; do
        if [ ! -d "$_p" ]; then
            if mkdir -p "$_p" 2>/dev/null; then
                echo "  Created host path $_p"
            else
                echo "  [WARNING] Could not create $_p (try: sudo mkdir -p $_p && sudo chown 33:33 $_p)"
            fi
        fi
    done
    unset _p

    # Render pip_packages from box_config.json into the requirements file that
    # the in-container `pip install -r` step (below) reads. A render failure is
    # NOT fatal to the container — it always comes up — but it is fatal to the
    # apply: the install steps below key off these files, so a failed render
    # means the box silently runs the previous package set (or none at all).
    # BOX_CONFIG_RENDER_FAILED makes the script exit non-zero at the end so
    # `lager box config apply` reports failure instead of stamping the
    # applied-hash on a config it never actually applied.
    if ! python3 "${SCRIPT_DIR}/lager/box_config/render_pip_requirements.py" \
            "$BOX_CONFIG_FILE" "$PIP_REQS_FILE" 2>&1; then
        echo "[ERROR] Failed to render pip_packages; using existing $PIP_REQS_FILE."
        BOX_CONFIG_RENDER_FAILED=1
    fi

    # Same idea for cargo_packages: render to a flat file the post-run loop
    # below reads, one crate spec per non-comment line.
    if ! python3 "${SCRIPT_DIR}/lager/box_config/render_cargo_packages.py" \
            "$BOX_CONFIG_FILE" "$CARGO_PKGS_FILE" 2>&1; then
        echo "[ERROR] Failed to render cargo_packages; using existing $CARGO_PKGS_FILE."
        BOX_CONFIG_RENDER_FAILED=1
    fi

    # Same for npm_packages — flat file the post-run loop reads. Container must
    # ship npm in its image (or have nodejs+npm in apt_packages); a missing
    # `npm` binary fails the install loop with a clear error.
    if ! python3 "${SCRIPT_DIR}/lager/box_config/render_npm_packages.py" \
            "$BOX_CONFIG_FILE" "$NPM_PKGS_FILE" 2>&1; then
        echo "[ERROR] Failed to render npm_packages; using existing $NPM_PKGS_FILE."
        BOX_CONFIG_RENDER_FAILED=1
    fi
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

# Start the Lager container. The ports below are what the container listens
# on; whether they are published on the host is decided by PORT_PUBLISH_ARGS
# further down, which is empty under --no-publish.
# Port 5000: Python Execution Service (replaces controller)
# Port 8100: MCP Server (AI agent integration)
# Port 8765: Debug Service
# Port 9000: UART HTTP+WebSocket Server
# Port 8081-8090: Remote debugging (PDB, etc.)
# Port 2331-2342: GDB / SWO / Telnet ports for the GDB-side window (3 ports
#   per slot × 4 slots). Slot N gets GDB=2331+3N, SWO=2332+3N, Telnet=2333+3N.
#   This window is shared between the J-Link and OpenOCD backends — the
#   server type bound to a port is determined by which probe occupies that
#   slot. The 3-port stride is required because JLinkGDBServer's hardcoded
#   SWO/Telnet defaults (2332/2333) collide with adjacent slots if the
#   stride is 1; OpenOCD just leaves SWO/Telnet unused.
# Port 4444-4447: OpenOCD interactive telnet (one port per slot, slot N=4444+N).
# Port 6666-6669: OpenOCD TCL/RPC (one port per slot, slot N=6666+N). The
#   lager debug service dispatches all OpenOCD runtime commands (flash/erase/
#   reset/memrd/RTT) through these ports.
# Port 9090-9097: RTT telnet (two channels per probe slot; up to 4 probes × 2
#   channels). Both J-Link (RTTTelnetPort) and OpenOCD (rtt server start)
#   bind to this range — slot N's RTT base is 9090+2N.
# NOTE: the hard-coded -v list below is reproduced as RESERVED_CONTAINER_PATHS
# in box/lager/box_config/config.py so that `lager box config mount add`
# rejects user mounts that would collide. If you add or rename anything here,
# update the constant too — otherwise users can write a config that validates
# but blows up at `docker run` mid-bounce with "Duplicate mount point".
#
# The two `lager-cargo` / `lager-npm-global` named volumes persist
# user-installed cargo crates and global npm packages across container
# recreation. Without them, every `lager update` (or `box config apply`)
# rebuilds the container from scratch and the post-run loops below recompile
# `cargo install` packages from source — minutes per update. With them, the
# second-and-onward run sees "already installed" and finishes in seconds.
# `lager update` wipes both volumes alongside `docker rmi lager` whenever
# the build-hash (Dockerfile + requirements.txt) changes, so a Dockerfile
# rustup/node bump can't leave a stale toolchain in the volume.
#
# The container's mount convention puts user files at /home/www-data/... (see
# the -v lines below and BOX_CONFIG_MOUNTS). Override HOME so ~-aware tools
# (cargo's env file, ssh's default config, pip user installs, etc.) find them.
# Without this, $HOME defaults to /var/www per /etc/passwd and ~-expansion
# misses everything.

# Instrument device access: udev_rules/99-instrument.rules sets instrument
# device nodes to MODE 0660, GROUP "lager" on the host. The container runs as
# www-data, so it needs the host group's GID as a supplementary group to open
# the devices. Resolved numerically because the GID mapping is what matters
# inside the container, not the group name.
LAGER_GROUP_ADD=()
LAGER_GID="$(getent group lager | cut -d: -f3)"
if [ -n "$LAGER_GID" ]; then
    LAGER_GROUP_ADD=(--group-add "$LAGER_GID")
else
    echo "[WARNING] Host group 'lager' not found. Instrument udev rules grant"
    echo "          access via GROUP=\"lager\"; without it the container cannot"
    echo "          open instrument USB devices. Create it with:"
    echo "              sudo groupadd lager && sudo udevadm trigger"
fi

# Host port publishing. Empty under --no-publish: lagernet-only, a reverse
# proxy on the same network owns the host ports.
PORT_PUBLISH_ARGS=()
if [ -z "$NO_PUBLISH" ]; then
    PORT_PUBLISH_ARGS=(
        -p 5000:5000
        -p 8301:5000
        -p 8080:8080
        -p 8081-8090:8081-8090
        -p 8100:8100
        -p 8765:8765
        -p 9000:9000
        -p 2331-2342:2331-2342
        -p 4444-4447:4444-4447
        -p 6666-6669:6666-6669
        -p 9090-9097:9090-9097
    )
else
    echo "Port publishing disabled (--no-publish): container reachable via lagernet only"
fi

docker run -d \
    --network lagernet \
    --privileged \
    "${LAGER_GROUP_ADD[@]}" \
    -v /tmp:/tmp \
    -v /dev:/dev \
    -v /sys/bus/usb:/sys/bus/usb:ro \
    -v /sys/devices:/sys/devices:ro \
    -v /var/run/dbus:/var/run/dbus \
    -v /etc/lager:/etc/lager \
    -v /home/lagerdata/.ssh:/home/www-data/.ssh \
    -v /etc/hostname:/host/etc/hostname:ro \
    -v /opt/SEGGER:/opt/SEGGER:ro \
    -v /opt/picoscope/lib:/opt/picoscope/lib:ro \
    -v lager-cargo:/opt/rust/cargo \
    -v lager-npm-global:/home/www-data/.npm-global \
    ${JL_MOUNT_PYTHON} \
    ${CUSTOMER_BINARIES_MOUNT} \
    ${OSCILLOSCOPE_MOUNT} \
    "${BOX_CONFIG_MOUNTS[@]}" \
    "${BOX_CONFIG_ENV[@]}" \
    "${PORT_PUBLISH_ARGS[@]}" \
    --env "PIGPIO_ADDR=$PIGPIO_ADDR" \
    --env "LAGER_HOST=$DOCKER_IFACE" \
    --env "PYTHONBREAKPOINT=lager.breakpoint.pause" \
    --env "LOCAL_ADDRESS=172.18.0.10" \
    -e HOME=/home/www-data \
    --log-driver json-file \
    --log-opt max-size=10m \
    --log-opt max-file=3 \
    --name lager \
    --restart always \
    lager

echo "Lager Box container started"
echo ""

# Install user-requested pip packages from box_config.json into the running
# container. The renderer above already wrote /etc/lager/user_requirements.txt
# from BoxConfig.pip_packages; here we apply it. Skipped when the file has no
# non-comment content. A failure here exits the script non-zero so
# `lager box config apply` can detect it and skip updating applied-hash.
#
# Timeouts: pip is bounded at PIP_INSTALL_TIMEOUT seconds so a stuck dep
# resolver or wedged download can't run past the SSH ceiling in
# _bounce_container (~900s) and split-brain with the CLI's rollback path.
PIP_INSTALL_TIMEOUT="${LAGER_PIP_INSTALL_TIMEOUT:-300}"
CARGO_INSTALL_TIMEOUT="${LAGER_CARGO_INSTALL_TIMEOUT:-180}"
NPM_INSTALL_TIMEOUT="${LAGER_NPM_INSTALL_TIMEOUT:-180}"
if [ -s "$PIP_REQS_FILE" ] && grep -qvE '^[[:space:]]*(#|$)' "$PIP_REQS_FILE"; then
    echo "Installing user pip packages into container (timeout: ${PIP_INSTALL_TIMEOUT}s)..."
    # Wait briefly for the container's services to be ready enough to exec.
    for _ in 1 2 3 4 5; do
        docker exec lager true 2>/dev/null && break
        sleep 1
    done
    # if/else (not `if ! ...`) so $? inside the else branch is the actual
    # exit code of the timeout/docker-exec — `!` inverts the exit status
    # and `$?` after `!cmd` is always 0 or 1, not the underlying rc.
    if timeout "$PIP_INSTALL_TIMEOUT" docker exec lager pip3 install -r "$PIP_REQS_FILE"; then
        :
    else
        _rc=$?
        if [ "$_rc" -eq 124 ]; then
            echo "[ERROR] User pip install timed out after ${PIP_INSTALL_TIMEOUT}s; container is up but pip_packages may be incomplete."
        else
            echo "[ERROR] User pip install failed (rc=$_rc); container is up but pip_packages may be incomplete."
        fi
        unset _rc
        # Exit 3, not 1: the container is UP (these installs run after `docker run`),
        # the config just didn't fully apply. `lager box config apply` must not roll
        # back a healthy container, and must not stamp the applied-hash.
        exit 3
    fi
    echo ""
fi

# Install user-requested cargo crates from box_config.json into the running
# container. Cargo install is idempotent (skips already-installed at the same
# version with a warning, no error), so always running this is safe.
#
# Per-crate timeout (vs. one timeout for the whole loop) so a single
# slow-compiling crate doesn't budget-starve the others, and so the user
# gets a precise error pointing at the culprit.
if [ -s "$CARGO_PKGS_FILE" ] && grep -qvE '^[[:space:]]*(#|$)' "$CARGO_PKGS_FILE"; then
    echo "Installing user cargo crates into container (per-crate timeout: ${CARGO_INSTALL_TIMEOUT}s)..."
    for _ in 1 2 3 4 5; do
        docker exec lager true 2>/dev/null && break
        sleep 1
    done
    _cargo_failed=0
    while IFS= read -r _crate_spec; do
        # Skip blanks and comments.
        case "$_crate_spec" in
            ''|\#*) continue ;;
        esac
        # Translate `name@version` to `name --version version` for cargo
        # install. Bare `name` passes through unchanged.
        if [[ "$_crate_spec" == *"@"* ]]; then
            _name="${_crate_spec%@*}"
            _ver="${_crate_spec#*@}"
            _args=("$_name" "--version" "$_ver")
        else
            _args=("$_crate_spec")
        fi
        # `bash -c` (not `bash -lc`): a login shell re-sources /etc/profile
        # which resets PATH and drops /opt/rust/cargo/bin from the Dockerfile's
        # ENV PATH, making `cargo` unfindable. Non-login `-c` inherits docker
        # ENV cleanly.
        if timeout "$CARGO_INSTALL_TIMEOUT" docker exec lager bash -c "cargo install ${_args[*]}"; then
            :
        else
            _rc=$?
            if [ "$_rc" -eq 124 ]; then
                echo "[ERROR] cargo install timed out after ${CARGO_INSTALL_TIMEOUT}s for: $_crate_spec"
            else
                echo "[ERROR] cargo install failed (rc=$_rc) for: $_crate_spec"
            fi
            _cargo_failed=1
        fi
    done < "$CARGO_PKGS_FILE"
    unset _crate_spec _name _ver _args _rc
    if [ "$_cargo_failed" -ne 0 ]; then
        echo "[ERROR] One or more cargo crates failed to install; container is up but cargo_packages may be incomplete."
        # Exit 3, not 1: the container is UP (these installs run after `docker run`),
        # the config just didn't fully apply. `lager box config apply` must not roll
        # back a healthy container, and must not stamp the applied-hash.
        exit 3
    fi
    echo ""
fi

# Install user-requested npm packages globally inside the container. `npm
# install -g <spec>` accepts both `name` and `name@version` directly (no
# arg splitting needed). Per-package timeout mirrors the cargo loop so one
# slow registry response can't dominate the SSH budget.
if [ -s "$NPM_PKGS_FILE" ] && grep -qvE '^[[:space:]]*(#|$)' "$NPM_PKGS_FILE"; then
    echo "Installing user npm packages into container (per-package timeout: ${NPM_INSTALL_TIMEOUT}s)..."
    for _ in 1 2 3 4 5; do
        docker exec lager true 2>/dev/null && break
        sleep 1
    done
    _npm_failed=0
    while IFS= read -r _npm_spec; do
        case "$_npm_spec" in
            ''|\#*) continue ;;
        esac
        # See cargo loop above for why `bash -c` (not `bash -lc`).
        if timeout "$NPM_INSTALL_TIMEOUT" docker exec lager bash -c "npm install -g $_npm_spec"; then
            :
        else
            _rc=$?
            if [ "$_rc" -eq 124 ]; then
                echo "[ERROR] npm install timed out after ${NPM_INSTALL_TIMEOUT}s for: $_npm_spec"
            else
                echo "[ERROR] npm install failed (rc=$_rc) for: $_npm_spec"
            fi
            _npm_failed=1
        fi
    done < "$NPM_PKGS_FILE"
    unset _npm_spec _rc
    if [ "$_npm_failed" -ne 0 ]; then
        echo "[ERROR] One or more npm packages failed to install; container is up but npm_packages may be incomplete."
        # Exit 3, not 1: the container is UP (these installs run after `docker run`),
        # the config just didn't fully apply. `lager box config apply` must not roll
        # back a healthy container, and must not stamp the applied-hash.
        exit 3
    fi
    echo ""
fi

echo "========================================"
echo "Box started successfully!"
echo "========================================"
echo ""
# Under --no-publish these are container ports on lagernet, not host ports, so
# every "<box-ip>:<port>" instruction below is wrong for that box. Say which it
# is rather than printing the published form unconditionally.
if [ -n "$NO_PUBLISH" ]; then
    echo "Services running (lagernet only -- ports are NOT published on the host):"
    echo "  Reach them at the container's lagernet address, or through the reverse"
    echo "  proxy that owns the host ports. <box-ip>:<port> will not connect."
else
    echo "Services running:"
fi
echo "  - Python Execution Service: port 5000 (and 8301 for backwards compatibility)"
if [ -n "$NO_PUBLISH" ]; then
    echo "  - MCP Server (AI): port 8100 (MCP clients: lagernet address, not <box-ip>)"
else
    echo "  - MCP Server (AI): port 8100 (MCP clients: http://<box-ip>:8100/mcp)"
fi
echo "  - Debug Service: port 8765"
echo "  - UART HTTP+WebSocket: port 9000"
echo "  - Remote PDB: ports 8081-8090"
echo "  - Debug GDB / SWO / Telnet (J-Link or OpenOCD): ports 2331-2342 (3 per slot × 4 slots)"
echo "  - OpenOCD interactive telnet: ports 4444-4447 (one per slot)"
echo "  - OpenOCD TCL/RPC: ports 6666-6669 (one per slot)"
echo "  - RTT telnet (J-Link or OpenOCD): ports 9090-9097 (2 channels × 4 slots)"
echo ""
echo "IMPORTANT: The controller container is NO LONGER NEEDED!"
echo "  All functionality has been moved to the lager container."
echo ""
docker ps --filter "name=lager"

# The container is up either way — that is deliberate, a box must never be left
# down by a bad config. But if any box_config renderer failed above, the config
# on disk is NOT what this container is running, so exit non-zero to say so.
#
# Exit 3 specifically, not 1: a failed `docker run` (exit 1) means the container
# is GONE and `lager box config apply` must roll back to the last good config.
# Here the container is healthy and the fault is environmental (typically
# /etc/lager not writable by this user), so rolling back and re-bouncing would
# fail in exactly the same way. Exit 3 tells apply: report the failure, do not
# stamp the applied-hash, do not roll back.
if [ "$BOX_CONFIG_RENDER_FAILED" = "1" ]; then
    echo ""
    echo "[ERROR] The container is running, but box_config.json was NOT applied"
    echo "        (one or more renderers failed above)."
    echo "        If this is a permissions error on /etc/lager, repair it with:"
    echo "            lager update --box <BOX>"
    exit 3
fi
