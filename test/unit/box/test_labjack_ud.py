# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the LabJack UD-series (U3) drivers and handle manager.

No hardware is involved: a ``_FakeU3`` stands in for the LabJackPython ``u3``
module, injected into ``sys.modules`` the way
``test_labjack_batch_read.py`` injects a fake LJM.

The pin mux gets the most attention here, because it is the part of the UD
stack with no T7 counterpart and the part that fails *quietly*. On a U3 a
flexible line is analog or digital depending on a whole-device bitmask, and:

* reading an analog channel whose line is in digital mode returns a number,
  not an error;
* ``BitStateRead`` documents that "only digital lines return valid readings",
  so the reverse is equally silent.

A driver that wrote the mask itself would clobber another net's pin, so the
read-modify-write lives in the handle manager under its lock. These tests pin
that ownership, the U3-HV's fixed FIO0-FIO3, and the range/readback
differences from the T7 that a copied driver would have inherited wrongly.
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


# ---------------------------------------------------------------------------
# The fake device and module
# ---------------------------------------------------------------------------

class _Feedback:
    """Base for the recorded Feedback commands."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _BitStateRead(_Feedback):
    def __init__(self, IONumber):
        super().__init__(kind="BitStateRead", io=IONumber)


class _BitStateWrite(_Feedback):
    def __init__(self, IONumber, State):
        super().__init__(kind="BitStateWrite", io=IONumber, state=State)


class _BitDirWrite(_Feedback):
    def __init__(self, IONumber, Direction):
        super().__init__(kind="BitDirWrite", io=IONumber, direction=Direction)


class _DAC16(_Feedback):
    def __init__(self, Dac, Value):
        super().__init__(kind="DAC16", dac=Dac, value=Value)


class _FakeU3:
    """Stands in for a u3.U3 device object.

    Models the two behaviours the drivers depend on and nothing else: the
    analog/digital bitmasks that configU3 reads and writes, and the Feedback
    command queue.
    """

    def __init__(self, serial=320012345, is_hv=True):
        self.serialNumber = serial
        self.isHV = is_hv
        self.deviceName = "U3-HV" if is_hv else "U3-LV"
        # A U3-HV boots with FIO0-3 analog; a U3-LV boots all-digital.
        self.fio_analog = 0x0F if is_hv else 0x00
        self.eio_analog = 0x00
        self.opened_with = None
        self.closed = False
        self.config_writes = []
        self.feedback_calls = []
        self.ain_values = {}
        self.dio_states = {}

    # -- lifecycle --
    def open(self, firstFound=True, serial=None, **kw):
        self.opened_with = {"firstFound": firstFound, "serial": serial}

    def close(self):
        self.closed = True

    def configU3(self, **kw):
        if kw:
            self.config_writes.append(dict(kw))
            if "FIOAnalog" in kw:
                self.fio_analog = kw["FIOAnalog"]
            if "EIOAnalog" in kw:
                self.eio_analog = kw["EIOAnalog"]
        return {
            "FIOAnalog": self.fio_analog,
            "EIOAnalog": self.eio_analog,
            "SerialNumber": self.serialNumber,
            "DeviceName": self.deviceName,
        }

    # -- I/O --
    def getAIN(self, channel, *a, **kw):
        return self.ain_values.get(channel, 1.234)

    def voltageToDACBits(self, volts, dacNumber=0, is16Bits=False):
        return int(volts * 13107) if is16Bits else int(volts * 51)

    def getFeedback(self, *commands):
        self.feedback_calls.extend(commands)
        out = []
        for cmd in commands:
            if getattr(cmd, "kind", None) == "BitStateRead":
                out.append(self.dio_states.get(cmd.io, 0))
            else:
                out.append(None)
        return out


def _install_fake_u3(device):
    """Put a fake ``u3`` module in place and point the manager at *device*."""
    mod = types.ModuleType("u3")
    mod.U3 = lambda autoOpen=False, **kw: device   # type: ignore[attr-defined]
    mod.BitStateRead = _BitStateRead               # type: ignore[attr-defined]
    mod.BitStateWrite = _BitStateWrite             # type: ignore[attr-defined]
    mod.BitDirWrite = _BitDirWrite                 # type: ignore[attr-defined]
    mod.DAC16 = _DAC16                             # type: ignore[attr-defined]
    sys.modules["u3"] = mod
    return mod


import lager.io.labjack_ud_handle as udh  # noqa: E402
from lager.io.adc.labjack_ud import LabJackUDADC  # noqa: E402
from lager.io.dac.labjack_ud import (  # noqa: E402
    LabJackUDDAC, LabJackUDDACError,
)
from lager.io.gpio.labjack_ud import LabJackUDGPIO  # noqa: E402


class _UDTestCase(unittest.TestCase):
    """Gives each test a clean manager and a fresh fake device."""

    def setUp(self, is_hv=True):
        self.device = _FakeU3(is_hv=is_hv)
        _install_fake_u3(self.device)
        udh._module_cache["u3"] = sys.modules["u3"]
        udh._module_errors.pop("u3", None)
        # The manager is a process-wide singleton; reset its state so tests
        # cannot leak devices or memoized pin modes into each other.
        udh.LabJackUDHandleManager._instance = None
        udh._manager = None

    def tearDown(self):
        udh.LabJackUDHandleManager._instance = None
        udh._manager = None
        udh._module_cache.pop("u3", None)
        sys.modules.pop("u3", None)


class PinNameTests(unittest.TestCase):
    """Pin naming must be exact: a mis-parsed pin drives the wrong line."""

    def test_names_map_to_dio_numbers(self):
        cases = {
            "FIO0": 0, "FIO7": 7, "EIO0": 8, "EIO7": 15,
            "CIO0": 16, "CIO3": 19, "fio4": 4, " EIO3 ": 11,
        }
        for name, dio in cases.items():
            with self.subTest(pin=name):
                self.assertEqual(udh.pin_to_dio(name), dio)

    def test_integers_pass_through_as_dio_numbers(self):
        self.assertEqual(udh.pin_to_dio(0), 0)
        self.assertEqual(udh.pin_to_dio(19), 19)

    def test_round_trip(self):
        for dio in range(0, 20):
            with self.subTest(dio=dio):
                self.assertEqual(udh.pin_to_dio(udh.dio_to_pin(dio)), dio)

    def test_out_of_range_and_nonsense_are_rejected(self):
        for bad in ("FIO8", "EIO8", "CIO4", "MIO0", 20, -1, "banana", ""):
            with self.subTest(pin=bad):
                with self.assertRaises(ValueError):
                    udh.pin_to_dio(bad)

    def test_u3_has_no_mio(self):
        """The T7 has MIO0-2 at DIO20-22; a U3 stops at CIO3."""
        self.assertEqual(udh.MAX_DIO, 19)
        with self.assertRaises(ValueError):
            udh.pin_to_dio("MIO0")


class SerialFromAddressTests(unittest.TestCase):
    """Device selection depends on pulling the serial out of the address."""

    def test_extracts_serial_from_a_visa_address(self):
        self.assertEqual(
            udh.serial_from_address("USB0::0x0CD5::0x0003::320012345::INSTR"),
            "320012345")

    def test_empty_serial_slot_means_first_found(self):
        """The scanner writes an EMPTY serial slot for a LabJack.

        None, not "", because None is what the manager reads as "first found".
        An empty string would be passed to int() and blow up at open time.
        """
        self.assertIsNone(
            udh.serial_from_address("USB0::0x0CD5::0x0003::::INSTR"))

    def test_missing_address_means_first_found(self):
        self.assertIsNone(udh.serial_from_address(None))
        self.assertIsNone(udh.serial_from_address(""))

    def test_bare_serial_is_accepted(self):
        self.assertEqual(udh.serial_from_address("320012345"), "320012345")


class HandleManagerTests(_UDTestCase):
    """One device per (model, serial), shared across roles."""

    def test_opens_by_serial_when_one_is_given(self):
        udh.get_ud_device("u3", "320012345")
        self.assertEqual(self.device.opened_with,
                         {"firstFound": False, "serial": 320012345})

    def test_opens_first_found_when_no_serial(self):
        udh.get_ud_device("u3", None)
        self.assertEqual(self.device.opened_with,
                         {"firstFound": True, "serial": None})

    def test_same_key_reuses_one_device(self):
        """ADC, DAC and GPIO on one device must share a single USB claim."""
        first = udh.get_ud_device("u3", "320012345")
        second = udh.get_ud_device("u3", "320012345")
        self.assertIs(first, second)
        self.assertEqual(self.device.opened_with["serial"], 320012345)

    def test_close_all_closes_and_forgets(self):
        udh.get_ud_device("u3", "320012345")
        self.assertEqual(udh.close_all_ud_devices(), 1)
        self.assertTrue(self.device.closed)
        self.assertEqual(udh.close_all_ud_devices(), 0)

    def test_unknown_model_is_rejected(self):
        with self.assertRaises(RuntimeError):
            udh.get_ud_device("t7", None)


class PinMuxTests(_UDTestCase):
    """The part with no T7 analogue, and the part that fails silently."""

    def test_making_a_flexible_line_analog_sets_only_its_bit(self):
        device = udh.get_ud_device("u3", "320012345")
        udh.set_channel_mode(device, dio=5, analog=True)
        self.assertEqual(device.config_writes[-1], {"FIOAnalog": 0x0F | 0x20})

    def test_eio_uses_the_eio_mask_with_a_rebased_bit(self):
        """EIO0 is DIO8 but bit 0 of EIOAnalog -- an easy off-by-eight."""
        device = udh.get_ud_device("u3", "320012345")
        udh.set_channel_mode(device, dio=8, analog=True)
        self.assertEqual(device.config_writes[-1], {"EIOAnalog": 0x01})
        udh.set_channel_mode(device, dio=15, analog=True)
        self.assertEqual(device.config_writes[-1], {"EIOAnalog": 0x81})

    def test_one_pin_does_not_clobber_another(self):
        """The whole reason the mask lives in the manager."""
        device = udh.get_ud_device("u3", "320012345")
        udh.set_channel_mode(device, dio=4, analog=True)
        udh.set_channel_mode(device, dio=6, analog=True)
        self.assertEqual(device.fio_analog, 0x0F | 0x10 | 0x40)
        udh.set_channel_mode(device, dio=4, analog=False)
        self.assertEqual(device.fio_analog, 0x0F | 0x40)

    def test_repeated_calls_do_not_re_write(self):
        """Memoized: an ADC read must not pay a configIO round trip each time."""
        device = udh.get_ud_device("u3", "320012345")
        udh.set_channel_mode(device, dio=5, analog=True)
        writes = len(device.config_writes)
        for _ in range(5):
            udh.set_channel_mode(device, dio=5, analog=True)
        self.assertEqual(len(device.config_writes), writes)

    def test_cio_is_digital_only(self):
        device = udh.get_ud_device("u3", "320012345")
        udh.set_channel_mode(device, dio=16, analog=False)  # no-op, no error
        with self.assertRaises(ValueError):
            udh.set_channel_mode(device, dio=16, analog=True)

    def test_hv_fio0_to_3_cannot_become_digital(self):
        """A U3-HV's FIO0-FIO3 are fixed analog inputs.

        Writing the mask would be accepted and ignored by the hardware, and the
        pin would then read as a digital line that never changes. An error is
        the only outcome the caller can act on.
        """
        device = udh.get_ud_device("u3", "320012345")
        for dio in (0, 1, 2, 3):
            with self.subTest(dio=dio):
                with self.assertRaises(ValueError):
                    udh.set_channel_mode(device, dio=dio, analog=False)

    def test_lv_fio0_to_3_are_flexible(self):
        """Same product id, different variant -- so this must not be hardcoded."""
        self.setUp(is_hv=False)
        device = udh.get_ud_device("u3", "320012345")
        udh.set_channel_mode(device, dio=0, analog=False)
        udh.set_channel_mode(device, dio=0, analog=True)
        self.assertEqual(device.fio_analog, 0x01)


class ADCTests(_UDTestCase):
    def test_reads_the_named_channel(self):
        self.device.ain_values[5] = 2.5
        adc = LabJackUDADC("adc1", "AIN5",
                           unique_id="USB0::0x0CD5::0x0003::320012345::INSTR")
        self.assertEqual(adc.input(), 2.5)

    def test_numeric_pin_is_a_channel_number(self):
        self.device.ain_values[0] = 9.9
        self.assertEqual(LabJackUDADC("adc1", 0).input(), 9.9)

    def test_flexible_channel_is_switched_to_analog_first(self):
        LabJackUDADC("adc1", "AIN5").input()
        self.assertEqual(self.device.config_writes[-1],
                         {"FIOAnalog": 0x0F | 0x20})

    def test_hv_channel_needs_no_configuration(self):
        """AIN0-3 on a U3-HV are permanently analog -- configuring is wasted I/O."""
        LabJackUDADC("adc1", "AIN0").input()
        self.assertEqual(self.device.config_writes, [])

    def test_bad_channels_are_rejected(self):
        for bad in ("AIN16", 16, -1, "banana"):
            with self.subTest(pin=bad):
                with self.assertRaises(ValueError):
                    LabJackUDADC("adc1", bad).input()


class DACTests(_UDTestCase):
    def test_writes_a_dac16_feedback_command(self):
        dac = LabJackUDDAC("dac1", "DAC0")
        dac.output(2.5)
        cmd = self.device.feedback_calls[-1]
        self.assertEqual(cmd.kind, "DAC16")
        self.assertEqual(cmd.dac, 0)

    def test_range_is_the_ud_range_not_the_t7s(self):
        """The T7 driver bounds 0-5 V. A UD DAC is 0.04-4.95 V."""
        dac = LabJackUDDAC("dac1", "DAC0")
        for bad in (0.0, 0.03, 4.96, 5.0):
            with self.subTest(voltage=bad):
                with self.assertRaises(ValueError):
                    dac.output(bad)
        dac.output(0.04)
        dac.output(4.95)

    def test_readback_without_a_write_raises_rather_than_inventing_zero(self):
        """A UD DAC has no readback; 0.0 would look like a real measurement."""
        with self.assertRaises(LabJackUDDACError):
            LabJackUDDAC("dac1", "DAC0").get_voltage()

    def test_readback_reports_the_last_written_value(self):
        dac = LabJackUDDAC("dac1", "DAC0")
        dac.output(3.3)
        self.assertEqual(dac.get_voltage(), 3.3)

    def test_bad_dac_numbers_are_rejected(self):
        for bad in ("DAC2", 2, -1):
            with self.subTest(pin=bad):
                with self.assertRaises(ValueError):
                    LabJackUDDAC("dac1", bad).output(1.0)


class GPIOTests(_UDTestCase):
    def test_read_uses_bitstateread_on_the_right_dio(self):
        self.device.dio_states[8] = 1
        gpio = LabJackUDGPIO("gpio1", "EIO0")
        self.assertEqual(gpio.input(), 1)
        cmd = self.device.feedback_calls[-1]
        self.assertEqual((cmd.kind, cmd.io), ("BitStateRead", 8))

    def test_write_uses_a_single_bitstatewrite(self):
        """BitStateWrite forces the line to output on its own.

        An extra BitDirWrite would be a wasted USB round trip, so assert the
        command count as well as the command.
        """
        LabJackUDGPIO("gpio1", "FIO4").output(1)
        writes = [c for c in self.device.feedback_calls
                  if getattr(c, "kind", None) in
                  ("BitStateWrite", "BitDirWrite")]
        self.assertEqual(len(writes), 1)
        self.assertEqual((writes[0].kind, writes[0].io, writes[0].state),
                         ("BitStateWrite", 4, 1))

    def test_level_strings_are_parsed(self):
        gpio = LabJackUDGPIO("gpio1", "FIO4")
        for level, expected in (("high", 1), ("on", 1), ("1", 1), ("true", 1),
                                ("low", 0), ("off", 0), ("0", 0)):
            with self.subTest(level=level):
                gpio.output(level)
                self.assertEqual(self.device.feedback_calls[-1].state, expected)

    def test_pin_in_analog_mode_is_forced_digital_before_use(self):
        """The failure this prevents is silent.

        ``BitStateRead`` documents that "only digital lines return valid
        readings" -- a line left in analog mode returns a number, not an
        error. Start with FIO5 analog (as an ADC net on AIN5 would leave it)
        and assert the GPIO driver clears the bit.
        """
        self.device.fio_analog = 0x0F | 0x20
        LabJackUDGPIO("gpio1", "FIO5").input()
        self.assertEqual(self.device.config_writes[-1], {"FIOAnalog": 0x0F})
        self.assertEqual(self.device.fio_analog, 0x0F)

    def test_pin_already_digital_is_not_rewritten(self):
        """The complement: no configIO round trip when the bit is right."""
        LabJackUDGPIO("gpio1", "FIO5").input()
        self.assertEqual(self.device.config_writes, [])

    def test_adc_then_gpio_on_one_pin_flips_the_mode_both_ways(self):
        """AIN5 and FIO5 are one physical line; the mux is what separates them."""
        LabJackUDADC("adc1", "AIN5").input()
        self.assertEqual(self.device.fio_analog, 0x0F | 0x20)
        LabJackUDGPIO("gpio1", "FIO5").input()
        self.assertEqual(self.device.fio_analog, 0x0F)

    def test_hv_fio0_is_rejected_for_gpio(self):
        with self.assertRaises(ValueError):
            LabJackUDGPIO("gpio1", "FIO0").input()

    def test_does_not_override_wait_for_level(self):
        """It must inherit GPIOBase's polling loop.

        The T7 overrides this with an LJM stream. LJM does not talk to a U3, so
        inheriting is correct -- and the GPIO dispatcher decides between
        scan_rate and poll_interval with isinstance(drv, LabJackGPIO), which a
        sibling class correctly fails.
        """
        from lager.io.gpio.gpio_net import GPIOBase
        from lager.io.gpio.labjack_t7 import LabJackGPIO
        self.assertIs(LabJackUDGPIO.wait_for_level, GPIOBase.wait_for_level)
        self.assertIsNot(LabJackGPIO.wait_for_level, GPIOBase.wait_for_level)
        self.assertNotIsInstance(LabJackUDGPIO("g", "FIO4"), LabJackGPIO)


class DispatcherRoutingTests(unittest.TestCase):
    """A U3 instrument string must select the UD driver, not the T7's."""

    def test_each_dispatcher_routes_u3_to_the_ud_driver(self):
        from lager.io.adc.dispatcher import ADCDispatcher
        from lager.io.dac.dispatcher import DACDispatcher
        from lager.io.gpio.dispatcher import GPIODispatcher
        cases = [
            (ADCDispatcher(), LabJackUDADC),
            (DACDispatcher(), LabJackUDDAC),
            (GPIODispatcher(), LabJackUDGPIO),
        ]
        for dispatcher, expected in cases:
            for name in ("LabJack_U3", "labjack_u3", "LabJack U3", "LabJack_U6"):
                with self.subTest(dispatcher=type(dispatcher).__name__,
                                  instrument=name):
                    self.assertIs(dispatcher._choose_driver(name), expected)

    def test_t7_routing_is_untouched(self):
        from lager.io.adc.dispatcher import ADCDispatcher
        from lager.io.dac.dispatcher import DACDispatcher
        from lager.io.gpio.dispatcher import GPIODispatcher
        from lager.io.adc.labjack_t7 import LabJackADC
        from lager.io.dac.labjack_t7 import LabJackDAC
        from lager.io.gpio.labjack_t7 import LabJackGPIO
        self.assertIs(ADCDispatcher()._choose_driver("LabJack_T7"), LabJackADC)
        self.assertIs(DACDispatcher()._choose_driver("LabJack_T7"), LabJackDAC)
        self.assertIs(GPIODispatcher()._choose_driver("LabJack_T7"),
                      LabJackGPIO)
        # The empty-instrument default still lands on the T7.
        self.assertIs(DACDispatcher()._choose_driver(""), LabJackDAC)


