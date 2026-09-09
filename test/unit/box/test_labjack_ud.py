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

    Models the behaviours the drivers depend on and nothing else: the
    analog/digital bitmasks, the Feedback command queue, and -- critically --
    the fact that configU3 and configIO are DIFFERENT state.

    configU3 carries the power-up defaults and the identity fields; configIO
    carries the live pin mux that getAIN obeys. Keeping them separate here is
    what lets these tests see a driver that configures the wrong one: such a
    driver reads its own write back through configU3 and looks correct, while
    the pin never moves and getAIN raises PIN_CONFIGURED_FOR_DIGITAL on real
    hardware.
    """

    def __init__(self, serial=320012345, is_hv=True):
        self.serialNumber = serial
        self.isHV = is_hv
        self.deviceName = "U3-HV" if is_hv else "U3-LV"
        # A U3-HV boots with FIO0-3 analog; a U3-LV boots all-digital.
        # These are the LIVE masks (configIO).
        self.fio_analog = 0x0F if is_hv else 0x00
        self.eio_analog = 0x00
        # The stored power-up defaults (configU3) start equal but move
        # independently, exactly as they do on the device.
        self.fio_analog_default = self.fio_analog
        self.eio_analog_default = self.eio_analog
        self.opened_with = None
        self.open_count = 0
        self.closed = False
        self.config_writes = []      # configIO writes -- the live mux
        self.configu3_writes = []    # configU3 writes -- power-up defaults
        self.feedback_calls = []
        self.ain_values = {}
        self.dio_states = {}
        # -- serial protocols --
        self.spi_calls = []
        self.i2c_calls = []
        self.i2c_devices = {}        # unshifted 7-bit address -> _FakeI2CSlave
        # Deliberately NOT the identity: a driver that hands back its own
        # transmit buffer instead of the response would pass against an echo.
        self.spi_response = lambda tx: [(b ^ 0xFF) & 0xFF for b in tx]

    # -- lifecycle --
    def open(self, firstFound=True, serial=None, **kw):
        # The Exodriver claims exclusively: opening a device this process
        # already holds fails. Modelling that is what makes the first-found
        # adoption path reachable in tests instead of silently succeeding.
        if self.open_count and not self.closed:
            raise RuntimeError(
                "Couldn't open device. Device access or claim error.")
        self.opened_with = {"firstFound": firstFound, "serial": serial}
        self.open_count += 1
        self.closed = False

    def close(self):
        self.closed = True

    def configU3(self, **kw):
        """Power-up defaults plus identity. Does NOT move the live pin mux."""
        if kw:
            self.configu3_writes.append(dict(kw))
            if "FIOAnalog" in kw:
                self.fio_analog_default = kw["FIOAnalog"]
            if "EIOAnalog" in kw:
                self.eio_analog_default = kw["EIOAnalog"]
        return {
            "FIOAnalog": self.fio_analog_default,
            "EIOAnalog": self.eio_analog_default,
            "SerialNumber": self.serialNumber,
            "DeviceName": self.deviceName,
        }

    def configIO(self, **kw):
        """The live pin mux -- the masks getAIN actually obeys."""
        if kw:
            self.config_writes.append(dict(kw))
            if "FIOAnalog" in kw:
                self.fio_analog = kw["FIOAnalog"]
            if "EIOAnalog" in kw:
                self.eio_analog = kw["EIOAnalog"]
        return {
            "FIOAnalog": self.fio_analog,
            "EIOAnalog": self.eio_analog,
        }

    # -- I/O --
    def getAIN(self, channel, *a, **kw):
        # A flexible channel only answers when the LIVE mask has its bit set.
        # AIN0-3 on an HV part are fixed analog and always answer. This is the
        # device's PIN_CONFIGURED_FOR_DIGITAL (98), and it is what turns a mux
        # written to the wrong command from a silent wrong reading into a
        # failing test.
        if channel >= 4 or not self.isHV:
            mask = self.fio_analog if channel < 8 else self.eio_analog
            bit = 1 << (channel if channel < 8 else channel - 8)
            if not mask & bit:
                raise RuntimeError(
                    f"PIN_CONFIGURED_FOR_DIGITAL (98): AIN{channel} is "
                    f"configured for digital; use ConfigIO to set it analog"
                )
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


    # -- serial protocols (U3 hardware >= 1.21; commands 0xF8/0x3A and 0x3B) --

    def _require_digital(self, dio, role):
        """A line must be OUT of analog mode before SPI/I2C can drive it.

        Reads the same live masks ``getAIN`` reads -- deliberately not a
        separate flag, because the whole value of modelling this is that one
        net's analog read and another net's SDA are the same physical pin.

        The firmware's SPI/I2C commands set pin DIRECTION (that is what
        ``DisableDirConfig=False`` buys) but never touch the analog/digital
        mux, which lives in configIO's FIOAnalog/EIOAnalog. So a pin left
        analog is not corrected by issuing the transfer.

        This raises where real hardware would more likely return garbage.
        That is a deliberately STRICTER model than the device: garbage is not
        assertable, and the point is to fail a driver that never called
        set_channel_mode at all. Whether the real part errors or silently
        returns rubbish is a bench question, not a unit-test one.
        """
        if dio <= 3 and self.isHV:
            raise _LabJackException(
                f"{role} pin FIO{dio} is a fixed high-voltage analog input on "
                f"a {self.deviceName} and cannot be driven digitally")
        if dio < 8:
            mask, bit = self.fio_analog, 1 << dio
        elif dio < 16:
            mask, bit = self.eio_analog, 1 << (dio - 8)
        else:
            return  # CIO0-3 are digital-only; no mask bit exists
        if mask & bit:
            raise _LabJackException(
                f"{role} pin DIO{dio} is configured for analog; use ConfigIO "
                f"to set it digital")

    def spi(self, SPIBytes, AutoCS=True, DisableDirConfig=False, SPIMode="A",
            SPIClockFactor=0, CSPinNum=4, CLKPinNum=5, MISOPinNum=6,
            MOSIPinNum=7):
        """Mirrors u3.U3.spi from LabJackPython 2.3.0.

        Keyword names and defaults are copied exactly, so a driver that invents
        its own (``mode=``, ``cs_pin=``) raises TypeError here rather than
        appearing to work.
        """
        if not isinstance(SPIBytes, list):
            raise _LabJackException("SPIBytes must be a list of bytes")
        if len(SPIBytes) > 50:
            raise _LabJackException(
                "The maximum number of bytes that can be sent/received is 50")
        if SPIMode not in ("A", "B", "C", "D"):
            raise _LabJackException(
                "Invalid SPIMode %r, valid modes are: %r"
                % (SPIMode, ("A", "B", "C", "D")))

        self._require_digital(CLKPinNum, "CLK")
        self._require_digital(MOSIPinNum, "MOSI")
        self._require_digital(MISOPinNum, "MISO")
        if AutoCS:
            self._require_digital(CSPinNum, "CS")

        self.spi_calls.append({
            "SPIBytes": list(SPIBytes), "AutoCS": AutoCS,
            "DisableDirConfig": DisableDirConfig, "SPIMode": SPIMode,
            "SPIClockFactor": SPIClockFactor, "CSPinNum": CSPinNum,
            "CLKPinNum": CLKPinNum, "MISOPinNum": MISOPinNum,
            "MOSIPinNum": MOSIPinNum,
        })

        # The command moves an even number of bytes. u3.spi() pads an odd
        # request with one zero and then reads back 8+numSPIBytes, where
        # numSPIBytes is the PADDED count -- so the returned list is one byte
        # too long and the library does NOT trim it. (i2c() is the opposite:
        # it trims its own odd response.) A driver that returns
        # result["SPIBytes"] verbatim hands back a phantom trailing byte.
        on_wire = list(SPIBytes) + ([0] if len(SPIBytes) % 2 else [])
        return {"NumSPIBytesTransferred": len(SPIBytes),
                "SPIBytes": list(self.spi_response(on_wire))}

    @staticmethod
    def _ack_array_value(slave, n_data):
        """Pack AckArray from the datasheet sentence, expressed only here.

        "Bit 0 corresponds to the last data byte, bit 1 corresponds to the
        second to last data byte, and so on up to the address byte. If n is
        the number of data bytes, the ACK value should be (2^(n+1))-1."

        So the ADDRESS byte's bit index is n and it MOVES with the transfer
        length -- bit 0 is the address only when there are no data bytes, which
        is exactly the scan case and exactly the case that misleads.

        Written from the documentation and sharing no code with any driver: if
        the packer and the driver's decoder agreed by construction, a test
        built on them would prove nothing.
        """
        if slave is None:
            return 0                     # an empty bus ACKs nothing
        value = 1 << n_data              # the address byte was ACKed
        acked = n_data if slave.nak_after is None else min(slave.nak_after,
                                                          n_data)
        for i in range(acked):           # data byte i, counting from the first
            value |= 1 << (n_data - 1 - i)
        # AckArray is 32 bits but the TX limit is 50 bytes, so above 31 data
        # bytes the high bits -- including the address ACK -- fall off the end.
        return value & 0xFFFFFFFF

    def i2c(self, Address, I2CBytes, EnableClockStretching=False,
            NoStopWhenRestarting=False, ResetAtStart=False, SpeedAdjust=0,
            SDAPinNum=6, SCLPinNum=7, NumI2CBytesToReceive=0,
            AddressByte=None):
        """Mirrors u3.U3.i2c from LabJackPython 2.3.0."""
        if not isinstance(I2CBytes, list):
            raise _LabJackException("I2CBytes must be a list")
        if len(I2CBytes) > 50:
            raise _LabJackException(
                "The maximum number of bytes that can be sent is 50")
        if NumI2CBytesToReceive > 52:
            raise _LabJackException(
                "The maximum number of bytes that can be received is 52")
        # u3.i2c() does command[10] = Address << 1 itself. A driver that
        # pre-shifts arrives here with an 8-bit value, matches no slave, and
        # would otherwise just look like an empty bus.
        if not 0 <= Address <= 0x7F:
            raise AssertionError(
                f"Address {Address:#04x} is not an unshifted 7-bit address; "
                f"u3.i2c() applies the << 1 itself")

        self._require_digital(SDAPinNum, "SDA")
        self._require_digital(SCLPinNum, "SCL")

        self.i2c_calls.append({
            "Address": Address, "I2CBytes": list(I2CBytes),
            "EnableClockStretching": EnableClockStretching,
            "NoStopWhenRestarting": NoStopWhenRestarting,
            "ResetAtStart": ResetAtStart, "SpeedAdjust": SpeedAdjust,
            "SDAPinNum": SDAPinNum, "SCLPinNum": SCLPinNum,
            "NumI2CBytesToReceive": NumI2CBytesToReceive,
            "AddressByte": AddressByte,
        })

        slave = self.i2c_devices.get(Address)
        if slave is not None:
            acked = (len(I2CBytes) if slave.nak_after is None
                     else min(slave.nak_after, len(I2CBytes)))
            slave.written.extend(I2CBytes[:acked])

        value = self._ack_array_value(slave, len(I2CBytes))
        received = []
        if NumI2CBytesToReceive:
            source = slave.read_data if slave is not None else []
            received = list(source[:NumI2CBytesToReceive])
            received += [0] * (NumI2CBytesToReceive - len(received))
        # u3.i2c() trims its own odd-length response, so the caller always sees
        # exactly NumI2CBytesToReceive bytes. Unlike spi(). Do not "fix" this.
        return {"AckArray": [(value >> (8 * i)) & 0xFF for i in range(4)],
                "I2CBytes": received}


class _LabJackException(Exception):
    """Stands in for LabJackPython's LabJackException."""


