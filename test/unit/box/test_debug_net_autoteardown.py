# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Wiring tests: ``DebugNet`` registers/unregisters for auto-teardown (GAP 1b).

``connect()`` must record the net (install hooks + register) so an aborted job
reaps its gdbserver on exit; ``disconnect()`` must unregister it. We load the
real ``DebugNet`` under a private stub package (same approach as
``test_debug_net_self_heal.py``) and inject a recording ``..debug.teardown_registry``
stub so we can assert the calls without touching real process state.
"""

import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

HERE = os.path.dirname(__file__)
NETS_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "..", "box", "lager", "nets"))
LAGER_DIR = os.path.dirname(NETS_DIR)
DEBUG_NET_PATH = os.path.join(NETS_DIR, "debug_net.py")
CONSTANTS_PATH = os.path.join(NETS_DIR, "constants.py")

PKG = "autoteardown_stub_pkg"
NETS_PKG = f"{PKG}.nets"
_INSTALLED = []


def _install(name, mod):
    sys.modules[name] = mod
    _INSTALLED.append(name)


def _unused(*a, **k):
    raise AssertionError("stub called without override")


def _build_debug_stub():
    m = types.ModuleType(f"{PKG}.debug")
    m.__path__ = []
    for attr in ("connect_jlink", "disconnect", "reset_device", "flash_device",
                 "chip_erase", "erase_flash", "read_memory", "start_openocd_gdbserver",
                 "stop_openocd"):
        setattr(m, attr, _unused)
    m.get_jlink_status = lambda **k: {"running": False, "pid": None}
    m.get_jlink_gdbserver_status = lambda **k: {"running": False, "pid": None}
    m.get_openocd_status = lambda **k: {"running": False, "pid": None}
    m.RTT = object
    m.DebugError = type("DebugError", (Exception,), {})
    m.JLinkNotRunning = type("JLinkNotRunning", (Exception,), {})
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


def _build_teardown_stub():
    m = types.ModuleType(f"{PKG}.debug.teardown_registry")
    m.install_handlers = MagicMock(name="install_handlers")
    m.register = MagicMock(name="register")
    m.unregister = MagicMock(name="unregister")
    return m


def _load_debug_net():
    if "lager" not in sys.modules:
        lager_pkg = types.ModuleType("lager")
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

    cspec = importlib.util.spec_from_file_location(f"{NETS_PKG}.constants", CONSTANTS_PATH)
    cmod = importlib.util.module_from_spec(cspec)
    cmod.__package__ = NETS_PKG
    _install(f"{NETS_PKG}.constants", cmod)
    cspec.loader.exec_module(cmod)

    _install(f"{PKG}.debug", _build_debug_stub())
    _install(f"{PKG}.debug.probes", _build_probes_stub())
    teardown = _build_teardown_stub()
    _install(f"{PKG}.debug.teardown_registry", teardown)

    spec = importlib.util.spec_from_file_location(f"{NETS_PKG}.debug_net", DEBUG_NET_PATH)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = NETS_PKG
    _install(f"{NETS_PKG}.debug_net", mod)
    spec.loader.exec_module(mod)
    return mod, teardown


debug_net, teardown = _load_debug_net()


def tearDownModule():
    for key in _INSTALLED:
        sys.modules.pop(key, None)


def _make_net():
    assert debug_net._debug_available, "expected the real DebugNet, got _NullDebug"
    return debug_net.DebugNet("dbg", {"channel": "NRF52840_XXAA", "instrument": "jlink"})


class AutoTeardownWiringTests(unittest.TestCase):
    def setUp(self):
        teardown.install_handlers.reset_mock()
        teardown.register.reset_mock()
        teardown.unregister.reset_mock()
        # Make the backend calls succeed so connect/disconnect complete.
        debug_net.connect_jlink = lambda **k: {"connected": True}
        debug_net.disconnect = lambda **k: {"stopped": True}
        debug_net._repoint_jlink_script = lambda script: None

    def test_connect_registers_and_installs_handlers(self):
        net = _make_net()
        net.connect()
        teardown.install_handlers.assert_called_once()
        teardown.register.assert_called_once_with(net)

    def test_register_happens_even_if_connect_fails(self):
        # Registration is at the top of connect(), so an abort during the
        # actual connect still leaves the net tracked for teardown.
        def _boom(**k):
            raise RuntimeError("probe vanished mid-connect")
        debug_net.connect_jlink = _boom
        net = _make_net()
        with self.assertRaises(RuntimeError):
            net.connect()
        teardown.register.assert_called_once_with(net)

    def test_disconnect_unregisters(self):
        net = _make_net()
        net.disconnect()
        teardown.unregister.assert_called_once_with(net)


if __name__ == "__main__":
    unittest.main()
