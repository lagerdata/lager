# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for DebugNet.rtt_read() — the one-shot RTT convenience wrapper.

Loads box/lager/nets/debug_net.py standalone with a fake ``lager.debug`` module
(providing a scripted RTT context manager) so the real DebugNet class is
exercised without J-Link tooling.
"""

import importlib.util
import os
import sys
import types
import unittest


_BOX_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "box"))


def _load_debug_net():
    """(Re)build the minimal package tree and return (debug_net_module, FakeRTT)."""
    lager = types.ModuleType("lager")
    lager.__path__ = [os.path.join(_BOX_ROOT, "lager")]
    sys.modules["lager"] = lager

    lager_constants = types.ModuleType("lager.constants")
    lager_constants.HARDWARE_SERVICE_PORT = 8080
    sys.modules["lager.constants"] = lager_constants

    nets = types.ModuleType("lager.nets")
    nets.__path__ = [os.path.join(_BOX_ROOT, "lager", "nets")]
    sys.modules["lager.nets"] = nets

    spec_c = importlib.util.spec_from_file_location(
        "lager.nets.constants", os.path.join(_BOX_ROOT, "lager", "nets", "constants.py")
    )
    constants_mod = importlib.util.module_from_spec(spec_c)
    sys.modules["lager.nets.constants"] = constants_mod
    spec_c.loader.exec_module(constants_mod)

    class FakeRTT:
        scripted = []  # successive return values for read_some(); pad with None after

        def __init__(self, **kwargs):
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read_some(self, timeout=1.0):
            cls = type(self)
            if self.calls < len(cls.scripted):
                value = cls.scripted[self.calls]
                self.calls += 1
                return value
            return None

        def write(self, data):
            return len(data)

    debug_pkg = types.ModuleType("lager.debug")
    debug_pkg.RTT = FakeRTT
    # status() reports already-connected so rtt_read()'s best-effort connect is a no-op
    debug_pkg.get_jlink_status = lambda *a, **k: {"running": True}
    debug_pkg.get_jlink_gdbserver_status = lambda *a, **k: {"running": True}
    for name in ("connect_jlink", "disconnect", "reset_device", "flash_device",
                 "chip_erase", "erase_flash"):
        setattr(debug_pkg, name, lambda *a, **k: None)
    debug_pkg.read_memory = lambda *a, **k: b""
    sys.modules["lager.debug"] = debug_pkg

    spec_d = importlib.util.spec_from_file_location(
        "lager.nets.debug_net", os.path.join(_BOX_ROOT, "lager", "nets", "debug_net.py")
    )
    debug_net = importlib.util.module_from_spec(spec_d)
    sys.modules["lager.nets.debug_net"] = debug_net
    spec_d.loader.exec_module(debug_net)
    return debug_net, FakeRTT


class TestDebugRttRead(unittest.TestCase):
    def setUp(self):
        self.debug_net, self.FakeRTT = _load_debug_net()
        self.assertTrue(self.debug_net._debug_available, "expected real DebugNet, got fallback")

    def _net(self):
        return self.debug_net.DebugNet("debug1", {"channel": "nRF5340_xxAA_APP"})

    def test_concatenates_and_decodes(self):
        self.FakeRTT.scripted = [b"blnk 0\r\nblnk 1\r\n", b"blnk 2\r\n", None, b"blnk 3\r\n"]
        out = self._net().rtt_read(timeout=0.4, settle=0)
        self.assertIsInstance(out, str)
        self.assertIn("blnk 0", out)
        self.assertIn("blnk 3", out)
        self.assertEqual(out.count("blnk"), 4)

    def test_returns_empty_string_not_none_when_no_data(self):
        self.FakeRTT.scripted = []
        out = self._net().rtt_read(timeout=0.15, settle=0)
        self.assertEqual(out, "")

    def test_max_chars_caps_output_and_stops_early(self):
        self.FakeRTT.scripted = [b"A" * 100, b"B" * 100]
        out = self._net().rtt_read(timeout=0.5, max_chars=50, settle=0)
        self.assertEqual(out, "A" * 50)

    def test_undecodable_bytes_are_replaced_not_raised(self):
        self.FakeRTT.scripted = [b"ok\xff\xfebad"]
        out = self._net().rtt_read(timeout=0.15, settle=0)
        self.assertTrue(out.startswith("ok"))
        self.assertIn("bad", out)

    def test_settle_delay_is_applied_before_reading(self):
        self.FakeRTT.scripted = [b"data"]
        start = __import__("time").monotonic()
        out = self._net().rtt_read(timeout=0.1, settle=0.3)
        elapsed = __import__("time").monotonic() - start
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertEqual(out, "data")

    def test_null_debug_stub_raises(self):
        out = self.debug_net._NullDebug("debug1")
        with self.assertRaises(RuntimeError):
            out.rtt_read(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
