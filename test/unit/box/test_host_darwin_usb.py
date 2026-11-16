# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/host/darwin_usb.py: USB enumeration on macOS for the
hardware service's self-restart gate.

The hardware service only restarts itself for a wedged USB session when the
device is still on the bus, and it answered that from Linux sysfs. On macOS
that read finds nothing, so the gate always concluded the device was
unplugged and the recovery never ran. The agent now supplies the answer from
the I/O Registry.

The registry data here is constructed, in the shape ``ioreg -p IOUSB -l -a``
is documented to produce. It was not captured from a real instrument: no USB
device was attached to the machine this was written on. These tests pin the
parsing and the wiring; that the keys match what real devices report is
still to be confirmed on hardware.
"""

import plistlib
import subprocess
import unittest
from unittest.mock import patch

from lager.host import darwin_usb
from lager.util import self_restart

RIGOL = "USB0::0x1AB1::0x0E11::DP8C000001::INSTR"


def _registry(*devices):
    """An IOUSB plane: one controller, the given devices behind one hub."""
    hub = {
        "IORegistryEntryName": "USB3.0 Hub",
        "idVendor": 0x2109, "idProduct": 0x0817,
        "IORegistryEntryChildren": list(devices),
    }
    return plistlib.dumps({
        "IORegistryEntryName": "Root",
        "IORegistryEntryChildren": [
            {"IORegistryEntryName": "AppleT8132USBXHCI", "locationID": 0,
             "IORegistryEntryChildren": [hub]},
            {"IORegistryEntryName": "AppleT8132USBXHCI", "locationID": 16777216},
        ],
    })


def _device(vid, pid, serial=None, key="USB Serial Number"):
    node = {"IORegistryEntryName": "dev", "idVendor": vid, "idProduct": pid}
    if serial is not None:
        node[key] = serial
    return node


class ParseTests(unittest.TestCase):
    def test_devices_nested_under_hubs_are_found(self):
        ids = darwin_usb.parse_ioreg(_registry(_device(0x1AB1, 0x0E11, "DP8C000001")))
        self.assertEqual(ids[(0x1AB1, 0x0E11)], ["DP8C000001"])
        self.assertEqual(ids[(0x2109, 0x0817)], [None])  # the hub itself

    def test_controllers_without_ids_are_not_devices(self):
        ids = darwin_usb.parse_ioreg(_registry())
        self.assertEqual(list(ids), [(0x2109, 0x0817)])

    def test_two_units_of_one_model_keep_both_serials(self):
        ids = darwin_usb.parse_ioreg(_registry(
            _device(0x1366, 0x0101, "000111"), _device(0x1366, 0x0101, "000222")))
        self.assertEqual(sorted(ids[(0x1366, 0x0101)]), ["000111", "000222"])

    def test_a_device_without_a_serial_is_listed_with_none(self):
        ids = darwin_usb.parse_ioreg(_registry(_device(0x0403, 0x6014)))
        self.assertEqual(ids[(0x0403, 0x6014)], [None])

    def test_the_alternate_serial_key_is_accepted(self):
        ids = darwin_usb.parse_ioreg(_registry(
            _device(0x05E6, 0x2281, "4518305", key="kUSBSerialNumberString")))
        self.assertEqual(ids[(0x05E6, 0x2281)], ["4518305"])

    def test_an_empty_plane_is_no_devices_not_an_error(self):
        self.assertEqual(darwin_usb.parse_ioreg(b""), {})
        self.assertEqual(darwin_usb.parse_ioreg(b"\n"), {})

    def test_result_has_the_shape_the_linux_gate_consumes(self):
        ids = darwin_usb.parse_ioreg(_registry(_device(0x1AB1, 0x0E11, "DP8C000001")))
        self.assertIs(self_restart.usb_address_enumerated(RIGOL, ids), True)
        other = "USB0::0x1AB1::0x0E11::SOMEONE_ELSE::INSTR"
        self.assertIs(self_restart.usb_address_enumerated(other, ids), False)
        self.assertIs(self_restart.usb_address_enumerated(
            "USB0::0x05E6::0x2281::4518305::INSTR", ids), False)


class EnumerationTests(unittest.TestCase):
    def _run_returning(self, stdout):
        return patch.object(darwin_usb.subprocess, "run",
                            return_value=subprocess.CompletedProcess([], 0, stdout, b""))

    def test_reads_the_registry_through_ioreg(self):
        with self._run_returning(_registry(_device(0x1AB1, 0x0E11, "X"))) as run:
            ids = darwin_usb.enumerated_usb_ids()
        self.assertEqual(ids[(0x1AB1, 0x0E11)], ["X"])
        self.assertEqual(tuple(run.call_args.args[0]), darwin_usb.IOREG_COMMAND)
        self.assertIn("timeout", run.call_args.kwargs)

    def test_every_failure_is_unknown_never_empty(self):
        failures = (
            FileNotFoundError("ioreg"),
            subprocess.CalledProcessError(1, "ioreg"),
            subprocess.TimeoutExpired("ioreg", 5),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch.object(darwin_usb.subprocess, "run", side_effect=failure):
                    self.assertIsNone(darwin_usb.enumerated_usb_ids())
        with self._run_returning(b"not a plist"):
            self.assertIsNone(darwin_usb.enumerated_usb_ids())


class GateWiringTests(unittest.TestCase):
    """The agent swaps one function on the shared module. These pin that the
    swap is still what the gate consults, so a change to that module that
    would silently disconnect macOS fails here."""

    def setUp(self):
        original = self_restart.enumerated_usb_ids
        self.addCleanup(setattr, self_restart, "enumerated_usb_ids", original)

    def test_the_linux_function_reports_nothing_for_this_device_by_default(self):
        with patch.object(self_restart, "enumerated_usb_ids", return_value={}):
            self.assertIs(self_restart.usb_device_enumerated(RIGOL), False)

    def test_after_install_the_gate_sees_a_device_that_is_on_the_bus(self):
        darwin_usb.install()
        present = _registry(_device(0x1AB1, 0x0E11, "DP8C000001"))
        with patch.object(darwin_usb.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, present, b"")):
            self.assertIs(self_restart.usb_device_enumerated(RIGOL), True)

    def test_after_install_an_unplugged_device_is_still_reported_absent(self):
        darwin_usb.install()
        with patch.object(darwin_usb.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, _registry(), b"")):
            self.assertIs(self_restart.usb_device_enumerated(RIGOL), False)

    def test_after_install_an_unreadable_registry_is_unknown(self):
        darwin_usb.install()
        with patch.object(darwin_usb.subprocess, "run", side_effect=FileNotFoundError()):
            self.assertIsNone(self_restart.usb_device_enumerated(RIGOL))

    def test_a_restart_becomes_due_only_when_the_device_is_present(self):
        darwin_usb.install()
        present = _registry(_device(0x1AB1, 0x0E11, "DP8C000001"))
        import os
        import tempfile
        stamp = os.path.join(tempfile.mkdtemp(), "stamp")
        with patch.object(darwin_usb.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, present, b"")):
            self.assertTrue(self_restart._restart_due(
                RIGOL, "test", service="t", stamp_path=stamp, cooldown_s=60, wedge="unreachable"))
        with patch.object(darwin_usb.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, _registry(), b"")), \
                patch.object(self_restart.time, "sleep"):
            self.assertFalse(self_restart._restart_due(
                RIGOL, "test", service="t", stamp_path=stamp + "2", cooldown_s=60, wedge="unreachable"))

    def test_non_usb_addresses_are_untouched(self):
        darwin_usb.install()
        self.assertIsNone(self_restart.usb_device_enumerated("TCPIP0::10.0.0.5::5025::SOCKET"))


if __name__ == "__main__":
    unittest.main()
