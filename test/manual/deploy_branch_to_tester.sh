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
LOCAL_HANDLER="$REPO_ROOT/box/lager/http_handlers/lock_handler.py"
CONTAINER_HANDLER="/box/lager/http_handlers/lock_handler.py"
BOX_STAGED="/tmp/lock_handler.py.new"
BOX_BACKUP="/tmp/lock_handler.py.orig"

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
    if [ ! -f "$LOCAL_HANDLER" ]; then
        echo "ERROR: $LOCAL_HANDLER not found" >&2
        exit 1
    fi

    echo "Deploying $LOCAL_HANDLER"
    echo "         -> ${BOX_SSH}:${CONTAINER_HANDLER}"
    check_ssh

    # 1. Stage the file on the box (no sudo needed for scp to /tmp).
    scp_to_box "$LOCAL_HANDLER" "$BOX_STAGED"

    # 2. Snapshot + docker cp + docker restart in ONE ssh-with-tty
    #    session so sudo's credential cache means at most one password
    #    prompt for this script. The remote script is sent as a single
    #    argument so heredoc/stdin doesn't fight sudo for the TTY.
    local remote_script
    remote_script=$(cat <<EOF
set -e
if [ -f "$BOX_BACKUP" ]; then
    echo "  Existing snapshot preserved at $BOX_BACKUP"
else
    echo "  Snapshotting current $CONTAINER_HANDLER -> $BOX_BACKUP"
    sudo docker cp lager:$CONTAINER_HANDLER $BOX_BACKUP
fi
echo "  docker cp $BOX_STAGED -> lager:$CONTAINER_HANDLER"
sudo docker cp $BOX_STAGED lager:$CONTAINER_HANDLER
echo "  docker restart lager"
sudo docker restart lager
EOF
)
    echo "  (you may be prompted for the sudo password on the box)"
    ssh_box_sudo "$remote_script" \
        || { echo "ERROR: remote docker cp / restart failed" >&2; exit 1; }

    wait_for_http

    # 4. Sanity check: the new heartbeat endpoint must be there.
    local rc
    rc=$(curl -s -o /dev/null -w '%{http_code}' \
        -X POST "http://${BOX_IP}:5000/lock/heartbeat" \
        -H 'Content-Type: application/json' \
        -d '{"user":"deploy-probe"}')
    case "$rc" in
        200|400|404|409)
            echo "  /lock/heartbeat reachable (HTTP $rc) — new code is live."
            ;;
        *)
            echo "  WARNING: /lock/heartbeat returned HTTP $rc; new code may not be live." >&2
            ;;
    esac
}

restore() {
    check_ssh
    if ! ssh_box "test -f '$BOX_BACKUP'"; then
        echo "ERROR: no snapshot at $BOX_BACKUP on the box; nothing to restore." >&2
        echo "       (Did you run --restore before deploying?)" >&2
        exit 1
    fi

    echo "Restoring $BOX_BACKUP -> ${BOX_SSH}:${CONTAINER_HANDLER}"
    # The snapshot was created by `sudo docker cp` so it's root-owned;
    # use sudo for the rm too. Single ssh-tty session to keep it to one
    # sudo prompt.
    local remote_script
    remote_script=$(cat <<EOF
set -e
echo "  docker cp $BOX_BACKUP -> lager:$CONTAINER_HANDLER"
sudo docker cp $BOX_BACKUP lager:$CONTAINER_HANDLER
echo "  docker restart lager"
sudo docker restart lager
echo "  removing $BOX_BACKUP and $BOX_STAGED"
sudo rm -f $BOX_BACKUP $BOX_STAGED
EOF
)
    echo "  (you may be prompted for the sudo password on the box)"
    ssh_box_sudo "$remote_script" \
        || { echo "ERROR: remote docker cp / restart failed" >&2; exit 1; }
    wait_for_http
    echo "  Snapshot cleared; deploy again to re-test."
}

case "$MODE" in
    deploy) deploy ;;
    restore) restore ;;
    *) echo "Unknown mode: $MODE" >&2; exit 1 ;;
esac
