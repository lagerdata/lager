#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Rollback behaviour of ``cli/deployment/scripts/configure_docker_dns.sh``.

Pointing Docker at the box's uplink resolvers is an optimization: it makes image
builds resolve reliably, and nothing more. It therefore must never be able to leave
the box worse off than it found it. The failure this guards against is not
hypothetical -- a config Docker refused to parse left a box's daemon dead across
reboots, and every re-run of the installer rewrote it.

So: if Docker will not come back with the new daemon.json, the previous one must be
restored and Docker restarted on it. `sudo`, `systemctl` and `docker` are stubbed on
PATH, and the daemon.json / resolv.conf paths are pointed at a temp dir, so this
runs without root and without a Docker daemon.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "cli"
    / "deployment"
    / "scripts"
    / "configure_docker_dns.sh"
)

ORIGINAL_CONFIG = {"log-driver": "journald", "dns": ["10.9.9.9"]}


def _write_stub(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(0o755)


@pytest.fixture
def box(tmp_path):
    """A fake box: stubbed sudo/systemctl/docker, and a resolv.conf worth reading."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # `sudo` just runs the command -- the test owns the files it touches.
    _write_stub(bin_dir, "sudo", 'exec "$@"')
    # `systemctl restart` fails when the test asks it to; is-active mirrors that.
    _write_stub(
        bin_dir,
        "systemctl",
        'if [ "$1" = "restart" ]; then exit "${RESTART_RC:-0}"; fi\n'
        'if [ "$1" = "is-active" ]; then exit "${RESTART_RC:-0}"; fi\n'
        "exit 0",
    )
    _write_stub(bin_dir, "docker", "exit 0")

    (tmp_path / "resolv.conf").write_text("nameserver 192.168.100.1\nnameserver fe80::1%3\n")

    return tmp_path, bin_dir


def _run(box, restart_rc, daemon_json_exists=True):
    tmp_path, bin_dir = box
    daemon_json = tmp_path / "daemon.json"
    if daemon_json_exists:
        daemon_json.write_text(json.dumps(ORIGINAL_CONFIG, indent=2) + "\n")

    env = dict(os.environ)
    env.update(
        PATH="{}:{}".format(bin_dir, env["PATH"]),
        DAEMON_JSON=str(daemon_json),
        RESOLV_CONF=str(tmp_path / "resolv.conf"),
        RESTART_RC=str(restart_rc),
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )
    return result, daemon_json


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_successful_restart_leaves_the_new_config_in_place(box):
    result, daemon_json = _run(box, restart_rc=0)

    assert result.returncode == 0, result.stderr
    written = json.loads(daemon_json.read_text())
    # The link-local resolver is gone; the real one and the fallbacks remain.
    assert written["dns"] == ["192.168.100.1", "1.1.1.1", "8.8.8.8"]
    # An operator's unrelated settings survive.
    assert written["log-driver"] == "journald"


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_failed_restart_restores_the_previous_config(box):
    """The property that matters: a box we could not improve is a box we did not break."""
    result, daemon_json = _run(box, restart_rc=1)

    assert result.returncode == 1
    assert json.loads(daemon_json.read_text()) == ORIGINAL_CONFIG
    assert "Restored the previous" in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_failed_restart_removes_config_that_did_not_exist_before(box):
    """Rolling back to "no daemon.json" means removing ours, not restoring an empty one."""
    result, daemon_json = _run(box, restart_rc=1, daemon_json_exists=False)

    assert result.returncode == 1
    assert not daemon_json.exists()
