# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""PicoScope discovery in ``box/lager/http_handlers/usb_scanner.py``.

Pico Technology ships a distinct USB product id for nearly every PicoScope
model. The scanner used to carry exactly one of them (0ce9:1007, the
2204A/2205A), so every other PicoScope -- the whole 3000, 4000 and 5000
series, and most of the 2000 series -- enumerated on the box and was silently
dropped, with no message anywhere to say why.

These tests cover the vendor-wide match that replaced it, and the channel
count read from the device rather than from a static table (2- and 4-channel
models share every series, so the entry name cannot imply it).
"""

import importlib.util
import os
import sys
import tempfile
import unittest


HERE = os.path.dirname(__file__)
SCANNER_PATH = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'http_handlers',
                 'usb_scanner.py')
)

PICO_VID = '0ce9'


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        'usb_scanner_pico_test', SCANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules['usb_scanner_pico_test'] = module
    spec.loader.exec_module(module)
    return module


class _FakePicoSysfs:
    """A minimal USB device node: idVendor, idProduct, serial, product."""

    def __init__(self, root, *, vid, pid, serial, product, bus_name):
        self.sys_bus = os.path.join(root, 'sys', 'bus', 'usb', 'devices')
        self.sys_class_tty = os.path.join(root, 'sys', 'class', 'tty')
        os.makedirs(self.sys_class_tty, exist_ok=True)
        dev = os.path.join(self.sys_bus, bus_name)
        os.makedirs(dev, exist_ok=True)
        for name, value in (('idVendor', vid), ('idProduct', pid),
                            ('serial', serial), ('product', product)):
            if value is not None:
                with open(os.path.join(dev, name), 'w') as f:
                    f.write(value + '\n')


class PicoScopeDiscovery(unittest.TestCase):

    def setUp(self):
        self.scanner = _load_scanner()
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(
            tmpdir, ignore_errors=True))
        self.tmpdir = tmpdir

    def _scan(self, devices):
        """Build a fake sysfs holding `devices` and return scanner entries."""
        fake = None
        for i, (pid, serial, product) in enumerate(devices):
            fake = _FakePicoSysfs(self.tmpdir, vid=PICO_VID, pid=pid,
                                  serial=serial, product=product,
                                  bus_name=f'3-{i + 1}')

        import pathlib
        real_path = pathlib.Path
        sys_bus, sys_tty = fake.sys_bus, fake.sys_class_tty

        def _path_shim(*args, **kw):
            if args and args[0] == '/sys/bus/usb/devices':
                return real_path(sys_bus)
            if args and args[0] == '/sys/class/tty':
                return real_path(sys_tty)
            return real_path(*args, **kw)

        original = self.scanner.Path
        self.scanner.Path = _path_shim
        try:
            return self.scanner.scan_usb()
        finally:
            self.scanner.Path = original

    def test_the_tested_2204a_still_resolves_to_its_named_entry(self):
        """The one model with a specific entry must keep using it."""
        entries = self._scan([('1007', 'AR911/011', 'PicoScope 2204A')])

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['name'], 'Picoscope_2000')
        self.assertEqual(entries[0]['net_type'], ['scope'])
        self.assertEqual(entries[0]['channels']['scope'], ['1', '2'])

    def test_a_pico_with_an_unlisted_product_id_is_still_found(self):
        """The regression this change exists to prevent.

        Before the vendor-wide match, this device produced no entry at all.
        """
        entries = self._scan([('1018', 'JY123/456', 'PicoScope 3403D')])

        self.assertEqual(len(entries), 1, 'unlisted PicoScope was dropped')
        self.assertEqual(entries[0]['name'], 'Picoscope')
        self.assertEqual(entries[0]['net_type'], ['scope'])

    def test_channel_count_comes_from_the_device_not_the_table(self):
        """A 4-channel scope must offer 4 channels, a 2-channel one 2."""
        four = self._scan([('1018', 'S4', 'PicoScope 3403D')])[0]
        self.assertEqual(four['channels']['scope'], ['1', '2', '3', '4'])

        two = self._scan([('10ff', 'S2', 'PicoScope 5242D')])[0]
        self.assertEqual(two['channels']['scope'], ['1', '2'])

    def test_channel_count_falls_back_to_two_when_unreadable(self):
        """An unparseable product string must not invent channels.

        Offering a channel that is not there fails later, at the point
        someone drives it; offering too few is corrected by the daemon's
        capability probe when the scope is opened.
        """
        entries = self._scan([('1018', 'S5', 'Some New PicoScope')])

        self.assertEqual(entries[0]['channels']['scope'], ['1', '2'])

    def test_the_model_string_is_reported(self):
        """Callers need the real model; the entry name is now generic."""
        entries = self._scan([('1018', 'S6', 'PicoScope 3403D')])

        self.assertEqual(entries[0]['model'], 'PicoScope 3403D')

    def test_channel_counts_across_every_supported_series(self):
        """The second digit of a Pico model number is the channel count."""
        cases = {
            'PicoScope 2204A': 2, 'PicoScope 2205A': 2, 'PicoScope 2405A': 4,
            'PicoScope 3204D': 2, 'PicoScope 3403D': 4,
            'PicoScope 4224': 2, 'PicoScope 4424': 4,
            'PicoScope 5242D': 2, 'PicoScope 5442D': 4,
        }
        for model, expected in cases.items():
            with self.subTest(model=model):
                self.assertEqual(
                    self.scanner._pico_channel_count(model), expected)

    def test_the_vendor_wide_entry_is_not_in_the_vidpid_table(self):
        """It has no product id, so it must not shadow a real one."""
        pico_entries = [name for (vid, _pid), name
                        in self.scanner._VIDPID_TO_NAME.items()
                        if vid == PICO_VID]

        self.assertEqual(pico_entries, ['Picoscope_2000'])
        self.assertEqual(self.scanner._VID_ONLY_VENDORS[PICO_VID], 'Picoscope')


class UdevGrantsAccessToEveryPicoScope(unittest.TestCase):
    """The scanner finding a scope is useless if the box cannot open it.

    Detection and permission have to widen together: a per-PID udev rule with
    a vendor-wide scanner would list a scope that then fails with EACCES.
    """

    def test_the_rule_is_vendor_wide(self):
        rules_path = os.path.normpath(
            os.path.join(HERE, '..', '..', '..', 'box', 'udev_rules',
                         '99-instrument.rules'))
        with open(rules_path) as f:
            rules = f.read()

        pico_rules = [line for line in rules.splitlines()
                      if f'"{PICO_VID}"' in line and not line.startswith('#')]

        self.assertEqual(len(pico_rules), 1,
                         'expected exactly one Pico udev rule, got: %s'
                         % pico_rules)
        self.assertNotIn('idProduct', pico_rules[0],
                         'Pico udev rule is per-product, so scopes the '
                         'scanner now finds would fail to open')
        self.assertIn('GROUP="lager"', pico_rules[0])


if __name__ == '__main__':
    unittest.main()
