# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for J-Link discovery in ``box/lager/http_handlers/usb_scanner.py``.

SEGGER ships J-Links under many USB product IDs. The scanner used to match
an exact VID:PID table, so a probe enumerating under an unlisted PID (an
on-board J-Link OB, ``1366:1015`` or ``1366:1051``) never appeared in
``lager instruments``, the nets TUI, or ``lager nets add``, even though the
udev rule grants access by vendor ID alone. The scanner now treats any VID
``0x1366`` device as a J-Link debug instrument: listed PIDs keep their model
names, and any other PID is reported as ``J-Link``.
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
    spec = importlib.util.spec_from_file_location('usb_scanner_jlink_under_test', SCANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules['usb_scanner_jlink_under_test'] = module
    spec.loader.exec_module(module)
    return module


class TestJLinkDiscovery(unittest.TestCase):
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
        self._next_port = 1

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

    def _plug(self, vid, pid, serial):
        """Add one USB device directory with idVendor/idProduct/serial."""
        dev = os.path.join(self.sys_bus, f'1-{self._next_port}')
        self._next_port += 1
        os.makedirs(dev)
        for name, value in (('idVendor', vid), ('idProduct', pid), ('serial', serial)):
            with open(os.path.join(dev, name), 'w') as f:
                f.write(value + '\n')

    def _scan_one(self, vid, pid, serial='000123456789'):
        self._plug(vid, pid, serial)
        return self.scanner.scan_usb()

    def _assert_jlink_debug(self, entry, name, pid, serial='000123456789'):
        self.assertEqual(entry['name'], name)
        self.assertEqual(entry['net_type'], ['debug'])
        self.assertEqual(entry['channels'], {'debug': ['DEVICE_TYPE']})
        self.assertEqual(entry['address'],
                         f'USB0::0x1366::0x{pid.upper()}::{serial}::INSTR')

    def test_onboard_jlink_ob_1015_is_a_debug_instrument(self):
        [entry] = self._scan_one('1366', '1015')
        self._assert_jlink_debug(entry, 'J-Link_OB', '1015')

    def test_onboard_jlink_ob_1051_is_a_debug_instrument(self):
        [entry] = self._scan_one('1366', '1051')
        self._assert_jlink_debug(entry, 'J-Link_OB_2VCOM', '1051')

    def test_jlink_ob_1025_falls_back_to_jlink(self):
        # A J-Link OB variant (two VCOMs + MSD) that has no named entry: it
        # is found through the vendor fallback, not the PID table.
        self.assertNotIn(('1366', '1025'), self.scanner._VIDPID_TO_NAME)
        [entry] = self._scan_one('1366', '1025')
        self._assert_jlink_debug(entry, 'J-Link', '1025')

    def test_unlisted_segger_pid_falls_back_to_jlink(self):
        [entry] = self._scan_one('1366', 'abcd')
        self._assert_jlink_debug(entry, 'J-Link', 'abcd')
        # The record keeps the PID the device actually reported.
        self.assertEqual(entry['pid'], 'abcd')

    def test_listed_pids_keep_their_model_names(self):
        listed = {
            '1024': 'J-Link',
            '0101': 'J-Link_Plus',
            '1020': 'J-Link_Base_Compact',
            '0503': 'Flasher_ARM',
            '0105': 'J-Link_Flasher_Pro',
        }
        for i, pid in enumerate(listed):
            self._plug('1366', pid, f'SN{i}')
        by_pid = {e['pid']: e for e in self.scanner.scan_usb()}
        self.assertEqual({pid: e['name'] for pid, e in by_pid.items()}, listed)
        for i, pid in enumerate(listed):
            self._assert_jlink_debug(by_pid[pid], listed[pid], pid, serial=f'SN{i}')

    def test_unknown_pid_from_another_vendor_is_skipped(self):
        # 0483 is a listed vendor (ST-Link) but this PID is not in the table;
        # only SEGGER gets the vendor-wide fallback.
        self.assertEqual(self._scan_one('0483', 'dead'), [])

    def test_unknown_vendor_is_skipped(self):
        self.assertEqual(self._scan_one('abcd', '1015'), [])

    def test_fallback_scan_does_not_mutate_the_catalog(self):
        self._scan_one('1366', 'abcd')
        self.assertEqual(self.scanner.CHANNEL_MAPS['J-Link'],
                         {'debug': ['DEVICE_TYPE']})


if __name__ == '__main__':
    unittest.main()
