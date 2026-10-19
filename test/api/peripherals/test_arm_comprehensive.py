# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Robot arm (Rotrics Dexarm) Python API test.

Hardware Required:
  - Rotrix Dexarm robotic arm, firmware V2.1.4 or later
  - Net configured as type=NetType.Arm (default name arm1; set ARM_NET to change)

Optional environment:
  ARM_SERIAL      USB serial the net must open (checked when set)
  ARM_MODULE      end-effector module the arm must report (PEN, LASER, PUMP, 3D)
  ARM_TRAVERSE_X  half-width of the X traverse the timeout check makes at
                  Y300 Z30 (default 100, so a 200 mm move)

Run with:
  lager python test/api/peripherals/test_arm_comprehensive.py --box MY-BOX \
      --passenv ARM_NET --passenv ARM_SERIAL --passenv ARM_MODULE

THIS MOVES THE ARM. Every target stays inside X -100..100, Y 200..320,
Z 0..40 (mm) around home (X0 Y300 Z0), and the test ends at home. It never
calls save_position() or read_and_save_position(): both send M889, which
overwrites the arm's stored calibration. It reads that calibration (the M889
line of the M503 settings report) before and after, and fails if it changed.
The arm's acceleration is restored to the values it started with.
"""

import os
import sys
import time

from lager import Net, NetType
from lager.automation.arm import Dexarm, MovementTimeoutError, OutOfBoundsError

NET_NAME = os.environ.get("ARM_NET", "arm1")
EXPECTED_SERIAL = os.environ.get("ARM_SERIAL", "")
EXPECTED_MODULE = os.environ.get("ARM_MODULE", "")
TRAVERSE_X = float(os.environ.get("ARM_TRAVERSE_X", "100"))
HOME = (0.0, 300.0, 0.0)
TOLERANCE_MM = 1.0
# Unlike any firmware default, so a write that did not land is caught.
TEST_ACCELERATION = (61, 62, 43)

_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def _near(pos, target, tol=TOLERANCE_MM):
    return len(pos) == len(target) and all(abs(p - t) <= tol for p, t in zip(pos, target))


def _fmt(pos):
    return "X=%.2f Y=%.2f Z=%.2f" % tuple(pos[:3])


def _calibration(arm):
    """The arm's stored calibration line from M503, e.g. "M889 X2766 Y1590 Z3346"."""
    for line in arm.read_settings():
        if "M889" in line:
            return line.split(":", 1)[-1].strip()
    return None


def _wait_near(arm, target, seconds):
    deadline = time.monotonic() + seconds
    pos = arm.position()
    while not _near(pos, target) and time.monotonic() < deadline:
        time.sleep(0.5)
        pos = arm.position()
    return pos


def _port_serial(arm):
    from serial.tools import list_ports
    for port in list_ports.comports():
        if port.device == arm.ser.port:
            return port.serial_number
    return None


def main():
    print("=== Robot Arm API Test ===\n")

    arm = None
    accel0 = None
    try:
        arm = Net.get(NET_NAME, type=NetType.Arm)
        serial = _port_serial(arm)
        _record("get_net_opens_a_dexarm",
                isinstance(arm, Dexarm) and (not EXPECTED_SERIAL or serial == EXPECTED_SERIAL),
                "%s on %s, USB serial %s" % (NET_NAME, arm.ser.port, serial))

        # Moves need firmware V2.1.4 or later: Rotrics swapped X and Y there.
        version = getattr(arm, "firmware_version", None)
        _record("firmware_v2_1_4_or_later", version is not None and version >= (2, 1, 4),
                "firmware %s" % (".".join(map(str, version)) if version else "unknown"))

        module = arm.get_module_type()
        _record("module_type_reported",
                module in ("PEN", "LASER", "PUMP", "3D")
                and (not EXPECTED_MODULE or module == EXPECTED_MODULE), str(module))

        cal0 = _calibration(arm)
        _record("calibration_read", bool(cal0), str(cal0))

        accel0 = arm.get_acceleration()
        _record("get_acceleration_returns_three_values",
                len(accel0) == 3 and all(v > 0 for v in accel0),
                "print/travel/retract %g/%g/%g" % accel0)

        arm.enable_motor()
        arm.go_home()
        pos = arm.position()
        # go_home() must return with the arm already at home, not while it is
        # still travelling.
        _record("go_home_returns_at_home", _near(pos, HOME), _fmt(pos))

        full = arm.get_full_position()
        _record("get_full_position_matches_position",
                len(full) == 7 and _near(full[:3], pos), _fmt(full))

        # enable_motor() above has to leave the arm able to move.
        target = (50.0, 250.0, 30.0)
        arm.move_to(*target, timeout=15)
        pos = arm.position()
        _record("move_to_arrives", _near(pos, target), _fmt(pos))

        new = arm.move_relative(dz=10, timeout=10)
        expected = (target[0], target[1], target[2] + 10)
        _record("move_relative_returns_new_position", _near(new, expected), _fmt(new))
        pos = arm.position()
        _record("move_relative_arrives", _near(pos, expected), _fmt(pos))

        before = arm.position()
        try:
            arm.move_to(0, 0, 0)
            _record("out_of_bounds_refused", False, "move_to(0, 0, 0) did not raise")
        except OutOfBoundsError:
            time.sleep(0.5)
            after = arm.position()
            _record("out_of_bounds_refused_without_motion", _near(after, before), _fmt(after))

        # A 200 mm traverse takes about 9 s, so a 1 s timeout has to expire.
        # The arm keeps going: the timeout only ends the wait.
        start = (-TRAVERSE_X, 300.0, 30.0)
        end = (TRAVERSE_X, 300.0, 30.0)
        arm.move_to(*start, timeout=15)
        try:
            arm.move_to(*end, timeout=1)
            _record("move_timeout_raises", False, "a 1 s wait for a 200 mm move did not time out")
        except MovementTimeoutError as exc:
            _record("move_timeout_raises", "longer timeout" in str(exc), str(exc)[:120])
        pos = _wait_near(arm, end, 20)
        _record("arm_reaches_target_after_timeout", _near(pos, end), _fmt(pos))

        p, t, r = TEST_ACCELERATION
        read_back = arm.set_acceleration(p, t, r)
        _record("set_acceleration_reads_back",
                read_back == (p, t, r) and arm.get_acceleration() == (p, t, r),
                "%g/%g/%g" % read_back)

        # Without a retract value the arm keeps its own.
        read_back = arm.set_acceleration(t, p)
        _record("set_acceleration_without_retract_keeps_it",
                read_back == (t, p, r), "%g/%g/%g" % read_back)

        arm.move_to(*HOME, timeout=15)
        pos = arm.position()
        _record("return_home", _near(pos, HOME), _fmt(pos))

        cal1 = _calibration(arm)
        _record("calibration_unchanged", bool(cal0) and cal1 == cal0, str(cal1))

    except Exception as exc:  # noqa: BLE001 -- a hardware test reports, it does not crash
        _record("unexpected_error", False, f"{type(exc).__name__}: {exc}")

    finally:
        if arm is not None:
            if accel0 is not None:
                try:
                    restored = arm.set_acceleration(*(int(v) for v in accel0))
                    _record("acceleration_restored", restored == accel0,
                            "%g/%g/%g" % restored)
                except Exception as exc:  # noqa: BLE001
                    _record("acceleration_restored", False, f"{type(exc).__name__}: {exc}")
            try:
                arm.go_home()
            except Exception:
                pass
            try:
                arm.close()
            except Exception:
                pass

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print(f"\n=== Summary: {passed} passed, {failed} failed out of {len(_results)} tests ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
