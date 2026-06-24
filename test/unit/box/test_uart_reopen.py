# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for UART serial->tty re-resolution + reopen (GAP 3).

``UARTBridge._reopen`` re-resolves the USB serial to its *current* ttyUSB node
(so a CP210x replug that renumbers ttyUSB0 -> ttyUSB1 is followed) and reopens.
``_reopen_with_backoff`` is gated by ``LAGER_UART_AUTORECONNECT`` (default off)
and bounded by ``LAGER_UART_REOPEN_TIMEOUT``.

``uart_bridge`` imports only ``serial`` + stdlib, so we load it standalone. No
real port is opened — ``_find_device_by_serial`` and ``_connect`` are mocked.
"""

import importlib.util
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Another test in the suite may have left a MagicMock ``serial`` stub in
# sys.modules; drop it so we load the real pyserial (we need a genuine
# ``serial.SerialException`` class to raise/catch). Real serial is never opened.
# A real module has a str ``__file__``; the stub's ``__getattr__`` returns a
# truthy MagicMock for everything, so test the type, not just truthiness.
_stub = sys.modules.get("serial")
if _stub is not None and not isinstance(getattr(_stub, "__file__", None), str):
    for _k in [k for k in list(sys.modules) if k == "serial" or k.startswith("serial.")]:
        del sys.modules[_k]
import serial  # noqa: E402  (real pyserial)

HERE = os.path.dirname(__file__)
UB_PATH = os.path.normpath(
    os.path.join(HERE, "..", "..", "..", "box", "lager", "protocols", "uart", "uart_bridge.py")
)
_spec = importlib.util.spec_from_file_location("uart_bridge_under_test", UB_PATH)
ub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ub)


def _make_bridge(bridge_serial="ABC123", device_path=None):
    """Construct a bridge without touching real sysfs."""
    with patch.object(ub.UARTBridge, "_find_device_by_serial", return_value="/dev/ttyUSB0"):
        return ub.UARTBridge(
            bridge_serial=bridge_serial, port="0", device_path=device_path
        )


class ResolvabilityTests(unittest.TestCase):
    def test_serial_is_resolvable(self):
        self.assertTrue(_make_bridge("ABC123")._resolvable)

    def test_device_path_override_not_resolvable(self):
        self.assertFalse(_make_bridge("ABC123", device_path="/dev/ttyUSB7")._resolvable)

    def test_dev_literal_serial_not_resolvable(self):
        b = _make_bridge("/dev/ttyUSB3")
        self.assertFalse(b._resolvable)
        self.assertEqual(b.device_path, "/dev/ttyUSB3")


class ReopenTests(unittest.TestCase):
    def setUp(self):
        for k in ("LAGER_UART_AUTORECONNECT", "LAGER_UART_REOPEN_TIMEOUT",
                  "LAGER_UART_REOPEN_INTERVAL"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.setUp()

    def test_reopen_repoints_to_renumbered_node(self):
        b = _make_bridge("ABC123")
        b._find_device_by_serial = MagicMock(return_value="/dev/ttyUSB1")
        b._connect = MagicMock()
        self.assertTrue(b._reopen())
        self.assertEqual(b.device_path, "/dev/ttyUSB1")
        b._connect.assert_called_once()

    def test_reopen_false_when_not_resolvable(self):
        b = _make_bridge("ABC123", device_path="/dev/ttyUSB7")
        b._connect = MagicMock()
        self.assertFalse(b._reopen())
        b._connect.assert_not_called()

    def test_reopen_false_when_serial_absent(self):
        b = _make_bridge("ABC123")
        b._find_device_by_serial = MagicMock(return_value=None)
        b._connect = MagicMock()
        self.assertFalse(b._reopen())
        b._connect.assert_not_called()

    def test_reopen_false_when_connect_fails(self):
        b = _make_bridge("ABC123")
        b._find_device_by_serial = MagicMock(return_value="/dev/ttyUSB1")
        b._connect = MagicMock(side_effect=serial.SerialException("busy"))
        self.assertFalse(b._reopen())


class ReopenWithBackoffTests(unittest.TestCase):
    def setUp(self):
        for k in ("LAGER_UART_AUTORECONNECT", "LAGER_UART_REOPEN_TIMEOUT",
                  "LAGER_UART_REOPEN_INTERVAL"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.setUp()

    def test_disabled_returns_false_immediately(self):
        b = _make_bridge("ABC123")
        b._reopen = MagicMock(return_value=True)
        self.assertFalse(b._reopen_with_backoff())
        b._reopen.assert_not_called()

    def test_enabled_retries_until_device_returns(self):
        os.environ["LAGER_UART_AUTORECONNECT"] = "1"
        os.environ["LAGER_UART_REOPEN_TIMEOUT"] = "5"
        b = _make_bridge("ABC123")
        # Fail twice, then succeed.
        b._reopen = MagicMock(side_effect=[False, False, True])
        with patch.object(ub.time, "sleep"):
            self.assertTrue(b._reopen_with_backoff())
        self.assertEqual(b._reopen.call_count, 3)

    def test_enabled_times_out(self):
        os.environ["LAGER_UART_AUTORECONNECT"] = "1"
        b = _make_bridge("ABC123")
        b._reopen = MagicMock(return_value=False)
        # monotonic: start, then jump past the deadline on the first check.
        with patch.object(ub.time, "sleep"), \
                patch.object(ub.time, "monotonic", side_effect=[100.0, 1000.0]):
            self.assertFalse(b._reopen_with_backoff())


if __name__ == "__main__":
    unittest.main()
