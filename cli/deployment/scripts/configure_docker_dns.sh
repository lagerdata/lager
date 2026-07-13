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
#
# Every privileged action here is one the box's sudoers file already grants
# NOPASSWD (see setup_and_deploy_box.sh): `install` from the fixed path
# /tmp/lager_daemon.json, and `systemctl restart docker`. Staging anywhere else --
# a mktemp path, say -- silently falls outside the grant and makes every run prompt
# for a password. Snapshot and restore therefore route through that same path, and
# the backup lives in /tmp, where no privileges are needed to write it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_JSON="${DAEMON_JSON:-/etc/docker/daemon.json}"
RESOLV_CONF="${RESOLV_CONF:-/run/systemd/resolve/resolv.conf}"
STAGED="${STAGED:-/tmp/lager_daemon.json}"
BACKUP="${BACKUP:-/tmp/lager_daemon.json.bak}"

rm -f "$STAGED" "$BACKUP" 2>/dev/null || true
trap 'rm -f "$STAGED" "$BACKUP" 2>/dev/null || true' EXIT

python3 "${SCRIPT_DIR}/configure_docker_dns.py" \
    --resolv-conf "$RESOLV_CONF" --daemon-json "$DAEMON_JSON" --out "$STAGED"

# Snapshot the current config so a failed restart can be undone. daemon.json is
# world-readable, so this needs no sudo. Track whether there was one at all.
if [ -f "$DAEMON_JSON" ]; then
    HAD_CONFIG=1
    cp -f "$DAEMON_JSON" "$BACKUP"
else
    HAD_CONFIG=0
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
    # Where there was no daemon.json, an empty object restores Docker's defaults --
    # which is what the absent file meant. We write that rather than removing the
    # file because `rm` on /etc/docker is not in the sudoers grant, and a recovery
    # path that stops to ask for a password is a recovery path that does not run.
    if [ "$HAD_CONFIG" -eq 1 ]; then
        cp -f "$BACKUP" "$STAGED"
    else
        echo '{}' > "$STAGED"
    fi
    sudo install -m 0644 "$STAGED" "$DAEMON_JSON" \
        || echo "WARNING: could not restore ${DAEMON_JSON}" >&2
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
