# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""LabJack U3 addressing in ``box/lager/http_handlers/usb_scanner.py`` (#515).

A U3 reports no USB serial, so every U3 used to get the same address,
``USB0::0x0CD5::0x0003::::INSTR``. With two on one box, ``nets add`` refused a
net on either, because the address could not say which one it meant. A U3 is
now topology-addressed like a Plugable hub: the sysfs name goes in the serial
slot behind ``port-``, and the box opens the U3 on that port.

Two U3s on one box are covered by these tests only; no box has had two
attached.
"""

import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
import unittest


HERE = os.path.dirname(__file__)
SCANNER_PATH = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'http_handlers',
                 'usb_scanner.py')
)


def _load_scanner():
    """Load ``usb_scanner.py`` standalone (no ``lager.*`` package deps)."""
    spec = importlib.util.spec_from_file_location('usb_scanner_u3_under_test', SCANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules['usb_scanner_u3_under_test'] = module
    spec.loader.exec_module(module)
    return module


class TestU3Addressing(unittest.TestCase):
    """``scan_usb`` over a throwaway ``/sys/bus/usb/devices`` tree."""

    @classmethod
    def setUpClass(cls):
        cls.scanner = _load_scanner()

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmpdir, ignore_errors=True))
        self.sys_bus = os.path.join(self.tmpdir, 'sys', 'bus', 'usb', 'devices')
        self.sys_class_tty = os.path.join(self.tmpdir, 'sys', 'class', 'tty')
        os.makedirs(self.sys_bus)
        os.makedirs(self.sys_class_tty)

        real_path = pathlib.Path
        sys_bus, sys_class_tty = self.sys_bus, self.sys_class_tty

        def _path_shim(*args, **kw):
            if args and args[0] == '/sys/bus/usb/devices':
                return real_path(sys_bus)
            if args and args[0] == '/sys/class/tty':
                return real_path(sys_class_tty)
            return real_path(*args, **kw)

        original_Path = self.scanner.Path
        self.scanner.Path = _path_shim  # type: ignore[attr-defined]
        self.addCleanup(lambda: setattr(self.scanner, 'Path', original_Path))

    def _plug(self, name, vid='0cd5', pid='0003', serial=None):
        """Add one USB device directory; a U3 has no `serial` file."""
        dev = os.path.join(self.sys_bus, name)
        os.makedirs(dev)
        fields = [('idVendor', vid), ('idProduct', pid)]
        if serial is not None:
            fields.append(('serial', serial))
        for field, value in fields:
            with open(os.path.join(dev, field), 'w') as f:
                f.write(value + '\n')

    def _u3s(self):
        return [e for e in self.scanner.scan_usb() if e['name'] == 'LabJack_U3']

    def test_a_u3_is_addressed_by_its_port(self):
        self._plug('1-1.3')
        [entry] = self._u3s()
        self.assertEqual(entry['address'], 'USB0::0x0CD5::0x0003::port-1-1.3::INSTR')
        self.assertIsNone(entry['serial'])

    def test_two_u3s_get_two_addresses(self):
        self._plug('1-1.2')
        self._plug('1-1.3')
        addresses = sorted(e['address'] for e in self._u3s())
        self.assertEqual(addresses, [
            'USB0::0x0CD5::0x0003::port-1-1.2::INSTR',
            'USB0::0x0CD5::0x0003::port-1-1.3::INSTR',
        ])

    def test_a_unique_real_serial_is_kept(self):
        # Never seen on hardware, but the topology rule only replaces a serial
        # that cannot identify the device.
        self._plug('1-1.2', serial='320012345')
        [entry] = self._u3s()
        self.assertEqual(entry['address'], 'USB0::0x0CD5::0x0003::320012345::INSTR')

    def test_the_hub_dedupe_still_applies_only_to_hubs(self):
        # The cascaded-tier and SuperSpeed-companion dedupe is for docks that
        # enumerate as several hubs. A U3 is one device and must never be
        # dropped as "the other half" of another U3.
        self.assertIn('LabJack_U3', self.scanner._TOPOLOGY_ADDRESSED)
        self.assertNotIn('LabJack_U3', self.scanner._MULTI_TIER_HUBS)
        self.assertIn('Plugable_USB_Hub', self.scanner._MULTI_TIER_HUBS)


if __name__ == '__main__':
    unittest.main()
