# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""``UARTNet.get_path(force=True)`` re-resolves past the cache (GAP 3).

Default ``get_path()`` caches the first resolution; ``force=True`` re-resolves
so a Python-API caller doesn't keep handing back a stale ``/dev/ttyUSB*`` after
a CP210x replug renumbered the node.

``uart_net`` lazy-imports ``.uart_bridge`` inside ``get_path``; we inject a stub
sibling so no sysfs/hardware is touched.
"""

import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(__file__)
UART_DIR = os.path.normpath(
    os.path.join(HERE, "..", "..", "..", "box", "lager", "protocols", "uart")
)

PKG = "uartnet_getpath_ut_pkg"
_INSTALLED = []
_PATHS = ["/dev/ttyUSB0", "/dev/ttyUSB1"]
_constructed = []


def _install(name, mod):
    sys.modules[name] = mod
    _INSTALLED.append(name)


class _FakeBridge:
    def __init__(self, **kwargs):
        # Hand back the next resolved path each time a bridge is constructed.
        self.device_path = _PATHS[min(len(_constructed), len(_PATHS) - 1)]
        _constructed.append(kwargs)


def _load():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [UART_DIR]
    _install(PKG, pkg)

    bridge_stub = types.ModuleType(f"{PKG}.uart_bridge")
    bridge_stub.UARTBridge = _FakeBridge
    _install(f"{PKG}.uart_bridge", bridge_stub)

    spec = importlib.util.spec_from_file_location(
        f"{PKG}.uart_net", os.path.join(UART_DIR, "uart_net.py")
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = PKG
    _install(f"{PKG}.uart_net", mod)
    spec.loader.exec_module(mod)
    return mod


uart_net = _load()


def tearDownModule():
    for key in _INSTALLED:
        sys.modules.pop(key, None)


class GetPathTests(unittest.TestCase):
    def setUp(self):
        _constructed.clear()
        self.net = uart_net.UARTNet("uart1", {"pin": "ABC123", "channel": "0"})

    def test_caches_by_default(self):
        self.assertEqual(self.net.get_path(), "/dev/ttyUSB0")
        self.assertEqual(self.net.get_path(), "/dev/ttyUSB0")
        self.assertEqual(len(_constructed), 1, "second call should hit the cache")

    def test_force_re_resolves(self):
        self.assertEqual(self.net.get_path(), "/dev/ttyUSB0")
        # Device renumbered -> force must re-resolve to the new node.
        self.assertEqual(self.net.get_path(force=True), "/dev/ttyUSB1")
        self.assertEqual(len(_constructed), 2)


if __name__ == "__main__":
    unittest.main()
