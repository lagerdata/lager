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
import contextlib
import hashlib
import io
import json
import os
import plistlib
import subprocess
import tarfile
import threading
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


@pytest.fixture(autouse=True)
def _a_terminal_is_assumed(monkeypatch):
    """No test may run the real `sudo -n true`. The tests of the check itself
    call _real_sudo_check."""
    monkeypatch.setattr(macos_box, "sudo_needs_a_terminal", lambda *a, **k: False)


_real_sudo_check = macos_box.sudo_needs_a_terminal


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
    assert macos_box.sdk_include_tree(layout, sdk) is None
    (sdk / "shared").mkdir(parents=True)
    (sdk / "libps2000").mkdir()
    (sdk / "shared" / "PicoStatus.h").write_text("s")
    (sdk / "libps2000" / "ps2000.h").write_text("f")
    tree = macos_box.sdk_include_tree(layout, sdk)
    assert sorted(p.name for p in (tree / "libps2000").iterdir()) == ["PicoStatus.h", "ps2000.h"]
    assert (tree / "libps2000" / "PicoStatus.h").read_text() == "s"


def _deb(files):
    """A .deb (ar archive) whose data.tar.xz holds ``{path: bytes}``."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:xz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    out = b"!<arch>\n"
    for name, body in ((b"debian-binary", b"2.0\n"), (b"data.tar.xz", data.getvalue())):
        out += (name.ljust(16) + b"0".ljust(12) + b"0".ljust(6) + b"0".ljust(6)
                + b"100644".ljust(8) + str(len(body)).encode().ljust(10) + b"`\n")
        out += body + (b"\n" if len(body) % 2 else b"")
    return out


class _PicoRepo:
    """Stands in for PicoTech's apt repository."""

    def __init__(self, debs, index_sha=None):
        self.files, stanzas = {}, []
        for package, deb in debs.items():
            path = f"pool/main/{package}_1_amd64.deb"
            self.files[f"{macos_box.PICO_APT_URL}/{path}"] = deb
            sha = (index_sha or {}).get(package) or hashlib.sha256(deb).hexdigest()
            stanzas.append(f"Package: {package}\nVersion: 1\nFilename: {path}\nSHA256: {sha}")
        self.files[macos_box.PICO_APT_INDEX] = "\n\n".join(stanzas).encode()

    def __call__(self, url):
        if url not in self.files:
            raise OSError(f"404 {url}")
        return self.files[url]


def _family_debs(extra=None):
    debs = {p: _deb({f"./opt/picoscope/include/{p}/{p}.h": p.encode(),
                     f"./opt/picoscope/lib/{p}.so": b"elf"})
            for p in macos_box.PICO_HEADER_PACKAGES}
    debs.update(extra or {})
    return debs


def test_pico_headers_come_from_the_apt_packages_and_nothing_else(layout):
    evil = _deb({"./opt/picoscope/include/../../../escape.h": b"x",
                 "./opt/picoscope/include/libps2000/ps2000.h": b"real"})
    assert macos_box.apt_include_tree(layout, _PicoRepo(_family_debs({"libps2000": evil}))) is None
    tree = layout.build_dir / "picoscope-include"
    headers = sorted(str(p.relative_to(tree)) for p in tree.rglob("*") if p.is_file())
    assert "libps2000/ps2000.h" in headers and len(headers) == len(macos_box.PICO_HEADER_PACKAGES)
    assert (tree / "libps2000" / "ps2000.h").read_bytes() == b"real"
    assert not any(layout.home.rglob("escape.h")), "a path out of the tree is skipped"
    assert not list(tree.rglob("*.so")), "only headers are kept"


def test_a_pico_package_with_the_wrong_checksum_is_refused(layout):
    repo = _PicoRepo(_family_debs(), index_sha={"libps4000a": "0" * 64})
    assert "checksum" in macos_box.apt_include_tree(layout, repo)
    assert not (layout.build_dir / "picoscope-include").exists()


def test_offline_the_headers_of_the_last_install_are_reused(layout):
    def offline(url):
        raise OSError("network is unreachable")
    assert "could not be downloaded" in macos_box.apt_include_tree(layout, offline)
    assert macos_box.apt_include_tree(layout, _PicoRepo(_family_debs())) is None
    assert macos_box.apt_include_tree(layout, offline) is None
    with mock.patch.object(macos_box, "sdk_include_tree", return_value=None):
        tree, reason = macos_box.pico_include_tree(layout, offline)
    assert reason is None and (tree / "libps5000a" / "libps5000a.h").exists()


def test_the_picoscope_app_is_installed_through_the_cask_only_when_missing(tmp_path):
    app = tmp_path / "PicoScope 7 T&M.app"
    app.mkdir()
    run = _Runner()
    with mock.patch.object(macos_box, "PICOSCOPE_APP", str(app)):
        assert macos_box.ensure_picoscope_app(run, echo=lambda *_: None) is None
    assert run.calls == []
    with mock.patch.object(macos_box, "PICOSCOPE_APP", str(tmp_path / "absent")), \
            mock.patch.object(macos_box, "homebrew_prefix_with",
                              return_value=str(tmp_path / "homebrew")):
        reason = macos_box.ensure_picoscope_app(run, echo=lambda *_: None)
    assert run.calls[0][0] == [str(tmp_path / "homebrew" / "bin" / "brew"),
                               "install", "--cask", "picoscope"]
    assert run.calls[0][1]["capture_output"] is False
    assert "brew install --cask picoscope" in reason


def test_a_cask_that_homebrew_still_records_is_reinstalled(tmp_path):
    """Homebrew keeps the cask's record after the app is deleted, and then
    `brew install` does nothing. The cask is reinstalled instead."""
    brew = tmp_path / "homebrew"
    (brew / "Caskroom" / "picoscope" / "7.2.30").mkdir(parents=True)
    app = tmp_path / "PicoScope 7 T&M.app"
    run = _Runner()

    def reinstall(argv, **kwargs):
        app.mkdir()
        return run(argv, **kwargs)

    with mock.patch.object(macos_box, "PICOSCOPE_APP", str(app)), \
            mock.patch.object(macos_box, "homebrew_prefix_with", return_value=str(brew)):
        assert macos_box.ensure_picoscope_app(reinstall, echo=lambda *_: None) is None
    assert run.calls == [([str(brew / "bin" / "brew"), "reinstall", "--cask", "picoscope"],
                          {"capture_output": False, "timeout": 1800})]


def test_a_failed_reinstall_names_the_reinstall_command(tmp_path):
    brew = tmp_path / "homebrew"
    (brew / "Caskroom" / "picoscope").mkdir(parents=True)
    with mock.patch.object(macos_box, "PICOSCOPE_APP", str(tmp_path / "absent")), \
            mock.patch.object(macos_box, "homebrew_prefix_with", return_value=str(brew)):
        reason = macos_box.ensure_picoscope_app(_Runner(), echo=lambda *_: None)
    assert "brew reinstall --cask picoscope" in reason


