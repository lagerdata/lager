# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""The FTDI GPIO/I2C/SPI drivers, addressed by part and by channel.

``test_ftdi_url.py`` covers the URL helper in isolation. This covers the three
drivers that call it, plus the two collisions that only appear once a second
interface is usable:

* the GPIO state cache was keyed ``serial:pin``, so AD0 on channel A and AD0
  on channel B of one chip shared an entry and clobbered each other between
  CLI invocations;
* ACBUS pins (8-15) were accepted on any part, but the FT4232H has no ACBUS.

Every driver is constructed directly -- no pyftdi, no hardware. ``_build_url``
and ``_get_cache_key`` are pure.
"""

from __future__ import annotations

import enum
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from lager.exceptions import GPIOBackendError
from lager.io.gpio.ft232h_gpio import FT232HGPIO
from lager.protocols.i2c.ft232h_i2c import FT232HI2C
from lager.protocols.spi.ft232h_spi import FT232HSPI
from lager.util.ftdi_url import FtdiUrlError

FT232H, FT2232H, FT4232H = '6014', '6010', '6011'


class UrlBackCompatTests(unittest.TestCase):
    """What every existing FTDI net on every bench produces today."""

    def test_gpio_default_url_is_unchanged(self):
        self.assertEqual(
            FT232HGPIO('g', 4, serial='FT123')._build_url(),
            'ftdi://ftdi:232h:FT123/1')

    def test_gpio_default_url_without_serial_is_unchanged(self):
        self.assertEqual(FT232HGPIO('g', 4)._build_url(), 'ftdi://ftdi:232h/1')

    def test_i2c_default_url_is_unchanged(self):
        self.assertEqual(
            FT232HI2C(serial='FT123')._build_url(), 'ftdi://ftdi:232h:FT123/1')

    def test_spi_default_url_is_unchanged(self):
        self.assertEqual(
            FT232HSPI(serial='FT123')._build_url(), 'ftdi://ftdi:232h:FT123/1')


class UrlAddressingTests(unittest.TestCase):

    def test_gpio_reaches_every_ft4232h_channel(self):
        for letter, slot in (('A', 1), ('B', 2), ('C', 3), ('D', 4)):
            with self.subTest(letter=letter):
                drv = FT232HGPIO('g', 4, serial='FT4', pid=FT4232H,
                                 interface=letter)
                self.assertEqual(drv._build_url(),
                                 f'ftdi://ftdi:4232h:FT4/{slot}')

    def test_control_lines_on_a_non_mpsse_channel(self):
        """A 4-channel FTDI driving control lines on a channel with no MPSSE.

        The combination that was unreachable: neither the part nor the
        channel could be named, and this one is not an MPSSE channel either.
        """
        drv = FT232HGPIO('ctrl', 5, serial='FT4', pid=FT4232H, interface='C')
        self.assertEqual(drv._build_url(), 'ftdi://ftdi:4232h:FT4/3')

    def test_i2c_and_spi_reach_channel_b(self):
        self.assertEqual(
            FT232HI2C(serial='FT4', pid=FT4232H, interface='B')._build_url(),
            'ftdi://ftdi:4232h:FT4/2')
        self.assertEqual(
            FT232HSPI(serial='FT4', pid=FT4232H, interface='B')._build_url(),
            'ftdi://ftdi:4232h:FT4/2')

    def test_ft2232h_gpio_opens_at_all(self):
        """Previously advertised by INSTRUMENT_NET_MAP and impossible to open."""
        self.assertEqual(
            FT232HGPIO('g', 4, serial='FT2', pid=FT2232H)._build_url(),
            'ftdi://ftdi:2232h:FT2/1')

    def test_explicit_url_is_used_verbatim(self):
        raw = 'ftdi://ftdi:4232h:FT4/3'
        for drv in (FT232HGPIO('g', 4, url=raw), FT232HI2C(url=raw),
                    FT232HSPI(url=raw)):
            with self.subTest(drv=type(drv).__name__):
                self.assertEqual(drv._build_url(), raw)


class MpsseConstraintTests(unittest.TestCase):
    """I2C/SPI need MPSSE; FT4232H C and D do not have it. GPIO does not care."""

    def test_i2c_refuses_a_non_mpsse_channel(self):
        with self.assertRaises(FtdiUrlError):
            FT232HI2C(serial='FT4', pid=FT4232H, interface='C')

    def test_spi_refuses_a_non_mpsse_channel(self):
        with self.assertRaises(FtdiUrlError):
            FT232HSPI(serial='FT4', pid=FT4232H, interface='D')

    def test_gpio_accepts_the_same_channel(self):
        drv = FT232HGPIO('g', 4, serial='FT4', pid=FT4232H, interface='C')
        self.assertEqual(drv._build_url(), 'ftdi://ftdi:4232h:FT4/3')

    def test_a_channel_the_part_does_not_have_is_refused(self):
        with self.assertRaises(FtdiUrlError):
            FT232HGPIO('g', 4, serial='FT1', pid=FT232H, interface='B')


class PinWidthTests(unittest.TestCase):

    def test_acbus_pin_rejected_on_the_ft4232h(self):
        """Bits 8-15 are not 'unused' there -- there is no ACBUS at all."""
        with self.assertRaises(GPIOBackendError) as ctx:
            FT232HGPIO('g', 'AC0', serial='FT4', pid=FT4232H)
        self.assertIn('ACBUS', str(ctx.exception))

    def test_acbus_pin_still_accepted_on_the_ft232h(self):
        self.assertEqual(FT232HGPIO('g', 'AC0')._pin_num, 8)

    def test_acbus_pin_still_accepted_on_the_ft2232h(self):
        self.assertEqual(
            FT232HGPIO('g', 'AC7', serial='FT2', pid=FT2232H)._pin_num, 15)

    def test_adbus_pins_fine_everywhere(self):
        for pid in (FT232H, FT2232H, FT4232H):
            with self.subTest(pid=pid):
                self.assertEqual(FT232HGPIO('g', 'AD7', pid=pid)._pin_num, 7)


class CacheKeyTests(unittest.TestCase):
    """AD0 on channel A and AD0 on channel B are different pins."""

    def test_key_includes_the_interface(self):
        a = FT232HGPIO('g', 0, serial='FT4', pid=FT4232H, interface='A')
        b = FT232HGPIO('g', 0, serial='FT4', pid=FT4232H, interface='B')
        self.assertNotEqual(a._get_cache_key(), b._get_cache_key())

    def test_default_interface_is_recorded_as_a(self):
        explicit = FT232HGPIO('g', 0, serial='FT4', pid=FT4232H, interface='A')
        default = FT232HGPIO('g', 0, serial='FT4', pid=FT4232H)
        self.assertEqual(explicit._get_cache_key(), default._get_cache_key())

    def test_two_channels_hold_independent_state(self):
        """The collision, end to end through the on-disk cache."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'cache.json')
            a = FT232HGPIO('g', 0, serial='FT4', pid=FT4232H, interface='A')
            b = FT232HGPIO('g', 0, serial='FT4', pid=FT4232H, interface='C')
            for drv in (a, b):
                drv._CACHE_FILE = path
                drv._LEGACY_CACHE_FILE = os.path.join(tmp, 'absent.json')

            a._set_cached_state(1, True)
            b._set_cached_state(0, True)

            self.assertEqual(a._get_cached_state()['value'], 1)
            self.assertEqual(b._get_cached_state()['value'], 0)
            with open(path) as f:
                self.assertEqual(len(json.load(f)), 2)


