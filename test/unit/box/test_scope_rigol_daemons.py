# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Starting and finding the daemon that drives a Rigol scope.

A stand-in daemon is used: a script named like the real one that listens on
the port it is given and records the environment it was started with.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import sys
import time

import pytest

from lager.measurement.scope import rigol_daemons, routing

ADDRESS = "USB0::0x1AB1::0x0515::MS5A244000001::INSTR"

STAND_IN = """#!%s
import json, os, socket, time
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", int(os.environ["LAGER_SCOPE_DATA_PORT"])))
listener.listen()
with open(os.environ["STAND_IN_RECORD"], "a") as f:
    f.write(json.dumps({k: os.environ.get(k) for k in (
        "LAGER_SCOPE_SCPI", "LAGER_SCOPE_VIDEO", "LAGER_SCOPE_SOCKET")}) + "\\n")
time.sleep(60)
"""


# A daemon on its way out after SIGTERM: its command line no longer names
# the daemon, but its socket is still open for a moment, as a real one's is
# between the kernel freeing its memory and closing its files.
LINGERING = """#!%s
import os, signal, socket, sys, time
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", int(os.environ["LAGER_SCOPE_DATA_PORT"])))
listener.listen()
def on_term(*_):
    listener.set_inheritable(True)
    os.execv(sys.executable, [sys.executable, "-c", "import time; time.sleep(1.5)"])
signal.signal(signal.SIGTERM, on_term)
with open(os.environ["STAND_IN_RECORD"], "a") as f:
    f.write("{}\\n")
time.sleep(60)
"""


def _install(tmp_path, monkeypatch, script):
    binary = tmp_path / "bin" / "oscilloscope-daemon"
    binary.parent.mkdir()
    binary.write_text(script % sys.executable)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    record = tmp_path / "started.jsonl"
    monkeypatch.setenv(rigol_daemons.DAEMON_BINARY_ENV, str(binary))
    monkeypatch.setenv("STAND_IN_RECORD", str(record))
    monkeypatch.setattr(rigol_daemons, "state_dir", lambda: str(tmp_path))
    return record


def _kill_left_over(tmp_path):
    for state in tmp_path.glob("*.json"):
        pid = json.loads(state.read_text()).get("pid")
        if pid:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    yield _install(tmp_path, monkeypatch, STAND_IN)
    _kill_left_over(tmp_path)


@pytest.fixture
def lingering_daemon(tmp_path, monkeypatch):
    yield _install(tmp_path, monkeypatch, LINGERING)
    _kill_left_over(tmp_path)


def _started(record):
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text().splitlines()]


def _wait_for(record, count):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(_started(record)) < count:
        time.sleep(0.05)
    return _started(record)


class TestEnsure:

    def test_a_daemon_is_started_for_the_scope_and_found_again(self, daemon):
        url = rigol_daemons.ensure(ADDRESS, "usb:1-1.3")
        port = int(url.rsplit(":", 1)[1].strip("/"))
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        (started,) = _wait_for(daemon, 1)
        assert started["LAGER_SCOPE_SCPI"] == ADDRESS
        assert started["LAGER_SCOPE_VIDEO"] == "usb:1-1.3"
        assert started["LAGER_SCOPE_SOCKET"].endswith(".sock")

        assert rigol_daemons.ensure(ADDRESS, "usb:1-1.3") == url
        time.sleep(0.2)
        assert len(_started(daemon)) == 1, "a running daemon was started again"

    def test_a_new_capture_card_restarts_it(self, daemon):
        first = rigol_daemons.ensure(ADDRESS, None)
        second = rigol_daemons.ensure(ADDRESS, "usb:1-1.4")
        assert first != second
        started = _wait_for(daemon, 2)
        assert [s["LAGER_SCOPE_VIDEO"] for s in started] == [None, "usb:1-1.4"]

    def test_stopping_it_leaves_nothing_running(self, daemon):
        url = rigol_daemons.ensure(ADDRESS)
        assert rigol_daemons.stop(ADDRESS)
        port = int(url.rsplit(":", 1)[1].strip("/"))
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        assert not rigol_daemons.stop(ADDRESS)

    @pytest.mark.parametrize("stat, exited", [
        (b"4242 (oscilloscope-da) S 1 4242 4242 0 -1", False),
        (b"4242 (oscilloscope-da) R 1 4242 4242 0 -1", False),
        (b"4242 (oscilloscope-da) Z 1 4242 4242 0 -1", True),
        (b"4242 (a) Z (b) S 1 4242 4242 0 -1", False),
        (b"4242 (a) S (b) Z 1 4242 4242 0 -1", True),
    ])
    def test_a_zombie_has_exited_and_a_sleeper_has_not(self, stat, exited):
        # Read past the last parenthesis: a command name can hold ") Z".
        assert rigol_daemons._stat_says_exited(stat) is exited

    def test_stopping_waits_for_its_socket_to_close(self, lingering_daemon):
        # Stop used to return once the command line stopped naming the
        # daemon, which happens before an exiting process closes its sockets
        # and devices: the port still answered, and a daemon started at once
        # could find the scope still held.
        url = rigol_daemons.ensure(ADDRESS)
        _wait_for(lingering_daemon, 1)
        assert rigol_daemons.stop(ADDRESS)
        port = int(url.rsplit(":", 1)[1].strip("/"))
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()

    def test_a_missing_daemon_binary_says_how_to_get_one(self, daemon, monkeypatch):
        monkeypatch.setenv(rigol_daemons.DAEMON_BINARY_ENV, "/nonexistent/oscilloscope-daemon")
        with pytest.raises(rigol_daemons.RigolDaemonError, match="lager update"):
            rigol_daemons.ensure(ADDRESS)

    def test_a_net_with_no_address_is_refused(self, daemon):
        with pytest.raises(rigol_daemons.RigolDaemonError, match="address"):
            rigol_daemons.ensure("")


