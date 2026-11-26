# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""`lager install` / `update` / `uninstall` when the SSH target is a Mac.

Covers detection (only a clean ``Darwin`` answer to ``uname -s`` leaves the
Linux path; anything else, or no answer, stays on it), the runner that
executes on the Mac (standard library only, outcome as JSON, errors kept
structured), copying the installer and a checkout's source, the saved Box
recording ``platform`` and its script port, and the routing of the three
commands. No SSH, Docker, launchd or network is used.
"""
import importlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from click.testing import CliRunner

from cli import box_storage, deployment
from cli.commands.utility import _remote_macos
from cli.deployment import macos_box
from cli.errors import LagerError

RUNNER_FILE = Path(deployment.__file__).parent / "macos_remote.py"


def _done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


# The real check, kept for its own tests below. Everywhere else in this file
# it is answered "no": pytest's stdin is not a terminal, so left live it would
# open a real SSH connection to the test's made-up host.
_REAL_MAC_SUDO_CHECK = _remote_macos.mac_sudo_needs_a_terminal


@pytest.fixture(autouse=True)
def _no_live_sudo_probe(monkeypatch):
    monkeypatch.setattr(_remote_macos, "mac_sudo_needs_a_terminal",
                        lambda *a, **k: False)


@pytest.fixture
def lager_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LAGER_CONFIG_FILE_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _saved(home):
    return json.loads((home / ".lager").read_text())["BOXES"]


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("macos_remote_under_test", RUNNER_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- detection

@pytest.mark.parametrize("result, expected", [
    (_done(0, "Darwin\n"), True),
    (_done(0, "Welcome to this Mac\nDarwin\n"), True),
    (_done(0, "Linux\n"), False),
    (_done(0, ""), False),
    (_done(255, "Darwin\n"), False),
    (_done(0, "Darwin Kernel\n"), False),
])
def test_only_a_clean_darwin_answer_is_a_mac(result, expected):
    run = mock.Mock(return_value=result)
    assert _remote_macos.remote_is_macos("u@h", ["-i", "key"], run=run) is expected
    argv = run.call_args.args[0]
    assert argv[:3] == ["ssh", "-i", "key"] and "BatchMode=yes" in argv
    assert "ConnectTimeout=10" in argv and argv[-2:] == ["u@h", "uname -s"]
    assert run.call_args.kwargs["timeout"] == 20


def test_the_probe_goes_through_subprocess_run_so_a_test_double_replaces_it():
    """It once bound subprocess.run at import and ran a real ssh from other tests."""
    with mock.patch.object(subprocess, "run", return_value=_done(0, "Darwin\n")) as run:
        assert _remote_macos.remote_is_macos("u@h", []) is True
    run.assert_called_once()
    with mock.patch.object(subprocess, "run", return_value=mock.Mock()):
        assert _remote_macos.remote_is_macos("u@h", []) is False  # not a real answer


@pytest.mark.parametrize("error", [OSError("no ssh"), ValueError("odd"),
                                   subprocess.TimeoutExpired("ssh", 20)])
def test_a_target_that_does_not_answer_is_not_a_mac(error):
    assert _remote_macos.remote_is_macos("u@h", [], run=mock.Mock(side_effect=error)) is False


def test_a_saved_box_is_a_mac_only_when_recorded_as_one(lager_home):
    assert _remote_macos.saved_macos_box("10.0.0.5") is False
    box_storage.add_box("linux", "10.0.0.5", user="lagerdata", version="1.0.0")
    box_storage.add_box("mac", "10.0.0.6", user="me", python_port=8301, platform="macos")
    assert _remote_macos.saved_macos_box("10.0.0.5") is False
    assert _remote_macos.saved_macos_box("10.0.0.6") is True
    with mock.patch.object(_remote_macos, "load_boxes", side_effect=ValueError):
        assert _remote_macos.saved_macos_box("10.0.0.6") is False


def test_default_name_is_usable_as_a_box_name():
    assert _remote_macos.default_name("10.0.0.6") == "mac-10-0-0-6"
    assert _remote_macos.default_name("bench.local") == "mac-bench-local"


# ------------------------------------------------------------- saved record

def test_a_linux_box_is_stored_exactly_as_before(lager_home):
    box_storage.add_box("b", "10.0.0.5", user="lagerdata", version="1.0.0")
    assert _saved(lager_home) == {"b": {"ip": "10.0.0.5", "user": "lagerdata",
                                        "version": "1.0.0"}}
    box_storage.add_box("legacy", "10.0.0.7")
    assert _saved(lager_home)["legacy"] == "10.0.0.7"


def test_platform_is_recorded_and_survives_re_adding_the_box(lager_home):
    box_storage.add_box("mac", "10.0.0.6", user="me", python_port=8301, platform="macos")
    box_storage.add_box("mac", "10.0.0.6", user="me", version="2.0.0")
    assert _saved(lager_home)["mac"] == {"ip": "10.0.0.6", "user": "me", "version": "2.0.0",
                                         "python_port": 8301, "platform": "macos"}


# ------------------------------------------------------ the runner on the Mac

def test_the_runner_and_the_installer_import_nothing_from_the_cli(tmp_path):
    """They are copied to the Mac alone and run by whatever Python it has."""
    for name in _remote_macos.SHIPPED_FILES:
        (tmp_path / name).write_bytes((RUNNER_FILE.parent / name).read_bytes())
    result = subprocess.run(
        [sys.executable, "-I", str(tmp_path / "macos_remote.py"),
         "--result", str(tmp_path / "r.json"), "pieces"],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=60,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    outcome = json.loads((tmp_path / "r.json").read_text())
    assert outcome["ok"] is True and any("container" in p for p in outcome["pieces"])


def test_the_runner_reports_an_install(runner):
    installer = mock.Mock(PYTHON_SERVICE_PORT=8301)
    installer.install.return_value = SimpleNamespace(
        box_version="9.9.9", image="img", skipped=["Oscilloscope support: no Rust"],
        ready=["J-Link debugging"])
    args = runner.parse(["--result", "r", "install", "--version", "v9.9.9",
                         "--cli-version", "9.9.9", "--source", "/src", "--skip", "picoscope"])
    assert runner.run(args, installer) == {
        "ok": True, "box_version": "9.9.9", "image": "img", "python_port": 8301,
        "skipped": ["Oscilloscope support: no Rust"], "ready": ["J-Link debugging"]}
    installer.install.assert_called_once_with("v9.9.9", "9.9.9", bind="0.0.0.0",
                                              source=Path("/src"), skip=["picoscope"])


@pytest.mark.parametrize("extra, skip", [
    ([], ()),                                         # an install: skip nothing
    (["--reuse-skips"], None),                        # an update: what the Mac recorded
    (["--reuse-skips", "--skip", "jlink"], ["jlink"]),  # explicit flags still win
])
def test_the_runner_reuses_the_recorded_skips_only_on_an_update(runner, extra, skip):
    installer = mock.Mock(PYTHON_SERVICE_PORT=8301)
    installer.install.return_value = SimpleNamespace(
        box_version="9.9.9", image="img", skipped=[], ready=[])
    args = runner.parse(["--result", "r", "install", "--version", "v9.9.9",
                         "--cli-version", "9.9.9", *extra])
    runner.run(args, installer)
    assert installer.install.call_args.kwargs["skip"] == skip


def test_a_remote_update_asks_the_mac_to_reuse_its_skips(lager_home):
    sent = []
    with mock.patch.object(_remote_macos, "ship_installer"), \
            mock.patch.object(macos_box, "running_from_checkout", return_value=None), \
            mock.patch.object(_remote_macos, "run_remote",
                              side_effect=lambda h, i, a: sent.append(a) or
                              {"ok": True, "box_version": "2.0.0", "python_port": 8301}):
        for update, flags in ((True, {}), (False, {}), (True, {"skip_jlink": True})):
            _remote_macos.install_remote_macos(
                mock.Mock(params={}), ssh_host="me@h", ip="10.0.0.5", user="me", box="mac",
                identity_args=[], version="v2.0.0", yes=True, update=update, **flags)
    assert "--reuse-skips" in sent[0]
    assert "--reuse-skips" not in sent[1]
    assert "--reuse-skips" not in sent[2] and "--skip jlink" in sent[2]


def test_the_runner_keeps_an_error_structured(runner, tmp_path):
    installer = mock.Mock()
    installer.uninstall.side_effect = runner.LagerError(
        "Docker is installed but not running.", cause="why", fixes=["Start it."])
    args = runner.parse(["--result", "r", "uninstall", "--purge-config"])
    assert runner.run(args, installer) == {
        "ok": False, "problem": "Docker is installed but not running.",
        "cause": "why", "fixes": ["Start it."]}
    installer.uninstall.assert_called_once_with(purge_config=True, keep_images=False)
    installer.uninstall.side_effect = RuntimeError("boom")
    outcome = runner.run(args, installer)
    assert outcome["ok"] is False and "RuntimeError: boom" in outcome["cause"]


def test_the_runner_adds_homebrew_to_an_ssh_sessions_path(runner, tmp_path):
    extra = tmp_path / "bin"
    extra.mkdir()
    environ = {"PATH": "/usr/bin:/bin"}
    with mock.patch.object(runner, "EXTRA_PATH", (str(extra), str(tmp_path / "missing"))):
        runner.extend_path(environ)
        runner.extend_path(environ)
    assert environ["PATH"] == f"/usr/bin:/bin:{extra}"


# -------------------------------------------------------- installer options

def test_a_remote_box_is_published_on_every_interface(tmp_path):
    argv = macos_box.docker_run_argv("docker", "img", tmp_path, "/private/etc/lager",
                                     "0.0.0.0")
    published = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert published and all(p.startswith("0.0.0.0:") for p in published)
    assert "--privileged" not in argv and "--device" not in argv


def test_a_copied_source_tree_is_used_as_is_and_must_be_a_box_tree(tmp_path):
    layout = macos_box.Layout(home=tmp_path)
    with pytest.raises(LagerError, match="no Lager Box source"):
        macos_box.ensure_source(layout, "v1", source=tmp_path / "nothing")
    tree = tmp_path / "shipped"
    (tree / "box" / "lager" / "host").mkdir(parents=True)
    (tree / "box" / "lager" / "host" / "__main__.py").write_text("")
    run = mock.Mock()
    assert macos_box.ensure_source(layout, "v1", run, source=tree) == tree
    run.assert_not_called()


def test_a_pull_is_retried_without_the_credential_store():
    run = mock.Mock(return_value=_done(0, "unix:///var/run/docker.sock\n"))
    env = macos_box.anonymous_env("docker", {"PATH": "/bin"}, run)
    assert env["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    # Not an empty directory: from one, the Docker CLI picks the keychain
    # helper anyway and sends a stale GHCR login, which GHCR refuses even
    # for a public image. An empty login for the registry turns that off.
    config = json.loads((Path(env["DOCKER_CONFIG"]) / "config.json").read_text())
    assert config == {"auths": {"ghcr.io": {}}}
    assert "credsStore" not in config and "credHelpers" not in config
    run.reset_mock()
    kept = macos_box.anonymous_env("docker", {"DOCKER_HOST": "unix:///x"}, run)
    assert kept["DOCKER_HOST"] == "unix:///x"
    run.assert_not_called()


# ------------------------------------------------------------------ copying

class _Popen:
    made = []

    def __init__(self, argv, **kwargs):
        self.argv, self.kwargs = list(argv), kwargs
        self.stdout = mock.Mock()
        self.stdin = mock.Mock()
        self.returncode = 0
        _Popen.made.append(self)

    def communicate(self, timeout=None):
        return "", ""

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def popen():
    _Popen.made = []
    return _Popen


def test_the_installer_files_exist_and_are_piped_to_the_mac(popen):
    for name in _remote_macos.SHIPPED_FILES:
        assert (RUNNER_FILE.parent / name).is_file()
    _remote_macos.ship_installer("u@h", ["-i", "k"], popen=popen)
    tar, ssh = popen.made
    assert tar.argv == ["tar", "-cf", "-", "macos_box.py", "macos_remote.py"]
    assert tar.kwargs["cwd"] == str(RUNNER_FILE.parent)
    assert tar.kwargs["env"]["COPYFILE_DISABLE"] == "1"
    assert ssh.argv[:3] == ["ssh", "-i", "k"] and ssh.argv[-2] == "u@h"
    assert "tar -xf -" in ssh.argv[-1] and "com.lagerdata.installer" in ssh.argv[-1]


def test_a_failed_copy_is_an_error(popen):
    with mock.patch.object(popen, "wait", lambda self, timeout=None: 0), \
            mock.patch.object(popen, "communicate",
                              lambda self, timeout=None: ("", "tar: write error\n")):
        def failing(argv, **kwargs):
            proc = popen(argv, **kwargs)
            proc.returncode = 1
            return proc
        with pytest.raises(LagerError, match="did not copy") as caught:
            _remote_macos.ship_installer("u@h", [], popen=failing)
    assert caught.value.cause == "tar: write error"


def test_only_tracked_source_files_that_exist_are_copied(popen, tmp_path):
    (tmp_path / "box").mkdir()
    (tmp_path / "box" / "a.py").write_text("")
    run = mock.Mock(side_effect=[_done(0, b"box/a.py\0box/deleted.py\0")])
    name = _remote_macos.ship_source(tmp_path, "u@h", [], run=run, popen=popen)
    (listed,) = [call.args[0] for call in run.call_args_list]
    assert listed[:4] == ["git", "-C", str(tmp_path), "ls-files"]
    assert listed[-2:] == ["box", "cli/__init__.py"]
    # Nothing is deleted first: the running Box still mounts the last copy.
    assert name.startswith("shipped-")
    remote = popen.made[1].argv[-1]
    assert f"src/{name}" in remote and "rm -rf" not in remote
    tar = popen.made[0]
    assert tar.argv == ["tar", "-cf", "-", "--null", "-T", "-"]
    tar.stdin.write.assert_called_once_with(b"box/a.py\0")
    with pytest.raises(LagerError, match="did not list"):
        _remote_macos.ship_source(tmp_path, "u@h", [], run=mock.Mock(return_value=_done(1, b"")),
                                  popen=popen)


@pytest.mark.parametrize("tty, rc, expected", [
    (True, 1, False),    # a terminal: the Mac's sudo can ask
    (False, 0, False),   # /etc/lager is ready, or sudo needs no password
    (False, 255, False),  # ssh itself failed: the install reports it
    (False, 1, True),    # it would need a password with nowhere to type it
])
def test_the_mac_sudo_check(tty, rc, expected):
    run = mock.Mock(return_value=_done(rc))
    assert _REAL_MAC_SUDO_CHECK("me@h", ["-i", "k"], isatty=lambda: tty, run=run) is expected
    if tty:
        run.assert_not_called()
    else:
        command = run.call_args.args[0][-1]
        assert "test -w /etc/lager" in command and "sudo -n true" in command
        assert "BatchMode=yes" in run.call_args.args[0]


def test_a_remote_install_with_no_terminal_stops_before_copying_anything(lager_home,
                                                                        monkeypatch):
    monkeypatch.setattr(_remote_macos, "mac_sudo_needs_a_terminal", lambda *a, **k: True)
    with mock.patch.object(_remote_macos, "ship_installer") as shipped, \
            mock.patch.object(_remote_macos, "run_remote") as ran:
        with pytest.raises(LagerError) as caught:
            _remote_macos.install_remote_macos(
                mock.Mock(params={}), ssh_host="me@h", ip="10.0.0.5", user="me",
                box="mac", identity_args=[], version="v9.9.9", yes=True)
    assert "no terminal" in caught.value.problem
    assert any("sudo mkdir -p /etc/lager" in fix for fix in caught.value.fixes)
    shipped.assert_not_called()
    ran.assert_not_called()


def test_each_copy_goes_to_its_own_directory(popen, tmp_path):
    (tmp_path / "box").mkdir()
    (tmp_path / "box" / "a.py").write_text("")
    names = [_remote_macos.ship_source(
        tmp_path, "u@h", [], run=mock.Mock(return_value=_done(0, b"box/a.py\0")),
        popen=popen, name=f"shipped-{i}") for i in range(2)]
    assert names == ["shipped-0", "shipped-1"]


def test_older_copies_are_pruned_keeping_the_running_one():
    run = mock.Mock(return_value=_done(0))
    _remote_macos.prune_shipped("u@h", [], "shipped-2", run=run)
    command = run.call_args.args[0][-1]
    assert '!= shipped-2' in command and 'rm -rf "$d"' in command
    assert "shipped-*" in command and command.rstrip().endswith("exit 0")


def test_a_failed_prune_does_not_fail_the_install():
    _remote_macos.prune_shipped("u@h", [], "shipped-2",
                                run=mock.Mock(side_effect=OSError("ssh gone")))


def test_the_copy_is_pruned_only_after_the_install_succeeded(lager_home):
    args = dict(ssh_host="me@h", ip="10.0.0.6", user="me", box="mac",
                identity_args=[], version="v9.9.9", yes=True)
    with mock.patch.object(_remote_macos, "ship_installer"), \
            mock.patch.object(_remote_macos, "ship_source", return_value="shipped-9"), \
            mock.patch.object(_remote_macos, "prune_shipped") as prune, \
            mock.patch.object(_remote_macos, "run_remote",
                              side_effect=LagerError("The installer failed on the Mac.")):
        with pytest.raises(LagerError):
            _remote_macos.install_remote_macos(mock.Mock(params={}), **args)
    prune.assert_not_called()
    with mock.patch.object(_remote_macos, "ship_installer"), \
            mock.patch.object(_remote_macos, "ship_source", return_value="shipped-9"), \
            mock.patch.object(_remote_macos, "prune_shipped") as prune, \
            mock.patch.object(_remote_macos, "run_remote",
                              return_value={"ok": True, "box_version": "9.9.9"}) as ran:
        _remote_macos.install_remote_macos(mock.Mock(params={}), **args)
    prune.assert_called_once_with("me@h", [], "shipped-9")
    assert "src/shipped-9" in ran.call_args.args[2]


# ---------------------------------------------------------- running it there

def test_the_installer_runs_with_a_terminal_and_its_outcome_is_fetched():
    run = mock.Mock(side_effect=[_done(0), _done(0, 'noise\n{"ok": true, "box_version": "1"}\n')])
    assert _remote_macos.run_remote("u@h", ["-i", "k"], "install --version v1", run=run) \
        == {"ok": True, "box_version": "1"}
    started, fetched = [call.args[0] for call in run.call_args_list]
    assert "-t" in started and "BatchMode=yes" not in started
    assert "macos_remote.py" in started[-1] and started[-1].endswith("install --version v1")
    assert "BatchMode=yes" in fetched and fetched[-1].startswith("cat ")


def test_a_failure_on_the_mac_is_raised_as_it_was_there():
    outcome = {"ok": False, "problem": "Docker is installed but not running.",
               "cause": None, "fixes": ["Start OrbStack."]}
    run = mock.Mock(side_effect=[_done(1), _done(0, json.dumps(outcome))])
    with pytest.raises(LagerError, match="not running") as caught:
        _remote_macos.run_remote("u@h", [], "install", run=run)
    assert caught.value.fixes == ["Start OrbStack."]


def test_no_python_and_no_outcome_are_explained():
    run = mock.Mock(return_value=_done(_remote_macos.NO_PYTHON_STATUS))
    with pytest.raises(LagerError, match="no Python 3"):
        _remote_macos.run_remote("u@h", [], "install", run=run)
    run = mock.Mock(side_effect=[_done(255), _done(1, "")])
    with pytest.raises(LagerError, match="did not report an outcome") as caught:
        _remote_macos.run_remote("u@h", [], "install", run=run)
    assert "255" in caught.value.cause


# ------------------------------------------------------------------ routing

def _install_remote(lager_home, args, outcome):
    ctx = mock.Mock(params={})
    with mock.patch.object(_remote_macos, "ship_installer") as installer, \
            mock.patch.object(_remote_macos, "ship_source", return_value="shipped-1") as source, \
            mock.patch.object(_remote_macos, "prune_shipped"), \
            mock.patch.object(_remote_macos, "run_remote", return_value=outcome) as ran:
        _remote_macos.install_remote_macos(ctx, **args)
    return installer, source, ran


def test_a_remote_install_says_the_box_is_open_to_that_network(lager_home, capsys):
    args = dict(ssh_host="me@10.0.0.6", ip="10.0.0.6", user="me", box="mac",
                identity_args=[], version="v9.9.9", yes=True)
    _install_remote(lager_home, args, {"ok": True, "box_version": "9.9.9"})
    out = capsys.readouterr().out
    assert "reachable from any machine on 10.0.0.6's network" in out
    assert "no firewall" in out and "8301" in out and "8765" in out


def test_a_remote_mac_is_always_saved_with_its_port_and_platform(lager_home, capsys):
    args = dict(ssh_host="me@10.0.0.6", ip="10.0.0.6", user="me", box=None,
                identity_args=[], version="v9.9.9", yes=True)
    outcome = {"ok": True, "box_version": "9.9.9", "python_port": 8301,
               "skipped": ["Oscilloscope support: no Rust"], "ready": ["J-Link debugging"]}
    installer, source, ran = _install_remote(lager_home, args, outcome)
    installer.assert_called_once()
    source.assert_called_once()  # the tests run from a checkout
    arguments = ran.call_args.args[2]
    assert "--bind 0.0.0.0" in arguments and "--source" in arguments
    assert _saved(lager_home) == {"mac-10-0-0-6": {
        "ip": "10.0.0.6", "user": "me", "version": "9.9.9",
        "python_port": 8301, "platform": "macos"}}
    assert box_storage.get_python_service_port("10.0.0.6") == 8301
    out = capsys.readouterr().out
    assert "Not set up -- Oscilloscope support: no Rust" in out
    assert "Set up: J-Link debugging." in out
    assert "lager hello --box mac-10-0-0-6" in out
    # Installed again under the name it already has.
    _install_remote(lager_home, dict(args, box="mac-10-0-0-6"), dict(outcome, box_version="10.0.0"))
    assert list(_saved(lager_home)) == ["mac-10-0-0-6"]
    assert _saved(lager_home)["mac-10-0-0-6"]["version"] == "10.0.0"


def test_a_release_cli_lets_the_mac_download_the_source(lager_home):
    args = dict(ssh_host="me@10.0.0.6", ip="10.0.0.6", user="me", box="m",
                identity_args=[], version="v9.9.9", yes=True)
    with mock.patch.object(macos_box, "running_from_checkout", return_value=None):
        _installer, source, ran = _install_remote(lager_home, args, {"ok": True})
    source.assert_not_called()
    assert "--source" not in ran.call_args.args[2]
    assert _saved(lager_home)["m"]["python_port"] == macos_box.PYTHON_SERVICE_PORT


def test_a_named_version_is_downloaded_by_the_mac_not_copied(lager_home):
    args = dict(ssh_host="me@10.0.0.6", ip="10.0.0.6", user="me", box="m",
                identity_args=[], version="de/macos-hostlink-net-info", yes=True,
                version_defaulted=False)
    with mock.patch.object(macos_box, "running_from_checkout", return_value=Path("/co")):
        _installer, source, ran = _install_remote(lager_home, args, {"ok": True})
    source.assert_not_called()
    assert "--version de/macos-hostlink-net-info" in ran.call_args.args[2]
    assert "--source" not in ran.call_args.args[2]


class _Stop(Exception):
    """Raised by the first Linux-only step, to end the command there."""


def _run_install(is_mac):
    install_module = importlib.import_module("cli.commands.utility.install")
    with mock.patch.object(install_module, "probe_box_identity",
                           return_value=("key", _done(0))), \
            mock.patch.object(_remote_macos, "remote_is_macos", return_value=is_mac) as asked, \
            mock.patch.object(_remote_macos, "install_remote_macos") as handed, \
            mock.patch.object(install_module, "gateway_query_argv", side_effect=_Stop):
        result = CliRunner().invoke(install_module.install,
                                    ["--ip", "10.0.0.6", "--user", "me", "--yes"], obj={})
    return result, asked, handed


def test_install_hands_a_mac_to_the_macos_path(lager_home):
    result, asked, handed = _run_install(True)
    assert result.exit_code == 0, result.output
    asked.assert_called_once_with("me@10.0.0.6", ["-i", "key"])
    assert handed.call_args.kwargs["ssh_host"] == "me@10.0.0.6"
    assert handed.call_args.kwargs["identity_args"] == ["-i", "key"]


def test_install_keeps_any_other_target_on_the_linux_path(lager_home):
    result, asked, handed = _run_install(False)
    asked.assert_called_once()
    handed.assert_not_called()
    assert isinstance(result.exception, _Stop)  # reached the next Linux step


def test_uninstall_and_update_go_by_the_saved_record_alone(lager_home):
    uninstall_module = importlib.import_module("cli.commands.utility.uninstall")
    from cli.commands.utility.update import update
    box_storage.add_box("mac", "10.0.0.6", user="me", python_port=8301, platform="macos")
    box_storage.add_box("linux", "10.0.0.5", user="lagerdata", version="1.0.0")
    with mock.patch.object(_remote_macos, "_connect", return_value=("me@10.0.0.6", [])), \
            mock.patch.object(_remote_macos, "ship_installer"), \
            mock.patch.object(_remote_macos, "run_remote", return_value={"ok": True}) as ran, \
            mock.patch("subprocess.run") as ssh:
        dry = CliRunner().invoke(uninstall_module.uninstall,
                                 ["--box", "mac", "--dry-run"], obj={})
        assert dry.exit_code == 0 and "nothing was changed" in dry.output
        ran.assert_not_called()
        result = CliRunner().invoke(uninstall_module.uninstall,
                                    ["--box", "mac", "--yes", "--purge-config"], obj={})
        assert result.exit_code == 0, result.output
        assert ran.call_args.args[2] == "uninstall --purge-config"
        ssh.assert_not_called()
    assert list(_saved(lager_home)) == ["linux"]

    box_storage.add_box("mac", "10.0.0.6", user="me", python_port=8301, platform="macos")
    with mock.patch.object(_remote_macos, "update_remote_macos") as updated:
        result = CliRunner().invoke(update, ["--box", "mac", "--yes"], obj={})
    assert result.exit_code == 0, result.output
    assert updated.call_args.kwargs["ip"] == "10.0.0.6"
    assert updated.call_args.kwargs["user"] == "me"

    # A Linux Box, saved with no platform, is not offered to the macOS path.
    with mock.patch.object(_remote_macos, "uninstall_remote_macos") as removed, \
            mock.patch.object(uninstall_module, "probe_box_identity",
                              side_effect=_Stop) as linux_step:
        CliRunner().invoke(uninstall_module.uninstall, ["--box", "linux", "--yes"], obj={})
    removed.assert_not_called()
    linux_step.assert_called_once_with("lagerdata@10.0.0.5")  # reached the SSH path


def test_update_of_a_remote_mac_is_the_install_again(lager_home):
    import click
    ctx = mock.Mock()
    with mock.patch.object(_remote_macos, "_connect", return_value=("me@h", ["-i", "k"])), \
            mock.patch.object(_remote_macos, "install_remote_macos") as again:
        _remote_macos.update_remote_macos(ctx, ip="h", user="me", version="v2", yes=True)
    assert again.call_args.kwargs == dict(ssh_host="me@h", ip="h", user="me", box=None,
                                          identity_args=["-i", "k"], version="v2",
                                          yes=True, fork=None, update=True,
                                          version_defaulted=False)
    ctx.exit.side_effect = click.exceptions.Exit(2)
    with pytest.raises(click.exceptions.Exit):
        _remote_macos.update_remote_macos(ctx, ip="h", user="me", version=None,
                                          yes=True, check=True)