class _FakeI2CSlave:
    """A device on the fake I2C bus.

    ``nak_after`` is how many data bytes it ACKs before NAKing; None means it
    ACKs everything. A real slave that NAKs ends the transfer, so bytes after
    the NAK are never sent and therefore never ACKed -- which is what makes a
    partial AckArray the discriminating case.
    """

    def __init__(self, read_data=None, nak_after=None):
        self.read_data = list(read_data or [])
        self.nak_after = nak_after
        self.written = []


def _install_fake_u3(device):
    """Put a fake ``u3`` module in place and point the manager at *device*."""
    mod = types.ModuleType("u3")
    mod.U3 = lambda autoOpen=False, **kw: device   # type: ignore[attr-defined]
    mod.BitStateRead = _BitStateRead               # type: ignore[attr-defined]
    mod.BitStateWrite = _BitStateWrite             # type: ignore[attr-defined]
    mod.BitDirWrite = _BitDirWrite                 # type: ignore[attr-defined]
    mod.DAC16 = _DAC16                             # type: ignore[attr-defined]
    mod.LabJackException = _LabJackException       # type: ignore[attr-defined]
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

    # -- one physical device, however it is named -----------------------
    #
    # A U3 reports no USB serial, so the scanner writes an empty serial slot
    # and its nets resolve to None ("first found"), while a hand-written or
    # migrated record may carry the real serial. Keying the cache on what the
    # caller ASKED for made those two names two entries, and the second open
    # then raced the claim this same process already held -- a hard
    # NullHandleException on hardware, in both orders, until close_all.

    def test_none_then_serial_is_one_device_and_one_open(self):
        first = udh.get_ud_device("u3", None)
        second = udh.get_ud_device("u3", str(self.device.serialNumber))
        self.assertIs(first, second)
        self.assertEqual(self.device.open_count, 1)

    def test_serial_then_none_is_one_device_and_one_open(self):
        first = udh.get_ud_device("u3", str(self.device.serialNumber))
        second = udh.get_ud_device("u3", None)
        self.assertIs(first, second)
        self.assertEqual(self.device.open_count, 1)

    def test_entry_is_keyed_by_the_reported_serial_not_the_request(self):
        udh.get_ud_device("u3", None)
        mgr = udh.LabJackUDHandleManager()
        self.assertEqual(list(mgr._devices), [("u3", str(self.device.serialNumber))])

    def test_release_by_the_other_name_still_decrements(self):
        udh.get_ud_device("u3", None)
        udh.get_ud_device("u3", str(self.device.serialNumber))
        mgr = udh.LabJackUDHandleManager()
        key = ("u3", str(self.device.serialNumber))
        self.assertEqual(mgr._ref_counts[key], 2)
        udh.release_ud_device("u3", None)
        self.assertEqual(mgr._ref_counts[key], 1)
        udh.release_ud_device("u3", str(self.device.serialNumber))
        self.assertEqual(mgr._ref_counts[key], 0)

    def test_first_found_adopts_the_device_this_process_already_holds(self):
        """A firstFound open that cannot claim is our own device, not a new one.

        The Exodriver claims exclusively, so with one U3 on the box a
        serial-less net asking for "first found" after a serial-carrying net
        opened it must adopt that claim rather than fail.
        """
        first = udh.get_ud_device("u3", str(self.device.serialNumber))

        def _claim_conflict(**kw):
            raise RuntimeError(
                "Couldn't open device. Device access or claim error.")

        self.device.open = _claim_conflict
        second = udh.get_ud_device("u3", None)
        self.assertIs(first, second)
        self.assertEqual(self.device.open_count, 1)

    def test_first_found_opens_a_free_device_rather_than_adopting(self):
        """With a second U3 free, "first found" must not bind to the busy one.

        The guard against the opposite mistake: reusing whichever entry happens
        to be open would silently point a serial-less net at the instrument
        some other net opened.
        """
        first = udh.get_ud_device("u3", str(self.device.serialNumber))
        other = _FakeU3(serial=320099999)
        sys.modules["u3"].U3 = lambda autoOpen=False, **kw: other
        second = udh.get_ud_device("u3", None)
        self.assertIsNot(second, first)
        self.assertEqual(second.serialNumber, 320099999)

    def test_force_close_by_the_other_name_closes_it(self):
        udh.get_ud_device("u3", str(self.device.serialNumber))
        udh.force_close_ud("u3", None)
        self.assertTrue(self.device.closed)
        self.assertEqual(udh.close_all_ud_devices(), 0)


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

    def test_u3_advertises_every_role_that_has_a_driver(self):
        """spi and i2c joined the list when their drivers landed.

        The house rule runs both ways: never advertise a role with no driver,
        and never withhold one that has a working driver. `nets add-all`
        enumerates this list, so a missing role is a role nobody can reach.
        """
        from lager.http_handlers import usb_scanner
        roles = usb_scanner.SUPPORTED_USB["LabJack_U3"]["net_type"]
        self.assertEqual(sorted(roles), ["adc", "dac", "gpio", "i2c", "spi"])

    def test_u3_spi_and_i2c_channels_avoid_the_high_voltage_pins(self):
        """Whatever spans are advertised, none may name FIO0-FIO3."""
        from lager.http_handlers import usb_scanner
        channels = usb_scanner.CHANNEL_MAPS["LabJack_U3"]
        for role in ("spi", "i2c"):
            for span in channels[role]:
                for pin in span.split("-"):
                    self.assertNotIn(pin, ("FIO0", "FIO1", "FIO2", "FIO3"),
                                     f"{role} span {span} names a fixed "
                                     f"high-voltage analog input")

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

    def test_u3_does_not_offer_the_hv_pins_as_gpio(self):
        """FIO0-FIO3 are the U3-HV's fixed high-voltage analog inputs.

        While they were advertised, `lager nets add` accepted a gpio net on
        them and the net then failed at first use, on hardware, with the
        driver's PIN error. The scanner reads a USB descriptor and a U3-LV
        reports the same product id, so the family is treated as HV.

        The whole list is pinned, not just the four absences: a merge that
        restores the old line puts them back in the middle of the list, and
        an assertNotIn-only test would still pass on the ones it names.
        """
        from lager.http_handlers import usb_scanner
        gpio = usb_scanner.CHANNEL_MAPS["LabJack_U3"]["gpio"]
        for pin in ("FIO0", "FIO1", "FIO2", "FIO3"):
            self.assertNotIn(pin, gpio)
        self.assertEqual(
            gpio,
            ["FIO4", "FIO5", "FIO6", "FIO7",
             "EIO0", "EIO1", "EIO2", "EIO3", "EIO4", "EIO5", "EIO6", "EIO7",
             "CIO0", "CIO1", "CIO2", "CIO3"],
        )

    def test_u3_still_offers_the_hv_pins_as_adc(self):
        """The same four pins, named for the mode they are stuck in.

        Dropping them from gpio must not cost the user the measurement they
        are actually for: AIN0-AIN3 are FIO0-FIO3 read as +/-10.3 V inputs.
        """
        from lager.http_handlers import usb_scanner
        adc = usb_scanner.CHANNEL_MAPS["LabJack_U3"]["adc"]
        for channel in ("AIN0", "AIN1", "AIN2", "AIN3"):
            self.assertIn(channel, adc)
        self.assertEqual(len(adc), 16)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# I2C (command 0xF8/0x3B)