def test_optional_steps_give_a_reason_instead_of_failing(layout, tmp_path):
    with mock.patch.object(macos_box, "homebrew_prefix_with", return_value=None):
        assert "libusb" in macos_box.build_labjack_library(layout, _Runner())
    with mock.patch.object(macos_box, "find_cargo", return_value=str(tmp_path / "no-cargo")):
        assert "Rust" in macos_box.build_scope_daemon(layout, tmp_path, _Runner())
    layout.lib_dir.mkdir(parents=True)
    (layout.lib_dir / "liblabjackusb.dylib").write_text("")
    run = _Runner()
    assert macos_box.build_labjack_library(layout, run) is None
    assert run.calls == [], "already built: nothing is run"


def _fake_ljm_package(layout, files=None):
    """A runner whose curl/unzip/pkgutil lay out LabJack's package on disk."""
    files = files if files is not None else {
        macos_box.LJM_LIBRARY: "ljm", macos_box.LJM_LIBUSB: "usb",
        f"{macos_box.LJM_SHARE}/ljm_constants.json": "{}",
        f"{macos_box.LJM_SHARE}/ljm_startup_configs.json": "{}"}
    body = b"the LabJack package"

    def curl(argv, kwargs):
        if argv[0] != "curl":
            return False
        Path(argv[argv.index("-o") + 1]).write_bytes(body)
        return True

    def expand(argv, kwargs):
        if argv[:2] != ["pkgutil", "--expand-full"]:
            return False
        for name, text in files.items():
            (Path(argv[3]) / name).parent.mkdir(parents=True, exist_ok=True)
            (Path(argv[3]) / name).write_text(text)
        return True

    run = _Runner([(curl, _done()), (expand, _done())])
    return run, hashlib.sha256(body).hexdigest()


def test_ljm_is_unpacked_into_the_agent_directory_not_installed(layout):
    run, digest = _fake_ljm_package(layout)
    with mock.patch.object(macos_box, "LJM_SHA256", digest):
        assert macos_box.install_ljm_library(layout, run) is None
    assert (layout.lib_dir / "libLabJackM.dylib").read_text() == "ljm"
    assert (layout.lib_dir / "libusb-1.0.0.dylib").read_text() == "usb"
    assert (layout.ljm_dir / "ljm_constants.json").exists()
    assert (layout.ljm_dir / "ljm_startup_configs.json").exists()
    commands = [argv[0] for argv, _ in run.calls]
    assert commands == ["curl", "unzip", "pkgutil"], "expanded, never `installer`"
    assert all("sudo" not in argv for argv, _ in run.calls)
    assert not (layout.build_dir / "ljm").exists(), "the download is cleaned up"
    run = _Runner()
    assert macos_box.install_ljm_library(layout, run) is None
    assert run.calls == [], "already installed: nothing is run"


def test_ljm_with_the_wrong_checksum_is_refused(layout):
    run, _ = _fake_ljm_package(layout)
    reason = macos_box.install_ljm_library(layout, run)
    assert "checksum" in reason
    assert [argv[0] for argv, _ in run.calls] == ["curl"]
    assert not (layout.lib_dir / "libLabJackM.dylib").exists()


def test_ljm_failures_give_a_reason_instead_of_failing(layout):
    failed = _Runner([(lambda argv, kw: argv[0] == "curl", _done(22))])
    assert macos_box.LJM_URL in macos_box.install_ljm_library(layout, failed)
    run, digest = _fake_ljm_package(layout, files={})
    with mock.patch.object(macos_box, "LJM_SHA256", digest):
        assert "missing" in macos_box.install_ljm_library(layout, run)


def test_the_launchd_job_hands_the_agent_ljms_constants_only_when_installed(layout, tmp_path):
    job = plistlib.loads(macos_box.launch_agent_plist(layout, tmp_path / "src"))
    assert macos_box.LJM_CONSTANTS_ENV not in job["EnvironmentVariables"]
    layout.ljm_dir.mkdir(parents=True)
    (layout.ljm_dir / "ljm_constants.json").write_text("{}")
    job = plistlib.loads(macos_box.launch_agent_plist(layout, tmp_path / "src"))
    assert job["EnvironmentVariables"][macos_box.LJM_CONSTANTS_ENV] == \
        str(layout.ljm_dir / "ljm_constants.json")


def _docker_answers(states):
    """find_docker stand-in: raises or returns from ``states`` in turn."""
    def find(run):
        state = states.pop(0) if len(states) > 1 else states[0]
        if isinstance(state, Exception):
            raise state
        return state
    return find


def test_a_stopped_runtime_is_started_and_waited_for(tmp_path):
    app = tmp_path / "OrbStack.app"
    app.mkdir()
    stopped = LagerError("Docker is installed but not running.")
    run = _Runner()
    with mock.patch.object(macos_box, "RUNTIME_APPS", (("OrbStack", str(app)),)), \
            mock.patch.object(macos_box, "find_docker",
                              _docker_answers([stopped, stopped, ("docker", {})])), \
            mock.patch.object(macos_box.time, "sleep"):
        assert macos_box.ensure_docker(run, echo=lambda *_: None) == ("docker", {})
    assert [argv for argv, _ in run.calls] == [["open", "-a", "OrbStack"]]


def test_a_missing_runtime_is_installed_with_brew_only_when_the_user_agrees(tmp_path):
    app = tmp_path / "OrbStack.app"
    missing = macos_box.DockerMissing("Docker is not installed on this Mac.")

    def brew_installs(argv, kwargs):
        if argv[1:] == ["install", "--cask", "orbstack"]:
            app.mkdir()
            return True
        return False

    run = _Runner([(brew_installs, _done())])
    patches = (mock.patch.object(macos_box, "RUNTIME_APPS", (("OrbStack", str(app)),)),
               mock.patch.object(macos_box, "homebrew_prefix_with",
                                 return_value="/opt/homebrew"),
               mock.patch.object(macos_box.time, "sleep"))
    with patches[0], patches[1], patches[2], \
            mock.patch.object(macos_box, "find_docker", _docker_answers([missing])):
        with pytest.raises(macos_box.DockerMissing):
            macos_box.ensure_docker(run, echo=lambda *_: None, confirm=lambda q: False)
        with pytest.raises(macos_box.DockerMissing):
            macos_box.ensure_docker(run, echo=lambda *_: None)  # no one to ask
    assert run.calls == [], "declined or unasked: nothing is installed"

    with patches[0], patches[1], patches[2], \
            mock.patch.object(macos_box, "find_docker",
                              _docker_answers([missing, ("docker", {})])):
        assert macos_box.ensure_docker(run, echo=lambda *_: None,
                                       confirm=lambda q: True) == ("docker", {})
    argv = [a for a, _ in run.calls]
    assert argv == [["/opt/homebrew/bin/brew", "install", "--cask", "orbstack"],
                    ["open", "-a", "OrbStack"]]
    assert run.calls[0][1]["capture_output"] is False


