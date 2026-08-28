# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for LabJack model disambiguation across the three routing layers.

Three places decided "is this net a LabJack?" with a bare ``"labjack" in
instrument`` substring test. That answer is correct for exactly as long as
``LabJack_T7`` is the only LabJack model any box has ever seen, because the
code behind all three assumes LJM -- and LJM does not speak to the U3/U6 at
all, only to the T-series.

The failure mode was not an error, which is what makes it worth pinning:

* ``io/dac/dispatcher.py`` returned ``LabJackDAC`` for any instrument
  containing "labjack". ``LabJackDAC`` opens through the LJM handle manager,
  which opens with ``device_type="T7"``. On a box with a T7 *and* a U3, a U3
  DAC net would therefore write ``DAC0`` on the **T7** and report success.
* ``http_handlers/nets_handler.py`` grouped any "labjack" net into the
  ``/labjack/batch_read`` work unit, which reads LJM Modbus register names. A
  U3 net would be reported with values read from the wrong device.
* ``http_handlers/net_command.py`` keyed the shared device lock on
  ``"labjack:" + address``, which collapses to ``"labjack:ANY"`` for any net
  saved without one -- and LabJack nets often are, because the ADC dispatcher
  resolves an empty address on the grounds that LabJack auto-discovers. Two
  different physical devices would then serialise on one lock.

Every fix here is additive: the T7 and every instrument that already fell
through to a default keep the exact behaviour they had. The regression guards
below assert that explicitly, because "we did not change the T7" is the whole
safety argument for landing this ahead of any U3 hardware.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted: str) -> None:
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


_HARDWARE_STUBS = [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core',
    'pigpio', 'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope', 'brainstem',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
    'flask_socketio', 'uldaq',
]
for _dep in _HARDWARE_STUBS:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.exceptions import DACBackendError  # noqa: E402
from lager.http_handlers import nets_handler  # noqa: E402
from lager.http_handlers.net_command import _physical_device_id  # noqa: E402
from lager.io.dac.dispatcher import DACDispatcher  # noqa: E402
from lager.io.dac.labjack_t7 import LabJackDAC  # noqa: E402
from lager.io.dac.usb202 import USB202DAC  # noqa: E402

# Real bench addresses. The serial field is EMPTY on this hardware -- that is
# not a placeholder, it is what the USB scanner produces for a LabJack -- so
# the PID is the only thing distinguishing the two. A test that invented a
# serial would hide the address-less case entirely.
T7_ADDRESS = "USB0::0x0CD5::0x0007::::INSTR"
U3_ADDRESS = "USB0::0x0CD5::0x0003::::INSTR"

T7 = "LabJack_T7"
U3 = "LabJack_U3"   # the name the scanner registers (U3-HV and U3-LV share a pid)


class DacDispatcherModelTests(unittest.TestCase):
    """_choose_driver must not hand a non-T7 LabJack to the T7 driver."""

    def setUp(self):
        self.choose = DACDispatcher()._choose_driver

    def test_t7_spellings_still_return_the_t7_driver(self):
        for name in ("LabJack_T7", "labjack_t7", "LabJack T7",
                     "labjack-t7", "LABJACK_T7"):
            with self.subTest(instrument=name):
                self.assertIs(self.choose(name), LabJackDAC)

    def test_bare_t7_and_empty_instrument_are_unchanged(self):
        """Backward compatibility, and the reason the fix is not just a regex.

        Saved nets predate the instrument column being reliable. An empty
        instrument has always meant "the LabJack", and a bare "t7" has always
        matched; narrowing to a strict ``labjack[_-\\s]*t7`` alone would have
        broken both.
        """
        self.assertIs(self.choose("t7"), LabJackDAC)
        self.assertIs(self.choose(""), LabJackDAC)
        self.assertIs(self.choose(None), LabJackDAC)

    def test_usb202_is_unchanged(self):
        for name in ("MCC_USB-202", "usb202", "mcc"):
            with self.subTest(instrument=name):
                self.assertIs(self.choose(name), USB202DAC)

    def test_non_t7_labjack_never_routes_to_the_t7_driver(self):
        """The bug itself.

        The invariant is "not ``LabJackDAC``", not any particular alternative.
        Returning ``LabJackDAC`` would open an LJM handle as a T7 and write to
        whichever LabJack LJM found first. Before a UD driver existed the only
        safe answer was an error; now these route to ``LabJackUDDAC``. Both
        satisfy the property this test exists to protect, so it asserts the
        property rather than the answer.
        """
        for name in ("LabJack_U3", "labjack_u3", "LabJack_U6"):
            with self.subTest(instrument=name):
                self.assertIsNot(self.choose(name), LabJackDAC)

    def test_an_unknown_instrument_is_still_rejected(self):
        """The error path must survive the UD branch being added above it."""
        for name in ("Some_Unknown_Box", "keithley_2281s"):
            with self.subTest(instrument=name):
                with self.assertRaises(DACBackendError):
                    self.choose(name)


