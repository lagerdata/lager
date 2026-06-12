#!/usr/bin/env bash
#
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# Deploy the in-tree box/lager/http_handlers/lock_handler.py onto a
# tester box without pushing the branch to GitHub.
#
# Why this exists: ``lager install --version <branch>`` clones from
# ``https://github.com/lagerdata/lager.git``, which a fork-only
# contributor cannot push to. This script bypasses the deploy script
# entirely and does a targeted ``docker cp`` of just the lock handler
# file, snapshotting the prior file so you can roll back.
#
# Usage:
#   test/manual/deploy_branch_to_tester.sh <BOX_NAME_OR_IP> [--user USER] [--password]
#   test/manual/deploy_branch_to_tester.sh <BOX_NAME_OR_IP> --restore
#
# Env vars (override SSH transport without editing this file):
#   LAGER_BOX_SSH_USER   default user (overrides .lager config / "lagerdata")
#   LAGER_BOX_SSH_KEY    path to private key (default: ~/.ssh/lager_box if present)
#   LAGER_BOX_SSH_PASS   if set to "1", use interactive password auth (no BatchMode)
#
# Workflow (typical pre-merge check):
#   pip install -e ./cli                                  # new CLI locally
#   test/manual/deploy_branch_to_tester.sh tester         # new server on box
#   test/manual/hardware_lock_smoke.sh tester             # run smoke
#   test/manual/deploy_branch_to_tester.sh tester --restore   # roll back code
#
# Notes:
#   - The smoke script does its own /etc/lager snapshot+restore for *data*;
#     this script handles *code* rollback for the one file we changed.
#   - Both snapshots live under /tmp on the box, so they survive an SSH
#     drop but not a reboot.

set -u
set -o pipefail

if [ $# -lt 1 ]; then
    cat <<EOF >&2
Usage: $0 <BOX_NAME_OR_IP> [--user USER] [--password]
       $0 <BOX_NAME_OR_IP> --restore

Deploys box/lager/http_handlers/lock_handler.py onto the box via
'docker cp' and restarts the lager container. Use --restore to undo.

Env vars: LAGER_BOX_SSH_USER, LAGER_BOX_SSH_KEY, LAGER_BOX_SSH_PASS=1
EOF
    exit 1
fi

BOX="$1"
shift

MODE="deploy"
BOX_SSH_USER="${LAGER_BOX_SSH_USER:-}"
USE_PASSWORD="${LAGER_BOX_SSH_PASS:-0}"

while [ $# -gt 0 ]; do
    case "$1" in
        --restore) MODE="restore"; shift ;;
        --user)    BOX_SSH_USER="$2"; shift 2 ;;
        --user=*)  BOX_SSH_USER="${1#*=}"; shift ;;
        --password|-p) USE_PASSWORD=1; shift ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

# Files to deploy. Each entry is "<local-path-relative-to-repo>:<container-path>".
# The lager container does NOT bind-mount the source tree (see
# box/start_box.sh — only /tmp, /dev, /etc/lager, etc. are mounted; the
# Python code is baked in via COPY in box/lager/docker/box.Dockerfile).
# Hence the docker cp approach below.
#
# IMPORTANT: keep this list in sync with the files actually changed on
# the branch. Stale entries are harmless (no-ops if the source file
# isn't different), but a missing entry will produce a half-deployed
# box that runs OLD code on some endpoints. The previous version of
# this script only deployed lock_handler.py and silently left the
# port-5000 server (python/service.py) running stale code, which
# wasted hours.
FILES_TO_DEPLOY=(
    "box/lager/lock_state.py:/app/lager/lager/lock_state.py"
    "box/lager/http_handlers/lock_handler.py:/app/lager/lager/http_handlers/lock_handler.py"
    "box/lager/python/service.py:/app/lager/lager/python/service.py"
)

