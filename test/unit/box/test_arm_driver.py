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
    ArmBackendError, NotHomedError, OutOfBoundsError, SettingNotAppliedError,
    UnsupportedFirmwareError)

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


def m503_report(p, r, t, *, idle_wait=False):
    """An M503 reply shaped like firmware V2.4.4's, with the M204 line set.

    ``idle_wait`` sprinkles the ``wait`` line V2.4.x prints every second while
    idle through the report.
    """
    lines = [
        b"echo:  G21    ; Units in mm (mm)\n",
        b"echo:; Steps per unit:\n",
        b"echo: M92 X482.54 Y482.54 Z482.54 E379.20\n",
        b"echo:; Filament load/unload lengths:\n",
        b"echo:; Acceleration (units/s2): P<print_accel> R<retract_accel> T<travel_accel>\n",
        ("echo:  M204 P%s R%s T%s\n" % (p, r, t)).encode(),
        b"echo:  M603 L0.00 U100.00\n",
        b"echo:  M889 X2766 Y1590 Z3346\n",
        b"The current module is PEN\n",
    ]
    if idle_wait:
        lines = [b"wait\n"] + lines[:4] + [b"wait\n"] + lines[4:] + [b"wait\n"]
    return lines + [b"ok\n"]


class AccelerationArm:
    """Replies like an arm that applies M204 and reports it back in M503."""

    def __init__(self, p=60.0, r=40.0, t=60.0, *, applies=True, idle_wait=False):
        self.p, self.r, self.t = p, r, t
        self.applies = applies
        self.idle_wait = idle_wait

    def __call__(self, data):
        if data.startswith(b"M503"):
            return m503_report("%.2f" % self.p, "%.2f" % self.r, "%.2f" % self.t,
                               idle_wait=self.idle_wait)
        if data.startswith(b"M204") and self.applies:
            fields = {part[0]: float(part[1:]) for part in data.decode().split()[1:]}
            self.p, self.t, self.r = fields["P"], fields["T"], fields["R"]
        return [b"ok\n"]


class TestSetAcceleration(_DriverTest):
    def test_sends_marlin_print_travel_retract_letters(self):
        arm, fake = self.make_arm(AccelerationArm())
        arm.set_acceleration(200, 150, 70)
        self.assertEqual(fake.writes, [b"M204 P200 T150 R70\r\n", b"M503\r\n"])

    def test_without_retract_the_arms_current_retract_is_kept(self):
        # The default used to be 60, which silently replaced V2.4.x's 40.
        arm, fake = self.make_arm(AccelerationArm(60, 40, 60))
        arm.set_acceleration(100, 80)
        self.assertEqual(
            fake.writes, [b"M503\r\n", b"M204 P100 T80 R40\r\n", b"M503\r\n"])

    def test_returns_the_values_read_back(self):
        arm, _ = self.make_arm(AccelerationArm())
        self.assertEqual(arm.set_acceleration(61, 62, 43), (61.0, 62.0, 43.0))

    def test_a_write_the_arm_did_not_apply_is_an_error(self):
        arm, _ = self.make_arm(AccelerationArm(60, 40, 60, applies=False))
        with self.assertRaises(SettingNotAppliedError) as caught:
            arm.set_acceleration(61, 62, 43)
        self.assertIsInstance(caught.exception, ArmBackendError)
        self.assertIn("print=60 travel=60 retract=40", str(caught.exception))
        self.assertIn("print=61 travel=62 retract=43", str(caught.exception))


class TestReadSettings(_DriverTest):
    def test_returns_the_m503_report_including_the_calibration(self):
        arm, fake = self.make_arm(AccelerationArm(idle_wait=True))
        lines = arm.read_settings()
        self.assertEqual(fake.writes, [b"M503\r\n"])
        self.assertIn("echo:  M889 X2766 Y1590 Z3346", lines)
        self.assertNotIn("wait", lines)
        self.assertNotIn("ok", lines)


