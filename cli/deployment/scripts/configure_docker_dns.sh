#!/bin/bash
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# Point Docker's container DNS at the box's real uplink resolvers. Runs on the box.
#
# On a systemd-resolved box, /etc/resolv.conf lists only the 127.0.0.53 stub, which
# a container cannot use, so Docker falls back to 8.8.8.8; where that resolver is
# unreachable, `docker build` cannot resolve github.com/pypi and the image build
# dies partway through. systemd-resolved's own resolv.conf names the real upstream
# servers, so we merge those into daemon.json. configure_docker_dns.py decides
# which of them Docker can actually use.
#
# This is an optimization, so it must never leave the box worse off than it found
# it: if Docker will not come back with the new config, the previous daemon.json is
# restored and Docker is restarted on it before we exit non-zero.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_JSON="${DAEMON_JSON:-/etc/docker/daemon.json}"
RESOLV_CONF="${RESOLV_CONF:-/run/systemd/resolve/resolv.conf}"
BACKUP="${DAEMON_JSON}.lager-bak"

STAGED=$(mktemp)
trap 'rm -f "$STAGED"' EXIT

python3 "${SCRIPT_DIR}/configure_docker_dns.py" \
    --resolv-conf "$RESOLV_CONF" --daemon-json "$DAEMON_JSON" --out "$STAGED"

# Snapshot the current config so a failed restart can be undone. Track whether
# there was one at all, so we restore the old file or remove ours accordingly.
if [ -f "$DAEMON_JSON" ]; then
    HAD_CONFIG=1
    sudo cp -f "$DAEMON_JSON" "$BACKUP"
else
    HAD_CONFIG=0
    sudo rm -f "$BACKUP"
fi

daemon_is_up() {
    # `systemctl is-active` reads the unit's own state: unlike `docker info` it
    # needs neither root nor docker-group membership, which a freshly added user
    # may not have picked up in this SSH session yet.
    local _
    for _ in 1 2 3 4 5; do
        if systemctl is-active --quiet docker; then
            return 0
        fi
        sleep 1
    done
    return 1
}

restore_previous() {
    if [ "$HAD_CONFIG" -eq 1 ]; then
        sudo install -m 0644 "$BACKUP" "$DAEMON_JSON"
    else
        sudo rm -f "$DAEMON_JSON"
    fi
    sudo systemctl restart docker || true
}

sudo install -m 0644 "$STAGED" "$DAEMON_JSON"

if ! sudo systemctl restart docker || ! daemon_is_up; then
    restore_previous
    echo "ERROR: Docker would not start with the new DNS configuration." >&2
    echo "       Restored the previous ${DAEMON_JSON} and restarted Docker." >&2
    exit 1
fi

echo "Docker restarted with the new DNS configuration"
