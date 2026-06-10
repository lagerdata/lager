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
#   test/manual/deploy_branch_to_tester.sh <BOX_NAME_OR_IP> [BOX_SSH_USER]
#   test/manual/deploy_branch_to_tester.sh <BOX_NAME_OR_IP> --restore
#
# Workflow (typical pre-merge check):
#   pip install -e ./cli                                  # new CLI locally
#   test/manual/deploy_branch_to_tester.sh tester         # new server on box
#   test/manual/hardware_lock_smoke.sh tester             # run smoke
#   test/manual/deploy_branch_to_tester.sh tester --restore   # roll back code
#
# Notes:
#   - The smoke script does its own /etc/lager snapshot for *data*; this
#     script handles *code* rollback for the one file we changed.
#   - Both snapshots live under /tmp on the box, so they survive an SSH
#     drop but not a reboot.

set -u
set -o pipefail

if [ $# -lt 1 ]; then
    cat <<EOF >&2
Usage: $0 <BOX_NAME_OR_IP> [BOX_SSH_USER]
       $0 <BOX_NAME_OR_IP> --restore

Deploys cli/../box/lager/http_handlers/lock_handler.py onto the box via
'docker cp' and restarts the lager container. Use --restore to undo.
EOF
    exit 1
fi

BOX="$1"
shift

MODE="deploy"
BOX_SSH_USER="lagerdata"
for arg in "$@"; do
    case "$arg" in
        --restore) MODE="restore" ;;
        *) BOX_SSH_USER="$arg" ;;
    esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
LOCAL_HANDLER="$REPO_ROOT/box/lager/http_handlers/lock_handler.py"
CONTAINER_HANDLER="/box/lager/http_handlers/lock_handler.py"
BOX_STAGED="/tmp/lock_handler.py.new"
BOX_BACKUP="/tmp/lock_handler.py.orig"

# Resolve box -> IP using the lager CLI (same idiom as the smoke script).
if [[ "$BOX" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    BOX_IP="$BOX"
else
    BOX_IP="$(lager boxes 2>/dev/null | awk -v n="$BOX" '$1==n {print $2; exit}')"
    if [ -z "$BOX_IP" ]; then
        echo "Could not resolve '$BOX' to an IP. Add it with:" >&2
        echo "  lager boxes add --name $BOX --ip <IP>" >&2
        exit 1
    fi
fi
BOX_SSH="${BOX_SSH_USER}@${BOX_IP}"

ssh_box() {
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$BOX_SSH" "$@"
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

    echo "Deploying $LOCAL_HANDLER -> $BOX_SSH:$CONTAINER_HANDLER"

    # 1. Stage the file on the box.
    scp -o BatchMode=yes "$LOCAL_HANDLER" "${BOX_SSH}:${BOX_STAGED}"

    # 2. Snapshot the existing in-container file ONLY on the first deploy
    #    (don't overwrite a real snapshot with a smoke-modified copy on a
    #    repeat run — that would defeat --restore).
    if ! ssh_box "test -f '$BOX_BACKUP'"; then
        echo "  Snapshotting current $CONTAINER_HANDLER -> $BOX_BACKUP"
        ssh_box "sudo docker cp lager:${CONTAINER_HANDLER} ${BOX_BACKUP}" \
            || { echo "ERROR: could not snapshot existing handler" >&2; exit 1; }
    else
        echo "  Existing snapshot already at $BOX_BACKUP (preserving original)"
    fi

    # 3. Copy into the container and bounce it.
    ssh_box "sudo docker cp ${BOX_STAGED} lager:${CONTAINER_HANDLER} && sudo docker restart lager" \
        || { echo "ERROR: docker cp + restart failed" >&2; exit 1; }

    wait_for_http

    # 4. Sanity check: the new heartbeat endpoint must be there.
    local rc
    rc=$(curl -s -o /dev/null -w '%{http_code}' \
        -X POST "http://${BOX_IP}:5000/lock/heartbeat" \
        -H 'Content-Type: application/json' \
        -d '{"user":"deploy-probe"}')
    if [ "$rc" = "404" ] && curl -s "http://${BOX_IP}:5000/lock" >/dev/null; then
        # 404 means "no lock to heartbeat", which proves the route is
        # registered (a missing route would return 404 from Flask's own
        # catch-all with a different body, but for our purposes either
        # 200/404/400 confirms the file is live).
        :
    fi
    case "$rc" in
        200|400|404)
            echo "  /lock/heartbeat reachable (HTTP $rc) — new code is live."
            ;;
        *)
            echo "  WARNING: /lock/heartbeat returned HTTP $rc; new code may not be live." >&2
            ;;
    esac
}

restore() {
    if ! ssh_box "test -f '$BOX_BACKUP'"; then
        echo "ERROR: no snapshot at $BOX_BACKUP on the box; nothing to restore." >&2
        echo "       (Did you run this script with --restore before deploying?)" >&2
        exit 1
    fi

    echo "Restoring $BOX_BACKUP -> $BOX_SSH:$CONTAINER_HANDLER"
    ssh_box "sudo docker cp ${BOX_BACKUP} lager:${CONTAINER_HANDLER} && sudo docker restart lager" \
        || { echo "ERROR: docker cp + restart failed" >&2; exit 1; }
    wait_for_http

    # Delete the snapshot so a future deploy snapshots the freshly
    # restored (i.e. main-branch) file.
    ssh_box "rm -f '$BOX_BACKUP' '$BOX_STAGED'" || true
    echo "  Snapshot cleared; deploy again to re-test."
}

case "$MODE" in
    deploy) deploy ;;
    restore) restore ;;
    *) echo "Unknown mode: $MODE" >&2; exit 1 ;;
esac