class TestGetAcceleration(_DriverTest):
    def test_reads_print_travel_retract_from_m503(self):
        # M503 prints P R T, in that order, not the P T R order M204 takes.
        arm, fake = self.make_arm(AccelerationArm(60, 40, 61))
        self.assertEqual(arm.get_acceleration(), (60.0, 61.0, 40.0))
        self.assertEqual(fake.writes, [b"M503\r\n"])

    def test_idle_wait_lines_do_not_end_or_break_the_read(self):
        arm, _ = self.make_arm(AccelerationArm(60, 40, 60, idle_wait=True))
        self.assertEqual(arm.get_acceleration(), (60.0, 60.0, 40.0))

    def test_a_settings_line_containing_ok_does_not_end_the_report_early(self):
        # _send_cmd ends on any line containing "ok"; this read needs "ok" alone.
        def replies(data):
            report = m503_report("60.00", "40.00", "60.00")
            return [b"echo:; Lookahead ok-ish setting\n"] + report
        arm, _ = self.make_arm(replies)
        self.assertEqual(arm.get_acceleration(), (60.0, 60.0, 40.0))

    def test_a_report_without_m204_is_an_error(self):
        arm, _ = self.make_arm(lambda data: [b"echo:  G21\n", b"ok\n"])
        with self.assertRaises(RuntimeError) as caught:
            arm.get_acceleration()
        self.assertIn("no M204 line", str(caught.exception))

    def test_a_silent_arm_times_out(self):
        arm, _ = self.make_arm(lambda data: [])
        clock = itertools.count(step=1.0)
        with mock.patch.object(rotrics.time, "monotonic", side_effect=lambda: next(clock)):
            with self.assertRaises(RuntimeError) as caught:
                arm.get_acceleration()
        self.assertIn("M503", str(caught.exception))


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


# Firmware V2.4.4's M114 reply (captured on a real arm): a Count line, a Real
# position line that also contains "X:", and a DEXARM Theta line.
V244_HOME_M114 = [
    b"X:0.00 Y:300.00 Z:0.00 E:0.00 Count A:0 B:25843 C:-29572\n",
    b"Count A:0 B:25843 C:-29572\n",
    b"Real position X:0.00 Y:300.00 Z:0.00 E:0.00\n",
    b"DEXARM Theta A:0.00  Theta B:53.56  Theta C:-61.28\n",
    b"\n",
    b"ok\n",
]


