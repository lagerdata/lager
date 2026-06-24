# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""``UARTBridge.monitor`` recovers from a transient read error (GAP 3).

A serial ``read`` raising ``SerialException``/``OSError`` triggers a bounded
reopen instead of immediately ending the session; an *empty* read (normal
pyserial timeout) must NOT, and a real Ctrl-C still exits cleanly.
"""

import importlib.util
import io
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Drop any MagicMock ``serial`` stub a prior test left behind so we load real
# pyserial (we need a genuine ``serial.SerialException``). No real port opened.
# Test for a str ``__file__``: the stub's ``__getattr__`` returns a truthy
# MagicMock for any attribute, so a plain truthiness check is fooled.
_stub = sys.modules.get("serial")
if _stub is not None and not isinstance(getattr(_stub, "__file__", None), str):
    for _k in [k for k in list(sys.modules) if k == "serial" or k.startswith("serial.")]:
        del sys.modules[_k]
import serial  # noqa: E402  (real pyserial)

HERE = os.path.dirname(__file__)
UB_PATH = os.path.normpath(
    os.path.join(HERE, "..", "..", "..", "box", "lager", "protocols", "uart", "uart_bridge.py")
)
_spec = importlib.util.spec_from_file_location("uart_bridge_monitor_ut", UB_PATH)
ub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ub)


class _FakeStream:
    def __init__(self):
        self.buffer = io.BytesIO()

    def isatty(self):
        return False

    def fileno(self):
        return 1


class _FakeSerial:
    def __init__(self, reads):
        self._reads = list(reads)
        self.in_waiting = 0
        self.is_open = True

    def read(self, _n):
        item = self._reads.pop(0) if self._reads else KeyboardInterrupt
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.is_open = False


def _bridge():
    with patch.object(ub.UARTBridge, "_find_device_by_serial", return_value="/dev/ttyUSB0"):
        return ub.UARTBridge(bridge_serial="ABC123", port="0")


class MonitorRetryTests(unittest.TestCase):
    def _run_monitor(self, bridge):
        """Run monitor() with stdio + signal/atexit side effects neutralised."""
        out, err = _FakeStream(), _FakeStream()
        with patch.object(ub.signal, "signal"), \
                patch.object(ub.atexit, "register"), \
                patch("sys.stdout", out), patch("sys.stderr", err):
            bridge.monitor()
        return out.buffer.getvalue(), err.buffer.getvalue()

    def test_recovers_after_transient_read_error(self):
        b = _bridge()
        fake = _FakeSerial([serial.SerialException("io"), b"hello", KeyboardInterrupt])
        b._connect = MagicMock(side_effect=lambda: setattr(b, "serial_conn", fake))
        b._reopen_with_backoff = MagicMock(return_value=True)

        out, err = self._run_monitor(b)
        self.assertIn(b"hello", out)
        self.assertIn(b"Reconnected", err)
        b._reopen_with_backoff.assert_called_once()

    def test_disconnects_when_reopen_fails(self):
        b = _bridge()
        fake = _FakeSerial([serial.SerialException("io")])
        b._connect = MagicMock(side_effect=lambda: setattr(b, "serial_conn", fake))
        b._reopen_with_backoff = MagicMock(return_value=False)

        out = _FakeStream()
        err = _FakeStream()
        with patch.object(ub.signal, "signal"), \
                patch.object(ub.atexit, "register"), \
                patch("sys.stdout", out), patch("sys.stderr", err):
            with self.assertRaises(serial.SerialException):
                b.monitor()
        self.assertIn(b"Disconnected", err.buffer.getvalue())

    def test_empty_read_does_not_trigger_reopen(self):
        b = _bridge()
        fake = _FakeSerial([b"", b"", KeyboardInterrupt])
        b._connect = MagicMock(side_effect=lambda: setattr(b, "serial_conn", fake))
        b._reopen_with_backoff = MagicMock(return_value=True)

        self._run_monitor(b)
        b._reopen_with_backoff.assert_not_called()

    def test_keyboard_interrupt_exits_cleanly(self):
        b = _bridge()
        fake = _FakeSerial([KeyboardInterrupt])
        b._connect = MagicMock(side_effect=lambda: setattr(b, "serial_conn", fake))
        b._reopen_with_backoff = MagicMock(return_value=True)

        out, err = self._run_monitor(b)  # must not raise
        b._reopen_with_backoff.assert_not_called()
        self.assertIn(b"Disconnected", err)


if __name__ == "__main__":
    unittest.main()