def test_an_arm_gdb_is_installed_with_brew_only_when_missing():
    run = _Runner()
    with mock.patch.object(macos_box, "homebrew_prefix_with",
                           side_effect=lambda rel: "/opt/homebrew"):
        assert macos_box.ensure_arm_gdb(run, echo=lambda *_: None) is None
    assert run.calls == [], "already installed: nothing is run"

    found = {"bin/brew": "/opt/homebrew"}
    with mock.patch.object(macos_box, "homebrew_prefix_with", side_effect=found.get):
        reason = macos_box.ensure_arm_gdb(run, echo=lambda *_: None)
    assert run.calls[0][0] == ["/opt/homebrew/bin/brew", "install", "arm-none-eabi-gdb"]
    assert "brew install arm-none-eabi-gdb" in reason, "still absent afterwards"

    with mock.patch.object(macos_box, "homebrew_prefix_with", return_value=None):
        assert "Homebrew" in macos_box.ensure_arm_gdb(_Runner(), echo=lambda *_: None)


def test_rust_is_installed_with_brew_only_when_no_cargo_is_found(tmp_path):
    cargo = tmp_path / "cargo"
    run = _Runner()
    with mock.patch.object(macos_box, "find_cargo", return_value=str(cargo)):
        cargo.write_text("")
        assert macos_box.ensure_rust(run, echo=lambda *_: None) is None
        assert run.calls == [], "already installed, however old: nothing is run"
        cargo.unlink()

        found = {"bin/brew": "/opt/homebrew"}
        with mock.patch.object(macos_box, "homebrew_prefix_with", side_effect=found.get):
            installs = _Runner()

            def brew(argv, **kwargs):
                cargo.write_text("")
                return installs(argv, **kwargs)
            assert macos_box.ensure_rust(brew, echo=lambda *_: None) is None
            assert [a for a, _ in installs.calls] == [
                ["/opt/homebrew/bin/brew", "install", "rust"]]
            cargo.unlink()

            reason = macos_box.ensure_rust(_Runner(), echo=lambda *_: None)
            assert "brew install rust" in reason, "still absent afterwards"

        with mock.patch.object(macos_box, "homebrew_prefix_with", return_value=None):
            assert "Homebrew" in macos_box.ensure_rust(_Runner(), echo=lambda *_: None)


def test_libusb_is_installed_with_brew_when_missing_and_named_when_it_cannot_be():
    run = _Runner()
    with mock.patch.object(macos_box, "homebrew_prefix_with", return_value="/opt/homebrew"):
        assert macos_box.ensure_libusb(run, echo=lambda *_: None) is None
    assert run.calls == [], "already installed: nothing is run"

    found = {"bin/brew": "/opt/homebrew"}
    with mock.patch.object(macos_box, "homebrew_prefix_with", side_effect=found.get):
        reason = macos_box.ensure_libusb(run, echo=lambda *_: None)
    assert run.calls[0][0] == ["/opt/homebrew/bin/brew", "install", "libusb"]
    assert "brew install libusb" in reason, "still absent afterwards"

    with mock.patch.object(macos_box, "homebrew_prefix_with", return_value=None):
        reason = macos_box.ensure_libusb(_Runner(), echo=lambda *_: None)
    assert "Homebrew is not available" in reason and "brew install libusb" in reason


def test_the_agent_is_asked_to_load_libusb_on_its_own_library_path(layout):
    run = _Runner()
    assert macos_box.agent_loads_libusb(layout, run) is None
    argv = run.calls[0][0]
    assert argv[0] == "/usr/bin/env"
    assert argv[1] == f"DYLD_FALLBACK_LIBRARY_PATH={macos_box.library_path(layout)}"
    assert argv[2] == str(layout.python)
    assert "/opt/homebrew/lib" in argv[1]
    no_backend = _Runner([(lambda a, k: True, _done(3))])
    assert "cannot load libusb" in macos_box.agent_loads_libusb(layout, no_backend)
    crashed = _Runner([(lambda a, k: True, _done(1, err="ImportError: no usb"))])
    assert "ImportError: no usb" in macos_box.agent_loads_libusb(layout, crashed)


def test_libusb_is_installed_only_when_the_agent_cannot_load_one(layout, tmp_path):
    steps = ("build_labjack_library", "install_ljm_library", "ensure_rust", "build_scope_daemon",
             "ensure_picoscope_app", "ensure_jlink", "ensure_arm_gdb", "start_agent",
             "start_container", "check_platform", "ensure_config_dir", "write_state")
    for loads, expected_installs in ((None, 0), ("cannot load libusb", 1)):
        with contextlib.ExitStack() as stack:
            for name in steps:
                stack.enter_context(mock.patch.object(macos_box, name, return_value=None))
            for name, value in (("ensure_docker", ("docker", {})), ("ensure_source", tmp_path),
                                ("source_version", "9.9.9"), ("ensure_agent_env", []),
                                ("wait_for_link", ""), ("BackgroundPull", mock.Mock())):
                stack.enter_context(mock.patch.object(macos_box, name, return_value=value))
            probe = stack.enter_context(mock.patch.object(
                macos_box, "agent_loads_libusb", side_effect=[loads, None]))
            install = stack.enter_context(mock.patch.object(
                macos_box, "ensure_libusb", return_value=None))
            result = macos_box.install("v9.9.9", "9.9.9", echo=lambda *_: None,
                                       layout=layout, source=tmp_path)
        assert install.call_count == expected_installs
        assert probe.call_count == 1 + expected_installs, "checked again after installing"
        assert macos_box.USB_INSTRUMENTS in result.ready


def test_jlink_is_installed_through_the_cask_only_when_missing(tmp_path):
    exe = tmp_path / "JLinkExe"
    exe.write_text("")
    run = _Runner()
    with mock.patch.object(macos_box, "JLINK_EXE", str(exe)):
        assert macos_box.ensure_jlink(run, echo=lambda *_: None) is None
    assert run.calls == [], "already installed: nothing is run"

    absent = str(tmp_path / "absent")
    with mock.patch.object(macos_box, "JLINK_EXE", absent), \
            mock.patch.object(macos_box, "homebrew_prefix_with", return_value=None):
        assert "Homebrew" in macos_box.ensure_jlink(run, echo=lambda *_: None)
    assert run.calls == []

    with mock.patch.object(macos_box, "JLINK_EXE", absent), \
            mock.patch.object(macos_box, "homebrew_prefix_with", return_value="/opt/homebrew"):
        reason = macos_box.ensure_jlink(run, echo=lambda *_: None)
    argv, kwargs = run.calls[0]
    assert argv == ["/opt/homebrew/bin/brew", "install", "--cask", "segger-jlink"]
    assert kwargs["capture_output"] is False, "sudo must be able to ask on the terminal"
    assert "brew install --cask segger-jlink" in reason, "still absent afterwards"


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


