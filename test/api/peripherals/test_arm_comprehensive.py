# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Robot arm (Rotrics Dexarm) Python API test.

Hardware Required:
  - Rotrix Dexarm robotic arm
  - Net configured as type=NetType.Arm (default name arm1; set ARM_NET to change)

Run with:
  lager python test/api/peripherals/test_arm_comprehensive.py --box MY-BOX

THIS MOVES THE ARM. Every target stays within about 50 mm of home
(X0 Y300 Z0), and the test ends at home. It never calls save_position() or
read_and_save_position(): both send M889, which overwrites the arm's stored
calibration.
"""

import os
import sys
import time

from lager import Net, NetType
from lager.automation.arm import OutOfBoundsError

NET_NAME = os.environ.get("ARM_NET", "arm1")
HOME = (0.0, 300.0, 0.0)
TOLERANCE_MM = 1.0

_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def _near(pos, target, tol=TOLERANCE_MM):
    return all(abs(p - t) <= tol for p, t in zip(pos, target))


def _fmt(pos):
    return "X=%.2f Y=%.2f Z=%.2f" % tuple(pos)


def main():
    print("=== Robot Arm API Test ===\n")

    arm = None
    try:
        arm = Net.get(NET_NAME, type=NetType.Arm)
        _record("get_net", True, NET_NAME)

        # Moves need firmware V2.1.4 or later: Rotrics swapped X and Y there.
        version = getattr(arm, "firmware_version", None)
        _record("firmware_v2_1_4_or_later", version is not None and version >= (2, 1, 4),
                "firmware %s" % (".".join(map(str, version)) if version else "unknown"))

        arm.enable_motor()
        _record("enable_motor", True)

        pos = arm.position()
        _record("position_is_three_floats",
                len(pos) == 3 and all(isinstance(v, float) for v in pos), _fmt(pos))

        # go_home() must return with the arm already at home, not while it is
        # still travelling.
        arm.go_home()
        pos = arm.position()
        _record("go_home_returns_at_home", _near(pos, HOME), _fmt(pos))

        target = (50.0, 250.0, 30.0)
        arm.move_to(*target, timeout=15)
        pos = arm.position()
        _record("move_to_arrives", _near(pos, target), _fmt(pos))

        new = arm.move_relative(dz=10, timeout=10)
        expected = (target[0], target[1], target[2] + 10)
        _record("move_relative_returns_new_position", _near(new, expected), _fmt(new))

        before = arm.position()
        try:
            arm.move_to(0, 0, 0)
            _record("out_of_bounds_refused", False, "move_to(0, 0, 0) did not raise")
        except OutOfBoundsError:
            time.sleep(0.5)
            after = arm.position()
            _record("out_of_bounds_refused_without_motion", _near(after, before), _fmt(after))

        arm.move_to(*HOME, timeout=15)
        pos = arm.position()
        _record("return_home", _near(pos, HOME), _fmt(pos))

    except Exception as exc:  # noqa: BLE001 -- a hardware test reports, it does not crash
        _record("unexpected_error", False, f"{type(exc).__name__}: {exc}")

    finally:
        if arm is not None:
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