# --------------------------------------------------------------------------

class UDI2CPinTests(unittest.TestCase):
    """Pin handling. No device is opened by any test in this class."""

    def _make(self, sda, scl):
        from lager.protocols.i2c.labjack_ud_i2c import LabJackUDI2C
        return LabJackUDI2C(sda_pin=sda, scl_pin=scl)

    def test_fio0_to_fio3_are_refused_at_construction(self):
        """The high-voltage pins fail when the net is built, not mid-run.

        Constructing must not open the device, so this cannot consult isHV --
        and does not need to: accepting a pin that can never work is the worse
        way to be wrong.
        """
        from lager.exceptions import I2CBackendError
        for pin in ("FIO0", "FIO3", 0, 3):
            with self.subTest(pin=pin):
                with self.assertRaises(I2CBackendError) as ctx:
                    self._make(pin, "FIO7")
                msg = str(ctx.exception)
                self.assertIn("high-voltage", msg)
                self.assertIn("FIO4-FIO7", msg)

    def test_the_usable_lines_are_accepted(self):
        for sda, scl in (("FIO6", "FIO7"), ("EIO0", "EIO1"), ("CIO0", "CIO3"),
                         (6, 7), ("eio7", "cio0")):
            with self.subTest(sda=sda, scl=scl):
                self._make(sda, scl)  # must not raise

    def test_sda_and_scl_must_differ(self):
        from lager.exceptions import I2CBackendError
        with self.assertRaises(I2CBackendError):
            self._make("FIO6", "FIO6")

    def test_a_nonsense_pin_is_refused(self):
        from lager.exceptions import I2CBackendError
        for pin in ("FIO9", "MIO0", "banana"):
            with self.subTest(pin=pin):
                with self.assertRaises(I2CBackendError):
                    self._make(pin, "FIO7")


