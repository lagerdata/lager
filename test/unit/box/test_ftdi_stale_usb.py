# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""An FTDI part re-plugged under a running process can be opened again.

The process's shared libusb context never learns the part's new bus address,
and every open failed until the container restarted. These tests run without
pyusb, pyftdi or hardware: the libusb contexts and the failing open are fakes
shaped like what the bench showed.
"""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from lager.exceptions import GPIOBackendError, I2CBackendError, SPIBackendError
from lager.io.gpio.ft232h_gpio import FT232HGPIO
from lager.protocols.i2c.ft232h_i2c import FT232HI2C
from lager.protocols.spi.ft232h_spi import FT232HSPI
from lager.util import ftdi_usb

FT4232H = '6011'


class _UsbError(Exception):
    """Shaped like ``usb.core.USBError``: an ``errno`` attribute, no OSError."""

    def __init__(self, err, msg):
        super().__init__(f"[Errno {err}] {msg}")
        self.errno = err


def _enodev():
    return _UsbError(errno.ENODEV,
                     "No such device (it may have been disconnected)")


class UsbToolsError(Exception):
    """Same name as ``pyftdi.usbtools.UsbToolsError``, raised by URL lookup."""


def _no_serial():
    return UsbToolsError("No USB device with S/N REV01_")


def _no_langid():
    return ValueError("The device has no langid (permission issue, no string "
                      "descriptors supported or device error)")


# ---------------------------------------------------------------- libusb fakes

class _Dev:
    def __init__(self, pid, bus, address):
        self.idProduct, self.bus, self.address = pid, bus, address


class _Backend:
    """A libusb context: the FTDI devices it lists."""

    def __init__(self, devices):
        self.devices = devices


class _FakeUsb:
    """Fake ``usb``/``usb.core``/``usb.backend.libusb1``/``pyftdi`` and sysfs.

    ``shared`` is what the process's context (pyusb's ``_lib_object``) lists;
    ``kernel`` is what sysfs lists; ``fresh`` is what a new ``libusb_init``
    would list. ``created`` counts the contexts the code under test made.
    """

    def __init__(self, test, shared, kernel, fresh=None):
        self.created = []
        self.libusb1 = types.ModuleType('usb.backend.libusb1')
        self.libusb1._lib = object()
        self.libusb1._lib_object = _Backend(shared)
        self.libusb1.get_backend = lambda: self.libusb1._lib_object

        def new_context(lib):
            ctx = _Backend(kernel if fresh is None else fresh)
            self.created.append(ctx)
            return ctx

        self.libusb1._LibUSB = new_context
        core = types.ModuleType('usb.core')
        core.find = lambda find_all, idVendor, backend: list(backend.devices)
        usb = types.ModuleType('usb')
        usb.core = core
        backend_pkg = types.ModuleType('usb.backend')
        backend_pkg.libusb1 = self.libusb1
        usb.backend = backend_pkg
        self.usbtools = mock.Mock()
        pyftdi = types.ModuleType('pyftdi')
        for p in (
            mock.patch.dict(sys.modules, {
                'usb': usb, 'usb.core': core, 'usb.backend': backend_pkg,
                'usb.backend.libusb1': self.libusb1,
                'pyftdi': pyftdi, 'pyftdi.usbtools': self.usbtools,
            }),
            mock.patch.object(ftdi_usb, 'SYSFS_USB_DEVICES',
                              self._sysfs(test, kernel)),
            mock.patch.object(ftdi_usb, '_RETIRED', []),
        ):
            p.start()
            test.addCleanup(p.stop)

    @staticmethod
    def _sysfs(test, devices):
        """A /sys/bus/usb/devices tree: the FTDI devices, a hub, an interface."""
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        entries = [(f'1-2.{i + 3}', '0403', f'{d.idProduct:04x}', d.bus,
                    d.address) for i, d in enumerate(devices)]
        entries.append(('1-2', '2400', '8011', 1, 2))  # the hub: not FTDI
        for name, vid, pid, bus, dev in entries:
            path = os.path.join(tmp.name, name)
            os.mkdir(path)
            for fname, value in (('idVendor', vid), ('idProduct', pid),
                                 ('busnum', str(bus)), ('devnum', str(dev))):
                with open(os.path.join(path, fname), 'w') as f:
                    f.write(value + '\n')
        os.mkdir(os.path.join(tmp.name, '1-2.3:1.0'))  # interface: no idVendor
        return tmp.name


OLD = [_Dev(0x6011, 1, 41)]
NEW = [_Dev(0x6011, 1, 42)]


class StaleCheckTests(unittest.TestCase):

    def test_old_address_in_shared_context_is_stale(self):
        """The bench: shared context at 41, sysfs at 42."""
        _FakeUsb(self, shared=OLD, kernel=NEW)
        self.assertTrue(ftdi_usb._shared_context_is_stale())

    def test_emptied_shared_context_is_stale(self):
        """After pyftdi's flush alone, the shared context listed nothing."""
        _FakeUsb(self, shared=[], kernel=NEW)
        self.assertTrue(ftdi_usb._shared_context_is_stale())

    def test_matching_lists_are_not_stale(self):
        _FakeUsb(self, shared=NEW, kernel=[_Dev(0x6011, 1, 42)])
        self.assertFalse(ftdi_usb._shared_context_is_stale())

    def test_part_really_unplugged_is_not_stale(self):
        _FakeUsb(self, shared=[], kernel=[])
        self.assertFalse(ftdi_usb._shared_context_is_stale())

    def test_the_check_creates_no_libusb_context(self):
        """A context made only to compare against, then garbage-collected,
        is what crashed hardware_service (libusb_exit before the last
        libusb_unref_device)."""
        for shared, kernel in ((NEW, NEW), (OLD, NEW)):
            with self.subTest(stale=shared is OLD):
                fake = _FakeUsb(self, shared=shared, kernel=kernel)
                ftdi_usb._shared_context_is_stale()
                self.assertEqual(fake.created, [])

    def test_no_backend_is_not_stale(self):
        fake = _FakeUsb(self, shared=OLD, kernel=NEW)
        fake.libusb1.get_backend = lambda: None
        self.assertFalse(ftdi_usb._shared_context_is_stale())

    def test_unreadable_sysfs_is_not_stale(self):
        _FakeUsb(self, shared=OLD, kernel=NEW)
        with mock.patch.object(ftdi_usb, 'SYSFS_USB_DEVICES', '/nonexistent'):
            self.assertFalse(ftdi_usb._shared_context_is_stale())

    def test_without_pyusb_is_not_stale(self):
        with mock.patch.dict(sys.modules, {'usb': None, 'usb.core': None,
                                           'usb.backend': None,
                                           'usb.backend.libusb1': None}):
            self.assertFalse(ftdi_usb._shared_context_is_stale())


