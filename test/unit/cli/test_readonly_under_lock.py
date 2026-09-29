# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Read-only commands under another holder's box lock.

The box lock is enforced by the CLI, not the box. A read-only command -- one
that writes nothing to the box or its instruments and takes no device lock --
prints a note naming the holder and proceeds. Everything else keeps refusing
exactly as before. Both sides are pinned here, for a user lock and a CI lock,
by running the real commands through the root CLI with only HTTP faked.

The read-only set is READ_ONLY below. Adding a command to it is a statement
that its box path cannot disturb a holder's run; check the box handler first
(see `nets state` in REFUSED for what disqualifies a "read").
"""

from __future__ import annotations

import importlib

import pytest
from click.testing import CliRunner

from cli import box_storage
from cli.main import cli

BOX = "BENCH"
BOX_IP = "192.0.2.10"
OTHER_IP = "192.0.2.11"

USER_LOCK = {"locked": True, "user": "bob", "holder_type": "user", "ttl_seconds": None}
CI_LOCK = {"locked": True, "user": "ci:github:org/repo#1-1/job@runner:42",
           "holder_type": "ci", "ttl_seconds": 1800}

NETS = [
    {"name": "n1", "role": "gpio", "instrument": "LabJack_T7", "pin": "FIO0", "address": "x"},
    {"name": "dbg", "role": "debug", "instrument": "J-Link", "pin": "0", "address": "y",
     "jlink_script": "// script"},
]

# The commands that proceed under a foreign lock.
READ_ONLY = [
    ["nets", "--box", BOX],
    ["nets", "show", "n1", "--box", BOX],
    ["nets", "show-script", "dbg", "--box", BOX],
    ["hello", "--box", BOX],
    ["binaries", "list", "--box", BOX],
    ["supply", "--box", BOX],
    ["battery", "--box", BOX],
    ["eload", "--box", BOX],
    ["solar", "--box", BOX],
    ["watt", "--box", BOX],
    ["energy"],
    ["usb", "--box", BOX],
    ["arm", "--box", BOX],
    ["debug", "--box", BOX],
    ["debug", "dbg", "health", "--box", BOX],
    ["webcam", "--box", BOX],
    ["logic", "--box", BOX],
    ["scope", "--box", BOX],
    ["i2c", "--box", BOX],
    ["spi", "--box", BOX],
    ["uart", "--box", BOX],
]

# A representative set of commands that must still refuse. `nets state` is
# here on purpose: several of its box-side probes write to instruments (PPK2
# source mode, pin direction, an I2C scan, solar output), so it is not a read.
REFUSED = [
    ["supply", "s1", "voltage", "3.3", "--box", BOX],
    ["nets", "delete", "n1", "gpio", "--yes", "--box", BOX],
    ["nets", "state", "--box", BOX],
    ["debug", "dbg", "status", "--box", BOX],
    ["instruments", "--box", BOX],
    ["adc", "a1", "--box", BOX],
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("LAGER_AUTO_LOCK_DISABLE", "LAGER_LOCK_HOLDER", "LAGER_USER", "LAGER_BOX",
                "LAGER_LOCK_WAIT", "CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL",
                "BITBUCKET_BUILD_NUMBER", "BUILD_NUMBER", "DRONE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(box_storage, "get_lager_user", lambda: "alice")
    monkeypatch.setattr(box_storage, "get_lock_holder", lambda: "alice")
    box_storage._lock_check_unsupported_warned.clear()


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = "Hello from the box"
        self.headers = {}
        self.ok = status_code < 400

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


@pytest.fixture
def box(monkeypatch):
    """A saved box BENCH, a default box, and fake HTTP. Returns the POST log."""
    # By module path: the packages re-export click groups under these names.
    context_mod = importlib.import_module("cli.context")
    nets_mod = importlib.import_module("cli.commands.box.nets")
    debug_mod = importlib.import_module("cli.commands.development.debug.commands")
    skew_mod = importlib.import_module("cli.core.version_skew")

    state = {"lock": None, "posts": []}

    monkeypatch.setattr(box_storage, "get_box_ip",
                        lambda name: BOX_IP if name == BOX else None)
    monkeypatch.setattr(box_storage, "_gateway_kwargs", lambda ip: {})
    monkeypatch.setattr(box_storage, "_check_gateway", lambda resp, ip: resp)
    monkeypatch.setattr(context_mod, "get_default_box", lambda ctx: BOX_IP)
    monkeypatch.setattr(nets_mod, "get_default_box", lambda ctx: BOX_IP)
    monkeypatch.setattr(skew_mod, "check_and_warn", lambda *a, **k: None)

    class _Client:
        def get_service_health(self, detailed=False):
            return {"status": "healthy", "version": "1.0.0", "features": [], "uptime": 1.0}

        def close(self):
            pass

    monkeypatch.setattr(debug_mod, "_get_service_client", lambda box_ip: _Client())

    def fake_get(url, *args, **kwargs):
        if url.endswith("/lock"):
            return _Resp(200, state["lock"] or {"locked": False})
        if url.endswith("/uart/nets/list"):
            return _Resp(200, {"nets": NETS})
        if url.endswith("/nets/list"):
            return _Resp(200, NETS)
        if url.endswith("/status"):
            return _Resp(200, {"version": "0.51.0"})
        if url.endswith("/binaries/list"):
            return _Resp(200, {"binaries": []})
        return _Resp(200, {})

    def fake_post(url, *args, **kwargs):
        state["posts"].append(url)
        # A lock POST from anyone but the holder is refused, as the box does.
        return _Resp(409, {"error": "Box is locked", "lock": state["lock"]})

    def fake_request(method, url, *args, **kwargs):
        if method.upper() == "GET":
            return fake_get(url)
        return fake_post(url)

    for verb in ("put", "delete"):
        monkeypatch.setattr(f"requests.{verb}", _forbidden, raising=False)
    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("requests.request", fake_request)
    return state


def _forbidden(*args, **kwargs):
    raise AssertionError("unexpected HTTP verb in a lock test")


def _run(args):
    return CliRunner().invoke(cli, args, catch_exceptions=False)


@pytest.mark.parametrize("lock", [USER_LOCK, CI_LOCK], ids=["user-lock", "ci-lock"])
@pytest.mark.parametrize("args", READ_ONLY, ids=lambda a: " ".join(a))
def test_a_read_only_command_proceeds_with_a_note(box, lock, args):
    box["lock"] = lock
    result = _run(args)
    assert "is locked by" in result.output, result.output
    assert "running read-only" in result.output, result.output
    assert "To force unlock" not in result.output, result.output
    assert result.exit_code == 0, result.output
    # It never tried to take the lock.
    assert not [u for u in box["posts"] if u.endswith("/lock")], box["posts"]


@pytest.mark.parametrize("lock", [USER_LOCK, CI_LOCK], ids=["user-lock", "ci-lock"])
@pytest.mark.parametrize("args", REFUSED, ids=lambda a: " ".join(a))
def test_a_state_changing_command_is_still_refused(box, lock, args):
    box["lock"] = lock
    result = _run(args)
    assert result.exit_code == 1, result.output
    assert "is locked by" in result.output
    assert "To force unlock: lager boxes unlock" in result.output
    assert "running read-only" not in result.output


@pytest.mark.parametrize("args", READ_ONLY, ids=lambda a: " ".join(a))
def test_an_unlocked_box_prints_no_note(box, args):
    result = _run(args)
    assert "is locked by" not in result.output, result.output


# `lager nets` used to check the lock only for a saved box name: a raw IP or
# the default box skipped it, so `nets delete` ran under another holder's lock.
NETS_TARGETS = {
    "saved-name": ["--box", BOX],
    "raw-ip": ["--box", OTHER_IP],
    "default-box": [],
}


@pytest.mark.parametrize("target", NETS_TARGETS.values(), ids=NETS_TARGETS.keys())
def test_nets_delete_is_refused_however_the_box_is_named(box, target):
    box["lock"] = USER_LOCK
    result = _run(["nets", "delete", "n1", "gpio", "--yes", *target])
    assert result.exit_code == 1, result.output
    assert "is locked by bob" in result.output


@pytest.mark.parametrize("target", NETS_TARGETS.values(), ids=NETS_TARGETS.keys())
def test_nets_list_proceeds_however_the_box_is_named(box, target):
    box["lock"] = CI_LOCK
    result = _run(["nets", *target])
    assert result.exit_code == 0, result.output
    assert "running read-only" in result.output


class TestCheckBoxLock:
    """The helper itself."""

    def test_read_only_notes_and_returns(self, box, capsys):
        box["lock"] = USER_LOCK
        box_storage._check_box_lock(BOX_IP, BOX, read_only=True)
        err = capsys.readouterr().err
        assert "Note: Box 'BENCH' is locked by bob; running read-only." in err

    def test_the_default_still_refuses(self, box):
        box["lock"] = USER_LOCK
        with pytest.raises(SystemExit) as exc:
            box_storage._check_box_lock(BOX_IP, BOX)
        assert exc.value.code == 1

    def test_our_own_lock_prints_nothing(self, box, capsys):
        box["lock"] = {"locked": True, "user": "alice", "holder_type": "user"}
        box_storage._check_box_lock(BOX_IP, BOX, read_only=True)
        assert capsys.readouterr().err == ""