class UDI2CSpeedTests(unittest.TestCase):
    """frequency_hz -> SpeedAdjust, an affine model over the period."""

    def setUp(self):
        from lager.protocols.i2c.labjack_ud_i2c import LabJackUDI2C
        self.cls = LabJackUDI2C
        LabJackUDI2C._speed_warning_shown = True   # silence the clamp warning

    def test_the_published_anchors_round_trip(self):
        """The two anchors the model is fitted to must come back exactly."""
        self.assertEqual(self.cls._speed_adjust_for(150_000), 0)
        self.assertEqual(self.cls._speed_adjust_for(10_000), 255)

    def test_the_unused_middle_anchor_predicts_the_datasheet(self):
        """The 70 kHz anchor is NOT an input to the fit -- it is the check.

        LabJack documents SpeedAdjust 20 as about 70 kHz. If the affine period
        model is the right shape, count 20 lands within a few percent of that
        without ever having been told so.
        """
        self.assertAlmostEqual(self.cls._frequency_for(20), 71_500, delta=2_000)

    def test_rounding_never_goes_faster_than_asked(self):
        for requested in (100_000, 90_000, 50_000, 25_000, 11_000):
            with self.subTest(requested=requested):
                adjust = self.cls._speed_adjust_for(requested)
                self.assertLessEqual(self.cls._frequency_for(adjust), requested)

    def test_the_hundred_kilohertz_default_is_reachable(self):
        adjust = self.cls._speed_adjust_for(100_000)
        self.assertEqual(adjust, 10)
        self.assertAlmostEqual(self.cls._frequency_for(adjust), 96_800, delta=500)

    def test_out_of_range_clamps_rather_than_raising(self):
        self.assertEqual(self.cls._speed_adjust_for(400_000), 0)
        self.assertEqual(self.cls._speed_adjust_for(100), 255)

    def test_a_nonsense_frequency_is_refused(self):
        from lager.exceptions import I2CBackendError
        for bad in (0, -1, "fast", None):
            with self.subTest(bad=bad):
                with self.assertRaises(I2CBackendError):
                    self.cls._speed_adjust_for(bad)


class UDI2CAckDecodeTests(unittest.TestCase):
    """AckArray decoding, checked against the datasheet with no fake involved.

    These hand-compute the value from the documented rule and hand it straight
    to the driver, so they compare the driver's layout against the
    documentation rather than against the test double.
    """

    def setUp(self):
        from lager.protocols.i2c.labjack_ud_i2c import LabJackUDI2C
        self.cls = LabJackUDI2C

    def test_four_bytes_combine_little_endian(self):
        self.assertEqual(self.cls._ack_value([0x01, 0x02, 0x00, 0x80]),
                         0x80000201)

    def test_the_address_bit_moves_with_the_transfer_length(self):
        """Bit 0 is the address ONLY when there are no data bytes."""
        self.assertTrue(self.cls._address_acked(0b1, 0))      # scan: bit 0
        self.assertTrue(self.cls._address_acked(0b1000, 3))   # 3 bytes: bit 3
        self.assertFalse(self.cls._address_acked(0b0111, 3))  # data only
        self.assertFalse(self.cls._address_acked(0b0, 0))

    def test_a_named_nak_points_at_the_right_data_byte(self):
        """0b1100 over a 3-byte write: address ACK, byte 0 ACK, byte 1 NAKed.

        Worked from the datasheet sentence by hand -- bit 0 is the LAST data
        byte -- and never through the fake.
        """
        from lager.exceptions import I2CBackendError
        driver = self.cls(sda_pin="FIO6", scl_pin="FIO7")
        with self.assertRaises(I2CBackendError) as ctx:
            driver._check_acks(0b1100, 0x48, 3)
        self.assertIn("byte 1", str(ctx.exception))

    def test_a_fully_acked_write_passes(self):
        driver = self.cls(sda_pin="FIO6", scl_pin="FIO7")
        for n in (0, 1, 3, 7):
            with self.subTest(n=n):
                driver._check_acks((1 << (n + 1)) - 1, 0x48, n)  # must not raise

    def test_a_silent_bus_names_the_missing_pull_ups(self):
        from lager.exceptions import I2CBackendError
        driver = self.cls(sda_pin="FIO6", scl_pin="FIO7")
        with self.assertRaises(I2CBackendError) as ctx:
            driver._check_acks(0, 0x48, 2)
        self.assertIn("pull-up", str(ctx.exception))