def test_the_manual_fix_is_one_command_per_line(tmp_path):
    with mock.patch.object(macos_box, "ETC_DIR", str(tmp_path / "absent")):
        with pytest.raises(LagerError) as caught:
            macos_box.ensure_config_dir(_Runner(), user="me")
    fixes = caught.value.fixes
    assert fixes[:3] == [f"sudo mkdir -p {tmp_path / 'absent'}",
                         f"sudo chown me {tmp_path / 'absent'}",
                         f"chmod 700 {tmp_path / 'absent'}"]
    assert not any("&&" in fix for fix in fixes)


def test_sudo_needs_a_terminal_only_without_one_and_without_a_credential():
    asked = _Runner()
    assert _real_sudo_check(asked, isatty=lambda: True) is False
    assert asked.calls == [], "with a terminal, sudo is not even asked"
    assert _real_sudo_check(_Runner([(lambda a, k: a[:2] == ["sudo", "-n"], _done(0))]),
                            isatty=lambda: False) is False
    assert _real_sudo_check(_Runner([(lambda a, k: a[:2] == ["sudo", "-n"], _done(1))]),
                            isatty=lambda: False) is True


def test_no_terminal_fails_before_any_question(lager_home, tmp_path, monkeypatch):
    from cli.commands.utility.install import install
    monkeypatch.setattr(macos_box, "sudo_needs_a_terminal", lambda *a, **k: True)
    with mock.patch.object(_local_install.sys, "platform", "darwin"), \
            mock.patch.object(macos_box, "ETC_DIR", str(tmp_path / "etc")), \
            mock.patch.object(macos_box, "install") as provision:
        result = CliRunner().invoke(install, [], obj={}, input="y\nbench\n")
    assert result.exit_code != 0
    assert "no terminal" in result.output
    assert "Continue?" not in result.output
    assert f"sudo mkdir -p {tmp_path / 'etc'}" in result.output
    provision.assert_not_called()


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


def test_uninstall_removes_the_box_images_unless_asked_to_keep_them(layout, tmp_path):
    listed = "ghcr.io/lagerdata/lager-box:v0.64.0\nghcr.io/lagerdata/lager-box:<none>\n"
    with mock.patch.object(macos_box, "ETC_DIR", str(tmp_path)), \
            mock.patch.object(macos_box, "find_docker", return_value=("docker", {})):
        run = _Runner([(lambda a, k: a[1] == "images", _done(0, listed))])
        macos_box.uninstall(run=run, layout=layout, echo=lambda *_: None)
        assert ["docker", "rmi", "-f", "ghcr.io/lagerdata/lager-box:v0.64.0"] \
            in [c[0] for c in run.calls]
        kept = _Runner([(lambda a, k: a[1] == "images", _done(0, listed))])
        macos_box.uninstall(run=kept, layout=layout, echo=lambda *_: None, keep_images=True)
        assert not any(c[0][1] in ("images", "rmi") for c in kept.calls)
    assert any("images" in piece for piece in macos_box.installed_pieces(layout))
    assert not any("images" in piece
                   for piece in macos_box.installed_pieces(layout, keep_images=True))


def test_uninstall_with_docker_stopped_removes_nothing(layout, tmp_path):
    """The container would come back with Docker and hold the Box's ports,
    so nothing is removed and the uninstall can be run again."""
    layout.app_dir.mkdir(parents=True)
    layout.plist.parent.mkdir(parents=True)
    layout.plist.write_text("")
    run = _Runner()
    with mock.patch.object(macos_box, "ETC_DIR", str(tmp_path)), \
            mock.patch.object(macos_box, "find_docker",
                              side_effect=LagerError("Docker is installed but not running.")):
        with pytest.raises(LagerError) as caught:
            macos_box.uninstall(run=run, layout=layout, echo=lambda *_: None)
    assert "Nothing was uninstalled" in caught.value.problem
    assert layout.app_dir.exists() and layout.plist.exists()
    assert run.calls == []


def test_uninstall_without_docker_installed_still_removes_the_agent(layout, tmp_path):
    layout.app_dir.mkdir(parents=True)
    layout.plist.parent.mkdir(parents=True)
    layout.plist.write_text("")
    lines = []
    with mock.patch.object(macos_box, "ETC_DIR", str(tmp_path)), \
            mock.patch.object(macos_box, "find_docker",
                              side_effect=macos_box.DockerMissing("Docker is not installed.")):
        macos_box.uninstall(run=_Runner(), layout=layout, echo=lines.append)
    assert not layout.app_dir.exists() and not layout.plist.exists()
    assert any("no Box container to remove" in line for line in lines)


def test_a_failed_local_uninstall_keeps_the_saved_box(lager_home):
    from cli.commands.utility.uninstall import uninstall
    box_storage.add_box("mac", "127.0.0.1", version="1.0.0", python_port=8301)
    with mock.patch.object(_local_install.sys, "platform", "darwin"), \
            mock.patch.object(macos_box, "installed_pieces", return_value=["the container"]), \
            mock.patch.object(macos_box, "uninstall",
                              side_effect=LagerError("Docker is installed but not running.")):
        result = CliRunner().invoke(uninstall, ["--box", "mac", "--yes"], obj={})
    assert result.exit_code != 0
    assert "Uninstall complete" not in result.output
    assert "mac" in _saved(lager_home)


def test_docker_pull_output_is_captured_not_printed(tmp_path):
    """Without a terminal Docker prints hundreds of progress lines."""
    missing = _Runner([(lambda a, k: a[1:3] == ["image", "inspect"], _done(1))])
    with mock.patch.object(macos_box, "ETC_DIR", str(tmp_path)):
        macos_box.start_container("docker", {}, "img:v1", tmp_path, missing)
    pulls = [(a, k) for a, k in missing.calls if a[1] == "pull"]
    assert [a for a, _ in pulls] == [["docker", "pull", "--platform", "linux/amd64", "img:v1"]]
    assert "capture_output" not in pulls[0][1], "the runner's default captures"


def test_the_image_downloads_in_the_background_and_says_it_is_still_going():
    release = threading.Event()

    def slow_pull(argv, **kwargs):
        if argv[1:3] == ["image", "inspect"]:
            return _done(1)
        release.wait(5)
        return _done()
    lines = []
    download = macos_box.BackgroundPull("docker", {}, "img:v1", slow_pull, lines.append)
    assert download.thread.is_alive(), "the install goes on while it downloads"
    threading.Timer(0.15, release.set).start()
    download.wait(lines.append, every_s=0.05)
    assert "Waiting for the Box image to finish downloading..." in lines
    assert any("still downloading" in line for line in lines)

    present = macos_box.BackgroundPull("docker", {}, "img:v1", _Runner(), lines.append)
    assert present.thread is None
    present.wait(lines.append)

    failing = _Runner([(lambda a, k: a[1:3] == ["image", "inspect"], _done(1)),
                       (lambda a, k: a[1] == "pull", _done(1, err="denied"))])
    with mock.patch.object(macos_box, "anonymous_env", return_value={}):
        failed = macos_box.BackgroundPull("docker", {}, "img:v1", failing, lines.append)
        with pytest.raises(LagerError) as caught:
            failed.wait(lines.append, every_s=0.05)
    assert caught.value.cause == "denied"


