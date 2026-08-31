# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Paths built from client-supplied names stay under their own root.

Three places name a file after something that arrives off the wire: a binary
name on /binaries/add and /binaries/remove, a VISA address used as a device
lock key, and the tty path handed to serial_id. Each already reduces or
validates that value, and those reductions remain the real defence -- these
tests pin them. The containment check beside each join says where the result is
allowed to land, so a later change to a reduction cannot silently widen it.

Where a reduction makes containment unreachable, the test says so rather than
implying the check is what rejects.
"""

import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted):
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


for _dep in [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core', 'pigpio',
    'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
]:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.binaries import store  # noqa: E402
from lager.util.device_lock import DeviceLockManager  # noqa: E402

# Names that would leave the target directory if joined without a check.
ESCAPING_NAMES = ['../evil', 'a/b', 'a\\b', '../../etc/passwd', 'x/../../y']


class BinaryNamesStayInTheBinariesDir(unittest.TestCase):
    """``_validate_name`` is what rejects; containment states where it lands."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='lager_store_test_')
        self._real = store.HOST_BINARIES_DIR
        store.HOST_BINARIES_DIR = self.tmp

    def tearDown(self):
        store.HOST_BINARIES_DIR = self._real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_escaping_names_are_refused_on_add(self):
        for name in ESCAPING_NAMES:
            with self.subTest(name=name):
                with self.assertRaises(store.StoreError) as ctx:
                    store.add_binary(name, b'payload')
                self.assertEqual(ctx.exception.status, 400)

    def test_escaping_names_are_refused_on_remove(self):
        for name in ESCAPING_NAMES:
            with self.subTest(name=name):
                with self.assertRaises(store.StoreError) as ctx:
                    store.remove_binary(name)
                self.assertEqual(ctx.exception.status, 400)

    def test_nothing_is_written_outside_the_directory(self):
        before = set(os.listdir(os.path.dirname(self.tmp)))
        for name in ESCAPING_NAMES:
            try:
                store.add_binary(name, b'payload')
            except store.StoreError:
                pass
        self.assertEqual(set(os.listdir(os.path.dirname(self.tmp))), before)

    def test_an_ordinary_name_lands_inside_and_is_unchanged(self):
        result = store.add_binary('rt_newtmgr', b'payload')
        self.assertEqual(result['name'], 'rt_newtmgr')
        written = os.path.join(self.tmp, 'rt_newtmgr')
        self.assertTrue(os.path.isfile(written))
        self.assertTrue(
            os.path.normpath(written).startswith(self.tmp + os.sep))

    def test_names_with_spaces_and_punctuation_still_work(self):
        # The CLI forwards the basename of any local file, so the accepted set
        # must stay wider than the containment check's own character needs.
        for name in ('my tool', 'rt_newtmgr_v1.2', 'sign+firmware', 'a(b)'):
            with self.subTest(name=name):
                store.add_binary(name, b'x')
                self.assertTrue(os.path.isfile(os.path.join(self.tmp, name)))


class DeviceLockPathsStayInTheLockDir(unittest.TestCase):
    """The slug makes escape impossible; the check pins where locks land."""

    def setUp(self):
        self.mgr = DeviceLockManager(lock_subdir='lager_test_locks')

    def tearDown(self):
        shutil.rmtree(self.mgr.lock_dir, ignore_errors=True)

    def test_hostile_addresses_produce_a_path_inside_the_lock_dir(self):
        for address in ('../../etc/passwd', 'USB0::0x1::0x2::../x::INSTR',
                        'a/b/c', '..'):
            with self.subTest(address=address):
                path = self.mgr._get_lock_path(address)
                self.assertTrue(
                    os.path.normpath(path).startswith(self.mgr.lock_dir + os.sep),
                    msg=f'{address!r} produced {path!r}')
                self.assertNotIn(os.sep, os.path.basename(path))

    def test_a_real_address_is_reduced_the_documented_way(self):
        addr = 'USB0::0x05E6::0x2281::AB12345::INSTR'
        expected = re.sub(r'[^a-zA-Z0-9_-]', '_', addr)
        self.assertEqual(
            self.mgr._get_lock_path(addr),
            os.path.join(self.mgr.lock_dir, f'device_{expected}.lock'))

    def test_acquiring_and_releasing_a_hostile_address_touches_nothing_outside(self):
        outside = set(os.listdir(os.path.dirname(self.mgr.lock_dir)))
        self.assertTrue(self.mgr.acquire_lock('../../escape', timeout=1.0))
        self.mgr.release_lock('../../escape')
        self.assertEqual(
            set(os.listdir(os.path.dirname(self.mgr.lock_dir))), outside)


if __name__ == '__main__':
    unittest.main()