class UDI2CTransactionTests(_UDTestCase):
    """The driver against the fake bus."""

    def setUp(self):
        super().setUp()
        from lager.protocols.i2c.labjack_ud_i2c import LabJackUDI2C
        LabJackUDI2C._speed_warning_shown = True
        self.driver = LabJackUDI2C(sda_pin="FIO6", scl_pin="FIO7")

    def _attach(self, address, **kw):
        slave = _FakeI2CSlave(**kw)
        self.device.i2c_devices[address] = slave
        return slave

    def test_the_address_reaches_the_library_unshifted(self):
        """u3.i2c() applies the << 1 itself; a driver that pre-shifts misses."""
        self._attach(0x48, read_data=[0xAB])
        self.driver.read(0x48, 1)
        self.assertEqual(self.device.i2c_calls[-1]["Address"], 0x48)

    def test_scan_finds_only_what_is_on_the_bus(self):
        self._attach(0x48)
        self._attach(0x50)
        self.assertEqual(self.driver.scan(), [0x48, 0x50])

    def test_scan_probes_with_no_data_at_all(self):
        """An address-only probe cannot disturb a register on a real slave."""
        self._attach(0x48)
        self.driver.scan(0x40, 0x4F)
        for call in self.device.i2c_calls:
            self.assertEqual(call["I2CBytes"], [])
            self.assertEqual(call["NumI2CBytesToReceive"], 0)

    def test_an_empty_bus_scans_empty(self):
        self.assertEqual(self.driver.scan(), [])

    def test_a_partially_acked_write_is_a_failure(self):
        """The discriminating case, and the one a ported T7 check misses.

        The T7 driver treats "no ACK" as acks == 0. Here the address and the
        first data byte ACKed, so the value is 12 -- non-zero. A driver that
        only checks for zero calls this a success and silently drops two bytes.
        """
        from lager.exceptions import I2CBackendError
        self._attach(0x48, nak_after=1)
        with self.assertRaises(I2CBackendError) as ctx:
            self.driver.write(0x48, [0x0A, 0x0B, 0x0C])
        self.assertIn("byte 1", str(ctx.exception))

    def test_a_fully_acked_write_reaches_the_slave(self):
        slave = self._attach(0x48)
        self.driver.write(0x48, [0x0A, 0x0B, 0x0C])
        self.assertEqual(slave.written, [0x0A, 0x0B, 0x0C])

    def test_writing_to_nothing_raises(self):
        from lager.exceptions import I2CBackendError
        with self.assertRaises(I2CBackendError):
            self.driver.write(0x48, [0x01])

    def test_an_odd_length_read_is_not_over_long(self):
        """u3.i2c() trims its own odd response -- unlike u3.spi().

        A driver that copied the SPI trim would come back a byte short here.
        """
        self._attach(0x48, read_data=[0x11, 0x22, 0x33, 0x44, 0x55])
        for n in (1, 2, 3, 5):
            with self.subTest(n=n):
                self.assertEqual(len(self.driver.read(0x48, n)), n)

    def test_write_read_returns_the_slave_data(self):
        self._attach(0x48, read_data=[0xDE, 0xAD])
        self.assertEqual(self.driver.write_read(0x48, [0x00], 2), [0xDE, 0xAD])

    def test_the_u3_byte_limits_are_enforced_and_are_not_the_t7s(self):
        from lager.exceptions import I2CBackendError
        from lager.protocols.i2c.labjack_ud_i2c import MAX_RX_BYTES, MAX_TX_BYTES
        self.assertEqual((MAX_TX_BYTES, MAX_RX_BYTES), (50, 52))
        self._attach(0x48)
        with self.assertRaises(I2CBackendError):
            self.driver.write(0x48, [0] * 51)
        with self.assertRaises(I2CBackendError):
            self.driver.read(0x48, 53)

    def test_the_configured_speed_reaches_the_wire(self):
        self._attach(0x48)
        self.driver.config(frequency_hz=10_000)
        self.driver.read(0x48, 1)
        self.assertEqual(self.device.i2c_calls[-1]["SpeedAdjust"], 255)

    def test_an_invalid_address_is_refused(self):
        for bad in (-1, 0x80, 0xFF):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.driver.read(bad, 1)


class UDI2CPinMuxTests(_UDTestCase):
    """The mux, which is what silently produced wrong answers last time."""

    def setUp(self):
        super().setUp()
        from lager.protocols.i2c.labjack_ud_i2c import LabJackUDI2C
        LabJackUDI2C._speed_warning_shown = True
        self.driver = LabJackUDI2C(sda_pin="FIO6", scl_pin="FIO7")
        self.device.i2c_devices[0x48] = _FakeI2CSlave(read_data=[0x5A])

    def test_a_transaction_forces_both_lines_digital(self):
        self.device.fio_analog = 0xFF          # everything analog to start
        self.driver.read(0x48, 1)
        self.assertFalse(self.device.fio_analog & (1 << 6))
        self.assertFalse(self.device.fio_analog & (1 << 7))

    def test_a_pin_claimed_by_an_adc_net_is_restored(self):
        """An adc net on AIN6 and this bus's SDA are the same physical line.

        Proves the mux is re-asserted per transaction rather than once at
        construction: the adc claim lands after this bus has already transacted
        successfully, and the next transaction has to undo it.

        The claim goes through the handle manager, which is the only way a mode
        actually changes -- the manager memoizes on (serial, dio) and trusts
        that cache, so a mask poked directly into the device behind its back
        would NOT be noticed. That is sound because the manager owns every
        writer by design, but it does mean this test is only meaningful when it
        goes through the same door a real adc read does.
        """
        self.driver.read(0x48, 1)
        udh.set_channel_mode(self.device, 6, analog=True)   # an adc net claims AIN6
        self.assertTrue(self.device.fio_analog & (1 << 6))
        self.assertEqual(self.driver.read(0x48, 1), [0x5A])
        self.assertFalse(self.device.fio_analog & (1 << 6))

    def test_the_transaction_genuinely_depends_on_the_mux_call(self):
        """The negative control for the test above.

        Without this, a passing suite proves nothing: the transaction might
        succeed whether or not set_channel_mode was ever called. Neutering it
        must break the read.
        """
        from lager.exceptions import I2CBackendError
        self.device.fio_analog = 0xFF
        original = udh.set_channel_mode
        udh.set_channel_mode = lambda *a, **kw: None
        try:
            with self.assertRaises(I2CBackendError):
                self.driver.read(0x48, 1)
        finally:
            udh.set_channel_mode = original

    def test_the_driver_never_writes_the_mask_itself(self):
        """Only the handle manager may touch configIO, and never configU3."""
        self.driver.read(0x48, 1)
        self.assertEqual(self.device.configu3_writes, [])
        for write in self.device.config_writes:
            self.assertTrue(set(write) <= {"FIOAnalog", "EIOAnalog"})


class UDI2CDispatcherTests(unittest.TestCase):
    """Routing, which is where a U3 net could silently reach the T7 driver."""

    def _rec(self, instrument, **kw):
        rec = {"name": "i2c1", "role": "i2c", "instrument": instrument,
               "pin": "FIO6-FIO7", "address": "USB0::0x0CD5::0x0003::::INSTR"}
        rec.update(kw)
        return rec

    def test_every_spelling_of_a_u3_reaches_the_ud_driver(self):
        from lager.protocols.i2c import dispatcher
        from lager.protocols.i2c.labjack_ud_i2c import LabJackUDI2C
        for name in ("LabJack_U3", "labjack_u3", "LabJack U3", "labjack-u3",
                     "LabJack_U6"):
            with self.subTest(instrument=name):
                driver = dispatcher._make_driver(self._rec(name), None)
                self.assertIsInstance(driver, LabJackUDI2C)

    def test_a_t7_still_reaches_the_t7_driver(self):
        from lager.protocols.i2c import dispatcher
        from lager.protocols.i2c.labjack_i2c import LabJackI2C
        for name in ("labjack_t7", "LabJack_T7", "t7", "labjack"):
            with self.subTest(instrument=name):
                driver = dispatcher._make_driver(
                    self._rec(name, pin="FIO4-FIO5"), None)
                self.assertIsInstance(driver, LabJackI2C)

    def test_eio_and_cio_spans_parse_for_a_u3(self):
        """The T7 parser cannot see these; two thirds of a U3's lines are here."""
        from lager.protocols.i2c import dispatcher
        cfg = dispatcher._get_pin_config(
            self._rec("LabJack_U3", pin="EIO0-EIO1"))
        self.assertEqual(cfg, {"sda_pin": 8, "scl_pin": 9})
        cfg = dispatcher._get_pin_config(
            self._rec("LabJack_U3", pin="CIO0-CIO3"))
        self.assertEqual(cfg, {"sda_pin": 16, "scl_pin": 19})

    def test_params_win_over_the_span(self):
        from lager.protocols.i2c import dispatcher
        cfg = dispatcher._get_pin_config(
            self._rec("LabJack_U3", params={"sda_pin": "EIO4", "scl_pin": 5}))
        self.assertEqual(cfg, {"sda_pin": 12, "scl_pin": 5})

    def test_a_high_voltage_pin_is_refused_by_the_dispatcher_too(self):
        from lager.exceptions import I2CBackendError
        from lager.protocols.i2c import dispatcher
        with self.assertRaises(I2CBackendError) as ctx:
            dispatcher._make_driver(
                self._rec("LabJack_U3", pin="FIO0-FIO1"), None)
        self.assertIn("high-voltage", str(ctx.exception))