def test_what_homebrew_will_install_is_known_before_the_install(tmp_path):
    with mock.patch.object(macos_box, "homebrew_prefix_with",
                           side_effect=lambda rel: "/opt/homebrew" if rel == "bin/brew" else None), \
            mock.patch.object(macos_box, "RUNTIME_APPS", (("OrbStack", str(tmp_path / "no")),)), \
            mock.patch.object(macos_box.shutil, "which", return_value=None), \
            mock.patch.object(macos_box, "JLINK_EXE", str(tmp_path / "no")), \
            mock.patch.object(macos_box, "PICOSCOPE_APP", str(tmp_path / "no")), \
            mock.patch.object(macos_box, "find_cargo", return_value=str(tmp_path / "no")):
        assert macos_box.homebrew_installs() == [
            "libusb", "OrbStack (Docker)", "SEGGER J-Link", "PicoScope 7", "Rust", "an ARM GDB"]
        assert macos_box.homebrew_installs(["jlink", "picoscope"]) == [
            "libusb", "OrbStack (Docker)"], "--skip-jlink leaves the ARM GDB out too"
        assert macos_box.homebrew_installs(["picoscope"]) == [
            "libusb", "OrbStack (Docker)", "SEGGER J-Link", "an ARM GDB"]
    with mock.patch.object(macos_box, "homebrew_prefix_with", return_value=None):
        assert macos_box.homebrew_installs() == [], "no Homebrew: nothing to install with"


def test_install_reports_what_it_set_up_and_leaves_out_what_is_skipped(layout, tmp_path):
    steps = {name: mock.patch.object(macos_box, name, return_value=None)
             for name in ("ensure_libusb", "agent_loads_libusb",
                          "build_labjack_library", "install_ljm_library",
                          "ensure_rust", "build_scope_daemon",
                          "ensure_picoscope_app", "ensure_jlink", "ensure_arm_gdb",
                          "start_agent", "start_container", "check_platform",
                          "ensure_config_dir", "write_state")}
    others = (mock.patch.object(macos_box, "ensure_docker", return_value=("docker", {})),
              mock.patch.object(macos_box, "ensure_source", return_value=tmp_path),
              mock.patch.object(macos_box, "source_version", return_value="9.9.9"),
              mock.patch.object(macos_box, "ensure_agent_env", return_value=[]),
              mock.patch.object(macos_box, "wait_for_link", return_value=""),
              mock.patch.object(macos_box, "BackgroundPull"))
    lines = []
    with contextlib.ExitStack() as stack:
        called = {name: stack.enter_context(patch) for name, patch in steps.items()}
        for patch in others:
            stack.enter_context(patch)
        called["ensure_arm_gdb"].return_value = "no Homebrew"
        result = macos_box.install("v9.9.9", "9.9.9", echo=lines.append, layout=layout,
                                   source=tmp_path, skip=["jlink", "picoscope"])
    assert result.ready == [macos_box.USB_INSTRUMENTS, "LabJack U3/U6 support",
                            "LabJack T4/T7/T8 support"]
    assert result.skipped == [], "nothing J-Link needs is attempted under --skip-jlink"
    for left_out in ("ensure_jlink", "ensure_arm_gdb", "ensure_picoscope_app",
                     "ensure_rust", "build_scope_daemon"):
        called[left_out].assert_not_called()
    assert "Leaving out J-Link debugging and its ARM GDB (--skip-jlink)." in lines
    assert "Leaving out PicoScope support (--skip-picoscope)." in lines


# ------------------------------------------------------------------ routing

def test_loopback_is_local_only_on_a_mac():
    for address in ("127.0.0.1", "localhost", "::1"):
        assert _local_install.is_local_target(address, platform="darwin")
        assert not _local_install.is_local_target(address, platform="linux")
        assert not _local_install.is_local_target(address, platform="win32")
    assert not _local_install.is_local_target("192.168.1.5", platform="darwin")


def test_a_local_install_on_windows_says_not_yet_supported():
    ctx = mock.Mock()
    with pytest.raises(LagerError, match="not yet supported") as caught:
        _local_install.install_locally(ctx, version="v1", version_defaulted=True,
                                       yes=True, platform="win32")
    assert "Windows" in caught.value.problem
    with pytest.raises(LagerError, match="not yet supported"):
        _local_install.uninstall_locally(ctx, purge_config=False, yes=True,
                                         dry_run=False, platform="win32")


def test_this_machine_is_an_ssh_target_only_on_linux():
    yes, no = (lambda: True), (lambda: False)
    for platform in ("darwin", "win32"):
        assert _local_install.local_ssh_target(platform=platform, answers=no) is None
    with mock.patch.object(_local_install.getpass, "getuser", return_value="me"):
        assert _local_install.local_ssh_target(platform="linux", answers=yes) \
            == ("127.0.0.1", "me")
    assert _local_install.local_ssh_target("pi", platform="linux", answers=yes) \
        == ("127.0.0.1", "pi")
    with pytest.raises(LagerError, match="does not accept SSH") as caught:
        _local_install.local_ssh_target(platform="linux", answers=no)
    assert any("openssh-server" in fix for fix in caught.value.fixes)


@pytest.fixture
def lager_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LAGER_CONFIG_FILE_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(macos_box, "BOX_NAME_FILE", str(tmp_path / "box-name"))
    monkeypatch.setattr(_local_install.getpass, "getuser", lambda: "me")
    monkeypatch.setattr(macos_box, "homebrew_installs", lambda skip=(): [])
    return tmp_path


def _saved(home):
    return json.loads((home / ".lager").read_text())["BOXES"]


def test_install_without_a_target_provisions_and_registers_the_port(lager_home):
    from cli.commands.utility.install import install
    result_value = macos_box.Result(source=Path("/src"), image="img", box_version="9.9.9",
                                    skipped=["Oscilloscope support: no Rust"],
                                    ready=["J-Link debugging"])
    with mock.patch.object(_local_install.sys, "platform", "darwin"), \
            mock.patch.object(macos_box, "install", return_value=result_value) as provision, \
            mock.patch("subprocess.run") as ssh:
        result = CliRunner().invoke(install, ["--yes"], obj={})
    assert result.exit_code == 0, result.output
    provision.assert_called_once()
    ssh.assert_not_called()
    assert "Not set up -- Oscilloscope support: no Rust" in result.output
    assert "Set up: J-Link debugging." in result.output
    assert _saved(lager_home) == {"local": {"ip": "127.0.0.1", "user": "me",
                                            "version": "9.9.9", "python_port": 8301}}
    assert box_storage.get_python_service_port("127.0.0.1") == 8301


class _Stop(Exception):
    """Raised by the first step of the SSH path, to end the command there."""