class BatchGroupingModelTests(unittest.TestCase):
    """/labjack/batch_read speaks LJM, so only the T-series may reach it."""

    def test_is_labjack_t7_matches_only_the_t_series(self):
        for name in ("LabJack_T7", "labjack_t7", "LabJack T7", "labjack-t7"):
            with self.subTest(instrument=name):
                self.assertTrue(
                    nets_handler._is_labjack_t7({"instrument": name}))

        for name in ("LabJack_U3-HV", "LabJack_U6", "MCC_USB-202", "", None):
            with self.subTest(instrument=name):
                self.assertFalse(
                    nets_handler._is_labjack_t7({"instrument": name}))

    def test_missing_instrument_key_does_not_raise(self):
        self.assertFalse(nets_handler._is_labjack_t7({}))

    def test_t7_net_still_groups_into_the_batch_work_unit(self):
        key = nets_handler._group_key(
            {"name": "gpio1", "role": "gpio",
             "instrument": T7, "address": T7_ADDRESS})
        self.assertEqual(key[0], "_labjack_")

    def test_u3_net_does_not_group_into_the_batch_work_unit(self):
        """A U3 falls back to the per-role probe rather than an LJM read."""
        key = nets_handler._group_key(
            {"name": "gpio1", "role": "gpio",
             "instrument": U3, "address": U3_ADDRESS})
        self.assertEqual(key, ("gpio", U3, U3_ADDRESS))
        self.assertNotEqual(key[0], "_labjack_")


class DeviceLockIdentityTests(unittest.TestCase):
    """Two LabJack models must not contend on one lock -- or lose their own."""

    def test_t7_lock_identity_is_byte_for_byte_unchanged(self):
        """The regression guard the whole change rests on.

        ``/invoke`` and ``/labjack/batch_read`` both key on this string, and
        ``hardware_service`` carries ``"labjack:ANY"`` as its documented
        fallback for an address-less record. If the T7's identity moved, those
        two would take different lock objects and interleave on one LJM handle.
        """
        rec = {"name": "gpio1", "instrument": T7, "address": T7_ADDRESS}
        self.assertEqual(
            _physical_device_id("gpio", T7, rec), "labjack:" + T7_ADDRESS)

        bare = {"name": "gpio1", "instrument": T7}
        self.assertEqual(_physical_device_id("gpio", T7, bare), "labjack:ANY")

    def test_unmatched_instruments_still_fall_through_unchanged(self):
        """Anything that reached the default keeps reaching it."""
        rec = {"name": "x", "instrument": "Some_Unknown_Box", "address": "A"}
        self.assertEqual(
            _physical_device_id("gpio", "Some_Unknown_Box", rec), "labjack:A")

    def test_u3_and_t7_do_not_share_a_lock(self):
        t7 = _physical_device_id(
            "dac", T7, {"instrument": T7, "address": T7_ADDRESS})
        u3 = _physical_device_id(
            "dac", U3, {"instrument": U3, "address": U3_ADDRESS})
        self.assertNotEqual(t7, u3)

    def test_u3_and_t7_stay_apart_with_no_address(self):
        """The case the address alone cannot cover.

        A LabJack net is routinely saved without an address -- the ADC
        dispatcher resolves an empty one deliberately, because LabJack
        auto-discovers. Keying on the address alone would collapse both models
        onto ``"labjack:ANY"``.
        """
        t7 = _physical_device_id("dac", T7, {"instrument": T7})
        u3 = _physical_device_id("dac", U3, {"instrument": U3})
        self.assertEqual(t7, "labjack:ANY")
        self.assertNotEqual(t7, u3)


if __name__ == "__main__":
    unittest.main()