# --------------------------------------------------------------------------
# SPI (command 0xF8/0x3A)
# --------------------------------------------------------------------------

class UDSPIConstructionTests(unittest.TestCase):
    """Everything refusable without opening a device."""

    def _make(self, **kw):
        from lager.protocols.spi.labjack_ud_spi import LabJackUDSPI
        args = dict(cs_pin="FIO4", clk_pin="FIO5", miso_pin="FIO6",
                    mosi_pin="FIO7")
        args.update(kw)
        return LabJackUDSPI(**args)

    def test_the_defaults_are_labjackpythons_own(self):
        from lager.protocols.spi.labjack_ud_spi import LabJackUDSPI
        driver = LabJackUDSPI(cs_pin=4)
        self.assertEqual(
            (driver._cs_dio, driver._clk_dio, driver._miso_dio, driver._mosi_dio),
            (4, 5, 6, 7))

    def test_high_voltage_pins_are_refused(self):
        from lager.exceptions import SPIBackendError
        for role in ("cs_pin", "clk_pin", "miso_pin", "mosi_pin"):
            with self.subTest(role=role):
                with self.assertRaises(SPIBackendError) as ctx:
                    self._make(**{role: "FIO0"})
                self.assertIn("high-voltage", str(ctx.exception))

    def test_cs_active_high_is_refused_not_ignored(self):
        """AutoCS is active-low with no polarity bit anywhere in the command."""
        from lager.exceptions import SPIBackendError
        with self.assertRaises(SPIBackendError) as ctx:
            self._make(cs_active="high")
        msg = str(ctx.exception)
        self.assertIn("no polarity control", msg)
        self.assertIn("cs_mode='manual'", msg)

    def test_auto_cs_without_a_cs_pin_is_refused(self):
        from lager.exceptions import SPIBackendError
        with self.assertRaises(SPIBackendError):
            self._make(cs_pin=None, cs_mode="auto")

    def test_a_three_wire_net_needs_no_cs_pin(self):
        driver = self._make(cs_pin=None, cs_mode="manual")
        self.assertIsNone(driver._cs_dio)

    def test_duplicate_pins_are_refused(self):
        from lager.exceptions import SPIBackendError
        with self.assertRaises(SPIBackendError):
            self._make(mosi_pin="FIO6", miso_pin="FIO6")

    def test_invalid_mode_word_size_and_bit_order_are_refused(self):
        from lager.exceptions import SPIBackendError
        for kw in ({"mode": 4}, {"mode": -1}, {"word_size": 12},
                   {"bit_order": "middle"}, {"cs_mode": "sometimes"}):
            with self.subTest(**kw):
                with self.assertRaises(SPIBackendError):
                    self._make(**kw)


class UDSPIClockTests(unittest.TestCase):
    """frequency_hz -> SPIClockFactor, from the datasheet formula."""

    def setUp(self):
        from lager.protocols.spi.labjack_ud_spi import LabJackUDSPI
        self.cls = LabJackUDSPI
        LabJackUDSPI._speed_warning_shown = True   # silence the clamp warning

    def test_the_formula_anchors(self):
        self.assertEqual(self.cls._clock_factor_for(100_000), 256)
        self.assertEqual(self.cls._clock_factor_for(50_000), 255)
        self.assertEqual(self.cls._clock_factor_for(391), 1)

    def test_factor_256_is_sent_as_the_byte_zero(self):
        """The wire encodes the maximum factor as 0."""
        driver = self.cls(cs_pin="FIO4")
        self.assertEqual(driver._clock_factor, 256)
        self.assertEqual(driver._wire_clock_factor, 0)

    def test_none_means_as_fast_as_the_part_goes(self):
        self.assertEqual(self.cls._clock_factor_for(None), 256)

    def test_the_gap_below_the_top_is_real(self):
        """Nothing exists between 50 kHz and 100 kHz, so 80 kHz lands on 50.

        Pinned because it looks like a bug from the outside and is not: the
        factor is an integer and 255 and 256 are adjacent.
        """
        self.assertEqual(self.cls._clock_factor_for(80_000), 255)
        self.assertAlmostEqual(self.cls._frequency_for(255), 50_000, delta=1)

    def test_the_clock_is_never_faster_than_asked(self):
        for requested in (100_000, 80_000, 40_000, 10_000, 1_000, 500):
            with self.subTest(requested=requested):
                factor = self.cls._clock_factor_for(requested)
                self.assertLessEqual(self.cls._frequency_for(factor), requested)

    def test_out_of_range_clamps(self):
        self.assertEqual(self.cls._clock_factor_for(10_000_000), 256)
        self.assertEqual(self.cls._clock_factor_for(1), 1)

    def test_a_nonsense_frequency_is_refused(self):
        from lager.exceptions import SPIBackendError
        for bad in (0, -5, "fast"):
            with self.subTest(bad=bad):
                with self.assertRaises(SPIBackendError):
                    self.cls._clock_factor_for(bad)


