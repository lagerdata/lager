# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for UARTBridge re-enumeration healing.

A UART net went permanently stale when its adapter re-enumerated: the bridge
resolved its /dev/tty* node once at construction and held the dead fd forever.
These tests cover the new machinery: device-gone error classification (which
must NOT fire for busy/locked ports), the durable usb_identity snapshot taken
at connect time, re-resolution in try_reopen, and the bounded reconnect loop.

The module is loaded standalone with the ``serial`` package stubbed (pattern
from test_box_http_server_capabilities.py) so no hardware deps are needed.
"""

import errno
import importlib.util
import os
import sys
import tempfile
import types
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")

if BOX_DIR not in sys.path:
    sys.path.insert(0, BOX_DIR)

# uart_bridge imports ``serial`` at module level; stub it if absent (mirrors
# the _HARDWARE_STUBS approach used by other box unit tests).
if 'serial' not in sys.modules:
    _serial_stub = types.ModuleType('serial')
    sys.modules['serial'] = _serial_stub


def _ensure_package(dotted, *parts):
    if dotted in sys.modules:
        return sys.modules[dotted]
    mod = types.ModuleType(dotted)
    mod.__path__ = [os.path.join(BOX_DIR, *parts)]
    mod.__package__ = dotted
    sys.modules[dotted] = mod
    return mod


def _load_module(dotted, filepath):
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


_ensure_package("lager", "lager")
_ensure_package("lager.devices", "lager", "devices")
uart_bridge = _load_module(
    "uart_bridge_reconnect_ut",
    os.path.join(BOX_DIR, "lager", "protocols", "uart", "uart_bridge.py"),
)
UARTBridge = uart_bridge.UARTBridge


class SerialException(Exception):
    """Stand-in for pyserial's SerialException (a plain Exception subclass)."""


class FakeConn:
    def __init__(self):
        self.is_open = True

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def close(self):
        self.is_open = False


def _fake_serial_ns(open_behavior):
    """A stand-in for the pyserial module; Serial() delegates to open_behavior."""
    ns = types.SimpleNamespace(
        PARITY_NONE='N', PARITY_EVEN='E', PARITY_ODD='O',
        PARITY_MARK='M', PARITY_SPACE='S',
        STOPBITS_ONE=1, STOPBITS_ONE_POINT_FIVE=1.5, STOPBITS_TWO=2,
        SerialException=SerialException,
    )
    ns.open_ports = []

    def Serial(port=None, **kwargs):
        ns.open_ports.append(port)
        return open_behavior(port)

    ns.Serial = Serial
    return ns


class FakeTime:
    """Deterministic clock: sleep() advances monotonic()."""

    def __init__(self):
        self.now = 0.0
        self.slept = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds


class UARTBridgeReconnectBase(unittest.TestCase):
    def setUp(self):
        # Control the lazy `from lager.devices import serial_id` imports.
        self._devices_pkg = sys.modules["lager.devices"]
        self._had_serial_id = hasattr(self._devices_pkg, "serial_id")
        self._old_serial_id = getattr(self._devices_pkg, "serial_id", None)
        self.fake_serial_id = types.SimpleNamespace(
            resolve_identity=lambda ident: None,
            identity_for_tty=lambda tty: None,
        )
        self._devices_pkg.serial_id = self.fake_serial_id

        self._old_serial_mod = uart_bridge.serial
        self._old_time_mod = uart_bridge.time

    def tearDown(self):
        if self._had_serial_id:
            self._devices_pkg.serial_id = self._old_serial_id
        else:
            del self._devices_pkg.serial_id
        uart_bridge.serial = self._old_serial_mod
        uart_bridge.time = self._old_time_mod

    def make_bridge(self, **kwargs):
        kwargs.setdefault("device_path", "/dev/ttyFAKE0")
        return UARTBridge("", "0", **kwargs)

    def use_serial(self, open_behavior):
        ns = _fake_serial_ns(open_behavior)
        uart_bridge.serial = ns
        return ns

    def use_fake_time(self):
        ft = FakeTime()
        uart_bridge.time = ft
        return ft