def test_install_without_a_target_on_linux_runs_the_ssh_path_against_this_machine(lager_home):
    """The same deploy as a remote Linux Box, aimed at loopback as this user."""
    import importlib
    module = importlib.import_module("cli.commands.utility.install")
    with mock.patch.object(_local_install.sys, "platform", "linux"), \
            mock.patch.object(_local_install, "_ssh_server_answers", return_value=True), \
            mock.patch.object(_local_install.getpass, "getuser", return_value="me"), \
            mock.patch.object(_local_install, "local_ssh_target",
                              wraps=_local_install.local_ssh_target) as asked, \
            mock.patch.object(macos_box, "install") as provision, \
            mock.patch.object(module, "probe_box_identity", side_effect=_Stop) as ssh:
        result = CliRunner().invoke(module.install, ["--yes"], obj={})
    asked.assert_called_once_with(None)
    ssh.assert_called_once_with("me@127.0.0.1")
    assert "over SSH as me@127.0.0.1" in result.output
    provision.assert_not_called()


def test_uninstall_without_a_target_off_a_mac_is_still_an_error(lager_home):
    """A forgotten --box on a Linux machine must not uninstall that machine,
    even with --yes (and --all would also reset its firewall)."""
    import importlib
    module = importlib.import_module("cli.commands.utility.uninstall")
    for platform in ("linux", "win32"):
        for extra in ([], ["--all"]):
            with mock.patch.object(_local_install.sys, "platform", platform), \
                    mock.patch.object(_local_install, "_ssh_server_answers", return_value=True), \
                    mock.patch.object(macos_box, "uninstall") as remove, \
                    mock.patch.object(module, "probe_box_identity", side_effect=_Stop) as ssh, \
                    mock.patch("subprocess.run") as run:
                result = CliRunner().invoke(module.uninstall, ["--yes", *extra], obj={})
            assert result.exit_code == 1, (platform, extra, result.output)
            assert "Either --box or --ip is required" in result.output
            assert "--ip 127.0.0.1" in result.output
            ssh.assert_not_called()
            run.assert_not_called()
            remove.assert_not_called()


def test_uninstall_of_this_linux_machine_is_still_possible_by_naming_it(lager_home):
    import importlib
    module = importlib.import_module("cli.commands.utility.uninstall")
    with mock.patch.object(_local_install.sys, "platform", "linux"), \
            mock.patch.object(module, "probe_box_identity", side_effect=_Stop) as ssh:
        CliRunner().invoke(module.uninstall, ["--ip", "127.0.0.1", "--yes"], obj={})
    ssh.assert_called_once_with("lagerdata@127.0.0.1")


def test_without_a_target_on_linux_and_no_ssh_server_the_fix_is_named(lager_home):
    from cli.commands.utility.install import install
    with mock.patch.object(_local_install.sys, "platform", "linux"), \
            mock.patch.object(_local_install, "_ssh_server_answers", return_value=False), \
            mock.patch("subprocess.run") as ssh:
        result = CliRunner().invoke(install, ["--yes"], obj={})
    assert result.exit_code == 1
    assert "does not accept SSH" in result.output and "openssh-server" in result.output
    ssh.assert_not_called()


def test_install_without_a_target_on_windows_exits_with_the_message(lager_home):
    from cli.commands.utility.install import install
    with mock.patch.object(_local_install.sys, "platform", "win32"), \
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
        assert _saved(lager_home)["mac"] == {"ip": "127.0.0.1", "user": "me",
                                             "version": "2.0.0", "python_port": 8301}
        dry = CliRunner().invoke(uninstall, ["--box", "mac", "--dry-run"], obj={})
        assert dry.exit_code == 0 and "nothing was changed" in dry.output
        remove.assert_not_called()
        result = CliRunner().invoke(uninstall, ["--box", "mac", "--yes"], obj={})
        assert result.exit_code == 0, result.output
        remove.assert_called_once()
    assert _saved(lager_home) == {}


def test_a_reinstall_after_an_uninstall_keeps_the_box_name(lager_home):
    from cli.commands.utility.install import install
    from cli.commands.utility.uninstall import uninstall
    value = macos_box.Result(source=Path("/src"), image="img", box_version="2.0.0")
    with mock.patch.object(_local_install.sys, "platform", "darwin"), \
            mock.patch.object(macos_box, "install", return_value=value), \
            mock.patch.object(macos_box, "uninstall"), \
            mock.patch.object(macos_box, "installed_pieces", return_value=["the container"]):
        first = CliRunner().invoke(install, ["--yes"], obj={}, input="")
        assert "Saved as 'local'" in first.output
        box_storage.delete_box("local")
        box_storage.add_box("bench", "127.0.0.1", version="2.0.0", python_port=8301)
        macos_box.remember_box_name("bench")
        CliRunner().invoke(uninstall, ["--yes"], obj={})
        assert _saved(lager_home) == {}
        again = CliRunner().invoke(install, ["--yes"], obj={})
    assert again.exit_code == 0, again.output
    assert list(_saved(lager_home)) == ["bench"]
    assert "lager hello --box bench" in again.output


def test_linux_only_options_are_named_as_ignored_and_the_skips_are_passed(lager_home):
    from cli.commands.utility.install import install
    value = macos_box.Result(source=Path("/src"), image="img", box_version="2.0.0")
    with mock.patch.object(_local_install.sys, "platform", "darwin"), \
            mock.patch.object(macos_box, "install", return_value=value) as provision:
        result = CliRunner().invoke(install, ["--yes", "--skip-firewall", "--timeout", "5",
                                              "--skip-jlink"], obj={})
        plain = CliRunner().invoke(install, ["--yes"], obj={})
    assert result.exit_code == 0, result.output
    assert "Ignoring --skip-firewall" in result.output
    assert "Ignoring --timeout" in result.output
    assert "Ignoring" not in plain.output
    assert provision.call_args_list[0].kwargs["skip"] == ["jlink"]
    assert provision.call_args_list[1].kwargs["skip"] == []


def test_the_password_is_mentioned_only_when_it_will_be_asked(lager_home, tmp_path):
    from cli.commands.utility.install import install
    value = macos_box.Result(source=Path("/src"), image="img", box_version="2.0.0")
    etc = tmp_path / "etc"
    with mock.patch.object(_local_install.sys, "platform", "darwin"), \
            mock.patch.object(macos_box, "install", return_value=value), \
            mock.patch.object(macos_box, "ETC_DIR", str(etc)):
        absent = CliRunner().invoke(install, ["--yes"], obj={})
        etc.mkdir()
        present = CliRunner().invoke(install, ["--yes"], obj={})
    assert "password" in absent.output
    assert "password" not in present.output