class UDSPITransferTests(_UDTestCase):
    """The driver against the fake device."""

    def setUp(self):
        super().setUp()
        from lager.protocols.spi.labjack_ud_spi import LabJackUDSPI
        LabJackUDSPI._speed_warning_shown = True
        self.driver = LabJackUDSPI(cs_pin="FIO4", clk_pin="FIO5",
                                   miso_pin="FIO6", mosi_pin="FIO7")

    def test_an_odd_length_transfer_is_trimmed(self):
        """THE headline bug this driver has to avoid.

        u3.spi() pads an odd request to an even length and returns the reply at
        the PADDED length without trimming. A driver that hands
        result["SPIBytes"] straight back grows a phantom trailing byte on every
        odd transfer -- and 1, 3 and 5 byte transfers are the common case.
        """
        for n in (1, 3, 5, 49):
            with self.subTest(n=n):
                self.assertEqual(len(self.driver.read_write([0xA5] * n)), n)

    def test_the_padding_byte_really_did_go_out(self):
        """The trim is on the way back, not a refusal to pad on the way out."""
        self.driver.read_write([0x11, 0x22, 0x33])
        self.assertEqual(self.device.spi_calls[-1]["SPIBytes"],
                         [0x11, 0x22, 0x33])

    def test_an_even_length_transfer_is_untouched(self):
        self.assertEqual(len(self.driver.read_write([0xA5] * 4)), 4)

    def test_loopback_returns_what_was_sent(self):
        """Models MOSI jumpered to MISO, which is the bench test."""
        self.device.spi_response = lambda tx: list(tx)
        pattern = [0x00, 0x55, 0xAA, 0xFF, 0x0F]
        self.assertEqual(self.driver.read_write(pattern), pattern)

    def test_the_driver_does_not_echo_its_own_buffer(self):
        """The fake's default reply differs from the request on purpose."""
        sent = [0x12, 0x34]
        self.assertNotEqual(self.driver.read_write(sent), sent)

    def test_mode_becomes_the_firmwares_letter(self):
        for mode, letter in enumerate(("A", "B", "C", "D")):
            with self.subTest(mode=mode):
                self.driver.config(mode=mode)
                self.driver.read_write([0x00, 0x01])
                self.assertEqual(self.device.spi_calls[-1]["SPIMode"], letter)

    def test_auto_cs_names_the_cs_pin(self):
        self.driver.read_write([0x00, 0x01])
        call = self.device.spi_calls[-1]
        self.assertTrue(call["AutoCS"])
        self.assertEqual(call["CSPinNum"], 4)

    def test_manual_cs_leaves_the_line_alone(self):
        from lager.protocols.spi.labjack_ud_spi import LabJackUDSPI
        driver = LabJackUDSPI(cs_pin=None, clk_pin="FIO5", miso_pin="FIO6",
                              mosi_pin="FIO7", cs_mode="manual")
        driver.read_write([0x00, 0x01])
        self.assertFalse(self.device.spi_calls[-1]["AutoCS"])

    def test_keep_cs_is_refused_under_auto_and_allowed_under_manual(self):
        """Refused, not ignored: a silent release returns data that is wrong."""
        from lager.exceptions import SPIBackendError
        from lager.protocols.spi.labjack_ud_spi import LabJackUDSPI
        with self.assertRaises(SPIBackendError) as ctx:
            self.driver.read_write([0x00], keep_cs=True)
        self.assertIn("manual", str(ctx.exception))

        manual = LabJackUDSPI(cs_pin=None, clk_pin="FIO5", miso_pin="FIO6",
                              mosi_pin="FIO7", cs_mode="manual")
        manual.read_write([0x00], keep_cs=True)   # must not raise

    def test_the_pin_numbers_reach_the_command(self):
        self.driver.read_write([0x00, 0x01])
        call = self.device.spi_calls[-1]
        self.assertEqual((call["CLKPinNum"], call["MISOPinNum"],
                          call["MOSIPinNum"]), (5, 6, 7))

    def test_the_clock_factor_reaches_the_command(self):
        self.driver.config(frequency_hz=50_000)
        self.driver.read_write([0x00, 0x01])
        self.assertEqual(self.device.spi_calls[-1]["SPIClockFactor"], 255)

    def test_the_fifty_byte_limit_is_enforced_and_is_not_the_t7s(self):
        from lager.exceptions import SPIBackendError
        from lager.protocols.spi.labjack_ud_spi import MAX_BYTES_PER_TRANSACTION
        self.assertEqual(MAX_BYTES_PER_TRANSACTION, 50)
        self.driver.read_write([0] * 50)                 # must not raise
        with self.assertRaises(SPIBackendError):
            self.driver.read_write([0] * 51)

    def test_read_clocks_out_the_fill_byte(self):
        self.driver.read(4, fill=0xAB)
        self.assertEqual(self.device.spi_calls[-1]["SPIBytes"], [0xAB] * 4)

    def test_an_empty_transfer_touches_no_hardware(self):
        self.assertEqual(self.driver.read_write([]), [])
        self.assertEqual(self.device.spi_calls, [])


class UDSPIWordTests(_UDTestCase):
    """word_size and bit_order, emulated with the helpers shared with the T7."""

    def setUp(self):
        super().setUp()
        from lager.protocols.spi.labjack_ud_spi import LabJackUDSPI
        LabJackUDSPI._speed_warning_shown = True
        self.cls = LabJackUDSPI
        self.device.spi_response = lambda tx: list(tx)   # loopback

    def _driver(self, **kw):
        return self.cls(cs_pin="FIO4", clk_pin="FIO5", miso_pin="FIO6",
                        mosi_pin="FIO7", **kw)

    def test_words_survive_a_loopback_at_every_size_and_order(self):
        cases = {8: [0x00, 0xA5, 0xFF], 16: [0x0000, 0x1234, 0xFFFF],
                 32: [0x00000000, 0xDEADBEEF, 0xFFFFFFFF]}
        for word_size, words in cases.items():
            for bit_order in ("msb", "lsb"):
                with self.subTest(word_size=word_size, bit_order=bit_order):
                    driver = self._driver(word_size=word_size,
                                          bit_order=bit_order)
                    self.assertEqual(driver.read_write(words), words)

    def test_a_sixteen_bit_word_goes_out_most_significant_byte_first(self):
        self._driver(word_size=16).read_write([0x1234])
        self.assertEqual(self.device.spi_calls[-1]["SPIBytes"], [0x12, 0x34])

    def test_lsb_first_reverses_in_software(self):
        self._driver(bit_order="lsb").read_write([0x01, 0x80])
        self.assertEqual(self.device.spi_calls[-1]["SPIBytes"], [0x80, 0x01])

    def test_the_byte_cap_becomes_a_word_cap(self):
        from lager.exceptions import SPIBackendError
        self.assertEqual(self._driver(word_size=16)._max_words, 25)
        self.assertEqual(self._driver(word_size=32)._max_words, 12)
        with self.assertRaises(SPIBackendError):
            self._driver(word_size=16).read(26)

    def test_an_oversized_word_is_refused(self):
        from lager.exceptions import SPIBackendError
        with self.assertRaises(SPIBackendError):
            self._driver(word_size=8).read_write([0x1FF])


