# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""FTDI nets reach the FTDI drivers through every dispatcher, on every part.

``test_ftdi_driver_addressing.py`` constructs the three FTDI drivers directly,
and that is why it could not see this bug. The drivers learned the part and the
channel in v0.43.0, but the I2C and SPI dispatchers routed only instrument
strings that named the FT232H, and the GPIO dispatcher passed the serial alone.
An ``FTDI_FT2232H`` or ``FTDI_FT4232H`` net was created and advertised, then
refused when a command used it -- or, for GPIO, opened as an FT232H on
channel A.

These tests go through the dispatchers with the records ``lager nets add``
saves, so the routing and the driver arguments are covered together. No pyftdi
and no hardware: the drivers open the device only when an operation runs.
"""

from __future__ import annotations

import unittest

from lager.exceptions import I2CBackendError, SPIBackendError
from lager.io.gpio.dispatcher import GPIODispatcher
from lager.io.gpio.ft232h_gpio import FT232HGPIO
from lager.protocols.i2c import dispatcher as i2c_dispatcher
from lager.protocols.i2c.ft232h_i2c import FT232HI2C
from lager.protocols.spi import dispatcher as spi_dispatcher
from lager.protocols.spi.ft232h_spi import FT232HSPI
from lager.util import ftdi_url

# Scanner instrument name -> (PID, pyftdi product token).
PARTS = {
    'FTDI_FT232H': ('6014', '232h'),
    'FTDI_FT2232H': ('6010', '2232h'),
    'FTDI_FT4232H': ('6011', '4232h'),
}


def _record(name, role, instrument, pin, interface=None, serial='FTX1'):
    """A net record in the shape ``lager nets add`` saves for an FTDI part."""
    pid = PARTS[instrument][0]
    rec = {
        'name': name,
        'role': role,
        'instrument': instrument,
        'pin': pin,
        'address': f'USB0::0x0403::0x{pid}::{serial}::INSTR',
    }
    if interface is not None:
        rec['params'] = {'interface': interface}
    return rec


class I2CRoutingTests(unittest.TestCase):

    def test_every_part_routes_to_the_ftdi_driver(self):
        for instrument, (_pid, product) in PARTS.items():
            with self.subTest(instrument=instrument):
                drv = i2c_dispatcher._make_driver(
                    _record('bus', 'i2c', instrument, 'I2C0'))
                self.assertIsInstance(drv, FT232HI2C)
                self.assertEqual(drv._build_url(),
                                 f'ftdi://ftdi:{product}:FTX1/1')

    def test_the_channel_reaches_the_driver(self):
        drv = i2c_dispatcher._make_driver(
            _record('bus', 'i2c', 'FTDI_FT4232H', 'I2C0', interface='B'))
        self.assertEqual(drv._build_url(), 'ftdi://ftdi:4232h:FTX1/2')

    def test_a_channel_without_mpsse_is_refused_with_the_reason(self):
        with self.assertRaises(I2CBackendError) as ctx:
            i2c_dispatcher._make_driver(
                _record('bus', 'i2c', 'FTDI_FT4232H', 'I2C0', interface='C'))
        self.assertIn('MPSSE', str(ctx.exception))

    def test_older_ft232h_spellings_still_route(self):
        for instrument in ('ft232h', 'FTDI_FT232H', 'ft232h_i2c'):
            with self.subTest(instrument=instrument):
                rec = _record('bus', 'i2c', 'FTDI_FT232H', 'I2C0')
                rec['instrument'] = instrument
                self.assertIsInstance(i2c_dispatcher._make_driver(rec),
                                      FT232HI2C)

    def test_ftdi_nets_need_no_labjack_pin_config(self):
        for instrument in PARTS:
            with self.subTest(instrument=instrument):
                self.assertEqual(
                    i2c_dispatcher._get_pin_config(
                        _record('bus', 'i2c', instrument, 'I2C0')),
                    {})


class SPIRoutingTests(unittest.TestCase):

    def test_every_part_routes_to_the_ftdi_driver(self):
        for instrument, (_pid, product) in PARTS.items():
            with self.subTest(instrument=instrument):
                drv = spi_dispatcher._make_driver(
                    _record('flash', 'spi', instrument, 'SPI0'))
                self.assertIsInstance(drv, FT232HSPI)
                self.assertEqual(drv._build_url(),
                                 f'ftdi://ftdi:{product}:FTX1/1')

    def test_the_channel_reaches_the_driver(self):
        drv = spi_dispatcher._make_driver(
            _record('flash', 'spi', 'FTDI_FT2232H', 'SPI0', interface='B'))
        self.assertEqual(drv._build_url(), 'ftdi://ftdi:2232h:FTX1/2')

    def test_a_channel_without_mpsse_is_refused_with_the_reason(self):
        with self.assertRaises(SPIBackendError) as ctx:
            spi_dispatcher._make_driver(
                _record('flash', 'spi', 'FTDI_FT4232H', 'SPI0', interface='D'))
        self.assertIn('MPSSE', str(ctx.exception))

    def test_older_ft232h_spellings_still_route(self):
        for instrument in ('ft232h', 'FTDI_FT232H', 'ft232h_spi'):
            with self.subTest(instrument=instrument):
                rec = _record('flash', 'spi', 'FTDI_FT232H', 'SPI0')
                rec['instrument'] = instrument
                self.assertIsInstance(spi_dispatcher._make_driver(rec),
                                      FT232HSPI)


class GPIORoutingTests(unittest.TestCase):

    def setUp(self):
        # The dispatcher caches drivers at class level; start each test clean.
        GPIODispatcher._driver_cache.clear()

    def tearDown(self):
        GPIODispatcher._driver_cache.clear()

    def test_the_part_and_the_channel_reach_the_driver(self):
        rec = _record('reset_line', 'gpio', 'FTDI_FT4232H', '5', interface='C')
        drv = GPIODispatcher()._make_driver(rec, 'reset_line', '5')
        self.assertIsInstance(drv, FT232HGPIO)
        self.assertEqual(drv._build_url(), 'ftdi://ftdi:4232h:FTX1/3')

    def test_an_ft2232h_net_opens_as_an_ft2232h(self):
        rec = _record('g4', 'gpio', 'FTDI_FT2232H', '4')
        drv = GPIODispatcher()._make_driver(rec, 'g4', '4')
        self.assertEqual(drv._build_url(), 'ftdi://ftdi:2232h:FTX1/1')

    def test_an_ft232h_net_is_unchanged(self):
        rec = _record('g4', 'gpio', 'FTDI_FT232H', '4')
        drv = GPIODispatcher()._make_driver(rec, 'g4', '4')
        self.assertEqual(drv._build_url(), 'ftdi://ftdi:232h:FTX1/1')

    def test_a_raw_ftdi_url_address_is_used_verbatim(self):
        rec = {'name': 'g', 'role': 'gpio', 'instrument': 'FTDI_FT4232H',
               'pin': '1', 'address': 'ftdi://ftdi:4232h:FT9/4'}
        drv = GPIODispatcher()._make_driver(rec, 'g', '1')
        self.assertEqual(drv._build_url(), 'ftdi://ftdi:4232h:FT9/4')

    def test_bare_part_names_choose_the_ftdi_driver(self):
        for name in ('ft2232h', 'FT4232H', 'FTDI_FT2232H', 'ftdi_ft4232h'):
            with self.subTest(name=name):
                self.assertIs(GPIODispatcher()._choose_driver(name), FT232HGPIO)


class ScannerAgreementTests(unittest.TestCase):
    """Every FTDI part the scanner offers gpio, i2c or spi on must route.

    The bug was two lists of FTDI names that drifted from the scanner's. This
    pins the shared set to the scanner's table, so a new part added there fails
    here until the dispatchers can open it.
    """

    def test_every_scanner_ftdi_part_is_served(self):
        from lager.http_handlers import usb_scanner

        served = 0
        for name, entry in usb_scanner.SUPPORTED_USB.items():
            if not name.startswith('FTDI_'):
                continue
            if not set(entry.get('net_type', [])) & {'gpio', 'i2c', 'spi'}:
                continue            # a UART-only part never reaches these drivers
            served += 1
            with self.subTest(part=name):
                self.assertTrue(ftdi_url.is_ftdi_instrument(name))
                self.assertIn(entry['pid'].lower(), ftdi_url._PID_TO_PRODUCT)
        self.assertGreaterEqual(served, 3)

    def test_the_shared_set_names_no_part_the_url_helper_cannot_build(self):
        for name in ftdi_url.FTDI_INSTRUMENT_NAMES:
            with self.subTest(name=name):
                part = name.replace('ftdi_', '').replace('ft', '', 1)
                self.assertIn(part, ftdi_url._PRODUCT_CHANNELS)


if __name__ == '__main__':
    unittest.main()