class _FakeFtdi:
    """Stands in for ``_HoldSafeFtdi``; records the latency it is given."""

    def __init__(self, log):
        self._log = log
        self.usb_dev = None

    def set_latency_timer(self, latency):
        self._log.append(('latency', latency))


class _FakeController:
    """Stands in for pyftdi's GpioAsyncController, recording every call.

    ``pins`` is the pin register. ``configure``/``write``/``set_direction``
    keep the pyftdi bookkeeping (``direction``) the driver reads.
    """

    instances = []

    def __init__(self):
        self.log = []
        self.direction = 0
        self.pins = 0xFF
        self._ftdi = None
        _FakeController.instances.append(self)

    def configure(self, url, direction=0):
        self.log.append(('configure', url, direction))
        self.direction = direction

    def write(self, value):
        self.log.append(('write', value))

    def set_direction(self, pins, direction):
        self.log.append(('set_direction', pins, direction))
        self.direction = (self.direction & ~pins) | (pins & direction)

    def read(self):
        self.log.append(('read',))
        return self.pins

    def close(self):
        self.log.append(('close',))


class _HoldSafeTestCase(unittest.TestCase):
    """Runs the open path against fakes: no pyftdi, no hardware.

    ``self.latency`` is what the probe finds on the channel: 16 after a
    power-up, ``HOLD_LATENCY`` on a channel the driver left driven.
    """

    latency = 16

    def setUp(self):
        from lager.io.gpio import ft232h_gpio
        self.mod = ft232h_gpio
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = os.path.join(self.tmp.name, 'cache.json')
        self.legacy_path = os.path.join(self.tmp.name, 'legacy.json')
        _FakeController.instances = []
        FT232HGPIO._controllers.clear()

        patches = [
            mock.patch.object(FT232HGPIO, '_CACHE_FILE', self.cache_path),
            mock.patch.object(FT232HGPIO, '_LEGACY_CACHE_FILE',
                              self.legacy_path),
            mock.patch.object(FT232HGPIO, '_get_pyftdi',
                              staticmethod(lambda: _FakeController)),
            mock.patch.object(FT232HGPIO, '_probe_channel',
                              staticmethod(lambda url: (self.latency, None))),
            mock.patch.object(FT232HGPIO, '_make_ftdi',
                              staticmethod(self._new_ftdi)),
            mock.patch.object(ft232h_gpio.atexit, 'register'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(FT232HGPIO._controllers.clear)
        self.addCleanup(self.tmp.cleanup)

    def _new_ftdi(self):
        self.ftdi_log = []
        return _FakeFtdi(self.ftdi_log)

    def write_cache(self, cache, path=None):
        with open(path or self.cache_path, 'w') as f:
            json.dump(cache, f)

    @staticmethod
    def net(pin, interface='C'):
        return FT232HGPIO('g', pin, serial='FT4', pid=FT4232H,
                          interface=interface)


class RestoreTests(_HoldSafeTestCase):
    """A channel found at power-up state gets the cached outputs back."""

    def test_restores_only_this_channels_pins(self):
        """Pin 1 of C and pin 1 of D used to overwrite each other."""
        self.write_cache({
            'FT4:C:1': {'value': 0, 'is_output': True},
            'FT4:C:2': {'value': 1, 'is_output': True},
            'FT4:D:1': {'value': 1, 'is_output': True},
            'FT4:D:5': {'value': 1, 'is_output': True},
        })
        ctrl = self.net(1)._ensure_open()
        self.assertEqual(ctrl.log[1:], [('write', 0b100),
                                        ('set_direction', 0b110, 0b110)])

    def test_inputs_are_not_restored_as_outputs(self):
        self.write_cache({
            'FT4:C:1': {'value': 1, 'is_output': True},
            'FT4:C:3': {'value': 1, 'is_output': False},
        })
        ctrl = self.net(1)._ensure_open()
        self.assertEqual(ctrl.direction, 0b10)

    def test_latch_written_before_direction(self):
        self.write_cache({'FT4:C:4': {'value': 1, 'is_output': True}})
        ctrl = self.net(4)._ensure_open()
        ops = [entry[0] for entry in ctrl.log]
        self.assertEqual(ops, ['configure', 'write', 'set_direction'])
        self.assertEqual(ctrl.log[0][2], 0, 'opened with every pin an input')

    def test_empty_cache_drives_nothing(self):
        ctrl = self.net(4)._ensure_open()
        self.assertEqual(ctrl.log, [('configure', 'ftdi://ftdi:4232h:FT4/3', 0)])

    def test_channel_marked_only_after_restore(self):
        self.write_cache({'FT4:C:4': {'value': 1, 'is_output': True}})
        self.net(4)._ensure_open()
        self.assertEqual(self.ftdi_log, [('latency', self.mod.HOLD_LATENCY)])


class HeldChannelTests(_HoldSafeTestCase):
    """A channel still marked as set up is opened without touching a pin."""

    latency = 77

    def test_held_channel_keeps_latch_and_direction(self):
        self.write_cache({
            'FT4:C:1': {'value': 0, 'is_output': True},
            'FT4:C:6': {'value': 1, 'is_output': True},
        })
        ctrl = self.net(1)._ensure_open()
        self.assertEqual(ctrl.log,
                         [('configure', 'ftdi://ftdi:4232h:FT4/3', 0b1000010)])

    def test_hold_latency_constant(self):
        self.assertEqual(self.mod.HOLD_LATENCY, self.latency)


class SharedControllerTests(_HoldSafeTestCase):

    def test_two_nets_on_one_channel_share_a_controller(self):
        a, b = self.net(1), self.net(2)
        self.assertIs(a._ensure_open(), b._ensure_open())
        self.assertEqual(len(_FakeController.instances), 1)

    def test_two_channels_do_not_share(self):
        self.assertIsNot(self.net(1, 'C')._ensure_open(),
                         self.net(1, 'D')._ensure_open())

    def test_one_net_does_not_turn_the_other_back_into_an_input(self):
        a, b = self.net(1), self.net(2)
        a.output(0)
        b.output(1)
        self.assertEqual(a._ensure_open().direction, 0b110)

    def test_close_releases_for_every_net_on_the_channel(self):
        a, b = self.net(1), self.net(2)
        first = a._ensure_open()
        b._close()
        self.assertEqual(first.log[-1], ('close',))
        self.assertIsNot(a._ensure_open(), first)

    def test_release_all_frees_channels_whose_driver_is_gone(self):
        """hardware_service's release before a script, after a cache drop."""
        c = self.net(1, 'C')._ensure_open()
        d = self.net(1, 'D')._ensure_open()
        import gc
        gc.collect()
        self.assertEqual(FT232HGPIO.release_all_channels(), 2)
        self.assertEqual((c.log[-1], d.log[-1]), (('close',), ('close',)))
        self.assertEqual(FT232HGPIO._controllers, {})

    def test_dropping_a_net_keeps_the_channel_open(self):
        a, b = self.net(1), self.net(2)
        ctrl = a._ensure_open()
        del a
        import gc
        gc.collect()
        self.assertIs(b._ensure_open(), ctrl)
        self.assertNotIn(('close',), ctrl.log)


class InputReadTests(_HoldSafeTestCase):

    def test_output_pin_is_read_from_the_register(self):
        """The cache said LOW while every pin floated HIGH."""
        net = self.net(1)
        net.output(0)
        ctrl = net._ensure_open()
        ctrl.pins = 0xFF  # the pin did not hold: report it
        del ctrl.log[:]
        self.assertEqual(net.input(), 1)
        self.assertNotIn('set_direction', [e[0] for e in ctrl.log])

    def test_output_pin_read_does_not_rewrite_the_cache(self):
        net = self.net(1)
        net.output(0)
        net._ensure_open().pins = 0xFF
        net.input()
        self.assertEqual(net._get_cached_state(),
                         {'value': 0, 'is_output': True})

    def test_input_pin_is_made_an_input(self):
        net = self.net(3)
        ctrl = net._ensure_open()
        ctrl.pins = 0b1000
        self.assertEqual(net.input(), 1)
        self.assertIn(('set_direction', 0b1000, 0), ctrl.log)


class CacheMigrationTests(_HoldSafeTestCase):

    def test_legacy_cache_is_read_while_the_new_one_is_missing(self):
        self.write_cache({'FT4:C:1': {'value': 1, 'is_output': True}},
                         path=self.legacy_path)
        self.assertEqual(self.net(1)._get_cached_state()['value'], 1)

    def test_first_save_moves_everything_to_the_new_path(self):
        self.write_cache({'FT4:C:1': {'value': 1, 'is_output': True}},
                         path=self.legacy_path)
        self.net(2)._set_cached_state(0, True)
        with open(self.cache_path) as f:
            self.assertEqual(set(json.load(f)), {'FT4:C:1', 'FT4:C:2'})

    def test_new_cache_wins_over_legacy(self):
        self.write_cache({'FT4:C:1': {'value': 1, 'is_output': True}},
                         path=self.legacy_path)
        self.write_cache({'FT4:C:1': {'value': 0, 'is_output': True}})
        self.assertEqual(self.net(1)._get_cached_state()['value'], 0)

    def test_corrupt_cache_reads_as_empty(self):
        with open(self.cache_path, 'w') as f:
            f.write('{not json')
        self.assertEqual(self.net(1)._load_cache(), {})


class PersistentPathTests(unittest.TestCase):

    def test_default_cache_path_survives_a_reboot(self):
        self.assertTrue(FT232HGPIO._CACHE_FILE.startswith('/etc/lager/'))
        self.assertEqual(FT232HGPIO._LEGACY_CACHE_FILE,
                         '/tmp/ft232h_gpio_cache.json')

    def test_driver_advertises_hold_safe(self):
        self.assertIs(FT232HGPIO.HOLD_SAFE, True)


class _StubFtdi:
    """The slice of pyftdi's ``Ftdi`` the subclass overrides, recording calls."""

    class BitMode(enum.IntEnum):
        RESET = 0x00
        BITBANG = 0x01

    def __init__(self):
        self.calls = []
        self._usb_dev = mock.Mock()
        self._index = 3

    @property
    def is_connected(self):
        return bool(self._usb_dev)

    def _is_pyusb_handle_active(self):
        return True

    def _reset_device(self):
        self.calls.append(('sio_reset',))

    def set_bitmode(self, bitmask, mode):
        self.calls.append(('set_bitmode', bitmask, mode))

    def close(self, freeze=False):
        self.calls.append(('stock_close', freeze))


class HoldSafeFtdiTests(unittest.TestCase):
    """The pyftdi subclass itself: no reset on open, release-only close.

    Built on a stub ``pyftdi`` (the suite mocks box-only libraries rather
    than installing them), so these run everywhere and check exactly what
    the overrides send and skip.
    """

    def setUp(self):
        from lager.io.gpio import ft232h_gpio
        self.release_interface = mock.Mock()
        self.release_device = mock.Mock()
        ftdi_mod = types.ModuleType('pyftdi.ftdi')
        ftdi_mod.Ftdi = _StubFtdi
        usbtools_mod = types.ModuleType('pyftdi.usbtools')
        usbtools_mod.UsbTools = mock.Mock(release_device=self.release_device)
        usb_util = types.ModuleType('usb.util')
        usb_util.release_interface = self.release_interface
        usb_mod = types.ModuleType('usb')
        usb_mod.util = usb_util
        for p in (
            mock.patch.dict(sys.modules, {
                'pyftdi': types.ModuleType('pyftdi'),
                'pyftdi.ftdi': ftdi_mod,
                'pyftdi.usbtools': usbtools_mod,
                'usb': usb_mod,
                'usb.util': usb_util,
            }),
            mock.patch.object(ft232h_gpio, '_hold_safe_ftdi_cls', None),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.ftdi = ft232h_gpio._hold_safe_ftdi()()

    def test_sio_reset_is_never_sent(self):
        self.ftdi._reset_device()
        self.assertEqual(self.ftdi.calls, [])

    def test_bit_mode_reset_is_never_sent(self):
        self.ftdi.set_bitmode(0, _StubFtdi.BitMode.RESET)
        self.assertEqual(self.ftdi.calls, [])

    def test_bitbang_mode_is_still_set(self):
        self.ftdi.set_bitmode(0x06, _StubFtdi.BitMode.BITBANG)
        self.assertEqual(self.ftdi.calls,
                         [('set_bitmode', 0x06, _StubFtdi.BitMode.BITBANG)])

    def test_close_only_releases_the_interface(self):
        dev = self.ftdi._usb_dev
        self.ftdi.close()
        self.release_interface.assert_called_once_with(dev, 2)
        self.release_device.assert_called_once_with(dev)
        self.assertEqual(self.ftdi.calls, [], 'no bit-mode reset, no stock close')
        dev.attach_kernel_driver.assert_not_called()
        self.assertFalse(self.ftdi.is_connected)

    def test_close_on_a_vanished_device_still_drops_it(self):
        dev = self.ftdi._usb_dev
        self.release_interface.side_effect = OSError('no such device')
        self.ftdi.close()
        self.release_device.assert_called_once_with(dev)
        self.assertFalse(self.ftdi.is_connected)

    def test_second_close_is_a_no_op(self):
        self.ftdi.close()
        self.ftdi.close()
        self.release_device.assert_called_once()


class AddressParsingTests(unittest.TestCase):
    """``_ftdi_address_parts`` -- what the net record actually hands over."""

    @staticmethod
    def _parts(address):
        from lager.nets.net import _ftdi_address_parts
        return _ftdi_address_parts(address)

    def test_visa_address_yields_serial_and_pid(self):
        """The PID was previously dropped, which is the whole bug."""
        self.assertEqual(
            self._parts('USB0::0x0403::0x6011::FT4ABCD::INSTR'),
            ('FT4ABCD', '0x6011', None))

    def test_empty_serial_slot_still_yields_the_pid(self):
        """Bare FTDI modules ship with an unburnt EEPROM and no serial."""
        serial, pid, url = self._parts('USB0::0x0403::0x6011::::INSTR')
        self.assertIsNone(serial)
        self.assertEqual(pid, '0x6011')

    def test_raw_ftdi_url_is_handed_back_verbatim(self):
        """It used to be recognised, discarded, and then overwritten."""
        raw = 'ftdi://ftdi:4232h:FT4/3'
        self.assertEqual(self._parts(raw), (None, None, raw))

    def test_bare_serial_still_works(self):
        self.assertEqual(self._parts('FT123'), ('FT123', None, None))

    def test_empty_and_none(self):
        self.assertEqual(self._parts(''), (None, None, None))
        self.assertEqual(self._parts(None), (None, None, None))

    def test_pid_from_a_visa_address_selects_the_part(self):
        """End to end: address -> parts -> driver -> URL."""
        serial, pid, url = self._parts('USB0::0x0403::0x6011::FT4::INSTR')
        drv = FT232HGPIO('g', 4, serial=serial, pid=pid, interface='C',
                         url=url)
        self.assertEqual(drv._build_url(), 'ftdi://ftdi:4232h:FT4/3')


if __name__ == '__main__':
    unittest.main()