def test_every_question_is_asked_before_the_install_starts(lager_home, monkeypatch):
    from cli.commands.utility.install import install
    value = macos_box.Result(source=Path("/src"), image="img", box_version="2.0.0")
    monkeypatch.setattr(macos_box, "homebrew_installs",
                        lambda skip=(): ["SEGGER J-Link", "an ARM GDB"])
    with mock.patch.object(_local_install.sys, "platform", "darwin"), \
            mock.patch.object(macos_box, "install", return_value=value) as provision:
        provision.side_effect = lambda *a, **k: (print("INSTALLING"), value)[1]
        result = CliRunner().invoke(install, [], obj={}, input="y\nbench\n")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Homebrew will install: SEGGER J-Link, an ARM GDB." in out
    assert "The SEGGER J-Link installer asks for your Mac password" in out
    assert out.index("Continue?") < out.index("Save the Box in this CLI as") \
        < out.index("INSTALLING")
    assert list(_saved(lager_home)) == ["bench"]


def test_update_of_the_local_box_is_the_install_again():
    ctx = mock.Mock()
    with mock.patch.object(_local_install, "install_locally") as again:
        _local_install.update_locally(ctx, version=None, yes=True)
    from cli.commands.utility.update import default_box_version
    again.assert_called_once_with(ctx, version=default_box_version(),
                                  version_defaulted=True, yes=True, fork=None,
                                  update=True)
    ctx.exit.side_effect = click.exceptions.Exit(2)
    with pytest.raises(click.exceptions.Exit):
        _local_install.update_locally(ctx, version=None, yes=True, check=True)


def test_an_update_leaves_out_what_the_install_left_out(lager_home, tmp_path):
    """`lager install --skip-jlink`, then `lager update`: no J-Link cask, no
    sudo prompt, on the update either."""
    from cli.commands.utility.install import install
    value = macos_box.Result(source=Path("/src"), image="img", box_version="2.0.0")
    skip_file = tmp_path / "install-skips"
    with mock.patch.object(_local_install.sys, "platform", "darwin"), \
            mock.patch.object(macos_box, "SKIP_FILE", str(skip_file)), \
            mock.patch.object(macos_box, "install", return_value=value) as provision:
        first = CliRunner().invoke(install, ["--yes", "--skip-jlink"], obj={})
        assert first.exit_code == 0, first.output
        macos_box.remember_skips(provision.call_args.kwargs["skip"])
        provision.reset_mock()
        _local_install.update_locally(mock.Mock(params={}), version=None, yes=True)
    assert provision.call_args.kwargs["skip"] == ["jlink"]


def test_an_explicit_install_does_not_reuse_old_skips(lager_home, tmp_path):
    from cli.commands.utility.install import install
    value = macos_box.Result(source=Path("/src"), image="img", box_version="2.0.0")
    skip_file = tmp_path / "install-skips"
    skip_file.write_text("jlink picoscope\n")
    with mock.patch.object(_local_install.sys, "platform", "darwin"), \
            mock.patch.object(macos_box, "SKIP_FILE", str(skip_file)), \
            mock.patch.object(macos_box, "install", return_value=value) as provision:
        result = CliRunner().invoke(install, ["--yes"], obj={})
    assert result.exit_code == 0, result.output
    assert provision.call_args.kwargs["skip"] == []


def test_skips_are_remembered_and_read_back(tmp_path):
    skip_file = tmp_path / "install-skips"
    with mock.patch.object(macos_box, "SKIP_FILE", str(skip_file)):
        assert macos_box.remembered_skips() == []
        macos_box.remember_skips(["picoscope", "jlink", "bogus"])
        assert macos_box.remembered_skips() == ["jlink", "picoscope"]
        macos_box.remember_skips([])
        assert macos_box.remembered_skips() == []
        skip_file.write_text("rm -rf jlink\n")
        assert macos_box.remembered_skips() == ["jlink"]


@pytest.mark.parametrize("version", ["v0.66.0", "0.66.0", "v1.2.3", "main", "staging",
                                     "de/macos-box", "a" * 40, "v0.66.0-rc1",
                                     "release_1.x"])
def test_a_release_branch_or_commit_is_accepted(version):
    macos_box.check_version(version)


@pytest.mark.parametrize("version", ["", "v0.66.0; rm -rf ~", "-x", "--upload-pack=evil",
                                     "a..b", "a//b", "feature/", "x.lock", "has space",
                                     "/abs"])
def test_a_version_that_is_no_ref_is_refused_before_anything_changes(version):
    with pytest.raises(LagerError) as caught:
        macos_box.check_version(version)
    assert "is not a Lager release, branch or commit" in caught.value.problem


def test_install_refuses_a_bad_version_before_touching_docker(tmp_path):
    layout = macos_box.Layout(home=tmp_path)
    with mock.patch.object(macos_box, "running_from_checkout", return_value=None), \
            mock.patch.object(macos_box, "check_platform"), \
            mock.patch.object(macos_box, "ensure_docker") as docker:
        with pytest.raises(LagerError):
            macos_box.install("main; rm -rf ~", "0.66.0", layout=layout, echo=lambda *a: None)
    docker.assert_not_called()


SHA = "37e453076622280e72783edf01f684d793f6bb59"


def _git(sha=SHA, fail_on=None, with_host=True):
    """A stand-in for git: the checkout step lays out a Box tree."""
    def answer(argv, **kwargs):
        if fail_on and fail_on in argv:
            return _done(128, err=f"fatal: couldn't find remote ref {argv[-1]}")
        if "checkout" in argv:
            tree = Path(argv[argv.index("-C") + 1])
            host = tree / "box" / "lager" / ("host" if with_host else "other")
            host.mkdir(parents=True)
            (host / "__main__.py").write_text("")
        if "rev-parse" in argv:
            return _done(out=sha + "\n")
        return _done()
    return answer


def _fetch(layout, ref, **kw):
    answer = _git(**kw)
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return answer(argv, **kwargs)
    with mock.patch.object(macos_box.shutil, "which", return_value="/usr/bin/git"):
        return macos_box.fetch_ref(layout, ref, run), calls


def test_a_branch_is_fetched_into_a_tree_named_by_its_commit(layout):
    tree, calls = _fetch(layout, "de/macos-hostlink-net-info")
    assert tree == layout.source_dir / "commits" / SHA
    assert (tree / "box" / "lager" / "host" / "__main__.py").exists()
    assert not (layout.source_dir / ".incoming").exists()
    fetch = next(c for c in calls if "fetch" in c)
    assert fetch[-2:] == [macos_box.SOURCE_REPO, "de/macos-hostlink-net-info"]
    assert "--depth" in fetch


def test_a_commit_already_here_is_reused_and_a_moved_branch_gets_a_new_tree(layout):
    first, _ = _fetch(layout, "main")
    (first / "marker").write_text("running")
    again, _ = _fetch(layout, "main")
    assert again == first and (first / "marker").exists(), "the running tree is left alone"
    moved, _ = _fetch(layout, "main", sha="b" * 40)
    assert moved != first and first.exists()


def test_a_missing_branch_is_an_error_and_leaves_nothing_behind(layout):
    with pytest.raises(LagerError) as caught:
        _fetch(layout, "no-such-branch", fail_on="fetch")
    assert caught.value.problem == "The CLI did not download Lager no-such-branch."
    assert "couldn't find remote ref" in caught.value.cause
    assert not (layout.source_dir / ".incoming").exists()
    assert not (layout.source_dir / "commits").exists()