class UDSPIPinMuxTests(_UDTestCase):
    """The mux, which is what silently produced wrong answers last time."""

    def setUp(self):
        super().setUp()
        from lager.protocols.spi.labjack_ud_spi import LabJackUDSPI
        LabJackUDSPI._speed_warning_shown = True
        self.driver = LabJackUDSPI(cs_pin="FIO4", clk_pin="FIO5",
                                   miso_pin="FIO6", mosi_pin="FIO7")
        self.device.spi_response = lambda tx: list(tx)

    def test_a_transfer_forces_every_line_digital(self):
        self.device.fio_analog = 0xFF
        self.driver.read_write([0x01, 0x02])
        for dio in (4, 5, 6, 7):
            self.assertFalse(self.device.fio_analog & (1 << dio))

    def test_a_pin_claimed_by_an_adc_net_is_restored(self):
        """MISO on FIO6 and an adc net on AIN6 are the same physical line."""
        self.driver.read_write([0x01, 0x02])
        udh.set_channel_mode(self.device, 6, analog=True)
        self.assertEqual(self.driver.read_write([0x01, 0x02]), [0x01, 0x02])

    def test_the_transfer_genuinely_depends_on_the_mux_call(self):
        """Negative control: neuter the mux and the transfer must break."""
        from lager.exceptions import SPIBackendError
        self.device.fio_analog = 0xFF
        original = udh.set_channel_mode
        udh.set_channel_mode = lambda *a, **kw: None
        try:
            with self.assertRaises(SPIBackendError):
                self.driver.read_write([0x01, 0x02])
        finally:
            udh.set_channel_mode = original

    def test_direction_config_is_left_to_the_firmware(self):
        """DisableDirConfig=False buys direction; it does NOT buy the mux."""
        self.driver.read_write([0x01, 0x02])
        self.assertFalse(self.device.spi_calls[-1]["DisableDirConfig"])

    def test_the_driver_never_writes_the_mask_itself(self):
        self.driver.read_write([0x01, 0x02])
        self.assertEqual(self.device.configu3_writes, [])


class UDSPIDispatcherTests(unittest.TestCase):
    """Routing and the span parser, including the MISO/MOSI order trap."""

    def _rec(self, instrument="LabJack_U3", **kw):
        rec = {"name": "spi1", "role": "spi", "instrument": instrument,
               "pin": "FIO4-FIO7", "address": "USB0::0x0CD5::0x0003::::INSTR"}
        rec.update(kw)
        return rec

    def test_every_spelling_of_a_u3_reaches_the_ud_driver(self):
        from lager.protocols.spi import dispatcher
        from lager.protocols.spi.labjack_ud_spi import LabJackUDSPI
        for name in ("LabJack_U3", "labjack_u3", "LabJack U3", "labjack-u3"):
            with self.subTest(instrument=name):
                self.assertIsInstance(
                    dispatcher._make_driver(self._rec(name), None), LabJackUDSPI)

    def test_a_t7_still_reaches_the_t7_driver(self):
        from lager.protocols.spi import dispatcher
        from lager.protocols.spi.labjack_spi import LabJackSPI
        for name in ("labjack_t7", "LabJack_T7", "t7", "labjack"):
            with self.subTest(instrument=name):
                self.assertIsInstance(
                    dispatcher._make_driver(self._rec(name, pin="FIO0-FIO3"),
                                            None), LabJackSPI)

    def test_the_t7_span_order_is_unchanged(self):
        """CS / CLK / MOSI / MISO on a T7. Pinned so the U3 cannot disturb it."""
        from lager.protocols.spi import dispatcher
        self.assertEqual(
            dispatcher._get_pin_config(self._rec("labjack_t7", pin="FIO0-FIO3")),
            {"cs_pin": 0, "clk_pin": 1, "mosi_pin": 2, "miso_pin": 3})

    def test_the_u3_span_order_is_miso_before_mosi(self):
        """CS / CLK / MISO / MOSI on a U3 -- the OTHER way round from the T7.

        This is LabJackPython's own default ordering and what LabJack's U3
        wiring diagrams show. Getting it wrong swaps the data lines with no
        error anywhere; only a scope or a loopback would ever show it.
        """
        from lager.protocols.spi import dispatcher
        self.assertEqual(
            dispatcher._get_pin_config(self._rec(pin="FIO4-FIO7")),
            {"cs_pin": 4, "clk_pin": 5, "miso_pin": 6, "mosi_pin": 7})

    def test_a_three_pin_u3_span_drops_cs_from_the_front(self):
        from lager.protocols.spi import dispatcher
        self.assertEqual(
            dispatcher._get_pin_config(self._rec(pin="FIO5-FIO7")),
            {"clk_pin": 5, "miso_pin": 6, "mosi_pin": 7})

    def test_a_four_pin_u3_span_defaults_to_auto_cs(self):
        """The T7 rule would call every U3 span 'manual'."""
        from lager.protocols.spi import dispatcher
        driver = dispatcher._make_driver(self._rec(pin="FIO4-FIO7"), None)
        self.assertEqual(driver._cs_mode, "auto")

    def test_a_three_pin_u3_span_defaults_to_manual_cs(self):
        from lager.protocols.spi import dispatcher
        driver = dispatcher._make_driver(self._rec(pin="FIO5-FIO7"), None)
        self.assertEqual(driver._cs_mode, "manual")

    def test_a_span_of_the_wrong_width_is_refused(self):
        """4 lines (with CS) or 3 (without) are the only widths that mean
        anything. FIO4-FIO6 is NOT here: that is the valid 3-wire case."""
        from lager.exceptions import SPIBackendError
        from lager.protocols.spi import dispatcher
        for span in ("FIO4-FIO5", "FIO4-EIO0", "FIO7-FIO4"):
            with self.subTest(span=span):
                with self.assertRaises(SPIBackendError) as ctx:
                    dispatcher._get_pin_config(self._rec(pin=span))
                self.assertIn("span", str(ctx.exception))

    def test_eio_spans_and_params_work(self):
        from lager.protocols.spi import dispatcher
        self.assertEqual(
            dispatcher._get_pin_config(self._rec(pin="EIO0-EIO3")),
            {"cs_pin": 8, "clk_pin": 9, "miso_pin": 10, "mosi_pin": 11})
        self.assertEqual(
            dispatcher._get_pin_config(self._rec(
                params={"clk_pin": "FIO5", "mosi_pin": "EIO0",
                        "miso_pin": "CIO0", "cs_pin": 4})),
            {"clk_pin": 5, "mosi_pin": 8, "miso_pin": 16, "cs_pin": 4})

    def test_a_net_that_asked_for_no_speed_gets_the_maximum(self):
        """The shared default is 1 MHz, twelve times a U3's ceiling."""
        from lager.protocols.spi import dispatcher
        driver = dispatcher._make_driver(self._rec(), None)
        self.assertEqual(driver._clock_factor, 256)
        self.assertEqual(driver._wire_clock_factor, 0)

    def test_an_explicit_speed_is_honoured(self):
        from lager.protocols.spi import dispatcher
        driver = dispatcher._make_driver(
            self._rec(params={"frequency_hz": 50_000}), None)
        self.assertEqual(driver._clock_factor, 255)

    def test_a_high_voltage_span_is_refused(self):
        from lager.exceptions import SPIBackendError
        from lager.protocols.spi import dispatcher
        with self.assertRaises(SPIBackendError) as ctx:
            dispatcher._make_driver(self._rec(pin="FIO0-FIO3"), None)
        self.assertIn("high-voltage", str(ctx.exception))
