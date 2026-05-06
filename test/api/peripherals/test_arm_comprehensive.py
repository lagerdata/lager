# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Comprehensive test for ARM (Robotic Arm) Net API

Hardware Required:
  - Rotrix Dexarm robotic arm
  - Net configured as type=NetType.Arm

Run with:
  lager python test/api/peripherals/test_arm_comprehensive.py --box MY-BOX

Safety Notes:
  - Keep clear of arm workspace during test
  - Test returns to home position when done
"""

from lager import Net, NetType
import sys
import time

_results = []
def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)

def _validate_position(pos, label):
    """Validate that a position value is a tuple/list of 3 numeric elements."""
    if not isinstance(pos, (tuple, list)):
        _record(f"{label}_type", False, f"expected tuple/list, got {type(pos).__name__}")
        return False
    if len(pos) != 3:
        _record(f"{label}_length", False, f"expected 3 elements, got {len(pos)}")
        return False
    for i, val in enumerate(pos):
        if not isinstance(val, (int, float)):
            _record(f"{label}_element_{i}", False, f"expected int/float, got {type(val).__name__}")
            return False
    _record(label, True, f"X={pos[0]:.1f}, Y={pos[1]:.1f}, Z={pos[2]:.1f}")
    return True

def main():
    print("=== ARM Comprehensive Test ===\n")

    net_name = 'arm1'
    arm = None

    try:
        arm = Net.get(net_name, type=NetType.Arm)
        _record("get_net", True, f"retrieved {net_name}")

        arm.enable_motor()
        _record("enable_motor", True)
        time.sleep(0.5)

        # Read initial position
        pos = arm.position()
        _validate_position(pos, "initial_position")

        # Go home
        arm.go_home()
        time.sleep(3)
        _record("go_home", True)

        # Move to absolute position
        arm.move_to(50, 250, 30, timeout=15)
        time.sleep(1)
        new_pos = arm.position()
        _validate_position(new_pos, "position_after_move_to")

        # Move relative
        arm.move_relative(10, 0, 5, timeout=10)
        time.sleep(1)
        _record("move_relative", True, "dx=10, dy=0, dz=5")
        delta_pos = arm.position()
        _validate_position(delta_pos, "position_after_move_relative")

        # Return home
        arm.go_home()
        time.sleep(3)
        _record("return_home", True)

        # Disable
        arm.disable_motor()
        _record("disable_motor", True)

    except Exception as e:
        _record("unexpected_error", False, str(e))

    finally:
        if arm is not None:
            try:
                arm.go_home()
                time.sleep(2)
            except Exception:
                pass
            try:
                arm.disable_motor()
            except Exception:
                pass

    # Summary
    passed = sum(1 for _, p, _ in _results if p)
    failed = sum(1 for _, p, _ in _results if not p)
    print(f"\n=== Summary: {passed} passed, {failed} failed out of {len(_results)} tests ===")
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