class TestIdleWaitLines(_DriverTest):
    """Firmware V2.4.x prints ``wait`` every second while idle."""

    def test_position_with_wait_lines_in_the_v244_reply(self):
        reply = [b"wait\n"] + V244_HOME_M114[:2] + [b"wait\n"] + V244_HOME_M114[2:]
        arm, _ = self.make_arm(replies_with_position(reply))
        self.assertEqual(arm.get_full_position(),
                         (0.0, 300.0, 0.0, 0.0, 0.0, 53.56, -61.28))

    def test_firmware_version_with_a_wait_line_first(self):
        sleep = mock.patch.object(rotrics.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

        def answer(data):
            if data.startswith(b"M2010"):
                return [b"wait\n", b"Firmware V2.4.4\r\n", b"ok\n"]
            return [b"ok\n"]
        with mock.patch.object(rotrics.serial, "Serial", return_value=FakeSerial(answer)):
            arm = rotrics.Dexarm(port="/dev/ttyFAKE")
        self.assertEqual(arm.firmware_version, (2, 4, 4))

    def test_module_type_skips_wait_lines(self):
        arm, fake = self.make_arm(lambda data: [
            b"wait\n", b"The current module is PEN\n", b"wait\n", b"ok\n"])
        self.assertEqual(arm.get_module_type(), "PEN")
        self.assertEqual(fake.writes, [b"M888\r"])

    def test_module_type_drains_a_stale_ok_before_asking(self):
        # A late "ok" from an earlier command used to end this read at once,
        # returning None before the arm had answered.
        arm, fake = self.make_arm(lambda data: [
            b"The current module is LASER\n", b"ok\n"])
        fake._pending.extend([b"ok\n", b"wait\n"])
        self.assertEqual(arm.get_module_type(), "LASER")


class TestExactGcode(_DriverTest):
    """Every accessory and raw-motion helper, pinned to the bytes it writes.

    None of these can run on a bench arm without the accessory attached, so
    this is their only coverage.
    """

    CASES = [
        ("soft_gripper_pick", (), b"M1001\r"),
        ("soft_gripper_place", (), b"M1000\r"),
        ("soft_gripper_neutral", (), b"M1002\r"),
        ("soft_gripper_stop", (), b"M1003\r"),
        # The air picker's pick/place codes are the reverse of the gripper's.
        ("air_picker_pick", (), b"M1000\r"),
        ("air_picker_place", (), b"M1001\r"),
        ("air_picker_neutral", (), b"M1002\r"),
        ("air_picker_stop", (), b"M1003\r"),
        ("laser_on", (), b"M3 S0\r"),
        ("laser_on", (128,), b"M3 S128\r"),
        ("laser_off", (), b"M5\r"),
        ("conveyor_belt_forward", (), b"M2012 F0D0\r"),
        ("conveyor_belt_forward", (3000,), b"M2012 F3000D0\r"),
        ("conveyor_belt_backward", (3000,), b"M2012 F3000D1\r"),
        ("conveyor_belt_stop", (), b"M2013\r"),
        ("sliding_rail_init", (), b"M2005\r"),
        ("set_module_type", (0,), b"M888 P0\r"),
        ("set_module_type", (1,), b"M888 P1\r"),
        ("set_workorigin", (), b"G92 X0 Y0 Z0 E0\r"),
        ("delay_ms", (250,), b"G4 P250\r"),
        ("delay_s", (2,), b"G4 S2\r"),
        ("dealy_ms", (250,), b"G4 P250\r"),
        ("dealy_s", (2,), b"G4 S2\r"),
        ("enable_motor", (), b"M17\r"),
        ("disable_motor", (), b"M18\r"),
        ("go_home", (), b"M1112\r"),
    ]

    def test_each_helper_writes_exactly_its_gcode(self):
        for method, args, expected in self.CASES:
            with self.subTest(method=method, args=args):
                arm, fake = self.make_arm(lambda data: [b"ok\n"])
                getattr(arm, method)(*args)
                self.assertEqual(fake.writes, [expected])

    def test_move_to_gcode_framing(self):
        arm, fake = self.make_arm(lambda data: [b"ok\n"])
        arm.move_to_gcode(10, 250.5, -3, e=1.6, feedrate=3000)
        self.assertEqual(fake.writes, [b"G1F3000X10Y250.5Z-3E2\r\n"])

    def test_move_to_gcode_leaves_out_unset_axes(self):
        arm, fake = self.make_arm(lambda data: [b"ok\n"])
        arm.move_to_gcode(z=5)
        self.assertEqual(fake.writes, [b"G1F2000Z5\r\n"])

    def test_fast_move_to_is_a_g0(self):
        arm, fake = self.make_arm(lambda data: [b"ok\n"])
        arm.fast_move_to(0, 300, 0)
        self.assertEqual(fake.writes, [b"G0F2000X0Y300Z0\r\n"])


class TestHazardousCommands(unittest.TestCase):
    """M892 reloads stored settings and M895 can halt an un-homed arm's firmware.

    The driver never sends either. M889 (recalibrate) is sent only by
    save_position, which read_and_save_position calls.
    """

    def test_driver_never_sends_m892_or_m895(self):
        import inspect
        source = inspect.getsource(rotrics)
        self.assertNotIn("M892", source)
        self.assertNotIn("M895", source)

    def test_only_save_position_sends_m889(self):
        import inspect
        senders = [
            name for name, fn in inspect.getmembers(rotrics.Dexarm, inspect.isfunction)
            if '"M889' in inspect.getsource(fn)
        ]
        self.assertEqual(senders, ["save_position"])
