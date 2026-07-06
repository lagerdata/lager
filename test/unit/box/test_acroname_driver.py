# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/automation/usb_hub/acroname.py.

Same device-contention regression as the YKUSH driver: Acroname hubs were held
connected indefinitely (class-level _cached_hubs), pinning the exclusive USB
claim so another process could not connect. The fix opens a fresh connection
per operation, disconnects immediately after, and serialises the whole cycle
within and across processes via the shared `hub_access` lock.

BrainStem is stubbed, so no hardware is needed. (The real Acroname path still
needs a hardware smoke test before merge — see the plan.)
"""

import importlib.util
import os
import sys
import threading
import time
import types
import unittest


def _load_real(module_name, relpath):
    box_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "box")
    )
    for pkg in ("lager", "lager.util", "lager.automation", "lager.automation.usb_hub"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = []
            sys.modules[pkg] = mod
    if module_name in sys.modules and getattr(sys.modules[module_name], "__file__", None):
        return sys.modules[module_name]
    path = os.path.join(box_root, relpath)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_load_real("lager.util.device_lock", "lager/util/device_lock.py")
_load_real("lager.automation.usb_hub.usb_net", "lager/automation/usb_hub/usb_net.py")
acroname = _load_real(
    "lager.automation.usb_hub.acroname", "lager/automation/usb_hub/acroname.py"
)


class _FakeResult:
    NO_ERROR = 0


# Shared simulated-exclusive-claim state, mirroring real hub behaviour.
_claim = {"held_by": None}
_opened: list = []
_closed: list = []
# The PHYSICAL hub persists port state across connect/disconnect cycles (real
# hardware does), so it lives at module scope — not on a per-connection object.
_hub_ports: dict = {}
_port_hook = None  # optional callable() invoked during a port operation


class _FakeUsb:
    def setPortEnable(self, port):
        if _port_hook:
            _port_hook()
        _hub_ports[port] = True

    def setPortDisable(self, port):
        if _port_hook:
            _port_hook()
        _hub_ports[port] = False

    def getPortState(self, port):
        val = 0b11 if _hub_ports.get(port) else 0
        return types.SimpleNamespace(error=_FakeResult.NO_ERROR, value=val)


class _FakeHub:
    _counter = 0

    def __init__(self):
        _FakeHub._counter += 1
        self.id = _FakeHub._counter
        self.connected = False
        self.usb = _FakeUsb()

    def discoverAndConnect(self, spec, serial=None):
        # Exclusive: if the hub is already claimed, connect "fails".
        if _claim["held_by"] is not None:
            return 1  # != NO_ERROR
        _claim["held_by"] = self.id
        self.connected = True
        _opened.append(self.id)
        return _FakeResult.NO_ERROR

    def disconnect(self):
        if self.connected:
            self.connected = False
            _claim["held_by"] = None
            _closed.append(self.id)


def _make_brainstem():
    stem = types.SimpleNamespace(
        USBHub3p=_FakeHub, USBHub3c=_FakeHub, USBHub2x4=_FakeHub
    )
    link = types.SimpleNamespace(Spec=types.SimpleNamespace(USB="usb-spec"))
    return types.SimpleNamespace(stem=stem, link=link)


class AcronameDriverTests(unittest.TestCase):
    def setUp(self):
        global _port_hook
        _claim["held_by"] = None
        _opened.clear()
        _closed.clear()
        _hub_ports.clear()
        _port_hook = None
        _FakeHub._counter = 0
        # Bypass the lazy BrainStem import by pre-seeding the module/Result.
        acroname.AcronameUSBNet._brainstem = _make_brainstem()
        acroname.AcronameUSBNet._Result = _FakeResult
        self.net = acroname.AcronameUSBNet(
            {"address": "USB0::0x24FF::0x0013::BFABDDC4::INSTR"}
        )

    def test_address_and_lock_key(self):
        self.assertEqual(self.net._serial, 0xBFABDDC4)
        self.assertEqual(self.net._lock_key(), "USB0::0x24FF::0x0013::BFABDDC4::INSTR")

    def test_hub_disconnected_after_each_operation(self):
        self.net.enable("CHARGE", 0)
        self.assertIsNone(
            _claim["held_by"], "hub left connected after enable() — the pinning bug"
        )
        self.assertEqual(len(_opened), 1)
        self.assertEqual(_opened, _closed)

    def test_sequential_ops_connect_fresh_and_disconnect(self):
        self.net.enable("CHARGE", 0)
        self.net.disable("CHARGE", 0)
        self.assertIsNone(_claim["held_by"])
        self.assertEqual(len(_opened), 2)
        self.assertEqual(_opened, _closed)

    def test_connect_fails_while_another_owner_holds_hub(self):
        _claim["held_by"] = 999
        with self.assertRaises(acroname.DeviceNotFoundError):
            self.net.enable("CHARGE", 0)
        _claim["held_by"] = None
        self.net.enable("CHARGE", 0)  # succeeds once freed
        self.assertIsNone(_claim["held_by"])

    def test_toggle_reads_then_flips(self):
        # port starts off → toggle returns True (now on)
        self.assertTrue(self.net.toggle("CHARGE", 0))
        # Each op reconnects fresh, so state must come from the (persistent)
        # hardware — the second toggle reads "on" and flips back to off.
        self.assertFalse(self.net.toggle("CHARGE", 0))
        self.assertIsNone(_claim["held_by"])

    def test_cross_thread_access_is_serialized_by_lock(self):
        global _port_hook
        state = {"n": 0, "peak": 0}
        guard = threading.Lock()

        def hook():
            with guard:
                state["n"] += 1
                state["peak"] = max(state["peak"], state["n"])
            time.sleep(0.02)
            with guard:
                state["n"] -= 1

        _port_hook = hook
        threads = [
            threading.Thread(target=self.net.enable, args=("CHARGE", 0))
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
