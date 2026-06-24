# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the optional post-toggle USB settle delay (GAP 2).

The Acroname/YKUSH backends gain a ``settle`` kwarg (seconds) so a caller can
have the box block after a port state change until it takes effect, instead of
returning fire-and-forget and racing a not-yet-(de)enumerated device.

Backends are loaded standalone under a private package (only relative import is
the stdlib-only ``.usb_net``). Vendor SDKs (BrainStem/pykush) are lazy-imported
inside ``_connect_hub``/``_ensure_library``, which we mock — no hardware/SDK.
"""

import importlib.util
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

HERE = os.path.dirname(__file__)
HUB_DIR = os.path.normpath(
    os.path.join(HERE, "..", "..", "..", "box", "lager", "automation", "usb_hub")
)

PKG = "usbhub_settle_ut_pkg"
_INSTALLED = []


def _install(name, mod):
    sys.modules[name] = mod
    _INSTALLED.append(name)


def _load():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [HUB_DIR]
    _install(PKG, pkg)
    mods = {}
    for name in ("usb_net", "acroname", "ykush"):
        spec = importlib.util.spec_from_file_location(
            f"{PKG}.{name}", os.path.join(HUB_DIR, f"{name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = PKG
        _install(f"{PKG}.{name}", mod)
        spec.loader.exec_module(mod)
        mods[name] = mod
    return mods


_MODS = _load()
acroname = _MODS["acroname"]
ykush = _MODS["ykush"]


def tearDownModule():
    for key in _INSTALLED:
        sys.modules.pop(key, None)


class AcronameSettleTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("LAGER_USB_SETTLE", None)
        self.acro = acroname.AcronameUSBNet()
        self.hub = MagicMock()
        self.acro._connect_hub = lambda: self.hub

    def tearDown(self):
        os.environ.pop("LAGER_USB_SETTLE", None)

    def test_enable_applies_settle(self):
        with patch("time.sleep") as sleep:
            self.acro.enable("usb1", 3, settle=0.5)
        self.hub.usb.setPortEnable.assert_called_once_with(3)
        sleep.assert_called_once_with(0.5)

    def test_zero_settle_does_not_sleep(self):
        with patch("time.sleep") as sleep:
            self.acro.enable("usb1", 3, settle=0)
        sleep.assert_not_called()

    def test_default_none_does_not_sleep(self):
        with patch("time.sleep") as sleep:
            self.acro.enable("usb1", 3)
        sleep.assert_not_called()

    def test_env_settle_used_when_arg_absent(self):
        os.environ["LAGER_USB_SETTLE"] = "0.3"
        with patch("time.sleep") as sleep:
            self.acro.disable("usb1", 3)
        self.hub.usb.setPortDisable.assert_called_once_with(3)
        sleep.assert_called_once_with(0.3)

    def test_arg_overrides_env(self):
        os.environ["LAGER_USB_SETTLE"] = "0.3"
        with patch("time.sleep") as sleep:
            self.acro.enable("usb1", 3, settle=0.7)
        sleep.assert_called_once_with(0.7)

    def test_toggle_settles_after_flip(self):
        self.acro._Result = SimpleNamespace(NO_ERROR="OK")
        self.hub.usb.getPortState.return_value = SimpleNamespace(error="OK", value=0)
        with patch("time.sleep") as sleep:
            self.acro.toggle("usb1", 3, settle=0.5)
        # value 0 -> port was off -> enable it
        self.hub.usb.setPortEnable.assert_called_once_with(3)
        sleep.assert_called_once_with(0.5)

    def test_library_missing_raises_before_settle(self):
        # No _connect_hub mock here -> real path tries to import BrainStem
        # (absent), so it must raise before any settle sleep.
        fresh = acroname.AcronameUSBNet()
        acroname.AcronameUSBNet._brainstem = None
        acroname.AcronameUSBNet._cached_hub = None
        with patch("time.sleep") as sleep:
            with self.assertRaises(acroname.LibraryMissingError):
                fresh.enable("usb1", 3, settle=0.5)
        sleep.assert_not_called()


class YkushSettleTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("LAGER_USB_SETTLE", None)
        self.y = ykush.YKUSHUSBNet({"serial": "YK1"})

    def tearDown(self):
        os.environ.pop("LAGER_USB_SETTLE", None)

    def test_enable_applies_settle(self):
        with patch.object(ykush, "_ensure_library", lambda: None), \
                patch.object(self.y, "_set_state") as set_state, \
                patch("time.sleep") as sleep:
            self.y.enable("usb1", 1, settle=0.4)
        set_state.assert_called_once()
        sleep.assert_called_once_with(0.4)


if __name__ == "__main__":
    unittest.main()
