# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the reconnect-aware J-Link RTT reader (``lager.debug.api.RTT``).

A J-Link ``flash()`` (and ``reset()`` via a Commander grab) briefly frees the
probe's USB and restarts the gdbserver on the *same* ports. Before the fix the
in-process RTT reader's socket would EOF and the reader went silent forever;
now it transparently re-attaches to the same RTT telnet port.

These tests are hermetic: ``api.py``'s heavy siblings (``jlink`` -> pexpect,
``gdb``, ``gdbserver`` ...) are stubbed in ``sys.modules`` so we can import the
module without a real probe, and a loopback TCP server stands in for J-Link's
RTT telnet listener.
"""

import importlib.util
import os
import socket
import sys
import threading
import time
import types
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")
API_PATH = os.path.join(BOX_DIR, "lager", "debug", "api.py")

# Load api.py under a PRIVATE package name so we never shadow the real
# ``lager.debug.*`` modules in sys.modules (doing so leaks stubs into other
# test modules in the same pytest session). api.py only uses relative imports,
# which resolve against this stub package, not ``lager``.
PKG = "rttstub.debug"
_INSTALLED = []


def _install(dotted, mod):
    sys.modules[dotted] = mod
    _INSTALLED.append(dotted)


def _stub(dotted, **attrs):
    mod = types.ModuleType(dotted)
    for k, v in attrs.items():
        setattr(mod, k, v)
    _install(dotted, mod)
    return mod


def _load_api():
    root = types.ModuleType("rttstub")
    root.__path__ = []
    _install("rttstub", root)
    pkg = types.ModuleType(PKG)
    pkg.__path__ = []
    _install(PKG, pkg)

    # Stub the sibling modules api.py imports at module load. The status helpers
    # are replaced per-test via monkeypatching on the loaded api module.
    _stub(f"{PKG}.jlink", JLink=object, commander=lambda *a, **k: None)
    _stub(
        f"{PKG}.mappings",
        get_jlink_status=lambda **k: {"running": False, "pid": None},
        readfile=lambda *a, **k: "",
        JL_LOGFILE="/tmp/jl.log",
    )
    _stub(f"{PKG}.process", stop_jlink=lambda **k: None)
    _stub(
        f"{PKG}.gdbserver",
        get_jlink_gdbserver_status=lambda **k: {"running": False, "pid": None},
        stop_jlink_gdbserver=lambda **k: None,
        start_jlink_gdbserver=lambda **k: {},
    )
    _stub(
        f"{PKG}.gdb",
        get_arch=lambda *a, **k: "arm",
        reset=lambda *a, **k: None,
        read_memory=lambda *a, **k: b"",
        get_controller=lambda *a, **k: None,
    )
    _stub(
        f"{PKG}.probes",
        gdb_port_for_slot=lambda slot: 2331 + 3 * slot,
        rtt_port_for_slot=lambda slot: 9090 + 2 * slot,
    )

    spec = importlib.util.spec_from_file_location(f"{PKG}.api", API_PATH)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = PKG
    _install(f"{PKG}.api", mod)
    spec.loader.exec_module(mod)
    return mod


api = _load_api()


def tearDownModule():
    for key in _INSTALLED:
        sys.modules.pop(key, None)
RTT = api.RTT
JLinkNotRunning = api.JLinkNotRunning


class _RttTelnetServer:
    """Loopback TCP server that serves a queue of byte 'sessions'.

    Each accepted connection sends the next queued payload then closes — this
    models J-Link dropping the RTT telnet socket when its gdbserver is bounced
    by a flash/reset, then a fresh server coming back on the same port.
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
                # Close the connection (drop), simulating the gdbserver bounce.

    def close(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass


class TestRttReconnect(unittest.TestCase):
    def _patch_server_running(self, running):
        api.get_jlink_status = lambda **k: {"running": running, "pid": 1 if running else None}
        api.get_jlink_gdbserver_status = lambda **k: {"running": running, "pid": 1 if running else None}
        api.detect_and_configure_rtt = lambda **k: {"found": False, "address": None, "error": None}

    def test_enter_raises_when_no_server(self):
        self._patch_server_running(False)
        rtt = RTT(rtt_telnet_port=59999)
        with self.assertRaises(JLinkNotRunning):
            rtt.__enter__()

    def test_reader_reattaches_across_bounce(self):
        self._patch_server_running(True)
        server = _RttTelnetServer([b"before-flash", b"after-flash"]).start()
        self.addCleanup(server.close)

        rtt = RTT(rtt_telnet_port=server.port, reconnect=True, reconnect_timeout=10.0)
        collected = []
        with rtt:
            deadline = time.time() + 8.0
            while time.time() < deadline and len(collected) < 2:
                data = rtt.read_some(timeout=0.2)
                if data:
                    collected.append(data)

        self.assertIn(b"before-flash", collected)
        self.assertIn(
            b"after-flash", collected,
            "reader did not re-attach to the RTT port after the socket dropped",
        )

    def test_bounded_giveup_when_server_stays_down(self):
        # Socket drops and the server never comes back (e.g. DA1469x flash leaves
        # it down). The reader must give up after the deadline, never hang.
        self._patch_server_running(False)
        rtt = RTT(rtt_telnet_port=59998, reconnect=True, reconnect_timeout=0.2)
        rtt._socket = None  # simulate a dropped connection
        start = time.time()
        for _ in range(5):
            self.assertIsNone(rtt.read_some(timeout=0.05))
        # _try_reconnect must short-circuit (server down) and not block per call.
        self.assertLess(time.time() - start, 3.0)

    def test_reconnect_disabled_does_not_reattach(self):
        self._patch_server_running(True)
        rtt = RTT(rtt_telnet_port=59997, reconnect=False)
        rtt._socket = None
        # Even though the server is "running", reconnect=False means no attach.
        self.assertIsNone(rtt.read_some(timeout=0.05))
        self.assertIsNone(rtt._socket)


if __name__ == "__main__":
    unittest.main()
