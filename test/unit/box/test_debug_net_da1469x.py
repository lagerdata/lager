# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for how ``DebugNet.flash()`` / ``erase()`` reach the OpenOCD
flash dispatch.

Mainline OpenOCD has no QSPI flash driver for the DA1469x family, so the
generic ``rpc.program`` / ``flash_erase_all`` calls can't touch external
NOR. The decision between those and the RAM-resident flash_loader lives in
``lager.debug.openocd_flash`` and is shared with the HTTP service. These
tests pin that DebugNet hands every OpenOCD flash and erase to that
dispatch -- with the device, so the dispatch can decide; with the shared
timeouts; with the RPC built to know its device -- and that everything
else (the J-Link backend, a down daemon, a deterministic loader failure
under ``_self_heal``) behaves as before. What the dispatch then selects is
``test_openocd_flash.py``'s to pin; that the two callers select the same
thing is ``test_debug_flash_dispatch_parity.py``'s.

Harness: same private-stub-package trick as ``test_debug_net_self_heal.py``
-- the real ``DebugNet`` is loaded with stub ``..debug`` / ``..debug.probes``
/ ``..debug.openocd_flash`` siblings.
"""

import importlib.util
import os
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(__file__)
NETS_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "..", "box", "lager", "nets"))
LAGER_DIR = os.path.dirname(NETS_DIR)  # box/lager
DEBUG_NET_PATH = os.path.join(NETS_DIR, "debug_net.py")
CONSTANTS_PATH = os.path.join(NETS_DIR, "constants.py")

PKG = "da1469x_net_stub_pkg"
NETS_PKG = f"{PKG}.nets"
_INSTALLED = []

QSPI_XIP_BASE = 0x16000000


class _DebugError(Exception):
    pass


class _JLinkNotRunning(_DebugError):
    pass


class _OpenOcdRpcError(Exception):
    pass


class _LoaderError(Exception):
    """Stands in for Da1469xLoaderError: NOT an OpenOcdRpcError, so
    ``_self_heal`` must let it through instead of retrying it."""


class _FakeRpc:
    """Records how DebugNet builds its RPC; never opens a socket."""

    def __init__(self, host="127.0.0.1", port=6666, timeout=10.0, device=None):
        self.host, self.port, self.timeout, self.device = host, port, timeout, device


def _install(name, mod):
    sys.modules[name] = mod
    _INSTALLED.append(name)


def _build_debug_stub():
    m = types.ModuleType(f"{PKG}.debug")
    m.__path__ = []  # package, so ``..debug.<sub>`` resolves as a submodule

    def _unused(*a, **k):
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
    m.get_openocd_status = lambda **k: {"running": True, "pid": 9}
    m.OpenOcdRpc = _FakeRpc
    m.OpenOcdRpcError = _OpenOcdRpcError
    return m


def _build_probes_stub():
    m = types.ModuleType(f"{PKG}.debug.probes")
    m.BACKEND_JLINK = "jlink"
    m.BACKEND_OPENOCD = "openocd"
    m.is_da1469x = lambda device: "DA1469" in str(device or "").upper()
    m.resolve_serial_from_net = lambda net: net.get("serial", "PROBE123")
    m.resolve_backend = lambda net: net.get("debug_backend", "jlink")
    m.gdb_port_for_slot = lambda slot: 2331 + 3 * slot
    m.rtt_port_for_slot = lambda slot: 9090 + 2 * slot
    m.openocd_telnet_port_for_slot = lambda slot: 4444 + 2 * slot
    m.openocd_tcl_port_for_slot = lambda slot: 6666 + 2 * slot
    m.parse_device_field = lambda d: (d, None)
    m.parse_probe_serial = lambda addr: None
    m.compute_slot = lambda serial, all_serials: 0
    m.sniff_script_backend = lambda *a, **k: "openocd"
    return m


def _build_dispatch_stub():
    """Stand-in ``..debug.openocd_flash`` recording every hand-off."""
    m = types.ModuleType(f"{PKG}.debug.openocd_flash")
    m.FLASH_RPC_TIMEOUT_S = 300
    m.ERASE_RPC_TIMEOUT_S = 120
    m.calls = []

    def flash_target(rpc, device, firmware_path, *, address=None):
        m.calls.append(("flash_target", rpc, device, firmware_path, address))
        yield f"flashed {device}"
        yield "done"

    def erase_target(rpc, device):
        m.calls.append(("erase_target", rpc, device))
        yield f"erased {device}"

    m.flash_target = flash_target
    m.erase_target = erase_target
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
    _install(f"{PKG}.debug.openocd_flash", _build_dispatch_stub())

    spec = importlib.util.spec_from_file_location(f"{NETS_PKG}.debug_net", DEBUG_NET_PATH)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = NETS_PKG
    _install(f"{NETS_PKG}.debug_net", mod)
    spec.loader.exec_module(mod)
    return mod


debug_net = _load_debug_net()
dispatch = sys.modules[f"{PKG}.debug.openocd_flash"]


def tearDownModule():
    for key in _INSTALLED:
        sys.modules.pop(key, None)


def _make_net(channel, backend="openocd"):
    assert debug_net._debug_available, "expected the real DebugNet, got _NullDebug"
    net_cfg = {"channel": channel, "instrument": "debugger", "debug_backend": backend}
    return debug_net.DebugNet("dbg", net_cfg)


class _Case(unittest.TestCase):
    def setUp(self):
        dispatch.calls.clear()
        self._orig = (dispatch.flash_target, dispatch.erase_target,
                      debug_net.flash_target, debug_net.erase_target)
        debug_net.get_openocd_status = lambda **k: {"running": True, "pid": 9}

    def tearDown(self):
        dispatch.flash_target, dispatch.erase_target = self._orig[:2]
        debug_net.flash_target, debug_net.erase_target = self._orig[2:]


class FlashHandOffTests(_Case):
    def test_da1469x_bin_hands_device_path_and_xip_address_to_the_dispatch(self):
        net = _make_net("DA14695")
        out = net.flash("/tmp/xl.img.bin", QSPI_XIP_BASE)

        self.assertEqual(len(dispatch.calls), 1)
        kind, rpc, device, path, address = dispatch.calls[0]
        self.assertEqual((kind, device, path, address),
                         ("flash_target", "DA14695", "/tmp/xl.img.bin", QSPI_XIP_BASE),
                         "the dispatch decides by device; DebugNet must not pre-translate")
        # The RPC is built with the shared flash budget and knows its device,
        # so the RPC layer's own guard can fire if the dispatch is bypassed.
        self.assertIsInstance(rpc, _FakeRpc)
        self.assertEqual(rpc.timeout, dispatch.FLASH_RPC_TIMEOUT_S)
        self.assertEqual(rpc.device, "DA14695")
        self.assertEqual(rpc.port, net.openocd_tcl_port)
        # Progress lines are joined into the string return value.
        self.assertEqual(out, "flashed DA14695\ndone")

    def test_other_devices_take_the_same_hand_off(self):
        # No per-device branching in DebugNet at all: that is what let the
        # two callers drift.
        net = _make_net("STM32F446RE")
        net.flash("/tmp/app.bin", 0x08000000)
        self.assertEqual(dispatch.calls[0][2:], ("STM32F446RE", "/tmp/app.bin", 0x08000000))

    def test_hex_and_elf_pass_no_address(self):
        net = _make_net("DA14695")
        net.flash("/tmp/app.hex", QSPI_XIP_BASE)
        net.flash("/tmp/app.elf")
        self.assertEqual([c[4] for c in dispatch.calls], [None, None])

    def test_bin_without_an_address_is_refused_before_the_dispatch(self):
        net = _make_net("DA14695")
        with self.assertRaises(ValueError):
            net.flash("/tmp/xl.img.bin")
        self.assertEqual(dispatch.calls, [])

    def test_jlink_backend_untouched(self):
        net = _make_net("DA14695", backend="jlink")
        seen = {}

        def fake_flash_device(files, **kwargs):
            seen["files"] = files
            yield "jlink flashed"

        debug_net.flash_device = fake_flash_device
        self.assertEqual(net.flash("/tmp/xl.img.bin", QSPI_XIP_BASE), "jlink flashed")
        self.assertEqual(seen["files"], ([], [("/tmp/xl.img.bin", QSPI_XIP_BASE)], []))
        self.assertEqual(dispatch.calls, [], "J-Link path has its own DA1469x handling")

    def test_daemon_down_raises_connect_first_before_the_dispatch(self):
        debug_net.get_openocd_status = lambda **k: {"running": False, "pid": None}
        net = _make_net("DA14695")
        with self.assertRaises(RuntimeError) as ctx:
            net.flash("/tmp/xl.img.bin", QSPI_XIP_BASE)
        self.assertIn("Call connect() first", str(ctx.exception))
        self.assertEqual(dispatch.calls, [])


class EraseHandOffTests(_Case):
    def test_erase_hands_device_to_the_dispatch(self):
        net = _make_net("DA14695")
        out = net.erase()
        self.assertEqual(len(dispatch.calls), 1)
        kind, rpc, device = dispatch.calls[0]
        self.assertEqual((kind, device), ("erase_target", "DA14695"))
        self.assertEqual(rpc.timeout, dispatch.ERASE_RPC_TIMEOUT_S)
        self.assertEqual(rpc.device, "DA14695")
        self.assertEqual(out, "erased DA14695")

    def test_other_devices_take_the_same_hand_off(self):
        net = _make_net("NRF52840_XXAA")
        net.erase()
        self.assertEqual(dispatch.calls[0][2], "NRF52840_XXAA")

    def test_jlink_backend_untouched(self):
        net = _make_net("DA14695", backend="jlink")
        debug_net.chip_erase = lambda **kwargs: iter(["jlink erased"])
        self.assertEqual(net.erase(), "jlink erased")
        self.assertEqual(dispatch.calls, [])

    def test_daemon_down_still_raises_connect_first(self):
        """_ensure_openocd_running fires before the dispatch -- its RuntimeError
        keeps the existing _self_heal semantics (retry, never autostart)."""
        debug_net.get_openocd_status = lambda **k: {"running": False, "pid": None}
        net = _make_net("DA14695")
        net.connect = lambda *a, **k: self.fail("must never auto-start a DA1469x server")
        with mock.patch("time.sleep"):  # erase() wraps _self_heal, which backs off
            with self.assertRaises(RuntimeError) as ctx:
                net.erase()
        self.assertIn("Call connect() first", str(ctx.exception))
        self.assertEqual(dispatch.calls, [])

    def test_loader_failure_is_not_retried_by_self_heal(self):
        # A deterministic loader failure (bad artefacts, loader rc != OK) is
        # not a transient daemon hiccup: retrying it thrice with backoff
        # would just re-erase a board three times. Only RuntimeError /
        # OpenOcdRpcError are transient.
        def failing_erase(rpc, device):
            dispatch.calls.append(("erase_target", rpc, device))
            raise _LoaderError("DA1469x flash_loader erase failed: fl_cmd_status=2")
            yield  # pragma: no cover -- makes this a generator

        debug_net.erase_target = failing_erase
        net = _make_net("DA14695")
        with mock.patch("time.sleep") as sleep:
            with self.assertRaises(_LoaderError):
                net.erase()
        self.assertEqual(len(dispatch.calls), 1, "must not retry a loader failure")
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
