# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The USB dispatcher forwards ``settle`` to the selected controller (GAP 2)."""

import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

HERE = os.path.dirname(__file__)
HUB_DIR = os.path.normpath(
    os.path.join(HERE, "..", "..", "..", "box", "lager", "automation", "usb_hub")
)

PKG = "usbhub_disp_ut_pkg"
_INSTALLED = []


def _install(name, mod):
    sys.modules[name] = mod
    _INSTALLED.append(name)


def _load():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [HUB_DIR]
    _install(PKG, pkg)
    for name in ("usb_net", "acroname", "ykush", "dispatcher"):
        spec = importlib.util.spec_from_file_location(
            f"{PKG}.{name}", os.path.join(HUB_DIR, f"{name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = PKG
        _install(f"{PKG}.{name}", mod)
        spec.loader.exec_module(mod)
    return sys.modules[f"{PKG}.dispatcher"]


dispatcher = _load()


def tearDownModule():
    for key in _INSTALLED:
        sys.modules.pop(key, None)


class DispatcherSettleTests(unittest.TestCase):
    def setUp(self):
        self.controller = MagicMock()
        dispatcher._load_net_definitions = lambda: {
            "usb1": {"port": 3, "instrument": "acroname", "address": ""}
        }
        dispatcher._controller_for = lambda info: self.controller

    def test_enable_forwards_settle(self):
        dispatcher.enable("usb1", settle=0.4)
        self.controller.enable.assert_called_once_with("usb1", 3, settle=0.4)

    def test_disable_forwards_settle(self):
        dispatcher.disable("usb1", settle=1.0)
        self.controller.disable.assert_called_once_with("usb1", 3, settle=1.0)

    def test_toggle_default_settle_is_none(self):
        dispatcher.toggle("usb1")
        self.controller.toggle.assert_called_once_with("usb1", 3, settle=None)

    def test_unknown_net_raises(self):
        with self.assertRaises(KeyError):
            dispatcher.enable("nope", settle=0.4)


if __name__ == "__main__":
    unittest.main()
