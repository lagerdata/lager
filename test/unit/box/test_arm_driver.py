# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the Rotrics Dexarm driver (box/lager/automation/arm/rotrics.py).

The serial port is a scripted fake: every write queues the reply lines the
firmware sends for that command, so these tests pin the exact G-code on the
wire and how the replies are parsed. Reply shapes are copied from a real Dexarm
(hardware V3.2): M114 prints no ``DEXARM Theta`` line, M2010 prints
``Firmware V<x.y.z>``, and M1112 prints ``busy: processing`` before its ok.

Covered:
  - set_acceleration sends Marlin's P/T/R letters. The old P/T/T string set
    travel acceleration to the retract value and never set retract.
  - move_to and move_relative refuse targets outside the workspace bounds
    before anything is written, as the `lager arm` path does.
  - move_to and move_relative refuse firmware older than V2.1.4, or firmware
    that does not report a version: Rotrics swapped X and Y in V2.1.4, so older
    firmware would move the arm sideways. go_home and position still work.
  - go_home returns when M1112 sends its ok, and times out if it never does.
  - a move before homing raises NotHomedError instead of timing out.
  - position parsing of the firmware's M114 reply, the retry when an ``ok``
    arrives before the position line, and a silent arm failing within three
    bounded attempts.
  - a saved net's arm serial comes from its VISA address when that is the only
    place it is recorded, so the net opens its own arm.