class RefreshTests(unittest.TestCase):

    def test_fresh_context_becomes_shared_and_pyftdi_is_flushed(self):
        fake = _FakeUsb(self, shared=OLD, kernel=NEW)
        ftdi_usb._refresh_context()
        self.assertEqual(len(fake.created), 1)
        self.assertIs(fake.libusb1.get_backend(), fake.created[0])
        self.assertEqual(fake.libusb1.get_backend().devices, NEW)
        fake.usbtools.UsbTools.flush_cache.assert_called_once_with()

    def test_replaced_context_is_kept_alive(self):
        fake = _FakeUsb(self, shared=OLD, kernel=NEW)
        old = fake.libusb1.get_backend()
        ftdi_usb._refresh_context()
        self.assertEqual(ftdi_usb._RETIRED, [old])

    def test_after_refresh_the_check_agrees(self):
        _FakeUsb(self, shared=OLD, kernel=NEW)
        ftdi_usb._refresh_context()
        self.assertFalse(ftdi_usb._shared_context_is_stale())


class OpenFreshTests(unittest.TestCase):

    def setUp(self):
        self.stale = mock.Mock(return_value=True)
        self.refresh = mock.Mock()
        for p in (mock.patch.object(ftdi_usb, '_shared_context_is_stale',
                                    self.stale),
                  mock.patch.object(ftdi_usb, '_refresh_context',
                                    self.refresh)):
            p.start()
            self.addCleanup(p.stop)

    def test_every_bench_error_shape_is_retried_after_a_refresh(self):
        for err in (_enodev, _no_serial, _no_langid):
            with self.subTest(err=err.__name__):
                self.refresh.reset_mock()
                calls = []

                def open_fn():
                    calls.append(self.refresh.call_count)
                    if len(calls) == 1:
                        raise err()
                    return 'ctrl'

                self.assertEqual(ftdi_usb.open_fresh(open_fn), 'ctrl')
                self.assertEqual(calls, [0, 1], 'retry runs after refresh')

    def test_failure_with_a_current_device_list_is_raised_untouched(self):
        self.stale.return_value = False
        err = _UsbError(errno.EBUSY, "Resource busy")
        open_fn = mock.Mock(side_effect=err)
        with self.assertRaises(_UsbError) as cm:
            ftdi_usb.open_fresh(open_fn)
        self.assertIs(cm.exception, err)
        self.assertEqual(open_fn.call_count, 1)
        self.refresh.assert_not_called()

    def test_failed_refresh_raises_the_open_error(self):
        err = _no_serial()
        self.refresh.side_effect = OSError("libusb_init failed")
        open_fn = mock.Mock(side_effect=err)
        with self.assertRaises(UsbToolsError) as cm:
            ftdi_usb.open_fresh(open_fn)
        self.assertIs(cm.exception, err)
        self.assertEqual(open_fn.call_count, 1)

    def test_second_failure_is_raised(self):
        second = _enodev()
        open_fn = mock.Mock(side_effect=[_enodev(), second])
        with self.assertRaises(_UsbError) as cm:
            ftdi_usb.open_fresh(open_fn)
        self.assertIs(cm.exception, second)
        self.assertEqual(open_fn.call_count, 2)

    def test_success_checks_nothing(self):
        self.assertEqual(ftdi_usb.open_fresh(lambda: 1), 1)
        self.stale.assert_not_called()

    def test_refresh_is_logged(self):
        log = []
        ftdi_usb.open_fresh(mock.Mock(side_effect=[_no_serial(), 'c']),
                            log=log.append)
        self.assertEqual(len(log), 1)
        self.assertIn('stale', log[0])


