# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager logs` connects as the box's own login user (#509).

Every SSH call in `cli/commands/utility/logs.py` was built as
`lagerdata@<ip>`, with no `-i`, so on a box whose login user is anything else
`lager logs` could not connect at all, and a box that authorizes only the
lager_box key refused it too. The calls now go through `default_ssh_runner`,
which resolves the saved user and offers the key.

`lager logs docker` also ran `sudo ls` over BatchMode SSH with its errors
discarded. Without passwordless sudo the size lines vanished and the command
still succeeded. The remote script now uses `sudo -n` and says when it could
not read a size; it is run here under bash with stub `docker` and `sudo`
commands.
"""

import importlib
import os
import pathlib
import stat
import subprocess
import types
from unittest import mock

import pytest
from click.testing import CliRunner

logs_mod = importlib.import_module("cli.commands.utility.logs")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
BOXES = {"bench-a": {"ip": "192.0.2.7", "user": "benchuser"}}


class FakeRunner:
    """Stands in for `default_ssh_runner`; answers each call in turn."""

    def __init__(self, *answers):
        self.answers = list(answers) or [(0, "", "")]
        self.calls = []

    def __call__(self, ip, cmd, *, stdin=None, timeout=60):
        self.calls.append((ip, cmd, timeout))
        return self.answers[min(len(self.calls), len(self.answers)) - 1]


def _logs(argv, runner, boxes=BOXES):
    with mock.patch.object(logs_mod, "list_boxes", return_value=boxes), \
            mock.patch.object(logs_mod, "default_ssh_runner", runner), \
            mock.patch.object(logs_mod, "resolve_box_user", return_value="benchuser"):
        return CliRunner().invoke(logs_mod.logs, argv, obj=types.SimpleNamespace())


def test_no_command_names_a_login_user_itself():
    source = (REPO_ROOT / "cli/commands/utility/logs.py").read_text()
    assert "lagerdata@" not in source.replace("hardcode `lagerdata@`", "")
    assert "subprocess" not in source


@pytest.mark.parametrize("argv", [
    ["size", "--box", "bench-a"],
    ["size", "--box", "bench-a", "--verbose"],
    ["clean", "--box", "bench-a", "--yes"],
    ["docker", "--box", "bench-a"],
])
def test_every_subcommand_goes_through_the_ssh_runner(argv):
    runner = FakeRunner((0, "12M\t/home/benchuser/box/logs/", ""))
    result = _logs(argv, runner)
    assert result.exit_code == 0, result.output
    assert runner.calls, "no SSH call was made"
    assert all(ip == "192.0.2.7" for ip, _, _ in runner.calls)


def test_size_with_no_box_checks_every_saved_box():
    boxes = {"bench-a": {"ip": "192.0.2.7"}, "bench-b": {"ip": "192.0.2.8"}}
    runner = FakeRunner((0, "1M\t/x", ""))
    result = _logs(["size"], runner, boxes)
    assert result.exit_code == 0, result.output
    assert sorted(ip for ip, _, _ in runner.calls) == ["192.0.2.7", "192.0.2.8"]


def test_an_auth_failure_names_the_saved_user():
    runner = FakeRunner((255, "", "benchuser@192.0.2.7: Permission denied (publickey)."))
    result = _logs(["clean", "--box", "bench-a", "--yes"], runner)
    assert result.exit_code == 1
    assert "ssh-copy-id benchuser@192.0.2.7" in result.output
    assert "lagerdata@" not in result.output


def test_a_timeout_is_reported_as_one():
    runner = FakeRunner((255, "", "ssh timed out after 10s to benchuser@192.0.2.7"))
    result = _logs(["size", "--box", "bench-a"], runner)
    assert "Connection timed out" in result.output


def test_docker_reports_a_size_it_could_not_read():
    marked = f"Container: lager\n  Size: {logs_mod._NO_SUDO_MARK}\n"
    result = _logs(["docker", "--box", "bench-a"], FakeRunner((0, marked, "")))
    assert result.exit_code == 0, result.output
    assert "The log sizes need root" in result.output
    assert "benchuser" in result.output


def test_docker_failing_is_not_reported_as_no_containers():
    runner = FakeRunner((3, "", "permission denied while trying to connect to the Docker daemon"))
    result = _logs(["docker", "--box", "bench-a"], runner)
    assert result.exit_code == 1
    assert "Failed to check Docker logs" in result.output
    assert "No containers found" not in result.output


# ---------------------------------------------------------------------------
# The remote script itself, under bash with stub commands
# ---------------------------------------------------------------------------

def _stub(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run_remote(tmp_path, *, container=None, sudo_ok=True, docker_ok=True):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _stub(bin_dir, "docker",
          ('exit 1\n' if not docker_ok else
           'case "$1" in\n'
           '  ps) [ "$2" = "-q" ] && echo abc || echo lager ;;\n'
           '  inspect) echo /var/lib/docker/containers/abc/abc-json.log ;;\n'
           'esac\n'))
    _stub(bin_dir, "sudo",
          ('[ "$1" = "-n" ] && shift; exec "$@"\n' if sudo_ok else
           'echo "sudo: a password is required" >&2; exit 1\n'))
    _stub(bin_dir, "ls", 'echo "-rw-r----- 1 root root 4.2M Sep 16 12:00 $2"\n')
    cmd = logs_mod._DOCKER_LOG_SIZES_SCRIPT.replace(
        "__CONTAINERS__", container or '$(docker ps --format "{{.Names}}")')
    if not container:
        cmd = "docker ps -q >/dev/null || exit 3; " + cmd
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env)


def test_the_script_prints_each_size(tmp_path):
    proc = _run_remote(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "Container: lager" in proc.stdout
    assert "Size: 4.2M Path: /var/lib/docker/containers/abc/abc-json.log" in proc.stdout


def test_the_script_says_when_sudo_wants_a_password(tmp_path):
    proc = _run_remote(tmp_path, sudo_ok=False)
    assert proc.returncode == 0, proc.stderr
    assert logs_mod._NO_SUDO_MARK in proc.stdout


def test_the_script_exits_3_when_docker_fails(tmp_path):
    assert _run_remote(tmp_path, docker_ok=False).returncode == 3
    assert _run_remote(tmp_path, container="lager", docker_ok=False).returncode == 3
