# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the DA1469x QSPI branch in ``DebugNet.flash()`` / ``erase()``.

Mainline OpenOCD has no QSPI flash driver for the DA1469x family, so the
generic ``rpc.program`` / ``flash_erase_all`` calls can't touch external NOR.
The HTTP service (``service.py`` /debug/flash, /debug/erase) already routes
this family through ``da1469x_loader``; these tests pin the Net API to the
same dispatch — and pin everything else (other devices, the J-Link backend)
to the unchanged paths.

Harness: same private-stub-package trick as ``test_debug_net_self_heal.py`` —
the real ``DebugNet`` is loaded with stub ``..debug`` / ``..debug.probes`` /
``..debug.da1469x_loader`` siblings. ``debug_net`` imports the loader lazily
inside the methods, so per-test overrides of the stub's generators take
effect without reloading anything.
"""

import importlib.util
import os
import sys
import types
import unittest

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
    pass


def _install(name, mod):
    sys.modules[name] = mod
    _INSTALLED.append(name)


def _build_debug_stub():
    m = types.ModuleType(f"{PKG}.debug")
    m.__path__ = []  # package, so ``..debug.da1469x_loader`` resolves as a submodule

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
    m.OpenOcdRpc = object
    m.OpenOcdRpcError = _OpenOcdRpcError
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
    m.sniff_script_backend = lambda *a, **k: "openocd"
    return m


def _build_loader_stub():
    """Stand-in ``..debug.da1469x_loader`` recording every dispatch.

    ``xip_to_flash_offset`` reproduces the real contract (absolute XIP
    address -> flash-relative offset, None/0 -> 0) so the tests can assert
    the translation is applied; the real arithmetic is pinned by
    ``test_da1469x_loader.py``.
    """
    m = types.ModuleType(f"{PKG}.debug.da1469x_loader")
    m.DA1469X_FAMILY = "da1469x"
    m.DEFAULT_ERASE_LENGTH = 1 << 20
    m.Da1469xLoaderError = _LoaderError
    m.calls = []

    def xip_to_flash_offset(addr):
        m.calls.append(("xip_to_flash_offset", addr))
        if addr is None or addr == 0:
            return 0
        return addr - QSPI_XIP_BASE

    def flash_image(rpc, image_path, *, family, flash_id=0, offset=0):
        m.calls.append(("flash_image", image_path, family, flash_id, offset))
        yield "Preparing DA1469x flash_loader from /stub/flash_loader.elf"
        yield f"Erasing flash_id={flash_id} offset={hex(offset)} bytes=4"
        yield f"Programmed 4 bytes successfully"

    def erase_range(rpc, *, family, flash_id=0, offset=0, length):
        m.calls.append(("erase_range", family, flash_id, offset, length))
        yield f"Erasing flash_id={flash_id} offset={hex(offset)} bytes={length}"
        yield f"Erased {length} bytes successfully"

    m.xip_to_flash_offset = xip_to_flash_offset
    m.flash_image = flash_image
    m.erase_range = erase_range
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
    _install(f"{PKG}.debug.da1469x_loader", _build_loader_stub())

    spec = importlib.util.spec_from_file_location(f"{NETS_PKG}.debug_net", DEBUG_NET_PATH)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = NETS_PKG
    _install(f"{NETS_PKG}.debug_net", mod)
    spec.loader.exec_module(mod)
    return mod


debug_net = _load_debug_net()
loader_stub = sys.modules[f"{PKG}.debug.da1469x_loader"]


def tearDownModule():
    for key in _INSTALLED:
        sys.modules.pop(key, None)


class _RecordingRpc:
    """Fails loudly if the generic (non-loader) commands are used."""

    def __init__(self):
        self.calls = []

    def program(self, *a, **k):
        self.calls.append(("program", a, k))
        return "programmed"

    def flash_erase_all(self):
        self.calls.append(("flash_erase_all",))
        return "erased"


def _make_net(channel, backend="openocd"):
    assert debug_net._debug_available, "expected the real DebugNet, got _NullDebug"
    net_cfg = {"channel": channel, "instrument": "debugger", "debug_backend": backend}
    net = debug_net.DebugNet("dbg", net_cfg)
    if backend == "openocd":
        net._rpc = _RecordingRpc()
        net._openocd_rpc = lambda **k: net._rpc
    return net


class Da1469xFlashDispatchTests(unittest.TestCase):
    def setUp(self):
        loader_stub.calls.clear()
        self._orig = (loader_stub.flash_image, loader_stub.erase_range)
        debug_net.get_openocd_status = lambda **k: {"running": True, "pid": 9}

    def tearDown(self):
        loader_stub.flash_image, loader_stub.erase_range = self._orig

    def test_flash_routes_to_loader_with_translated_offset(self):
        net = _make_net("DA14695")
        out = net.flash("/tmp/xl.img.bin", QSPI_XIP_BASE)

        dispatch = [c for c in loader_stub.calls if c[0] == "flash_image"]
        self.assertEqual(
            dispatch,
            [("flash_image", "/tmp/xl.img.bin", "da1469x", 0, 0)],
            "XIP 0x16000000 must reach the loader as flash offset 0x0",
        )
        self.assertIn(("xip_to_flash_offset", QSPI_XIP_BASE), loader_stub.calls)
        self.assertEqual(net._rpc.calls, [], "must not fall through to rpc.program")
        # Progress lines are joined into the return value (service.py parity).
        self.assertIn("Preparing DA1469x flash_loader", out)
        self.assertIn("Programmed 4 bytes successfully", out)

    def test_flash_translates_nonzero_xip_offset(self):
        net = _make_net("DA14695")
        net.flash("/tmp/xl.img.bin", QSPI_XIP_BASE + 0x8000)
        dispatch = [c for c in loader_stub.calls if c[0] == "flash_image"]
        self.assertEqual(dispatch[0][4], 0x8000)

    def test_flash_other_device_still_uses_program(self):
        net = _make_net("STM32F446RE")
        out = net.flash("/tmp/app.bin", 0x08000000)
        self.assertEqual(out, "programmed")
        self.assertEqual(net._rpc.calls[0][0], "program")
        self.assertEqual(loader_stub.calls, [], "loader must not run for non-DA1469x")

    def test_flash_jlink_backend_untouched(self):
        net = _make_net("DA14695", backend="jlink")
        seen = {}

        def fake_flash_device(files, **kwargs):
            seen["files"] = files
            yield "jlink flashed"

        debug_net.flash_device = fake_flash_device
        self.assertEqual(net.flash("/tmp/xl.img.bin", QSPI_XIP_BASE), "jlink flashed")
        self.assertEqual(seen["files"], ([], [("/tmp/xl.img.bin", QSPI_XIP_BASE)], []))
        self.assertEqual(loader_stub.calls, [], "J-Link path has its own DA1469x handling")


class Da1469xEraseDispatchTests(unittest.TestCase):
    def setUp(self):
        loader_stub.calls.clear()
        self._orig = (loader_stub.flash_image, loader_stub.erase_range)
        debug_net.get_openocd_status = lambda **k: {"running": True, "pid": 9}

    def tearDown(self):
        loader_stub.flash_image, loader_stub.erase_range = self._orig

    def test_erase_routes_to_loader_range_erase(self):
        net = _make_net("DA14695")
        out = net.erase()
        self.assertEqual(
            loader_stub.calls,
            [("erase_range", "da1469x", 0, 0, loader_stub.DEFAULT_ERASE_LENGTH)],
        )
        self.assertEqual(net._rpc.calls, [], "flash_erase_all can't reach QSPI NOR")
        self.assertIn("Erased 1048576 bytes successfully", out)

    def test_erase_other_device_still_uses_flash_erase_all(self):
        net = _make_net("NRF52840_XXAA")
        self.assertEqual(net.erase(), "erased")
        self.assertEqual(net._rpc.calls, [("flash_erase_all",)])
        self.assertEqual(loader_stub.calls, [])


class Da1469xLoaderFailureTests(unittest.TestCase):
    """A failed loader run must raise a message naming the loader failure —
    not a raw OpenOCD tcl traceback — and say when the board may be blank."""

    def setUp(self):
        loader_stub.calls.clear()
        self._orig = (loader_stub.flash_image, loader_stub.erase_range)
        debug_net.get_openocd_status = lambda **k: {"running": True, "pid": 9}

    def tearDown(self):
        loader_stub.flash_image, loader_stub.erase_range = self._orig

    def test_program_failure_after_erase_warns_board_may_be_blank(self):
        def failing_flash(rpc, image_path, *, family, flash_id=0, offset=0):
            yield "Preparing DA1469x flash_loader from /stub/flash_loader.elf"
            yield f"Erasing flash_id={flash_id} offset={hex(offset)} bytes=4"
            raise loader_stub.Da1469xLoaderError("fl_cmd_status=3 (program error)")

        loader_stub.flash_image = failing_flash
        net = _make_net("DA14695")
        with self.assertRaises(loader_stub.Da1469xLoaderError) as ctx:
            net.flash("/tmp/xl.img.bin", QSPI_XIP_BASE)
        msg = str(ctx.exception)
        self.assertIn("DA1469x flash_loader flash failed", msg)
        self.assertIn("fl_cmd_status=3", msg)
        self.assertIn("Erasing flash_id=0", msg, "should name the last progress line")
        self.assertIn("blank", msg, "erase ran but program didn't — say so")

    def test_early_failure_has_no_blank_warning(self):
        def failing_flash(rpc, image_path, *, family, flash_id=0, offset=0):
            raise loader_stub.Da1469xLoaderError("loader artefacts not found")
            yield  # pragma: no cover — makes this a generator

        loader_stub.flash_image = failing_flash
        net = _make_net("DA14695")
        with self.assertRaises(loader_stub.Da1469xLoaderError) as ctx:
            net.flash("/tmp/xl.img.bin", QSPI_XIP_BASE)
        msg = str(ctx.exception)
        self.assertIn("loader artefacts not found", msg)
        self.assertNotIn("blank", msg, "nothing was erased; don't cry wolf")

    def test_rpc_error_is_wrapped_with_loader_context(self):
        def failing_flash(rpc, image_path, *, family, flash_id=0, offset=0):
            yield "Preparing DA1469x flash_loader from /stub/flash_loader.elf"
            raise debug_net.OpenOcdRpcError("tcl connection reset")

        loader_stub.flash_image = failing_flash
        net = _make_net("DA14695")
        with self.assertRaises(loader_stub.Da1469xLoaderError) as ctx:
            net.flash("/tmp/xl.img.bin", QSPI_XIP_BASE)
        self.assertIn("tcl connection reset", str(ctx.exception))

    def test_erase_failure_names_the_erase(self):
        def failing_erase(rpc, *, family, flash_id=0, offset=0, length):
            yield f"Erasing flash_id={flash_id} offset={hex(offset)} bytes={length}"
            raise loader_stub.Da1469xLoaderError("fl_cmd_status=2 (erase error)")

        loader_stub.erase_range = failing_erase
        net = _make_net("DA14695")
        with self.assertRaises(loader_stub.Da1469xLoaderError) as ctx:
            net.erase()
        msg = str(ctx.exception)
        self.assertIn("DA1469x flash_loader erase failed", msg)
        self.assertIn("fl_cmd_status=2", msg)

    def test_daemon_down_still_raises_connect_first(self):
        """_ensure_openocd_running fires before the loader — its RuntimeError
        keeps the existing _self_heal semantics (retry, never autostart)."""
        from unittest import mock

        debug_net.get_openocd_status = lambda **k: {"running": False, "pid": None}
        net = _make_net("DA14695")
        net.connect = lambda *a, **k: self.fail("must never auto-start a DA1469x server")
        with mock.patch("time.sleep"):  # erase() wraps _self_heal, which backs off
            with self.assertRaises(RuntimeError) as ctx:
                net.erase()
        self.assertIn("Call connect() first", str(ctx.exception))
        self.assertEqual(loader_stub.calls, [])


if __name__ == "__main__":
    unittest.main()