class TestCaptureCards:

    def test_a_card_is_named_by_its_usb_port(self):
        rec = {"capture": {"vid": "534d", "pid": "2109", "port_path": "1-1.3"}}
        assert rigol_daemons.capture_spec(rec) == "usb:1-1.3"
        assert rigol_daemons.capture_spec({"capture": {"device": "/dev/video4"}}) == "/dev/video4"
        assert rigol_daemons.capture_spec({"capture": {"device": "screen"}}) == "screen"
        assert rigol_daemons.capture_spec({}) is None

    def test_the_channel_nets_share_the_scope_net_s_card(self):
        nets = [
            {"name": "s", "role": "scope", "address": ADDRESS,
             "capture": {"port_path": "1-1.3"}},
            {"name": "s ch1", "role": "scope-channel", "address": ADDRESS, "pin": 1},
            {"name": "other", "role": "scope", "address": "USB0::0x1AB1::0x0515::X::INSTR"},
        ]
        assert rigol_daemons.capture_for_address(ADDRESS, nets) == "usb:1-1.3"
        assert rigol_daemons.capture_for_address(
            "USB0::0x1AB1::0x0515::X::INSTR", nets) is None


class TestRouting:

    @pytest.mark.parametrize("instrument,family", [
        ("picoscope_2000", routing.PICOSCOPE),
        ("PicoScope_5444D", routing.PICOSCOPE),
        ("Rigol_MSO5204", routing.RIGOL_MSO5000),
        ("rigol_mso5000", routing.RIGOL_MSO5000),
        ("Keysight_DSOX1204G", None),
        ("", None),
    ])
    def test_the_family_comes_from_the_instrument(self, instrument, family):
        assert routing.scope_family(instrument) == family

    def test_a_picoscope_is_the_box_daemon_s(self):
        from lager.measurement.scope import daemon_client
        assert routing.daemon_url_for({"instrument": "picoscope_2000"}) == \
            daemon_client.daemon_url()

    def test_a_rigol_gets_a_daemon_for_its_address(self, monkeypatch):
        asked = []
        monkeypatch.setattr(rigol_daemons, "ensure",
                            lambda address, video=None: asked.append((address, video)) or "ws://x/")
        rec = {"instrument": "Rigol_MSO5204", "address": ADDRESS,
               "capture": {"port_path": "2-1"}}
        assert routing.daemon_url_for(rec) == "ws://x/"
        assert asked == [(ADDRESS, "usb:2-1")]

    def test_only_a_rigol_with_a_card_has_a_live_view(self, monkeypatch):
        monkeypatch.setattr(rigol_daemons, "capture_for_address", lambda address: None)
        assert routing.has_live_view({"instrument": "picoscope_2000"})
        assert not routing.has_live_view({"instrument": "Rigol_MSO5204", "address": ADDRESS})
        assert routing.has_live_view({"instrument": "Rigol_MSO5204", "address": ADDRESS,
                                      "capture": {"port_path": "1-1"}})
