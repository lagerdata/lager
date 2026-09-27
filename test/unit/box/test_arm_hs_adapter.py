# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the robot-arm hardware_service adapter (box/lager/arm_hs.py).

The Dexarm driver is replaced with a fake, and ``Net.get_from_saved_json`` is
patched to hand the adapter the next fake each time it opens the port.

Covered:
  - a serial error drops the cached port, so the next command reopens it.
    hardware_service's own retry recognizes only VISA and ENODEV errors, and
    pyserial's disconnect error is neither.
  - position retries once on the reopened port. Motion never retries, because
    the move may already have reached the arm.
  - move and move_by refuse a wait past MAX_MOVE_TIMEOUT_S before opening the
    port. A longer call outlives hardware_service's 30 s deadline, and that
    restarts the service.
"""

import unittest
from unittest import mock

from serial.serialutil import SerialException

from lager import arm_hs

DISCONNECT = SerialException(
    "device reports readiness to read but returned no data "
    "(device disconnected or multiple access on port?)")


class FakeArm:
    def __init__(self, fail=None):
        self.fail = dict(fail or {})
        self.calls = []
        self.closed = False

    def _record(self, name, *args):
        self.calls.append((name,) + args)
        if name in self.fail:
            raise self.fail[name]

    def position(self):
        self._record("position")
        return (0.0, 300.0, 0.0)

    def move_to(self, x, y, z, *, timeout):
        self._record("move_to", x, y, z, timeout)

    def move_relative(self, dx, dy, dz, *, timeout):
        self._record("move_relative", dx, dy, dz, timeout)
        return (dx, 300.0 + dy, dz)

    def enable_motor(self):
        self._record("enable_motor")

    def close(self):
        self.closed = True


class TestArmAdapter(unittest.TestCase):
    def open_with(self, *arms):
        """Each (re)open of the port returns the next fake arm."""
        patcher = mock.patch("lager.nets.net.Net.get_from_saved_json",
                             side_effect=list(arms))
        opener = patcher.start()
        self.addCleanup(patcher.stop)
        return arm_hs.ArmHardwareAdapter("arm1"), opener

    def test_serial_exception_is_an_oserror(self):
        # The adapter catches OSError; this pins pyserial's hierarchy.
        self.assertTrue(issubclass(SerialException, OSError))

    def test_position_reopens_and_retries_after_a_serial_error(self):
        stale, fresh = FakeArm(fail={"position": DISCONNECT}), FakeArm()
        adapter, opener = self.open_with(stale, fresh)

        self.assertEqual(adapter.position(), [0.0, 300.0, 0.0])
        self.assertTrue(stale.closed)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(fresh.calls, [("position",)])

    def test_move_is_not_retried_but_the_next_command_reopens(self):
        stale, fresh = FakeArm(fail={"move_to": DISCONNECT}), FakeArm()
        adapter, opener = self.open_with(stale, fresh)

        with self.assertRaises(RuntimeError) as caught:
            adapter.move(0, 300, 0)
        self.assertIn("connection lost", str(caught.exception))
        self.assertTrue(stale.closed)
        self.assertEqual([c[0] for c in stale.calls], ["move_to"])

        adapter.enable_motor()
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(fresh.calls, [("enable_motor",)])

    def test_a_non_serial_error_keeps_the_port(self):
        arm = FakeArm(fail={"move_to": ValueError("Coordinates out of bounds")})
        adapter, opener = self.open_with(arm)

        with self.assertRaises(ValueError):
            adapter.move(0, 0, 0)
        self.assertFalse(arm.closed)
        adapter.position()
        self.assertEqual(opener.call_count, 1)

    def test_move_passes_the_wait_and_returns_the_new_position(self):
        arm = FakeArm()
        adapter, _ = self.open_with(arm)
        self.assertEqual(adapter.move(10, 250, 5, timeout=20), [0.0, 300.0, 0.0])
        self.assertEqual(arm.calls[0], ("move_to", 10.0, 250.0, 5.0, 20.0))

    def test_move_by_returns_the_driver_result(self):
        arm = FakeArm()
        adapter, _ = self.open_with(arm)
        self.assertEqual(adapter.move_by(dz=10, timeout=5), [0.0, 300.0, 10.0])

    def test_wait_past_the_cap_is_refused_before_opening_the_port(self):
        adapter, opener = self.open_with(FakeArm())
        with self.assertRaises(ValueError):
            adapter.move(0, 300, 0, timeout=25.5)
        with self.assertRaises(ValueError):
            adapter.move_by(dz=1, timeout=-1)
        opener.assert_not_called()

    def test_cap_stays_under_the_hardware_service_deadline(self):
        from lager import hardware_service
        self.assertLess(arm_hs.MAX_MOVE_TIMEOUT_S,
                        hardware_service._INVOKE_DEADLINE_S)


if __name__ == "__main__":
    unittest.main()