def test_a_branch_without_the_mac_box_is_refused(layout):
    with pytest.raises(LagerError) as caught:
        _fetch(layout, "old-branch", with_host=False)
    assert "predates the macOS Box" in caught.value.problem


def test_only_the_running_commit_tree_is_kept(layout):
    keep = layout.source_dir / "commits" / SHA
    old = layout.source_dir / "commits" / ("c" * 40)
    for tree in (keep, old):
        tree.mkdir(parents=True)
    macos_box.prune_commit_trees(layout, keep)
    assert keep.exists() and not old.exists()


def test_a_release_still_uses_its_own_clone_and_a_branch_is_fetched(layout):
    with mock.patch.object(macos_box, "running_from_checkout", return_value=Path("/co")), \
            mock.patch.object(macos_box, "fetch_ref", return_value=Path("/fetched")) as fetch:
        assert macos_box.ensure_source(layout, "main") == Path("/co")
        assert macos_box.ensure_source(layout, "main", use_checkout=False) == Path("/fetched")
    fetch.assert_called_once()


def _install_steps(tmp_path):
    """Every step of an install stubbed, so install() itself can be checked."""
    names = ("ensure_libusb", "agent_loads_libusb", "build_labjack_library",
             "install_ljm_library", "ensure_rust", "build_scope_daemon",
             "ensure_picoscope_app", "ensure_jlink", "ensure_arm_gdb", "start_agent",
             "start_container", "check_platform", "ensure_config_dir", "write_state",
             "remember_skips", "prune_commit_trees")
    patches = {name: mock.patch.object(macos_box, name, return_value=None) for name in names}
    patches.update({
        "ensure_docker": mock.patch.object(macos_box, "ensure_docker", return_value=("docker", {})),
        "ensure_agent_env": mock.patch.object(macos_box, "ensure_agent_env", return_value=[]),
        "wait_for_link": mock.patch.object(macos_box, "wait_for_link", return_value=""),
        "BackgroundPull": mock.patch.object(macos_box, "BackgroundPull"),
        "running_from_checkout": mock.patch.object(macos_box, "running_from_checkout",
                                                   return_value=tmp_path / "checkout"),
    })
    return patches


def test_a_branch_runs_its_code_on_the_image_of_its_release(layout, tmp_path):
    tree = layout.source_dir / "commits" / SHA
    lines = []
    with contextlib.ExitStack() as stack:
        called = {n: stack.enter_context(p) for n, p in _install_steps(tmp_path).items()}
        stack.enter_context(mock.patch.object(macos_box, "fetch_ref", return_value=tree))
        stack.enter_context(mock.patch.object(macos_box, "source_version", return_value="0.66.0"))
        result = macos_box.install("de/macos-hostlink-net-info", "0.66.0", echo=lines.append,
                                   layout=layout, skip=["jlink", "picoscope"],
                                   use_checkout=False)
    assert result.source == tree
    assert result.image == "ghcr.io/lagerdata/lager-box:v0.66.0"
    called["start_container"].assert_called_once()
    assert called["start_container"].call_args.args[3] == tree
    called["write_state"].assert_called_once_with(
        "0.66.0", "0.66.0", f"de/macos-hostlink-net-info@{SHA}")
    called["prune_commit_trees"].assert_called_once_with(layout, keep=tree)
    assert any(line.startswith(f"Installing de/macos-hostlink-net-info (commit {SHA[:12]})")
               for line in lines)


def test_a_release_install_is_unchanged(layout, tmp_path):
    tree = layout.source_dir / "v0.66.0"
    with contextlib.ExitStack() as stack:
        called = {n: stack.enter_context(p) for n, p in _install_steps(tmp_path).items()}
        stack.enter_context(mock.patch.object(macos_box, "ensure_source", return_value=tree))
        stack.enter_context(mock.patch.object(macos_box, "source_version", return_value="0.66.0"))
        result = macos_box.install("v0.66.0", "0.66.0", echo=lambda *a: None, layout=layout,
                                   skip=["jlink", "picoscope"], use_checkout=False)
    assert result.image == "ghcr.io/lagerdata/lager-box:v0.66.0"
    called["write_state"].assert_called_once_with("0.66.0", "0.66.0", "v0.66.0")
    called["prune_commit_trees"].assert_not_called()


@pytest.mark.parametrize("argv, uses_checkout", [(["--yes"], True),
                                                 (["--yes", "--version", "main"], False)])
def test_a_named_version_wins_over_the_checkout(lager_home, argv, uses_checkout):
    from cli.commands.utility.install import install
    result_value = macos_box.Result(source=Path("/src"), image="img", box_version="9.9.9")
    with mock.patch.object(_local_install.sys, "platform", "darwin"), \
            mock.patch.object(macos_box, "running_from_checkout", return_value=Path("/co")), \
            mock.patch.object(macos_box, "install", return_value=result_value) as provision:
        result = CliRunner().invoke(install, argv, obj={})
    assert result.exit_code == 0, result.output
    assert provision.call_args.kwargs["use_checkout"] is uses_checkout
    assert ("from the checkout at /co" in result.output) is uses_checkout


def test_the_agent_is_replaced_only_once_the_image_is_here(tmp_path):
    """A failed image download leaves the running agent alone."""
    order = []

    class Pull:
        def __init__(self, *a, **k):
            pass

        def wait(self, echo):
            order.append("image")
            raise LagerError("The Box image did not download.")

    layout = macos_box.Layout(home=tmp_path)
    src = tmp_path / "src"
    with mock.patch.object(macos_box, "running_from_checkout", return_value=None), \
            mock.patch.object(macos_box, "check_platform"), \
            mock.patch.object(macos_box, "ensure_docker", return_value=("docker", {})), \
            mock.patch.object(macos_box, "ensure_config_dir"), \
            mock.patch.object(macos_box, "ensure_source", return_value=src), \
            mock.patch.object(macos_box, "source_version", return_value="0.66.0"), \
            mock.patch.object(macos_box, "BackgroundPull", Pull), \
            mock.patch.object(macos_box, "ensure_agent_env", return_value=[]), \
            mock.patch.object(macos_box, "agent_loads_libusb", return_value=None), \
            mock.patch.object(macos_box, "build_labjack_library", return_value=None), \
            mock.patch.object(macos_box, "install_ljm_library", return_value=None), \
            mock.patch.object(macos_box, "remembered_skips", return_value=["jlink", "picoscope"]), \
            mock.patch.object(macos_box, "start_agent",
                              side_effect=lambda *a, **k: order.append("agent")) as agent:
        with pytest.raises(LagerError):
            macos_box.install("v0.66.0", "0.66.0", layout=layout, echo=lambda *a: None)
    agent.assert_not_called()
    assert order == ["image"]


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
