# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``box/lager/http_handlers/usb_scanner.py`` UART
enumeration with and without a USB serial.

Regression coverage for the FT4232H bug: when the chip has no
programmed EEPROM serial, the scanner used to advertise UART channels
as bare interface indices ``"0"/"1"/"2"/"3"`` from the static
``CHANNEL_MAPS`` fallback. Those values landed in the saved net's
``pin`` field and broke the box-side dispatcher's ``/sys/class/tty``
lookup. The fix:

  * ``_get_ttys_for_usb_device`` walks ``/sys/class/tty`` matching by
    USB sysfs path (not by serial), returning real ``/dev/tty*`` entries
    even for serial-less chips.
  * ``CHANNEL_MAPS`` no longer hard-codes ``"0"/"1"/"2"/"3"`` for the
    FT4232H — UART channels are populated at scan time, or the role is
    dropped entirely when no tty can be enumerated.
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


def _load_scanner():
    """Load ``usb_scanner.py`` standalone (no ``lager.*`` package deps)."""
    spec = importlib.util.spec_from_file_location('usb_scanner_under_test', SCANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules['usb_scanner_under_test'] = module
    spec.loader.exec_module(module)
    return module


class _FakeSysfs:
    """Builds a throwaway sysfs tree that mimics how the Linux kernel
    exposes an FT4232H: one parent USB device with idVendor/idProduct
    (and optionally a ``serial`` file), four child interface dirs
    ``X-Y:1.N``, each linking to its own ``ttyUSB`` under
    ``/sys/class/tty/``.

    Designed to be small enough that the test can audit by reading; we
    only build what the scanner actually walks.
    """

    def __init__(self, root: str, *, vid: str, pid: str, serial: str | None,
                 num_interfaces: int = 4):
        self.root = root
        self.sys_bus = os.path.join(root, 'sys', 'bus', 'usb', 'devices')
        self.sys_class_tty = os.path.join(root, 'sys', 'class', 'tty')
        self.sys_devices = os.path.join(root, 'sys', 'devices')
        os.makedirs(self.sys_bus)
        os.makedirs(self.sys_class_tty)
        os.makedirs(self.sys_devices)

        # The "real" USB device directory lives under /sys/devices and
        # is symlinked from /sys/bus/usb/devices/<name>.
        self.bus_name = '3-1'
        self.device_dir = os.path.join(self.sys_devices, self.bus_name)
        os.makedirs(self.device_dir)
        with open(os.path.join(self.device_dir, 'idVendor'), 'w') as f:
            f.write(vid + '\n')
        with open(os.path.join(self.device_dir, 'idProduct'), 'w') as f:
            f.write(pid + '\n')
        if serial is not None:
            with open(os.path.join(self.device_dir, 'serial'), 'w') as f:
                f.write(serial + '\n')

        # Bus-side symlink the scanner iterates over.
        os.symlink(self.device_dir, os.path.join(self.sys_bus, self.bus_name))

        # Four interface child dirs + tty nodes that link back into them.
        self.tty_paths = []
        for iface in range(num_interfaces):
            iface_dir = os.path.join(self.device_dir, f'{self.bus_name}:1.{iface}')
            os.makedirs(iface_dir)
            tty_name = f'ttyUSB{iface}'
            tty_holder = os.path.join(iface_dir, tty_name)
            os.makedirs(tty_holder)
            tty_class_link = os.path.join(self.sys_class_tty, tty_name)
            # /sys/class/tty/ttyUSB<N>/device → the iface dir
            os.makedirs(tty_class_link)
            os.symlink(iface_dir, os.path.join(tty_class_link, 'device'))
            self.tty_paths.append(f'/dev/{tty_name}')

    @property
    def usb_device_path(self):
        from pathlib import Path
        return Path(os.path.join(self.sys_bus, self.bus_name))


class TestSerialLessTtyWalk(unittest.TestCase):
    """``_get_ttys_for_usb_device`` is the fallback used when an FTDI
    has no programmed serial. It must enumerate every interface tty
    bound to the given sysfs USB device, with the correct interface
    index, even without ever reading a ``serial`` file."""

    @classmethod
    def setUpClass(cls):
        cls.scanner = _load_scanner()

    def _with_fake_sysfs(self, **kwargs):
        """Return (tmpdir, fake_sysfs) and patch ``Path`` lookups inside
        the scanner module so it sees our fake tree."""
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: _rmtree(tmpdir))
        fake = _FakeSysfs(tmpdir, **kwargs)

        # Swap the scanner's ``Path`` binding with a thin shim that
        # rewrites the ``/sys/class/tty`` root to our fake tree. The
        # scanner only ever uses ``Path`` as a constructor, so a
        # function shim works fine — instances returned are real
        # ``pathlib.Path`` objects with .resolve / .iterdir intact.
        import pathlib

        real_path = pathlib.Path

        def _path_shim(*args, **kw):
            if args and args[0] == '/sys/class/tty':
                return real_path(fake.sys_class_tty)
            return real_path(*args, **kw)

        original_Path = self.scanner.Path
        self.scanner.Path = _path_shim  # type: ignore[attr-defined]
        self.addCleanup(lambda: setattr(self.scanner, 'Path', original_Path))

        return tmpdir, fake

    def test_enumerates_all_interfaces_without_serial(self):
        _tmp, fake = self._with_fake_sysfs(
            vid='0403', pid='6011', serial=None, num_interfaces=4,
        )

        ttys = self.scanner._get_ttys_for_usb_device(fake.usb_device_path)
        # Drop the optional ``with_timeout`` envelope if it returned None
        self.assertIsNotNone(ttys)
        ifaces = sorted(t['interface'] for t in ttys)
        self.assertEqual(ifaces, [0, 1, 2, 3])
        paths = sorted(t['path'] for t in ttys)
        self.assertEqual(paths, sorted(fake.tty_paths))

    def test_returns_empty_for_missing_device(self):
        _tmp, _fake = self._with_fake_sysfs(
            vid='0403', pid='6011', serial=None,
        )
        from pathlib import Path
        ttys = self.scanner._get_ttys_for_usb_device(
            Path('/does/not/exist/3-99')
        )
        self.assertEqual(ttys, [])

    def test_serial_keyed_walk_still_works_with_serial(self):
        _tmp, fake = self._with_fake_sysfs(
            vid='0403', pid='6011', serial='FT5XYZAB', num_interfaces=2,
        )

        ttys = self.scanner._get_ttys_for_usb_serial('FT5XYZAB')
        self.assertIsNotNone(ttys)
        ifaces = sorted(t['interface'] for t in ttys)
        self.assertEqual(ifaces, [0, 1])

    def test_serial_keyed_walk_misses_when_serial_mismatches(self):
        _tmp, _fake = self._with_fake_sysfs(
            vid='0403', pid='6011', serial='FT5XYZAB',
        )

        ttys = self.scanner._get_ttys_for_usb_serial('different-serial')
        self.assertEqual(ttys, [])


class TestChannelMapDefaults(unittest.TestCase):
    """Static fallback list must not contain bare interface indices for
    FT2232H / FT4232H any more; those values used to leak into the
    saved net's ``pin`` field and break the dispatcher."""

    @classmethod
    def setUpClass(cls):
        cls.scanner = _load_scanner()

    def test_ft4232h_uart_default_is_empty(self):
        self.assertEqual(self.scanner.CHANNEL_MAPS['FTDI_FT4232H']['uart'], [])

    def test_ft2232h_uart_default_is_empty(self):
        self.assertEqual(self.scanner.CHANNEL_MAPS['FTDI_FT2232H']['uart'], [])

    def test_ft232h_uart_default_is_empty(self):
        # Pre-existing behaviour; pin it so a future refactor doesn't
        # silently regress the policy.
        self.assertEqual(self.scanner.CHANNEL_MAPS['FTDI_FT232H']['uart'], [])


def _rmtree(path):
    import shutil
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass


if __name__ == '__main__':
    unittest.main()