class IsDeviceGoneTests(UARTBridgeReconnectBase):
    def test_positives(self):
        gone = [
            OSError(errno.ENODEV, "No such device"),
            OSError(errno.ENOENT, "No such file or directory"),
            OSError(errno.ENXIO, "No such device or address"),
            OSError(errno.EIO, "Input/output error"),
            FileNotFoundError(errno.ENOENT, "No such file or directory"),
            SerialException(
                "device reports readiness to read but returned no data "
                "(device disconnected or multiple access on port?)"),
            SerialException("read failed: [Errno 19] No such device"),
            Exception("Input/output error"),
        ]
        for exc in gone:
            self.assertTrue(UARTBridge.is_device_gone(exc), repr(exc))

    def test_negatives_busy_and_unrelated(self):
        alive = [
            OSError(errno.EAGAIN, "Resource temporarily unavailable"),
            OSError(errno.EBUSY, "Device or resource busy"),
            SerialException(
                "Could not exclusively lock port /dev/ttyUSB0: "
                "[Errno 11] Resource temporarily unavailable"),
            SerialException("[Errno 16] Device or resource busy: '/dev/ttyUSB0'"),
            ValueError("invalid baudrate"),
            Exception("something else entirely"),
        ]
        for exc in alive:
            self.assertFalse(UARTBridge.is_device_gone(exc), repr(exc))


class InitResolutionTests(UARTBridgeReconnectBase):
    def test_identity_preferred_over_stored_path(self):
        self.fake_serial_id.resolve_identity = lambda ident: "/dev/ttyUSB7"
        bridge = UARTBridge("", "0", device_path="/dev/ttyUSB0",
                            usb_identity={"vid": "0403", "pid": "6011"})
        self.assertEqual(bridge.device_path, "/dev/ttyUSB7")

    def test_unresolvable_identity_falls_back_to_stored_path(self):
        self.fake_serial_id.resolve_identity = lambda ident: None
        bridge = UARTBridge("", "0", device_path="/dev/ttyUSB0",
                            usb_identity={"vid": "0403", "pid": "6011"})
        self.assertEqual(bridge.device_path, "/dev/ttyUSB0")

    def test_pin_as_device_path_still_works(self):
        bridge = UARTBridge("/dev/ttyUSB2", "0")
        self.assertEqual(bridge.device_path, "/dev/ttyUSB2")
        self.assertEqual(bridge.bridge_serial, "")

    def test_unresolvable_raises_file_not_found(self):
        # No identity, no override, and the sysfs walk finds nothing.
        with self.assertRaises(FileNotFoundError):
            UARTBridge("NO-SUCH-SERIAL", "0")


class ConnectSnapshotTests(UARTBridgeReconnectBase):
    def test_connect_snapshots_identity(self):
        ident = {"vid": "10c4", "pid": "ea60", "serial": "S1",
                 "port_path": "1-1.1", "interface": 0}
        self.fake_serial_id.identity_for_tty = lambda tty: ident
        self.use_serial(lambda port: FakeConn())
        bridge = self.make_bridge()
        bridge._connect()
        self.assertEqual(bridge.usb_identity, ident)

    def test_connect_snapshot_failure_is_nonfatal(self):
        def boom(tty):
            raise RuntimeError("sysfs unavailable")
        self.fake_serial_id.identity_for_tty = boom
        self.use_serial(lambda port: FakeConn())
        bridge = self.make_bridge()
        bridge._connect()  # must not raise
        self.assertIsNone(bridge.usb_identity)