# Files we keep a per-file snapshot of on the box for --restore.
# Named by the basename of the file so concurrent deploys to the same
# box don't clobber each other's backups.
BOX_STAGE_DIR="/tmp/lager_branch_deploy"
BOX_BACKUP_DIR="/tmp/lager_branch_deploy.orig"

# Resolve box -> IP via the lager CLI, and pick up the stored SSH user
# if the caller didn't override it. Matches the resolution policy used
# by the rest of the lager CLI (see cli/commands/box/_ssh.py).
if [[ "$BOX" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    BOX_IP="$BOX"
else
    if ! command -v lager >/dev/null 2>&1; then
        echo "ERROR: '$BOX' is not an IP and 'lager' CLI is not on PATH." >&2
        exit 1
    fi
    BOX_LINE="$(lager boxes 2>/dev/null | awk -v n="$BOX" '$1==n {print; exit}')"
    if [ -z "$BOX_LINE" ]; then
        echo "Could not resolve '$BOX' to an IP. Add it with:" >&2
        echo "  lager boxes add --name $BOX --ip <IP> --user <ssh-user>" >&2
        exit 1
    fi
    BOX_IP="$(echo "$BOX_LINE" | awk '{print $2}')"
    # `lager boxes` columns differ between versions; if there's a 3rd
    # column it's typically the SSH user. Only adopt it when --user
    # wasn't supplied.
    if [ -z "$BOX_SSH_USER" ]; then
        col3="$(echo "$BOX_LINE" | awk '{print $3}')"
        if [ -n "$col3" ] && [[ ! "$col3" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            BOX_SSH_USER="$col3"
        fi
    fi
fi

# Final SSH user fallback.
BOX_SSH_USER="${BOX_SSH_USER:-lagerdata}"
BOX_SSH="${BOX_SSH_USER}@${BOX_IP}"

# Build SSH/scp option arrays. We mirror the policy in
# cli/commands/box/_ssh.py: prefer ~/.ssh/lager_box if present (that's
# the key `lager install` provisions), then your default agent/key,
# and only drop to interactive password if the caller asks for it.
LAGER_KEY="${LAGER_BOX_SSH_KEY:-$HOME/.ssh/lager_box}"

# Non-sudo commands (file existence checks, scp): BatchMode is fine and
# lets us fail fast on auth errors instead of hanging on a prompt.
SSH_OPTS=( -o ConnectTimeout=10 )
# Sudo commands (docker cp / docker restart): must have a TTY so sudo
# can read a password. -tt forces tty allocation even when the local
# stdin isn't a terminal.
SSH_OPTS_SUDO=( -tt -o ConnectTimeout=10 )

if [ "$USE_PASSWORD" = "1" ]; then
    echo "  (password auth enabled — you may be prompted multiple times)"
    for arr in SSH_OPTS SSH_OPTS_SUDO; do
        eval "$arr+=( -o NumberOfPasswordPrompts=3 -o PreferredAuthentications=password,keyboard-interactive )"
    done
else
    SSH_OPTS+=( -o BatchMode=yes )
    # SSH_OPTS_SUDO deliberately does NOT set BatchMode — sudo needs to
    # prompt. Key auth still works because the -i flag below is added
    # for both arrays.
    if [ -f "$LAGER_KEY" ]; then
        SSH_OPTS+=( -i "$LAGER_KEY" )
        SSH_OPTS_SUDO+=( -i "$LAGER_KEY" )
    fi
fi

ssh_box() { ssh "${SSH_OPTS[@]}" "$BOX_SSH" "$@"; }
ssh_box_sudo() { ssh "${SSH_OPTS_SUDO[@]}" "$BOX_SSH" "$@"; }
scp_to_box() { scp "${SSH_OPTS[@]}" "$1" "${BOX_SSH}:$2"; }

check_ssh() {
    if ! ssh_box 'echo ok' >/dev/null 2>&1; then
        cat <<EOF >&2

ERROR: SSH to ${BOX_SSH} failed.

Tried: $(echo "${SSH_OPTS[*]}")

Pick one of:
  1. If you have the lager-provisioned key, put it at $LAGER_KEY
  2. Authorize your own default key:
       ssh-copy-id ${BOX_SSH}
  3. Re-run with password auth:
       $0 $BOX --password $([ "$MODE" = "restore" ] && echo "--restore" || true)
  4. Use a different SSH user you already have access to:
       $0 $BOX --user <your-user>
     (or:  LAGER_BOX_SSH_USER=<your-user> $0 $BOX )
EOF
        exit 1
    fi
}

wait_for_http() {
    local attempts=0
    while ! curl -s -m 3 "http://${BOX_IP}:5000/lock" >/dev/null 2>&1; do
        attempts=$((attempts + 1))
        if [ "$attempts" -gt 30 ]; then
            echo "WARNING: box HTTP server did not come back within 30s" >&2
            return 1
        fi
        sleep 1
    done
    echo "  HTTP server back after ${attempts}s"
}

deploy() {
    # 1. Verify all local files exist before touching the box.
    local local_path container_path entry missing=0
    for entry in "${FILES_TO_DEPLOY[@]}"; do
        local_path="${entry%%:*}"
        container_path="${entry##*:}"
        if [ ! -f "$REPO_ROOT/$local_path" ]; then
            echo "ERROR: $REPO_ROOT/$local_path not found" >&2
            missing=1
        fi
    done
    [ "$missing" = "0" ] || exit 1

    echo "Deploying ${#FILES_TO_DEPLOY[@]} file(s) to ${BOX_SSH}:"
    for entry in "${FILES_TO_DEPLOY[@]}"; do
        echo "  $(basename "${entry%%:*}") -> ${entry##*:}"
    done
    check_ssh

    # 2. Stage all files under one tmpdir on the box. scp can't write
    #    into a missing dir, so create it first via the non-sudo
    #    ssh channel.
    ssh_box "mkdir -p '$BOX_STAGE_DIR'" \
        || { echo "ERROR: could not create $BOX_STAGE_DIR on box" >&2; exit 1; }

    for entry in "${FILES_TO_DEPLOY[@]}"; do
        local_path="${entry%%:*}"
        scp_to_box "$REPO_ROOT/$local_path" "$BOX_STAGE_DIR/$(basename "$local_path")"
    done

    # 3. Snapshot + docker cp + docker restart in ONE ssh-with-tty
    #    session so sudo's credential cache gives at most one prompt.
    #    The remote script is a single argument so heredoc/stdin
    #    doesn't fight sudo for the TTY.
    local remote_script
    remote_script="set -e
mkdir -p '$BOX_BACKUP_DIR'
"
    for entry in "${FILES_TO_DEPLOY[@]}"; do
        local_path="${entry%%:*}"
        container_path="${entry##*:}"
        local fname
        fname="$(basename "$local_path")"
        # Snapshot the in-container file ONLY on the first deploy.
        # `docker cp` from container -> host may fail if the path
        # doesn't exist in the container yet (e.g. lock_state.py on a
        # box that hasn't been rebuilt yet). Treat that as "no snapshot
        # needed" rather than fatal — restore can simply rm the file.
        remote_script+="
if [ -f '$BOX_BACKUP_DIR/$fname' ]; then
    echo '  preserving existing snapshot of $fname'
else
    if sudo docker cp 'lager:$container_path' '$BOX_BACKUP_DIR/$fname' 2>/dev/null; then
        echo '  snapshot lager:$container_path -> $BOX_BACKUP_DIR/$fname'
    else
        echo 'MISSING' | sudo tee '$BOX_BACKUP_DIR/$fname.absent' >/dev/null
        echo '  $container_path absent in container (first deploy of a new file)'
    fi
fi
echo '  docker cp $BOX_STAGE_DIR/$fname -> lager:$container_path'
sudo docker cp '$BOX_STAGE_DIR/$fname' 'lager:$container_path'
"
    done
    remote_script+="
echo '  docker restart lager'
sudo docker restart lager
"
    echo "  (you may be prompted for the sudo password on the box)"
    ssh_box_sudo "$remote_script" \
        || { echo "ERROR: remote docker cp / restart failed" >&2; exit 1; }

    wait_for_http

    # 4. Sanity check: actually probe behavior. The new server echoes
    #    `holder_type` and `ttl_seconds` back in the POST /lock response;
    #    the old server ignores both fields. Route-existence checks lie
    #    (Flask returns 404 either way) — only the response body proves
    #    the new code is loaded.
    local probe
    probe=$(curl -s --max-time 5 \
        -X POST "http://${BOX_IP}:5000/lock" \
        -H 'Content-Type: application/json' \
        -d '{"user":"deploy-probe","holder_type":"ci","ttl_seconds":5}')
    # Best-effort cleanup whichever code is running.
    curl -s --max-time 5 \
        -X POST "http://${BOX_IP}:5000/unlock" \
        -H 'Content-Type: application/json' \
        -d '{"user":"deploy-probe","force":true}' >/dev/null
    if echo "$probe" | grep -q '"holder_type"' && echo "$probe" | grep -q '"ttl_seconds"'; then
        echo "  Sanity probe: new code is live (response includes holder_type + ttl_seconds)."
    else
        cat >&2 <<EOF

  ERROR: Sanity probe says the OLD lock_handler is still serving requests.
         Response was: $probe

         Things to check on the box:
           sudo docker exec lager grep -c lock_heartbeat \\
               /app/lager/lager/http_handlers/lock_handler.py
             # must be > 0; if 0, the docker cp didn't take

           sudo docker logs lager --tail 50
             # look for Flask startup or import errors

           sudo docker exec lager find /app/lager/lager/http_handlers \\
               -name '*.pyc' -newer /tmp/lock_handler.py.new
             # stale .pyc would override the .py — extremely unlikely

         If the file is present and looks new, try forcing a fresh
         container restart:
           sudo docker stop lager && sudo docker start lager
EOF
        exit 1
    fi
}

restore() {
    check_ssh
    if ! ssh_box "test -d '$BOX_BACKUP_DIR'"; then
        echo "ERROR: no snapshot dir at $BOX_BACKUP_DIR on the box; nothing to restore." >&2
        echo "       (Did you run --restore before deploying?)" >&2
        exit 1
    fi

    echo "Restoring files snapshotted under $BOX_BACKUP_DIR"
    local entry container_path fname
    local remote_script="set -e
"
    for entry in "${FILES_TO_DEPLOY[@]}"; do
        container_path="${entry##*:}"
        fname="$(basename "${entry%%:*}")"
        # If the file was absent in the container at first deploy
        # (lock_state.py before any image build), restore = remove it
        # from the container.
        remote_script+="
if sudo test -f '$BOX_BACKUP_DIR/$fname.absent'; then
    echo '  removing $container_path (was absent before deploy)'
    sudo docker exec lager rm -f '$container_path' || true
elif sudo test -f '$BOX_BACKUP_DIR/$fname'; then
    echo '  docker cp $BOX_BACKUP_DIR/$fname -> lager:$container_path'
    sudo docker cp '$BOX_BACKUP_DIR/$fname' 'lager:$container_path'
else
    echo '  no snapshot for $fname (skipping)'
fi
"
    done
    remote_script+="
echo '  docker restart lager'
sudo docker restart lager
echo '  clearing snapshot + stage dirs'
sudo rm -rf '$BOX_BACKUP_DIR' '$BOX_STAGE_DIR'
"
    echo "  (you may be prompted for the sudo password on the box)"
    ssh_box_sudo "$remote_script" \
        || { echo "ERROR: remote restore failed" >&2; exit 1; }
    wait_for_http
    echo "  Snapshot cleared; deploy again to re-test."
}

case "$MODE" in
    deploy) deploy ;;
    restore) restore ;;
    *) echo "Unknown mode: $MODE" >&2; exit 1 ;;
esac