# ---------------------------------------------------------------- drivers

class _FakeFtdi:
    usb_dev = None

    def set_latency_timer(self, latency):
        pass


def _controller_class(failures, error=_no_serial):
    """A fake pyftdi controller whose first ``failures`` configures fail."""

    class Ctrl:
        made = []

        def __init__(self):
            self.direction = 0
            Ctrl.made.append(self)

        def configure(self, url, **kwargs):
            if len(Ctrl.made) <= failures:
                raise error()

        def write(self, value):
            pass

        def set_direction(self, pins, direction):
            self.direction = (self.direction & ~pins) | (pins & direction)

        def read(self):
            return 0

        def get_port(self, **kwargs):
            return 'port'

        def close(self):
            pass

    return Ctrl


class _RefreshPatched(unittest.TestCase):
    """The stale check says "stale" and the refresh is recorded."""

    def setUp(self):
        self.install = mock.Mock()
        for p in (mock.patch.object(ftdi_usb, '_shared_context_is_stale',
                                    lambda: True),
                  mock.patch.object(ftdi_usb, '_refresh_context',
                                    self.install)):
            p.start()
            self.addCleanup(p.stop)


class GpioReplugTests(_RefreshPatched):
    """The GPIO driver: a stale open is retried from the probe onwards."""

    def setUp(self):
        super().setUp()
        from lager.io.gpio import ft232h_gpio
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        FT232HGPIO._controllers.clear()
        self.addCleanup(FT232HGPIO._controllers.clear)
        self.probes = []

        def probe(url):
            self.probes.append(self.install.call_count)
            return 77, None

        self.Ctrl = _controller_class(failures=1)
        for p in (
            mock.patch.object(FT232HGPIO, '_CACHE_FILE',
                              os.path.join(tmp.name, 'cache.json')),
            mock.patch.object(FT232HGPIO, '_LEGACY_CACHE_FILE',
                              os.path.join(tmp.name, 'legacy.json')),
            mock.patch.object(FT232HGPIO, '_get_pyftdi',
                              staticmethod(lambda: self.Ctrl)),
            mock.patch.object(FT232HGPIO, '_probe_channel',
                              staticmethod(probe)),
            mock.patch.object(FT232HGPIO, '_make_ftdi',
                              staticmethod(_FakeFtdi)),
            mock.patch.object(ft232h_gpio.atexit, 'register'),
        ):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def net():
        return FT232HGPIO('g', 6, serial='FT4', pid=FT4232H, interface='C')

    def test_open_succeeds_after_a_replug(self):
        ctrl = self.net()._ensure_open()
        self.assertIs(ctrl, self.Ctrl.made[-1])
        self.assertEqual(len(self.Ctrl.made), 2)
        self.install.assert_called_once()

    def test_probe_runs_again_after_the_refresh(self):
        """The first probe's lookup was stale; the held/setup decision must
        come from a probe of the re-enumerated part."""
        self.net()._ensure_open()
        self.assertEqual(self.probes, [0, 1])

    def test_only_the_working_controller_is_shared(self):
        ctrl = self.net()._ensure_open()
        self.assertIs(FT232HGPIO._controllers['ftdi://ftdi:4232h:FT4/3'], ctrl)

    def test_input_works_without_a_restart(self):
        """What `lager gpi` hit on the bench: GPIOBackendError, no retry."""
        self.assertEqual(self.net().input(), 0)

    def test_part_still_gone_reports_the_open_failure(self):
        self.Ctrl = _controller_class(failures=2)
        with self.assertRaises(GPIOBackendError) as cm:
            self.net()._ensure_open()
        self.assertIn('Failed to open FT232H GPIO', str(cm.exception))
        self.assertEqual(FT232HGPIO._controllers, {})


