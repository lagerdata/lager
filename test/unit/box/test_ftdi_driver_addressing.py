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

import json
import os
import tempfile
import unittest

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
            a._CACHE_FILE = path
            b._CACHE_FILE = path

            a._set_cached_state(1, True)
            b._set_cached_state(0, True)

            self.assertEqual(a._get_cached_state()['value'], 1)
            self.assertEqual(b._get_cached_state()['value'], 0)
            with open(path) as f:
                self.assertEqual(len(json.load(f)), 2)


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