class TryReopenTests(UARTBridgeReconnectBase):
    def test_reopen_via_identity_swaps_path_and_closes_old_fd(self):
        ns = self.use_serial(lambda port: FakeConn())
        bridge = self.make_bridge()
        bridge._connect()
        old_conn = bridge.serial_conn
        bridge.usb_identity = {"vid": "0403", "pid": "6011"}
        self.fake_serial_id.resolve_identity = lambda ident: "/dev/ttyUSB5"

        self.assertTrue(bridge.try_reopen())
        self.assertEqual(bridge.device_path, "/dev/ttyUSB5")
        self.assertFalse(old_conn.is_open)
        self.assertEqual(ns.open_ports[-1], "/dev/ttyUSB5")

    def test_reopen_fails_while_device_not_back(self):
        self.use_serial(lambda port: FakeConn())
        bridge = self.make_bridge()
        bridge.usb_identity = {"vid": "0403", "pid": "6011"}
        self.fake_serial_id.resolve_identity = lambda ident: None
        self.assertFalse(bridge.try_reopen())

    def test_reopen_open_failure_returns_false(self):
        def fail_open(port):
            raise SerialException("could not open port")
        self.use_serial(fail_open)
        bridge = self.make_bridge()
        bridge.usb_identity = {"vid": "0403", "pid": "6011"}
        self.fake_serial_id.resolve_identity = lambda ident: "/dev/ttyUSB5"
        self.assertFalse(bridge.try_reopen())

    def test_reopen_raw_path_fallback_requires_existing_node(self):
        # No identity and no USB serial: retry the stored path only once the
        # node exists again.
        ns = self.use_serial(lambda port: FakeConn())
        with tempfile.NamedTemporaryFile() as fake_node:
            bridge = self.make_bridge(device_path=fake_node.name)
            self.assertTrue(bridge.try_reopen())
            self.assertEqual(ns.open_ports[-1], fake_node.name)
        bridge2 = self.make_bridge(device_path="/dev/ttyDOES-NOT-EXIST")
        self.assertFalse(bridge2.try_reopen())


class ReconnectTests(UARTBridgeReconnectBase):
    def test_reconnect_succeeds_after_retries_and_reports_status(self):
        ft = self.use_fake_time()
        bridge = self.make_bridge()
        attempts = []
        bridge.try_reopen = lambda: (attempts.append(1), len(attempts) >= 3)[1]
        statuses = []

        ok = bridge.reconnect(on_status=statuses.append, total_timeout=60.0)
        self.assertTrue(ok)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(statuses, ['reconnecting', 'reconnected'])
        self.assertGreater(ft.slept, 0)

    def test_reconnect_times_out(self):
        ft = self.use_fake_time()
        bridge = self.make_bridge()
        bridge.try_reopen = lambda: False
        statuses = []

        ok = bridge.reconnect(on_status=statuses.append, total_timeout=10.0)
        self.assertFalse(ok)
        self.assertEqual(statuses, ['reconnecting'])
        self.assertLessEqual(ft.now, 10.0 + 5.0)  # bounded by deadline + max delay

    def test_reconnect_aborts_on_stop_check(self):
        self.use_fake_time()
        bridge = self.make_bridge()
        bridge.try_reopen = lambda: self.fail("must not attempt after stop")
        ok = bridge.reconnect(stop_check=lambda: True, total_timeout=60.0)
        self.assertFalse(ok)

    def test_reconnect_stop_mid_backoff(self):
        ft = self.use_fake_time()
        bridge = self.make_bridge()
        bridge.try_reopen = lambda: False
        # Stop once the fake clock has advanced past 1s of backoff.
        ok = bridge.reconnect(stop_check=lambda: ft.now > 1.0, total_timeout=60.0)
        self.assertFalse(ok)
        self.assertLess(ft.now, 5.0)

    def test_reconnect_status_callback_errors_ignored(self):
        self.use_fake_time()
        bridge = self.make_bridge()
        bridge.try_reopen = lambda: True

        def bad_status(_):
            raise RuntimeError("emit failed")
        self.assertTrue(bridge.reconnect(on_status=bad_status, total_timeout=5.0))


if __name__ == "__main__":
    unittest.main()
