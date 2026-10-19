# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Modules no test imported: `lager instruments`, the hidden `lager authorize`,
`lager terminal`, the PyPI check behind `box-config pip add`, and the two
debug tunnel helpers.

`debug/service_helper.py` and `debug/tunnel.py` are re-exported from
`cli.commands.development.debug` but nothing under cli/ calls them today; the
tests here pin their contracts so a caller that comes back finds them working.
"""

import errno
import importlib
import io
import subprocess
import sys
import types
import urllib.error

import pytest
import trio

pipv = importlib.import_module("cli.commands.box._pip_validation")
service_helper = importlib.import_module("cli.commands.development.debug.service_helper")
tunnel = importlib.import_module("cli.commands.development.debug.tunnel")
ssh_setup_mod = importlib.import_module("cli.commands.box.ssh_setup")
instruments_mod = importlib.import_module("cli.commands.box.instruments")


# ---------------------------------------------------------------------------
# lager instruments
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _instruments_resolves_to_the_fake_box(monkeypatch, request):
    # instruments.py binds resolve_and_validate_box at import, so the
    # fake_box fixture's patch on cli.box_storage does not reach it.
    if "fake_box" in request.fixturenames:
        ip = request.getfixturevalue("fake_box").IP
        monkeypatch.setattr(instruments_mod, "resolve_and_validate_box",
                            lambda ctx, name=None, *a, **k: ip)


def _instruments(fake_box, payload, status=200):
    real_get = fake_box.get

    def get(url, **kwargs):
        if url.endswith("/instruments/list"):
            fake_box.gets.append(url)
            return fake_box._resp(payload, status)
        return real_get(url, **kwargs)
    return get


def test_instruments_renders_each_device(lager, fake_box, monkeypatch):
    monkeypatch.setattr("requests.get", _instruments(fake_box, [
        {"name": "Rigol_DP821", "address": "USB0::0x1AB1::0x0E11::SER1::INSTR",
         "channels": {"power-supply": ["1", "2"]}},
        {"name": "LabJack_T7", "address": "USB0::0x0CD5::0x0007::SER2::INSTR",
         "channels": {"gpio": ["FIO2"], "adc": []}},
    ]))
    result = lager("instruments")
    assert result.exit_code == 0, result.output
    assert "Rigol_DP821" in result.output and "SER1" in result.output
    assert "power-supply: 1, 2" in result.output
    assert "adc: " in result.output
    assert fake_box.gets[-1] == f"http://{fake_box.IP}:9000/instruments/list"


def test_instruments_empty_list(lager, fake_box, monkeypatch):
    monkeypatch.setattr("requests.get", _instruments(fake_box, []))
    result = lager("instruments")
    assert result.exit_code == 0
    assert "No instruments detected." in result.output


@pytest.mark.parametrize("payload, status", [({"error": "boom"}, 200), ([], 500)])
def test_instruments_unparseable_answer_is_an_error(lager, fake_box, monkeypatch, payload, status):
    monkeypatch.setattr("requests.get", _instruments(fake_box, payload, status))
    # A 500 never parses as the list, even when the body happens to be one.
    result = lager("instruments")
    assert result.exit_code == 1
    assert "did not parse" in result.output


def test_instruments_unreachable_box(lager, fake_box, monkeypatch):
    import requests

    def get(url, **kwargs):
        raise requests.ConnectionError("no route to host")
    monkeypatch.setattr("requests.get", get)
    result = lager("instruments")
    assert result.exit_code == 1
    assert "Error querying instruments" in result.output
    assert "lager hello" in result.output


def test_instruments_hides_devices_that_share_one_address(lager, fake_box, monkeypatch):
    dup = "USB0::0x0CD5::0x0007::::INSTR"
    monkeypatch.setattr("requests.get", _instruments(fake_box, [
        {"name": "LabJack_T7", "address": dup, "channels": {}},
        {"name": "LabJack_T7", "address": dup, "channels": {}},
        {"name": "Rigol_DP821", "address": "USB0::1::2::SER1::INSTR", "channels": {}},
    ]))
    result = lager("instruments")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    notes = [line for line in lines if "more than one device reports this address" in line]
    rows = [line for line in lines if line not in notes]
    # The ambiguous pair is explained once, and neither is listed as a row a
    # net could be built from.
    assert len(notes) == 1 and notes[0].startswith(f"LabJack_T7 at {dup}")
    assert any(row.startswith("Rigol_DP821") for row in rows)
    assert not any(row.startswith("LabJack_T7") for row in rows)


# ---------------------------------------------------------------------------
# lager authorize (hidden alias)
# ---------------------------------------------------------------------------

def test_authorize_warns_and_delegates_with_the_same_box(lager, fake_box, monkeypatch):
    seen = {}

    def fake_ssh_setup(*args, **kwargs):
        seen.update(kwargs)
    monkeypatch.setattr(ssh_setup_mod.ssh_setup, "callback", fake_ssh_setup)
    result = lager("authorize", "--box", "bench-a")
    assert result.exit_code == 0, result.output
    assert "DEPRECATED: `lager authorize` is now `lager ssh-setup`" in result.output
    assert seen == {"box": "bench-a"}


def test_authorize_is_hidden_from_the_root_help(lager):
    assert "authorize" not in lager("--help").output


# ---------------------------------------------------------------------------
# lager terminal
# ---------------------------------------------------------------------------

def test_terminal_runs_the_repl(lager, monkeypatch):
    runs = []

    class FakeREPL:
        def run(self):
            runs.append(True)
    fake = types.ModuleType("cli.terminal.ui.repl")
    fake.LagerREPL = FakeREPL
    monkeypatch.setitem(sys.modules, "cli.terminal.ui.repl", fake)
    result = lager("terminal")
    assert result.exit_code == 0, result.output
    assert runs == [True]


def test_terminal_without_its_dependencies_exits_non_zero(lager, monkeypatch):
    monkeypatch.setitem(sys.modules, "cli.terminal.ui.repl", None)
    result = lager("terminal")
    assert result.exit_code == 1
    assert "Lager Terminal dependencies not installed" in result.output


def test_bare_lager_without_terminal_dependencies_shows_help(lager, monkeypatch):
    monkeypatch.setitem(sys.modules, "cli.terminal.ui.repl", None)
    result = lager()
    assert result.exit_code == 0
    assert "Showing help instead" in result.output
    assert "Usage:" in result.output


# ---------------------------------------------------------------------------
# box-config pip add: the PyPI check
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec, name", [
    ("requests", "requests"),
    ("requests==2.31.0", "requests"),
    ("requests>=2,<3", "requests"),
    ("requests[socks]~=2.31", "requests"),
    ("pkg @ https://example.com/pkg.whl", "pkg"),
    ("  spaced  ", "spaced"),
])
def test_normalize_for_pypi(spec, name):
    assert pipv.normalize_for_pypi(spec) == name


def test_direct_references_are_not_looked_up(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a direct reference must not hit PyPI")
    monkeypatch.setattr(pipv.urllib.request, "urlopen", boom)
    assert pipv.is_direct_ref("pkg @ https://example.com/pkg.whl")
    assert pipv.validate_on_pypi(["pkg @ https://example.com/pkg.whl"]) == ([], [])


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_by_name(table):
    def urlopen(req, timeout):
        name = req.full_url.split("/pypi/")[1].split("/")[0]
        outcome = table[name]
        if isinstance(outcome, BaseException):
            raise outcome
        return _Resp(outcome)
    return urlopen


def _http_error(code):
    return urllib.error.HTTPError("https://pypi.org", code, "x", {}, io.BytesIO())


def test_pypi_outcomes_are_sorted_into_invalid_and_network(monkeypatch):
    monkeypatch.setattr(pipv.urllib.request, "urlopen", _urlopen_by_name({
        "good": 200,
        "missing": _http_error(404),
        "forbidden": _http_error(403),
        "teapot": _http_error(418),
        "offline": urllib.error.URLError("dns failed"),
        "slow": TimeoutError(),
    }))
    invalid, network = pipv.validate_on_pypi(
        ["good", "missing", "forbidden", "teapot", "offline", "slow"])
    assert invalid == [("missing", "not found on PyPI"), ("forbidden", "access forbidden"),
                       ("teapot", "HTTP error 418")]
    assert network == [("offline", "network error: dns failed"), ("slow", "connection timed out")]


def test_a_bare_specifier_has_no_name_to_check(monkeypatch):
    monkeypatch.setattr(pipv.urllib.request, "urlopen", _urlopen_by_name({}))
    invalid, network = pipv.validate_on_pypi(["==1.0"])
    assert invalid == [("==1.0", "could not extract package name")]
    assert network == []


def test_the_lookup_url_and_user_agent(monkeypatch):
    seen = []

    def urlopen(req, timeout):
        seen.append((req.full_url, req.get_header("User-agent"), timeout))
        return _Resp(200)
    monkeypatch.setattr(pipv.urllib.request, "urlopen", urlopen)
    pipv.validate_on_pypi(["requests[socks]==2.31.0"])
    assert seen == [("https://pypi.org/pypi/requests/json", "lager-cli", pipv._PYPI_TIMEOUT)]


# ---------------------------------------------------------------------------
# debug/service_helper.py
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _empty_tunnel_cache():
    service_helper._tunnel_cache.clear()
    yield
    service_helper._tunnel_cache.clear()


def test_ensure_service_tunnel_reuses_a_live_cached_tunnel(monkeypatch):
    created = []
    monkeypatch.setattr(service_helper.ServiceTunnel, "is_tunnel_active", lambda self: False)

    def establish(self):
        created.append(self)
        return True
    monkeypatch.setattr(service_helper.ServiceTunnel, "establish_tunnel", establish)
    first = service_helper.ensure_service_tunnel("192.0.2.10", "benchuser")
    assert first is not None and len(created) == 1
    monkeypatch.setattr(service_helper.ServiceTunnel, "is_tunnel_active", lambda self: True)
    assert service_helper.ensure_service_tunnel("192.0.2.10", "benchuser") is first
    assert len(created) == 1


def test_ensure_service_tunnel_replaces_a_dead_cached_tunnel(monkeypatch):
    monkeypatch.setattr(service_helper.ServiceTunnel, "is_tunnel_active", lambda self: False)
    monkeypatch.setattr(service_helper.ServiceTunnel, "establish_tunnel", lambda self: True)
    first = service_helper.ensure_service_tunnel("192.0.2.10")
    second = service_helper.ensure_service_tunnel("192.0.2.10")
    assert second is not first
    assert service_helper._tunnel_cache["192.0.2.10:lagerdata"] is second


def test_ensure_service_tunnel_is_keyed_by_user(monkeypatch):
    monkeypatch.setattr(service_helper.ServiceTunnel, "is_tunnel_active", lambda self: False)
    monkeypatch.setattr(service_helper.ServiceTunnel, "establish_tunnel", lambda self: True)
    a = service_helper.ensure_service_tunnel("192.0.2.10", "alice")
    b = service_helper.ensure_service_tunnel("192.0.2.10", "bob")
    assert a is not b and a.username == "alice" and b.username == "bob"


def test_ensure_service_tunnel_failure_caches_nothing(monkeypatch):
    monkeypatch.setattr(service_helper.ServiceTunnel, "is_tunnel_active", lambda self: False)
    monkeypatch.setattr(service_helper.ServiceTunnel, "establish_tunnel", lambda self: False)
    assert service_helper.ensure_service_tunnel("192.0.2.10") is None
    assert service_helper._tunnel_cache == {}


def test_is_service_available_needs_a_tunnel_and_a_healthy_answer(monkeypatch):
    import requests
    monkeypatch.setattr(service_helper.ServiceTunnel, "is_tunnel_active", lambda self: True)
    monkeypatch.setattr(service_helper.requests, "get",
                        lambda url, timeout: types.SimpleNamespace(status_code=200))
    assert service_helper.is_service_available("192.0.2.10") is True
    monkeypatch.setattr(service_helper.requests, "get",
                        lambda url, timeout: types.SimpleNamespace(status_code=503))
    assert service_helper.is_service_available("192.0.2.10") is False

    def refused(url, timeout):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(service_helper.requests, "get", refused)
    assert service_helper.is_service_available("192.0.2.10") is False


def test_is_service_available_without_a_tunnel_does_not_probe(monkeypatch):
    monkeypatch.setattr(service_helper.ServiceTunnel, "is_tunnel_active", lambda self: False)
    monkeypatch.setattr(service_helper.ServiceTunnel, "establish_tunnel", lambda self: False)

    def no_probe(*a, **k):
        raise AssertionError("probed the health endpoint with no tunnel up")
    monkeypatch.setattr(service_helper.requests, "get", no_probe)
    assert service_helper.is_service_available("192.0.2.10") is False


class _Proc:
    def __init__(self, hang=False):
        self.hang = hang
        self.calls = []

    def terminate(self):
        self.calls.append("terminate")

    def wait(self, timeout):
        self.calls.append("wait")
        if self.hang:
            raise subprocess.TimeoutExpired("ssh", timeout)

    def kill(self):
        self.calls.append("kill")


@pytest.mark.parametrize("hang, expected", [(False, ["terminate", "wait"]),
                                            (True, ["terminate", "wait", "kill"])])
def test_close_tunnel_kills_an_ssh_that_ignores_terminate(hang, expected):
    t = service_helper.ServiceTunnel("192.0.2.10")
    t.tunnel_process = proc = _Proc(hang=hang)
    t.close_tunnel()
    assert proc.calls == expected
    assert t.tunnel_process is None


def test_establish_tunnel_builds_a_local_forward(monkeypatch):
    launched = []

    class Pool:
        def ensure_connection(self, ip, user):
            launched.append(("ensure", ip, user))

        def get_ssh_options(self, ip):
            return ["-o", "ControlPath=/tmp/x"]
    monkeypatch.setattr("cli.core.ssh_utils.get_ssh_connection_pool", lambda: Pool())
    monkeypatch.setattr(service_helper.subprocess, "Popen",
                        lambda cmd, **kw: launched.append(cmd) or _Proc())
    monkeypatch.setattr(service_helper.time, "sleep", lambda s: None)
    monkeypatch.setattr(service_helper.ServiceTunnel, "is_tunnel_active", lambda self: True)
    t = service_helper.ServiceTunnel("192.0.2.10", username="benchuser")
    assert t.establish_tunnel() is True
    assert launched[0] == ("ensure", "192.0.2.10", "benchuser")
    assert launched[1] == ["ssh", "-N", "-L", "8765:127.0.0.1:8765",
                           "-o", "ControlPath=/tmp/x", "benchuser@192.0.2.10"]


def test_cleanup_tunnels_survives_a_failing_close(monkeypatch):
    t = service_helper.ServiceTunnel("192.0.2.10")

    def boom():
        raise RuntimeError("already gone")
    t.close_tunnel = boom
    service_helper._tunnel_cache["192.0.2.10:lagerdata"] = t
    service_helper.cleanup_tunnels()
    assert service_helper._tunnel_cache == {}


# ---------------------------------------------------------------------------
# debug/tunnel.py
# ---------------------------------------------------------------------------

def _serve_with_port_in_use(port):
    async def serve_tcp(*args, task_status=trio.TASK_STATUS_IGNORED, **kwargs):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    async def main():
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tunnel.trio, "serve_tcp", serve_tcp)
            await tunnel.serve_tunnel("127.0.0.1", port, ("ws://x", {}), "pdb", debug=False)
    trio.run(main)


def test_serve_tunnel_on_the_shared_pdb_port_already_in_use_returns():
    # 5555 is the pdb port every `lager python` run asks for; another run
    # already serving it is normal, not an error.
    _serve_with_port_in_use(5555)


def test_serve_tunnel_on_any_other_busy_port_raises():
    with pytest.raises(BaseException) as info:
        _serve_with_port_in_use(5556)
    leaf = info.value
    # trio may wrap it in an exception group (the builtin on 3.11+, the
    # `exceptiongroup` backport on 3.10); both expose `.exceptions`.
    while getattr(leaf, "exceptions", None):
        leaf = leaf.exceptions[0]
    assert isinstance(leaf, OSError) and leaf.errno == errno.EADDRINUSE


def test_send_to_websocket_forwards_every_client_message():
    sent = []

    class Stream:
        def __init__(self, msgs):
            self.msgs = list(msgs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.msgs:
                raise StopAsyncIteration
            return self.msgs.pop(0)

    class WS:
        async def send_message(self, msg):
            sent.append(msg)

    async def main():
        async with trio.open_nursery() as nursery:
            await tunnel.send_to_websocket(WS(), Stream([b"a", b"b"]), nursery)
    trio.run(main)
    assert sent == [b"a", b"b"]


# ---------------------------------------------------------------------------
# lager box (hidden, deprecated alias group)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("old, new, target", [
    ("config", "lager box-config", "cli.commands.box.config:box_config"),
    ("dut", "lager dut", "cli.commands.box.dut:box_dut"),
])
def test_box_alias_warns_and_reaches_the_same_command(lager, old, new, target):
    from cli.main import cli
    import click
    module, attr = target.split(":")
    ctx = click.Context(cli)
    assert cli.get_command(ctx, "box").commands[old] is getattr(importlib.import_module(module), attr)
    result = lager("box", old, "--help")
    assert result.exit_code == 0, result.output
    assert f"DEPRECATED: `lager box {old}` is now `{new}`" in result.output


def test_box_alias_is_hidden_from_the_root_help(lager):
    assert "\n  box " not in lager("--help").output
