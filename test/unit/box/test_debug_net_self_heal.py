# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``DebugNet`` self-heal retry (Issue 2) and ``session`` (Issue 3).

We load the real ``DebugNet`` class — not the ``_NullDebug`` fallback — by
providing stub ``..debug`` / ``..debug.probes`` modules under a private package
name so the module's ``try: from ..debug import (...)`` block succeeds. Every
sibling the class touches at runtime (``reset_device``, ``connect_jlink``,
``disconnect``, the status helpers) is a plain function on the loaded module, so
tests drive scenarios by swapping those attributes.

All stubs live under a unique package name and are popped in tearDownModule so
they never shadow the real ``lager`` for other tests in the session.
"""

import importlib.util
import os
import socket
import sys
import threading
import time
import types
import unittest

HERE = os.path.dirname(__file__)
NETS_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "..", "box", "lager", "nets"))
LAGER_DIR = os.path.dirname(NETS_DIR)  # box/lager
DEBUG_NET_PATH = os.path.join(NETS_DIR, "debug_net.py")
CONSTANTS_PATH = os.path.join(NETS_DIR, "constants.py")

# debug_net lives at ``lager.nets.debug_net`` and does ``from ..debug import``,
# so the stub must be two levels deep (PKG.nets.debug_net) for the relative
# import to resolve to PKG.debug.
PKG = "selfheal_stub_pkg"
NETS_PKG = f"{PKG}.nets"
_INSTALLED = []


class _DebugError(Exception):
    pass


class _JLinkNotRunning(_DebugError):
    pass


def _install(name, mod):
    sys.modules[name] = mod
    _INSTALLED.append(name)


def _build_debug_stub():
    """A stand-in ``..debug`` exporting exactly what debug_net imports."""
    m = types.ModuleType(f"{PKG}.debug")  # noqa: F841 (name set by caller)
    m.__path__ = []  # package, so ``..debug.probes`` can be a submodule

    def _unused(*a, **k):  # default: tests override the ones they exercise
        raise AssertionError("stub called without override")

    m.connect_jlink = _unused
    m.disconnect = _unused
    m.reset_device = _unused
    m.flash_device = _unused
    m.chip_erase = _unused
    m.erase_flash = _unused
    m.get_jlink_status = lambda **k: {"running": False, "pid": None}
    m.get_jlink_gdbserver_status = lambda **k: {"running": False, "pid": None}
    m.read_memory = _unused
    m.RTT = object
    m.DebugError = _DebugError
    m.JLinkNotRunning = _JLinkNotRunning
    m.start_openocd_gdbserver = _unused
    m.stop_openocd = _unused
    m.get_openocd_status = lambda **k: {"running": False, "pid": None}
    m.OpenOcdRpc = object
    m.OpenOcdRpcError = Exception
    return m


def _build_probes_stub():
    m = types.ModuleType(f"{PKG}.debug.probes")
    m.BACKEND_JLINK = "jlink"
    m.BACKEND_OPENOCD = "openocd"
    m.resolve_serial_from_net = lambda net: net.get("serial", "PROBE123")
    m.resolve_backend = lambda net: net.get("debug_backend", "jlink")
    m.gdb_port_for_slot = lambda slot: 2331 + 3 * slot
    m.rtt_port_for_slot = lambda slot: 9090 + 2 * slot
    m.openocd_telnet_port_for_slot = lambda slot: 4444 + 2 * slot
    m.openocd_tcl_port_for_slot = lambda slot: 6666 + 2 * slot
    m.parse_device_field = lambda d: (d, None)
    m.parse_probe_serial = lambda addr: None
    m.compute_slot = lambda serial, all_serials: 0
    return m


def _load_debug_net():
    # box/lager/nets/constants.py does ``from lager.constants import
    # HARDWARE_SERVICE_PORT`` at import — provide a minimal real-shaped stub.
    if "lager" not in sys.modules:
        lager_pkg = types.ModuleType("lager")
        # Real __path__ so a later-collected test's ``import lager.debug`` still
        # resolves while our stub sits in sys.modules (an empty __path__ here
        # would shadow real submodules until tearDownModule).
        lager_pkg.__path__ = [LAGER_DIR]
        _install("lager", lager_pkg)
    if "lager.constants" not in sys.modules:
        lc = types.ModuleType("lager.constants")
        lc.HARDWARE_SERVICE_PORT = 0
        _install("lager.constants", lc)

    pkg = types.ModuleType(PKG)
    pkg.__path__ = []
    _install(PKG, pkg)

    nets_pkg = types.ModuleType(NETS_PKG)
    nets_pkg.__path__ = [NETS_DIR]
    _install(NETS_PKG, nets_pkg)

    # constants submodule (real file)
    cspec = importlib.util.spec_from_file_location(f"{NETS_PKG}.constants", CONSTANTS_PATH)
    cmod = importlib.util.module_from_spec(cspec)
    cmod.__package__ = NETS_PKG
    _install(f"{NETS_PKG}.constants", cmod)
    cspec.loader.exec_module(cmod)

    # stub debug + probes (siblings of nets) so the heavy import branch succeeds
    _install(f"{PKG}.debug", _build_debug_stub())
    _install(f"{PKG}.debug.probes", _build_probes_stub())

    spec = importlib.util.spec_from_file_location(f"{NETS_PKG}.debug_net", DEBUG_NET_PATH)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = NETS_PKG
    _install(f"{NETS_PKG}.debug_net", mod)
    spec.loader.exec_module(mod)
    return mod


debug_net = _load_debug_net()


def tearDownModule():
    for key in _INSTALLED:
        sys.modules.pop(key, None)


def _make_net(channel="NRF52840_XXAA", backend="jlink"):
    assert debug_net._debug_available, "expected the real DebugNet, got _NullDebug"
    net_cfg = {"channel": channel, "instrument": "jlink", "debug_backend": backend}
    return debug_net.DebugNet("dbg", net_cfg)


class SelfHealRetryTests(unittest.TestCase):
    def test_retries_without_restarting_a_running_server(self):
        """Settling window: op fails once, server IS running -> retry, never connect()."""
        net = _make_net()
        calls = {"op": 0, "connect": 0}

        def op():
            calls["op"] += 1
            if calls["op"] == 1:
                raise debug_net.JLinkNotRunning("settling")
            return "ok"

        # Server reports running the whole time -> guard must skip connect().
        debug_net.get_jlink_gdbserver_status = lambda **k: {"running": True, "pid": 7}
        net.connect = lambda *a, **k: calls.__setitem__("connect", calls["connect"] + 1)

        self.assertEqual(net._self_heal(op, backoff=0.0), "ok")
        self.assertEqual(calls["op"], 2)
        self.assertEqual(calls["connect"], 0, "must not restart a live server")

    def test_reconnects_only_when_server_is_down(self):
        net = _make_net()
        calls = {"op": 0, "connect": 0}

        def op():
            calls["op"] += 1
            if calls["op"] == 1:
                raise debug_net.JLinkNotRunning("down")
            return "ok"

        debug_net.get_jlink_status = lambda **k: {"running": False, "pid": None}
        debug_net.get_jlink_gdbserver_status = lambda **k: {"running": False, "pid": None}
        net.connect = lambda *a, **k: calls.__setitem__("connect", calls["connect"] + 1)

        self.assertEqual(net._self_heal(op, backoff=0.0), "ok")
        self.assertEqual(calls["connect"], 1, "should reconnect when nothing is running")

    def test_da1469x_retries_but_never_autostarts_a_server(self):
        """DA1469x flash deliberately leaves the server down; self-heal must NOT
        auto-connect (avoids unhalted-XIP garbage / frozen attach). It still
        retries, then surfaces the original error unchanged."""
        net = _make_net(channel="DA14695")
        calls = {"op": 0, "connect": 0}

        def op():
            calls["op"] += 1
            raise debug_net.JLinkNotRunning("server intentionally down post-flash")

        debug_net.get_jlink_status = lambda **k: {"running": False, "pid": None}
        debug_net.get_jlink_gdbserver_status = lambda **k: {"running": False, "pid": None}
        net.connect = lambda *a, **k: calls.__setitem__("connect", calls["connect"] + 1)

        with self.assertRaises(debug_net.JLinkNotRunning):
            net._self_heal(op, retries=2, backoff=0.0)
        self.assertEqual(calls["op"], 3, "should still retry (1 + 2)")
        self.assertEqual(calls["connect"], 0, "must never auto-start a DA1469x server")

    def test_persistent_failure_propagates_original(self):
        net = _make_net()

        def op():
            raise debug_net.JLinkNotRunning("never recovers")

        debug_net.get_jlink_gdbserver_status = lambda **k: {"running": False, "pid": None}
        net.connect = lambda *a, **k: None
        with self.assertRaises(debug_net.JLinkNotRunning):
            net._self_heal(op, retries=2, backoff=0.0)

    def test_reset_routes_through_self_heal(self):
        """reset() wires reset_device through the retry path."""
        net = _make_net()
        attempts = {"n": 0}

        def fake_reset_device(*a, **k):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise debug_net.JLinkNotRunning("flash settling")
            yield "reset ok"

        debug_net.reset_device = fake_reset_device
        debug_net.get_jlink_gdbserver_status = lambda **k: {"running": True, "pid": 7}
        net.connect = lambda *a, **k: None

        self.assertEqual(net.reset(halt=False), "reset ok")
        self.assertEqual(attempts["n"], 2)


class OpenOcdSelfHealTests(unittest.TestCase):
    """The self-heal must cover OpenOCD too — not just J-Link."""

    def test_openocd_retries_and_reconnects_when_daemon_down(self):
        net = _make_net(backend="openocd")
        self.assertEqual(net.backend, "openocd")
        calls = {"op": 0, "connect": 0}

        def op():
            calls["op"] += 1
            if calls["op"] == 1:
                # _ensure_openocd_running raises RuntimeError when the daemon
                # isn't up — the OpenOCD "not connected" signal.
                raise RuntimeError("OpenOCD is not running")
            return "ok"

        debug_net.get_openocd_status = lambda **k: {"running": False, "pid": None}
        net.connect = lambda *a, **k: calls.__setitem__("connect", calls["connect"] + 1)

        self.assertEqual(net._self_heal(op, backoff=0.0), "ok")
        self.assertEqual(calls["connect"], 1, "should restart a down OpenOCD daemon")

    def test_openocd_leaves_running_daemon_untouched(self):
        net = _make_net(backend="openocd")
        calls = {"op": 0, "connect": 0}

        def op():
            calls["op"] += 1
            if calls["op"] == 1:
                raise RuntimeError("transient rpc fault")
            return "ok"

        debug_net.get_openocd_status = lambda **k: {"running": True, "pid": 9}
        net.connect = lambda *a, **k: calls.__setitem__("connect", calls["connect"] + 1)

        self.assertEqual(net._self_heal(op, backoff=0.0), "ok")
        self.assertEqual(calls["connect"], 0, "must not bounce a live OpenOCD daemon")

    def test_openocd_da1469x_never_autostarts(self):
        net = _make_net(channel="DA14695", backend="openocd")
        calls = {"connect": 0}

        def op():
            raise RuntimeError("OpenOCD is not running")

        debug_net.get_openocd_status = lambda **k: {"running": False, "pid": None}
        net.connect = lambda *a, **k: calls.__setitem__("connect", calls["connect"] + 1)

        with self.assertRaises(RuntimeError):
            net._self_heal(op, retries=1, backoff=0.0)
        self.assertEqual(calls["connect"], 0, "DA1469x guard must hold for OpenOCD too")


class _RttTelnetServer:
    """Loopback TCP server serving a queue of byte 'sessions', one per connect.

    Each accepted connection sends the next queued payload then closes, modelling
    the rtt server socket dropping (daemon bounce / rtt server restart) and a
    fresh listener coming back on the same port.
    """

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _serve(self):
        self._srv.settimeout(0.2)
        idx = 0
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                if idx < len(self._payloads):
                    try:
                        conn.sendall(self._payloads[idx])
                    except OSError:
                        pass
                    idx += 1

    def close(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass


class _FakeOpenOcdRpc:
    """No-op stand-in for OpenOcdRpc — the rtt setup commands all succeed."""

    def __init__(self, *a, **k):
        pass

    def rtt_setup(self, *a, **k):
        pass

    def rtt_start(self, *a, **k):
        pass

    def rtt_server_stop(self, *a, **k):
        pass

    def rtt_server_start(self, *a, **k):
        pass


class OpenOcdRttReconnectTests(unittest.TestCase):
    """RTT reconnect must work for the OpenOCD backend too (parity with J-Link)."""

    def setUp(self):
        self._orig_rpc = debug_net.OpenOcdRpc
        debug_net.OpenOcdRpc = _FakeOpenOcdRpc

    def tearDown(self):
        debug_net.OpenOcdRpc = self._orig_rpc

    def test_reader_reattaches_across_socket_drop(self):
        server = _RttTelnetServer([b"before-bounce", b"after-bounce"]).start()
        self.addCleanup(server.close)

        rtt = debug_net._OpenOcdRtt(
            channel=0, rtt_telnet_port=server.port, tcl_port=0,
            reconnect=True, reconnect_timeout=10.0,
        )
        collected = []
        with rtt:
            deadline = time.time() + 8.0
            while time.time() < deadline and len(collected) < 2:
                data = rtt.read_some(timeout=0.2)
                if data:
                    collected.append(data)

        self.assertIn(b"before-bounce", collected)
        self.assertIn(
            b"after-bounce", collected,
            "OpenOCD RTT reader did not re-attach after the socket dropped",
        )

    def test_reconnect_disabled_does_not_reattach(self):
        server = _RttTelnetServer([b"only-once"]).start()
        self.addCleanup(server.close)

        rtt = debug_net._OpenOcdRtt(
            channel=0, rtt_telnet_port=server.port, tcl_port=0, reconnect=False,
        )
        rtt.__enter__()
        self.addCleanup(rtt.__exit__, None, None, None)
        # Drain the first session then force a drop.
        time.sleep(0.2)
        rtt._close_socket()
        # reconnect disabled -> no re-attach, read returns None and stays down.
        self.assertIsNone(rtt.read_some(timeout=0.2))
        self.assertIsNone(rtt._socket)


class SessionHelperTests(unittest.TestCase):
    def test_session_connects_and_tears_down(self):
        net = _make_net()
        seq = []
        net.connect = lambda **k: seq.append(("connect", k.get("ignore_if_connected")))
        net.disconnect = lambda: seq.append(("disconnect", None))

        with net.session() as s:
            self.assertIs(s, net)
            seq.append(("body", None))

        self.assertEqual(
            seq,
            [("connect", True), ("body", None), ("disconnect", None)],
        )

    def test_session_no_disconnect_when_opted_out(self):
        net = _make_net()
        seq = []
        net.connect = lambda **k: seq.append("connect")
        net.disconnect = lambda: seq.append("disconnect")

        with net.session(disconnect_on_exit=False):
            seq.append("body")

        self.assertEqual(seq, ["connect", "body"])

    def test_session_skips_connect_when_disabled(self):
        net = _make_net()
        seq = []
        net.connect = lambda **k: seq.append("connect")
        net.disconnect = lambda: seq.append("disconnect")

        with net.session(connect=False, disconnect_on_exit=False):
            seq.append("body")

        self.assertEqual(seq, ["body"])

    def test_session_tears_down_even_on_exception(self):
        net = _make_net()
        seq = []
        net.connect = lambda **k: seq.append("connect")
        net.disconnect = lambda: seq.append("disconnect")

        with self.assertRaises(ValueError):
            with net.session():
                raise ValueError("boom")
        self.assertEqual(seq, ["connect", "disconnect"])


if __name__ == "__main__":
    unittest.main()