"""

import itertools
import unittest
from unittest import mock

from lager.automation.arm import rotrics
from lager.automation.arm.arm_net import (
    ArmBackendError, NotHomedError, OutOfBoundsError, UnsupportedFirmwareError)

HOME_M114 = [b"X:0.00 Y:300.00 Z:0.00 E:0.00 Count X:0 Y:0 Z:0\n", b"ok\n"]
SUPPORTED_FIRMWARE = b"Firmware V2.1.4\r\n"
OLD_FIRMWARE = b"Firmware V2.1.3\r\n"


class FakeSerial:
    """Scripted stand-in for ``serial.Serial``."""

    def __init__(self, replies):
        self.writes = []
        self._pending = []
        self._replies = replies

    def isOpen(self):
        return True

    @property
    def in_waiting(self):
        return sum(len(line) for line in self._pending)

    def read(self, size=1):
        self._pending.clear()
        return b""

    def write(self, data):
        self.writes.append(data)
        self._pending.extend(self._replies(data))
        return len(data)

    def readline(self):
        return self._pending.pop(0) if self._pending else b""

    def close(self):
        pass


def replies_with_position(m114_lines=HOME_M114):
    def replies(data):
        if data.startswith(b"M114"):
            return list(m114_lines)
        return [b"ok\n"]
    return replies


class _DriverTest(unittest.TestCase):
    def make_arm(self, replies, firmware=SUPPORTED_FIRMWARE):
        def answer(data):
            if data.startswith(b"M2010"):
                return ([firmware] if firmware else []) + [b"ok\n"]
            return replies(data)

        sleep = mock.patch.object(rotrics.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        fake = FakeSerial(answer)
        with mock.patch.object(rotrics.serial, "Serial", return_value=fake):
            arm = rotrics.Dexarm(port="/dev/ttyFAKE")
        fake.writes.clear()   # drop the M2010 version query sent when the port opens
        return arm, fake

    def fake_clock(self, step=0.5):
        """Make every time.time() call advance by ``step`` seconds."""
        clock = itertools.count(step=step)
        patcher = mock.patch.object(rotrics.time, "time", side_effect=lambda: next(clock))
        patcher.start()
        self.addCleanup(patcher.stop)


class TestSetAcceleration(_DriverTest):
    def test_sends_marlin_print_travel_retract_letters(self):
        arm, fake = self.make_arm(replies_with_position())
        arm.set_acceleration(200, 150, 70)
        self.assertEqual(fake.writes, [b"M204 P200 T150 R70\r\n"])

    def test_retract_defaults_to_60(self):
        arm, fake = self.make_arm(replies_with_position())
        arm.set_acceleration(100, 80)
        self.assertEqual(fake.writes, [b"M204 P100 T80 R60\r\n"])


class TestWorkspaceBounds(_DriverTest):
    def test_move_to_outside_bounds_writes_nothing(self):
        arm, fake = self.make_arm(replies_with_position())
        with self.assertRaises(OutOfBoundsError) as caught:
            arm.move_to(500, 300, 0)
        self.assertIn("X=500", str(caught.exception))
        self.assertEqual(fake.writes, [])

    def test_move_relative_checks_the_resulting_target(self):
        # Z is 0 at home; dz=+150 would end at Z=150, above the 100 mm bound.
        arm, fake = self.make_arm(replies_with_position())
        with self.assertRaises(OutOfBoundsError):
            arm.move_relative(dz=150)
        self.assertEqual(fake.writes, [b"M114\r"])  # read the pose, sent no move

    def test_bounds_are_inclusive(self):
        rotrics.Dexarm.check_bounds(-300, 170, -140)
        rotrics.Dexarm.check_bounds(300, 360, 100)

    def test_out_of_bounds_is_an_arm_error_and_a_value_error(self):
        with self.assertRaises(OutOfBoundsError) as caught:
            rotrics.Dexarm.check_bounds(0, 0, 0)
        self.assertIsInstance(caught.exception, ArmBackendError)
        self.assertIsInstance(caught.exception, ValueError)


class TestFirmwareVersion(_DriverTest):
    def test_version_is_read_when_the_port_opens(self):
        arm, _ = self.make_arm(replies_with_position(), firmware=OLD_FIRMWARE)
        self.assertEqual(arm.firmware_version, (2, 1, 3))

    def test_old_firmware_refuses_move_to_before_any_write(self):
        arm, fake = self.make_arm(replies_with_position(), firmware=OLD_FIRMWARE)
        with self.assertRaises(UnsupportedFirmwareError) as caught:
            arm.move_to(0, 300, 0)
        self.assertIn("V2.1.3", str(caught.exception))
        self.assertEqual(fake.writes, [])

    def test_old_firmware_refuses_move_relative_before_any_write(self):
        arm, fake = self.make_arm(replies_with_position(), firmware=OLD_FIRMWARE)
        with self.assertRaises(UnsupportedFirmwareError):
            arm.move_relative(dz=5)
        self.assertEqual(fake.writes, [])

    def test_unreported_version_refuses_moves(self):
        arm, _ = self.make_arm(replies_with_position(), firmware=None)
        self.assertIsNone(arm.firmware_version)
        with self.assertRaises(UnsupportedFirmwareError):
            arm.move_to(0, 300, 0)

    def test_supported_firmware_moves(self):
        arm, fake = self.make_arm(replies_with_position(), firmware=b"Firmware V2.1.5\r\n")
        arm.move_to(0, 300, 0)
        self.assertTrue(any(w.startswith(b"G1") for w in fake.writes))

    def test_old_firmware_still_homes_and_reads_position(self):
        def replies(data):
            if data.startswith(b"M1112"):
                return [b"M1112\n", b"ok\n"]
            return replies_with_position()(data)

        arm, fake = self.make_arm(replies, firmware=OLD_FIRMWARE)
        arm.go_home()
        self.assertEqual(arm.position(), (0.0, 300.0, 0.0))
        self.assertEqual(fake.writes, [b"M1112\r", b"M114\r"])


class TestGoHome(_DriverTest):
    def test_go_home_returns_when_m1112_sends_its_ok(self):
        def replies(data):
            if data.startswith(b"M1112"):
                return [b"M1112\n", b"echo:busy: processing\n", b"ok\n"]
            return replies_with_position()(data)

        arm, fake = self.make_arm(replies)
        arm.go_home()
        self.assertEqual(fake.writes, [b"M1112\r"])

    def test_go_home_times_out_if_the_arm_never_finishes(self):
        def replies(data):
            if data.startswith(b"M1112"):
                return [b"M1112\n", b"echo:busy: processing\n"]   # no ok
            return replies_with_position()(data)

        arm, _ = self.make_arm(replies)
        self.fake_clock()
        with self.assertRaises(RuntimeError):
            arm.go_home(timeout=5)


class TestNotHomed(_DriverTest):
    def test_move_before_homing_raises_not_homed(self):
        def replies(data):
            if data.startswith(b"G1"):
                return [b"Send M1112 or click HOME to initialize DexArm first "
                        b"before any motion.\n", b"ok\n"]
            return replies_with_position()(data)

        arm, _ = self.make_arm(replies)
        with self.assertRaises(NotHomedError):
            arm.move_to(0, 300, 0)


class TestPosition(_DriverTest):
    def test_parses_the_firmware_m114_reply(self):
        # Un-homed after power-on: Y=0 and Z=200 with zero X/Y step counts.
        lines = [b"X:0.00 Y:0.00 Z:200.00 E:0.00 Count X:0 Y:0 Z:96508\n", b"ok\n"]
        arm, _ = self.make_arm(replies_with_position(lines))
        self.assertEqual(arm.position(), (0.0, 0.0, 200.0))
        # This firmware prints no DEXARM Theta line, so there are no joint angles.
        self.assertEqual(arm.get_full_position()[4:], (None, None, None))

    def test_ok_before_the_position_line_is_retried(self):
        calls = {"m114": 0}

        def replies(data):
            if data.startswith(b"M114"):
                calls["m114"] += 1
                return [b"ok\n"] if calls["m114"] == 1 else list(HOME_M114)
            return [b"ok\n"]

        arm, _ = self.make_arm(replies)
        self.assertEqual(arm.position(), (0.0, 300.0, 0.0))
        self.assertEqual(calls["m114"], 2)

    def test_a_silent_arm_fails_within_three_bounded_attempts(self):
        # A halted arm answers nothing. The read must give up on its own well
        # inside hardware_service's call budget, not wait 15 s per attempt.
        arm, fake = self.make_arm(lambda data: [])
        self.fake_clock()
        with self.assertRaises(RuntimeError):
            arm.position()
        self.assertEqual(fake.writes.count(b"M114\r"), 3)


class TestMoveTimeout(_DriverTest):
    def test_timeout_message_points_at_a_longer_timeout(self):
        # Bounds are checked before a move is sent, so the message must not
        # blame them; a long move that needs more time is the likelier cause.
        from lager.automation.arm.arm_net import MovementTimeoutError

        arm, _ = self.make_arm(replies_with_position())   # M114 never reaches the target
        clock = itertools.count(step=1.0)
        with mock.patch.object(rotrics.time, "monotonic", side_effect=lambda: next(clock)):
            with self.assertRaises(MovementTimeoutError) as caught:
                arm.move_to(50, 250, 30, timeout=5)
        message = str(caught.exception)
        self.assertIn("within 5 s", message)
        self.assertIn("longer timeout", message)
        self.assertNotIn("coordinates are out of bounds", message)


class TestSerialFromNetRecord(unittest.TestCase):
    serial_from = staticmethod(rotrics.Dexarm.serial_from_net_record)

    def test_address_is_used_when_it_is_the_only_record_of_the_serial(self):
        # The shape `lager nets add-all` saves for an arm.
        rec = {"name": "arm1", "role": "arm", "pin": "/dev/ttyACM0",
               "address": "USB0::0x0483::0x5740::ARM0001234::INSTR"}
        self.assertEqual(self.serial_from(rec), "ARM0001234")

    def test_explicit_serial_wins(self):
        rec = {"serial": "EXPLICIT", "location": {"serial_number": "LOC"},
               "address": "USB0::0x0483::0x5740::ADDR::INSTR"}
        self.assertEqual(self.serial_from(rec), "EXPLICIT")

    def test_location_serial_beats_address(self):
        rec = {"location": {"serial_number": "LOC"},
               "address": "USB0::0x0483::0x5740::ADDR::INSTR"}
        self.assertEqual(self.serial_from(rec), "LOC")

    def test_mux_mapping_device_override(self):
        mapping = {"location": "/dev/ttyACM0",
                   "device_override": "USB0::0x0483::0x5740::MUX1::INSTR"}
        self.assertEqual(self.serial_from(mapping), "MUX1")

    def test_no_serial_anywhere(self):
        self.assertIsNone(self.serial_from({"pin": "/dev/ttyACM0",
                                            "address": "/dev/ttyACM0"}))
        self.assertIsNone(self.serial_from(None))


class TestSavedNetOpensItsOwnArm(unittest.TestCase):
    def test_add_all_record_opens_by_the_serial_in_its_address(self):
        from lager.nets.net import Net
        from lager.nets.constants import NetType

        rec = {"name": "arm1", "role": "arm", "instrument": "Rotrix_Dexarm",
               "pin": "/dev/ttyACM0",
               "address": "USB0::0x0483::0x5740::ARM0001234::INSTR"}
        with mock.patch.object(Net, "get_local_nets", return_value=[rec]), \
                mock.patch("lager.nets.net.Dexarm") as dexarm:
            dexarm.serial_from_net_record = rotrics.Dexarm.serial_from_net_record
            Net.get_from_saved_json("arm1", NetType.Arm)

        dexarm.assert_called_once_with(
            port=None, serial_number="ARM0001234", name="arm1", pin="/dev/ttyACM0")


if __name__ == "__main__":
    unittest.main()