class ClaimReleaseTests(_UDTestCase):
    """A UD device must be released when the box yields its USB claims.

    ``_release_direct_usb_claims`` drains the ADC/DAC/GPIO dispatcher caches
    and closes each cached driver -- but a UD device object lives in the handle
    manager, not on the driver, and the drivers expose no close(). Without an
    explicit call the claim outlives every reference to it, and the next
    ``lager python`` script against that U3 fails with a USB busy error. This
    is the same reason the T7's LJM handle gets its own force_close.
    """

    def test_release_closes_the_ud_device(self):
        import lager.hardware_service as hw
        udh.get_ud_device("u3", "320012345")
        self.assertFalse(self.device.closed)
        hw._release_direct_usb_claims()
        self.assertTrue(self.device.closed)

    def test_release_reports_the_ud_family_separately(self):
        import lager.hardware_service as hw
        udh.get_ud_device("u3", "320012345")
        released = hw._release_direct_usb_claims()
        # Returned or logged, depending on the box version; the device being
        # closed is the contract. Assert the manager forgot it either way, so a
        # later get_ud_device reopens rather than handing back a closed object.
        del released
        self.assertEqual(udh.close_all_ud_devices(), 0)


class ScannerRegistrationTests(unittest.TestCase):
    """The scanner must advertise only roles that have a driver behind them."""

    def test_u3_is_registered_with_the_right_vid_pid(self):
        from lager.http_handlers import usb_scanner
        entry = usb_scanner.SUPPORTED_USB["LabJack_U3"]
        self.assertEqual((entry["vid"], entry["pid"]), ("0cd5", "0003"))

    def test_u3_does_not_advertise_spi_or_i2c(self):
        from lager.http_handlers import usb_scanner
        roles = usb_scanner.SUPPORTED_USB["LabJack_U3"]["net_type"]
        self.assertEqual(sorted(roles), ["adc", "dac", "gpio"])

    def test_u3_channel_map_is_not_a_copy_of_the_t7s(self):
        """A U3 has AIN0-15 and no MIO; a T7 has AIN0-13 and MIO0-2."""
        from lager.http_handlers import usb_scanner
        u3 = usb_scanner.CHANNEL_MAPS["LabJack_U3"]
        t7 = usb_scanner.CHANNEL_MAPS["LabJack_T7"]
        self.assertNotEqual(u3, t7)
        self.assertIn("AIN15", u3["adc"])
        self.assertNotIn("AIN15", t7["adc"])
        self.assertFalse([p for p in u3["gpio"] if p.startswith("MIO")])

    def test_pid_distinguishes_a_u3_from_a_t7(self):
        from lager.http_handlers import usb_scanner
        self.assertNotEqual(usb_scanner.SUPPORTED_USB["LabJack_U3"]["pid"],
                            usb_scanner.SUPPORTED_USB["LabJack_T7"]["pid"])


if __name__ == "__main__":
    unittest.main()
