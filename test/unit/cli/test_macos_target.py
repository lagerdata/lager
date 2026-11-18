# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Commands the CLI runs on a Box over SSH, when the Box is a Mac.

The Box on this Mac is run in a local shell instead of over SSH; a command
that manages a Linux host is refused by name for any Mac Box; and for every
Box that is not a Mac the shared SSH runner is reached exactly as before.
No SSH or shell command is really run.
"""
import subprocess
from unittest import mock

import pytest

from cli import box_storage
from cli.commands.box import _macos_target, _ssh
from cli.commands.utility import _local_install


@pytest.fixture
def lager_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LAGER_CONFIG_FILE_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    box_storage.add_box("linux", "10.0.0.5", user="lagerdata", version="1.0.0")
    box_storage.add_box("mac", "10.0.0.6", user="me", python_port=8301, platform="macos")
    return tmp_path


def _on(platform):
    return mock.patch.object(_local_install.sys, "platform", platform)


def _done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


def test_which_boxes_are_macs(lager_home):
    with _on("darwin"):
        assert _macos_target.is_macos_box("127.0.0.1")
        assert _macos_target.is_macos_box("10.0.0.6")
        assert not _macos_target.is_macos_box("10.0.0.5")
    with _on("linux"):
        assert not _macos_target.is_macos_box("127.0.0.1")
        assert _macos_target.is_macos_box("10.0.0.6")  # by its saved record


@pytest.mark.parametrize("cmd", [
    "sudo -n apt-get update -qq && sudo -n apt-get install -y curl",
    "sudo -n /usr/sbin/sysctl --system",
    "sudo -n /usr/bin/udevadm control --reload-rules",
    "systemctl is-enabled --quiet x || sudo -n systemctl enable --now x",
    "sudo tee /etc/sudoers.d/lager-bench-json",
    "cat > /etc/udev/rules.d/99-lager.rules",
    "cd ~/box && ./start_box.sh",
])
def test_linux_host_management_is_refused_by_name(cmd):
    assert "not supported for a Box on macOS" in _macos_target.refusal(cmd)


@pytest.mark.parametrize("cmd", [
    "cat /etc/lager/bench.json 2>/dev/null || true",
    "docker exec -i lager python3 - show",
    "cat > /tmp/lager-bench.json.tmp && mv -f /tmp/lager-bench.json.tmp /etc/lager/bench.json",
    "ls ~/my-apt-notes /opt/sysctl-like",
    "uname -s",
])
def test_ordinary_commands_are_not_refused(cmd):
    assert _macos_target.refusal(cmd) is None


def test_the_box_on_this_mac_runs_in_a_local_shell(lager_home):
    runner = mock.Mock(return_value=_done(3, "out", "err"))
    with _on("darwin"):
        result = _macos_target.run("127.0.0.1", "cat x", stdin="body", timeout=9, runner=runner)
    assert result == (3, "out", "err")
    runner.assert_called_once_with(["/bin/sh", "-c", "cat x"], input="body",
                                   capture_output=True, text=True, timeout=9)
    runner = mock.Mock(side_effect=subprocess.TimeoutExpired("sh", 9))
    with _on("darwin"):
        rc, _out, err = _macos_target.run("127.0.0.1", "cat x", timeout=9, runner=runner)
    assert rc != 0 and "timed out after 9s" in err


def test_a_refused_command_is_never_run_on_any_mac(lager_home):
    runner = mock.Mock()
    with _on("darwin"):
        for box in ("127.0.0.1", "10.0.0.6"):
            rc, out, err = _macos_target.run(box, "sudo -n apt-get install -y curl",
                                             runner=runner)
            assert rc == _macos_target.REFUSED_STATUS and out == "" and "apt-get" in err
    runner.assert_not_called()


def test_a_remote_mac_and_every_linux_box_are_left_to_ssh(lager_home):
    runner = mock.Mock()
    with _on("darwin"):
        assert _macos_target.run("10.0.0.6", "cat x", runner=runner) is None
        assert _macos_target.run("10.0.0.5", "sudo -n apt-get update", runner=runner) is None
    with _on("linux"):
        assert _macos_target.run("127.0.0.1", "sudo -n apt-get update", runner=runner) is None
    runner.assert_not_called()


def test_the_shared_runner_uses_ssh_for_a_linux_box_exactly_as_before(lager_home):
    with _on("linux"), \
            mock.patch.object(_ssh, "lager_box_key_if_present", return_value=None), \
            mock.patch.object(_ssh.subprocess, "run", return_value=_done(0, "ok", "")) as ssh:
        assert _ssh.default_ssh_runner("10.0.0.5", "sudo -n apt-get update", stdin="x",
                                       timeout=7) == (0, "ok", "")
    ssh.assert_called_once_with(
        ["ssh", "-o", "BatchMode=yes", "lagerdata@10.0.0.5", "sudo -n apt-get update"],
        input="x", capture_output=True, text=True, timeout=7)


def test_the_shared_runner_never_sshes_to_the_box_on_this_mac(lager_home):
    with _on("darwin"), \
            mock.patch.object(subprocess, "run", return_value=_done(0, "Darwin\n", "")) as ran:
        assert _ssh.default_ssh_runner("127.0.0.1", "uname -s") == (0, "Darwin\n", "")
    ran.assert_called_once()
    assert ran.call_args.args[0] == ["/bin/sh", "-c", "uname -s"]


def test_lager_ssh_is_a_local_shell_only_for_the_box_on_this_mac(lager_home):
    with _on("darwin"):
        assert _macos_target.local_shell_argv("127.0.0.1", (), {"SHELL": "/bin/zsh"}) \
            == ["/bin/zsh", "-l"]
        assert _macos_target.local_shell_argv("127.0.0.1", ("sh", "-c", "exit 7"), {}) \
            == ["/bin/sh", "-c", "sh -c 'exit 7'"]
        assert _macos_target.local_shell_argv("10.0.0.6", ("ls",)) is None
        assert _macos_target.local_shell_argv("10.0.0.5", ("ls",)) is None
    with _on("linux"):
        assert _macos_target.local_shell_argv("127.0.0.1", ("ls",)) is None
