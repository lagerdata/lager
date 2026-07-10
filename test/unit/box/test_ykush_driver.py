# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/automation/usb_hub/ykush.py.

Regression coverage for the device-contention bug: the driver used to cache an
open pykush handle indefinitely (YKUSHUSBNet._devices), which in a long-lived
process (box_http_server) pinned the hub's exclusive libusb/usbfs claim forever
and made every other process — notably an in-container `lager python` test in
its own subprocess — fail to open the same hub with "OSError: open failed".

The fix: open a fresh handle per operation, release it immediately, and
serialise the whole open→operate→close cycle within and across processes via
the shared `hub_access` lock (usb_net.py → util/device_lock.py). These tests
load the REAL usb_net + device_lock (so the lock is genuinely exercised) with a
stubbed pykush, so they need no hardware.
"""

import importlib.util
import os
import sys
import threading
import time
import types
import unittest


_created_pkg_stubs = []


def _load_real(module_name, relpath):
    """Load a real box module by path into a minimal fake `lager` package tree
    (so the drivers' relative/absolute imports resolve without importing the
    whole heavyweight `lager` package)."""
    box_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "box")
    )
    for pkg in ("lager", "lager.util", "lager.automation", "lager.automation.usb_hub"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = []  # mark as package
            sys.modules[pkg] = mod
            _created_pkg_stubs.append(pkg)
    if module_name in sys.modules and getattr(sys.modules[module_name], "__file__", None):
        return sys.modules[module_name]
    path = os.path.join(box_root, relpath)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Real cross-process/thread lock + base class, then the driver under test.
_load_real("lager.util.device_lock", "lager/util/device_lock.py")
_load_real("lager.automation.usb_hub.usb_net", "lager/automation/usb_hub/usb_net.py")
ykush = _load_real("lager.automation.usb_hub.ykush", "lager/automation/usb_hub/ykush.py")

# The bare package stubs exist only so the drivers' top-level imports resolve
# while loading. Drop them now: a pathless `lager` left in sys.modules poisons
# every later real `import lager.*` in the same pytest process.
for _pkg in _created_pkg_stubs:
    sys.modules.pop(_pkg, None)


class _FakePyKush:
    """Stub pykush.YKUSH that simulates an EXCLUSIVE libusb claim: a second
    open while one is already held raises OSError('open failed'), mirroring
    real hub behaviour."""

    _claim = {"held_by": None}
    opened: list = []
    closed: list = []
    _counter = 0
    set_hook = None  # optional callable(port, state) for concurrency tests

    class _Handle:
        def __init__(self, owner):
            self.owner = owner

        def close(self):
            _FakePyKush.closed.append(self.owner)
            _FakePyKush._claim["held_by"] = None

    def __init__(self, serial=None):
        _FakePyKush._counter += 1
        self.id = _FakePyKush._counter
        if _FakePyKush._claim["held_by"] is not None:
            raise OSError("open failed")
        _FakePyKush._claim["held_by"] = self.id
        self._devhandle = _FakePyKush._Handle(self.id)
        _FakePyKush.opened.append(self.id)

    def set_port_state(self, port, state):
        if _FakePyKush.set_hook:
            _FakePyKush.set_hook(port, state)
        return True

    def get_port_state(self, port):
        return 1


class YkushDriverTests(unittest.TestCase):
    def setUp(self):
        _FakePyKush._claim = {"held_by": None}
        _FakePyKush.opened = []
        _FakePyKush.closed = []
        _FakePyKush._counter = 0
        _FakePyKush.set_hook = None
        ykush._YKUSH_CLS = _FakePyKush
        ykush._PORT_UP = 1
        ykush._PORT_DOWN = 0
        ykush._LIBRARY_CHECKED = True
        self.net = ykush.YKUSHUSBNet(
            {"address": "USB0::0x04D8::0xF2F7::YK28339::INSTR"}
        )

    def test_serial_and_lock_key_from_address(self):
        self.assertEqual(self.net.serial, "YK28339")
        # Lock key must identify the physical hub (its address), so every net on
        # the hub serialises against the others.
        self.assertEqual(self.net._lock_key(), "USB0::0x04D8::0xF2F7::YK28339::INSTR")

    def test_handle_released_after_each_operation(self):
        # The core regression: the hub must NOT stay claimed after the call.
        self.net.enable("CLI_USB", 2)
        self.assertIsNone(
            _FakePyKush._claim["held_by"],
            "hub left claimed after enable() — this is the pinning bug",
        )
        self.assertEqual(_FakePyKush.opened, [1])
        self.assertEqual(_FakePyKush.closed, [1])

    def test_sequential_ops_open_fresh_and_release(self):
        self.net.enable("CLI_USB", 2)
        self.net.disable("CLI_USB", 2)
        self.assertIsNone(_FakePyKush._claim["held_by"])
        self.assertEqual(_FakePyKush.opened, [1, 2])
        self.assertEqual(_FakePyKush.closed, [1, 2])

    def test_open_fails_while_another_owner_holds_hub(self):
        # Reproduce the reported failure: another process holds the hub open.
        _FakePyKush._claim["held_by"] = 999
        with self.assertRaises(OSError):
            self.net.state("CLI_USB", 2)  # both the attempt and its retry fail
        # Once released, the next call succeeds.
        _FakePyKush._claim["held_by"] = None
        self.assertTrue(self.net.state("CLI_USB", 2))

    def test_cross_thread_access_is_serialized_by_lock(self):
        state = {"n": 0, "peak": 0}
        guard = threading.Lock()

        def hook(port, st):
            with guard:
                state["n"] += 1
                state["peak"] = max(state["peak"], state["n"])
            time.sleep(0.02)
            with guard:
                state["n"] -= 1

        _FakePyKush.set_hook = hook
        threads = [
            threading.Thread(target=self.net.enable, args=("CLI_USB", 2))
            for _ in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(
            state["peak"], 1, "hub_access did not serialise concurrent access"
        )


if __name__ == "__main__":
    unittest.main()
