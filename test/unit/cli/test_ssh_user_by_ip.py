# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager ssh` and `lager update` log in as the user saved for the box (#509).

Both looked the user up with `get_box_user(box)`, which is keyed by the saved
box NAME. With `--box <ip>` the lookup never matched, and with no `--box` the
value had already become the default box's IP, so both logged in as
`lagerdata` on a box saved with a different user. They now fall back to the
user saved for the resolved IP, the way `lager ssh-setup` already did.

The saved-box lookups are patched here, so the real ~/.lager is never read.
"""

import importlib
import subprocess

import pytest
from click.testing import CliRunner

bs = importlib.import_module("cli.box_storage")
ssh_mod = importlib.import_module("cli.commands.box.ssh")
update_mod = importlib.import_module("cli.commands.utility.update")

SAVED = {"bench-a": {"ip": "192.0.2.7", "user": "benchuser"}}


def _user_by_name(name):
    return SAVED.get(name, {}).get("user")


def _name_by_ip(ip):
    return next((name for name, box in SAVED.items() if box["ip"] == ip), None)


@pytest.fixture(autouse=True)
def saved_boxes(monkeypatch):
    monkeypatch.setattr(bs, "get_box_user", _user_by_name)
    monkeypatch.setattr(bs, "get_box_name_by_ip", _name_by_ip)
    monkeypatch.setattr(update_mod, "get_box_user", _user_by_name)


def _ssh_destination(monkeypatch, argv, resolved_ip):
    calls = []
    monkeypatch.setattr(ssh_mod.subprocess, "call", lambda cmd, **kw: calls.append(cmd) or 0)
    monkeypatch.setattr(ssh_mod, "resolve_and_validate_box", lambda ctx, box: resolved_ip)
    monkeypatch.setattr(ssh_mod, "get_default_box", lambda ctx: resolved_ip)
    result = CliRunner().invoke(ssh_mod.ssh, argv)
    assert result.exit_code == 0, result.output
    return next(arg for arg in calls[0] if "@" in arg)


@pytest.mark.parametrize("argv", [
    ["--box", "bench-a"],
    ["--box", "192.0.2.7"],
    [],                                  # the default box, which resolves to its IP
], ids=["name", "ip", "default"])
def test_lager_ssh_logs_in_as_the_saved_user(monkeypatch, argv):
    assert _ssh_destination(monkeypatch, argv, "192.0.2.7") == "benchuser@192.0.2.7"


def test_an_unsaved_ip_still_logs_in_as_lagerdata(monkeypatch):
    assert _ssh_destination(monkeypatch, ["--box", "192.0.2.99"], "192.0.2.99") == \
        "lagerdata@192.0.2.99"


def _unreachable(cmd, *args, **kwargs):
    return subprocess.CompletedProcess(cmd, 255, "", "ssh: connect to host: Connection timed out")


@pytest.mark.parametrize("box_arg", ["bench-a", "192.0.2.7"])
def test_lager_update_probes_as_the_saved_user(monkeypatch, tmp_path, box_arg):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "lager_box").write_text("PRIVATE KEY PLACEHOLDER\n")
    probed = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(update_mod, "resolve_and_validate_box", lambda ctx, box: "192.0.2.7")
    monkeypatch.setattr(update_mod, "key_installed_on_box",
                        lambda host, *a, **k: probed.append(host) or False)
    monkeypatch.setattr(update_mod.subprocess, "run", _unreachable)
    monkeypatch.setattr(update_mod.subprocess, "check_output",
                        lambda *a, **k: (_ for _ in ()).throw(subprocess.CalledProcessError(255, "ssh")))
    CliRunner().invoke(update_mod.update, ["--box", box_arg, "--check"])
    assert probed, "update never probed the box"
    assert probed[0].startswith("benchuser@192.0.2.7"), probed
