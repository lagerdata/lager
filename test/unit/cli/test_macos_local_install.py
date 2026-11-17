# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""`lager install` / `update` / `uninstall` for a Box on the Mac the CLI runs on.

Covers the macOS provisioner's pure pieces (platform and Docker detection,
the launchd job, the container's arguments, the Rust toolchain choice, the
PicoTech header tree, optional steps reporting a reason instead of failing),
the routing that sends "no --box/--ip" and loopback addresses on a Mac to the
local path while leaving every other platform on SSH, and the saved Box
keeping its script-service port. No Docker, launchd, sudo or network is used.
"""
import json
import os
import plistlib
import subprocess
from pathlib import Path
from unittest import mock

import click
import pytest
from click.testing import CliRunner

from cli import box_storage
from cli.commands.utility import _local_install
from cli.deployment import macos_box
from cli.errors import LagerError


def _done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class _Runner:
    """Stands in for subprocess: records argv, answers by first match."""

    def __init__(self, answers=()):
        self.calls = []
        self.answers = list(answers)

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        for match, result in self.answers:
            if match(argv, kwargs):
                return result
        return _done()


@pytest.fixture
def layout(tmp_path):
    return macos_box.Layout(home=tmp_path)


# ---------------------------------------------------------------- detection

def test_only_apple_silicon_macs_are_accepted():
    macos_box.check_platform("Darwin", "arm64")
    for system, machine in (("Linux", "x86_64"), ("Darwin", "x86_64"), ("Windows", "AMD64")):
        with pytest.raises(LagerError):
            macos_box.check_platform(system, machine)


def test_docker_is_found_through_a_known_socket_when_the_default_fails(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text("")
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    run = _Runner([(lambda argv, kw: "DOCKER_HOST" in kw["env"], _done(0)),
                   (lambda argv, kw: True, _done(1))])
    with mock.patch.object(macos_box, "DOCKER_PATHS", (str(docker),)), \
            mock.patch.object(macos_box, "DOCKER_SOCKETS", (str(sock),)), \
            mock.patch.object(macos_box.shutil, "which", return_value=None):
        found, env = macos_box.find_docker(run, environ={"PATH": "/nowhere"})
    assert found == str(docker)
    assert env["DOCKER_HOST"] == f"unix://{sock}"


def test_missing_and_stopped_docker_are_different_errors(tmp_path):
    with mock.patch.object(macos_box, "DOCKER_PATHS", ()), \
            mock.patch.object(macos_box.shutil, "which", return_value=None):
        with pytest.raises(LagerError, match="not installed"):
            macos_box.find_docker(_Runner(), environ={})
    docker = tmp_path / "docker"
    docker.write_text("")
    with mock.patch.object(macos_box, "DOCKER_PATHS", (str(docker),)), \
            mock.patch.object(macos_box, "DOCKER_SOCKETS", ()), \
            mock.patch.object(macos_box.shutil, "which", return_value=None):
        with pytest.raises(LagerError, match="not running"):
            macos_box.find_docker(_Runner([(lambda a, k: True, _done(1))]), environ={})


def test_source_version_and_image():
    root = macos_box.running_from_checkout()
    assert root is not None and (root / "box" / "lager" / "host").is_dir()
    from cli import __version__
    assert macos_box.source_version(root) == __version__
    assert macos_box.image_for("v1.2.3") == macos_box.image_for("1.2.3") \
        == "ghcr.io/lagerdata/lager-box:v1.2.3"


# -------------------------------------------------------------------- steps

def test_the_launchd_job_sets_the_library_path_immediately_before_python(layout, tmp_path):
    job = plistlib.loads(macos_box.launch_agent_plist(layout, tmp_path / "src"))
    args = job["ProgramArguments"]
    assert args[0] == "/usr/bin/env"
    assert args[1].startswith(f"DYLD_FALLBACK_LIBRARY_PATH={layout.lib_dir}:")
    assert args[2:] == [str(layout.python), "-m", "lager.host", "run"]
    assert job["Label"] == macos_box.LABEL
    assert job["RunAtLoad"] is True and job["KeepAlive"] is True
    assert job["EnvironmentVariables"]["PYTHONPATH"] == str(tmp_path / "src" / "box")
    assert "LAGER_HOST_SCOPE_DAEMON" not in job["EnvironmentVariables"]
    layout.scope_daemon.parent.mkdir(parents=True)
    layout.scope_daemon.write_text("")
    job = plistlib.loads(macos_box.launch_agent_plist(layout, tmp_path / "src"))
    assert job["EnvironmentVariables"]["LAGER_HOST_SCOPE_DAEMON"] == str(layout.scope_daemon)


def test_the_container_gets_no_privileges_and_no_devices(tmp_path):
    argv = macos_box.docker_run_argv("docker", "img:v1", tmp_path, "/private/etc/lager")
    joined = " ".join(argv)
    for forbidden in ("--privileged", "/dev", "/sys", "dbus", "--network", "--device"):
        assert forbidden not in joined
    assert argv[-2:] == ["img:v1", macos_box.START_SCRIPT]
    assert "--restart" in argv and argv[argv.index("--name") + 1] == "lager"
    assert f"{tmp_path}/box/lager:/app/lager/lager:ro" in argv
    assert "/private/etc/lager:/etc/lager" in argv
    published = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert "127.0.0.1:8301:5000" in published and "127.0.0.1:9000:9000" in published
    assert all(p.startswith("127.0.0.1:") for p in published)
    assert not any(p.split(":")[1] == "5000" for p in published), "AirPlay holds 5000"


def test_rust_toolchain_prefers_the_default_then_the_newest_installed(tmp_path):
    rustup = tmp_path / "rustup"
    rustup.write_text("")
    cargo = str(tmp_path / "cargo")

    def runner(version, toolchains=""):
        return _Runner([(lambda a, k: a[-1] == "--version", _done(0, version)),
                        (lambda a, k: a[-1] == "list", _done(0, toolchains))])

    with mock.patch.object(macos_box.shutil, "which", return_value=None):
        assert macos_box.rust_toolchain(cargo, runner("cargo 1.97.0 (abc 2026)")) == []
        listed = ("stable-aarch64-apple-darwin (active, default)\n"
                  "1.85.0-aarch64-apple-darwin\n1.97-aarch64-apple-darwin\n"
                  "1.90.1-aarch64-apple-darwin\n")
        assert macos_box.rust_toolchain(cargo, runner("cargo 1.86.0", listed)) \
            == ["+1.97-aarch64-apple-darwin"]
        assert macos_box.rust_toolchain(
            cargo, runner("cargo 1.86.0", "1.85.0-aarch64-apple-darwin\n")) is None


def test_pico_headers_are_laid_out_with_the_shared_ones_beside_each_family(layout, tmp_path):
    sdk = tmp_path / "Headers"
    assert macos_box.pico_include_tree(layout, sdk) is None
    (sdk / "shared").mkdir(parents=True)
    (sdk / "libps2000").mkdir()
    (sdk / "shared" / "PicoStatus.h").write_text("s")
    (sdk / "libps2000" / "ps2000.h").write_text("f")
    tree = macos_box.pico_include_tree(layout, sdk)
    assert sorted(p.name for p in (tree / "libps2000").iterdir()) == ["PicoStatus.h", "ps2000.h"]
    assert (tree / "libps2000" / "PicoStatus.h").read_text() == "s"


def test_optional_steps_give_a_reason_instead_of_failing(layout, tmp_path):
    with mock.patch.object(macos_box, "homebrew_prefix_with", return_value=None):
        assert "libusb" in macos_box.build_labjack_library(layout, _Runner())
    with mock.patch.object(macos_box.shutil, "which", return_value=None), \
            mock.patch.object(macos_box.os.path, "exists", return_value=False):
        assert "Rust" in macos_box.build_scope_daemon(layout, tmp_path, _Runner())
    layout.lib_dir.mkdir(parents=True)
    (layout.lib_dir / "liblabjackusb.dylib").write_text("")
    run = _Runner()
    assert macos_box.build_labjack_library(layout, run) is None
    assert run.calls == [], "already built: nothing is run"


def test_an_optional_driver_package_that_fails_is_reported_not_fatal(layout):
    layout.python.parent.mkdir(parents=True)
    layout.python.write_text("")
    run = _Runner([(lambda a, k: a[-1] == "aardvark_py", _done(1, "", "no wheel\n"))])
    skipped = macos_box.ensure_agent_env(layout, run)
    assert len(skipped) == 1 and "Aardvark" in skipped[0] and "no wheel" in skipped[0]
    required = _Runner([(lambda a, k: "numpy>=1.26" in a, _done(1, "", "offline"))])
    with pytest.raises(LagerError):
        macos_box.ensure_agent_env(layout, required)


def test_config_dir_needs_sudo_only_when_missing(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    run = _Runner()
    with mock.patch.object(macos_box, "ETC_DIR", str(etc)):
        assert macos_box.ensure_config_dir(run) is False
    assert run.calls == [] and (etc.stat().st_mode & 0o777) == 0o700
    with mock.patch.object(macos_box, "ETC_DIR", str(tmp_path / "absent")):
        with pytest.raises(LagerError):
            macos_box.ensure_config_dir(run, user="me")
    assert run.calls[0][0][0] == "sudo" and "chown me" in run.calls[0][0][-1]


def test_state_files_match_what_a_linux_install_writes(tmp_path):
    with mock.patch.object(macos_box, "ETC_DIR", str(tmp_path)):
        macos_box.write_state("0.64.0", "0.64.1", "v0.64.0")
    assert (tmp_path / "version").read_text() == "0.64.0|0.64.1\n"
    assert (tmp_path / "ref").read_text() == "v0.64.0\n"


def test_uninstall_keeps_config_unless_purged_and_backs_it_up(layout, tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "saved_nets.json").write_text("[1]")
    layout.app_dir.mkdir(parents=True)
    layout.plist.parent.mkdir(parents=True)
    layout.plist.write_text("")
    lines = []
    with mock.patch.object(macos_box, "ETC_DIR", str(etc)), \
            mock.patch.object(macos_box, "find_docker", return_value=("docker", {})):
        run = _Runner()
        macos_box.uninstall(run=run, layout=layout, echo=lines.append)
        assert (etc / "saved_nets.json").exists()
        assert not layout.app_dir.exists() and not layout.plist.exists()
        assert ["docker", "rm", "-f", "lager"] in [c[0] for c in run.calls]
        assert any(c[0][:2] == ["launchctl", "bootout"] for c in run.calls)
        macos_box.uninstall(purge_config=True, run=_Runner(), layout=layout, echo=lines.append)
    assert etc.is_dir() and list(etc.iterdir()) == []
    backups = list(tmp_path.glob("lager-config-backup-*"))
    assert len(backups) == 1 and (backups[0] / "saved_nets.json").read_text() == "[1]"


# ------------------------------------------------------------------ routing

def test_loopback_is_local_only_on_a_mac():
    for address in ("127.0.0.1", "localhost", "::1"):
        assert _local_install.is_local_target(address, platform="darwin")
        assert not _local_install.is_local_target(address, platform="linux")
        assert not _local_install.is_local_target(address, platform="win32")
    assert not _local_install.is_local_target("192.168.1.5", platform="darwin")


def test_a_local_install_off_macos_says_not_yet_supported():
    ctx = mock.Mock()
    for platform, word in (("linux", "Linux"), ("win32", "Windows")):
        with pytest.raises(LagerError, match="not yet supported") as caught:
            _local_install.install_locally(ctx, version="v1", version_defaulted=True,
                                           yes=True, platform=platform)
        assert word in caught.value.problem
        with pytest.raises(LagerError, match="not yet supported"):
            _local_install.uninstall_locally(ctx, purge_config=False, yes=True,
                                             dry_run=False, platform=platform)


@pytest.fixture
def lager_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LAGER_CONFIG_FILE_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _saved(home):
    return json.loads((home / ".lager").read_text())["BOXES"]


def test_install_without_a_target_provisions_and_registers_the_port(lager_home):
    from cli.commands.utility.install import install
    result_value = macos_box.Result(source=Path("/src"), image="img", box_version="9.9.9",
                                    skipped=["Oscilloscope support: no Rust"])
    with mock.patch.object(_local_install.sys, "platform", "darwin"), \
            mock.patch.object(macos_box, "install", return_value=result_value) as provision, \
            mock.patch("subprocess.run") as ssh:
        result = CliRunner().invoke(install, ["--yes"], obj={})
    assert result.exit_code == 0, result.output
    provision.assert_called_once()
    ssh.assert_not_called()
    assert "Not set up -- Oscilloscope support: no Rust" in result.output
    assert _saved(lager_home) == {"local": {"ip": "127.0.0.1", "version": "9.9.9",
                                            "python_port": 8301}}
    assert box_storage.get_python_service_port("127.0.0.1") == 8301


def test_install_without_a_target_on_linux_exits_with_the_message(lager_home):
    from cli.commands.utility.install import install
    with mock.patch.object(_local_install.sys, "platform", "linux"), \
            mock.patch.object(macos_box, "install") as provision:
        result = CliRunner().invoke(install, ["--yes"], obj={})
    assert result.exit_code == 1
    assert "not yet supported" in result.output
    provision.assert_not_called()


def test_a_saved_loopback_box_goes_local_on_a_mac_for_install_and_uninstall(lager_home):
    from cli.commands.utility.install import install
    from cli.commands.utility.uninstall import uninstall
    box_storage.add_box("mac", "127.0.0.1", version="1.0.0", python_port=8301)
    value = macos_box.Result(source=Path("/src"), image="img", box_version="2.0.0")
    with mock.patch.object(_local_install.sys, "platform", "darwin"), \
            mock.patch.object(macos_box, "install", return_value=value) as provision, \
            mock.patch.object(macos_box, "uninstall") as remove, \
            mock.patch.object(macos_box, "installed_pieces", return_value=["the container"]):
        result = CliRunner().invoke(install, ["--box", "mac", "--yes"], obj={})
        assert result.exit_code == 0, result.output
        provision.assert_called_once()
        assert _saved(lager_home)["mac"] == {"ip": "127.0.0.1", "version": "2.0.0",
                                             "python_port": 8301}
        dry = CliRunner().invoke(uninstall, ["--box", "mac", "--dry-run"], obj={})
        assert dry.exit_code == 0 and "nothing was changed" in dry.output
        remove.assert_not_called()
        result = CliRunner().invoke(uninstall, ["--box", "mac", "--yes"], obj={})
        assert result.exit_code == 0, result.output
        remove.assert_called_once()
    assert _saved(lager_home) == {}


def test_update_of_the_local_box_is_the_install_again():
    ctx = mock.Mock()
    with mock.patch.object(_local_install, "install_locally") as again:
        _local_install.update_locally(ctx, version=None, yes=True)
    from cli.commands.utility.update import default_box_version
    again.assert_called_once_with(ctx, version=default_box_version(),
                                  version_defaulted=True, yes=True, fork=None)
    ctx.exit.side_effect = click.exceptions.Exit(2)
    with pytest.raises(click.exceptions.Exit):
        _local_install.update_locally(ctx, version=None, yes=True, check=True)


# ------------------------------------------------------------ saved Box port

def test_add_box_without_a_port_stores_exactly_what_it_did_before(lager_home):
    box_storage.add_box("a", "10.0.0.1")
    box_storage.add_box("b", "10.0.0.2", user="u", version="v1")
    assert _saved(lager_home) == {"a": "10.0.0.1",
                                  "b": {"ip": "10.0.0.2", "user": "u", "version": "v1"}}


def test_a_recorded_port_survives_re_adding_the_box(lager_home):
    box_storage.add_box("mac", "127.0.0.1", python_port=8301)
    box_storage.add_box("mac", "127.0.0.1", version="v2")
    assert _saved(lager_home)["mac"] == {"ip": "127.0.0.1", "version": "v2",
                                         "python_port": 8301}
    box_storage.add_box("other", "10.0.0.9", version="v2")
    assert "python_port" not in _saved(lager_home)["other"]