class I2cSpiReplugTests(_RefreshPatched):
    """I2C and SPI nets on the same part hit the same stale context."""

    def setUp(self):
        super().setUp()
        for mod in ('lager.protocols.i2c.ft232h_i2c',
                    'lager.protocols.spi.ft232h_spi'):
            p = mock.patch(f'{mod}.atexit.register')
            p.start()
            self.addCleanup(p.stop)

    def _i2c(self, failures):
        Ctrl = _controller_class(failures)
        drv = FT232HI2C(serial='FT4', pid=FT4232H, interface='B')
        drv._get_pyftdi = lambda: (Ctrl, Exception)
        return drv, Ctrl

    def _spi(self, failures):
        Ctrl = _controller_class(failures)
        drv = FT232HSPI(serial='FT4', pid=FT4232H, interface='B')
        drv._get_pyftdi = lambda: Ctrl
        return drv, Ctrl

    def test_i2c_open_succeeds_after_a_replug(self):
        drv, Ctrl = self._i2c(failures=1)
        self.assertIs(drv._ensure_open(), Ctrl.made[-1])
        self.install.assert_called_once()

    def test_spi_open_succeeds_after_a_replug(self):
        drv, Ctrl = self._spi(failures=1)
        self.assertEqual(drv._ensure_open(), 'port')
        self.assertIs(drv._controller, Ctrl.made[-1])
        self.install.assert_called_once()

    def test_i2c_part_still_gone_keeps_its_error(self):
        drv, _ = self._i2c(failures=2)
        with self.assertRaises(I2CBackendError) as cm:
            drv._ensure_open()
        self.assertIn('Failed to open FT232H I2C', str(cm.exception))

    def test_spi_part_still_gone_keeps_its_error(self):
        drv, _ = self._spi(failures=2)
        with self.assertRaises(SPIBackendError) as cm:
            drv._ensure_open()
        self.assertIn('Failed to open FT232H SPI', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
