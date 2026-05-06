#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Rotation Encoder Python API test using only `from lager import Net, NetType`.

Hardware Required:
  - Phidget rotation encoder
  - Net configured as type=NetType.Rotation

Run with:
  lager python test/api/peripherals/test_rotation_encoder.py --box <PHIDGET_BOX>
"""
import sys
import os
import time
import traceback

# ----- Configuration -----
ROTATION_NET = os.environ.get("ROTATION_NET", "rotation1")

# ----- Test framework -----
_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


# ===================================================================
# 1. Net.get Factory
# ===================================================================
def test_net_get():
    """Verify Net.get returns a Rotation object."""
    print("\n" + "=" * 60)
    print("TEST GROUP 1: Net.get Factory")
    print("=" * 60)

    from lager import Net, NetType
    ok = True

    # 1a. Basic get succeeds
    try:
        rotation = Net.get(ROTATION_NET, type=NetType.Rotation)
        passed = rotation is not None
        _record("Net.get returns object", passed, f"type={type(rotation).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("Net.get returns object", False, str(e))
        ok = False

    # 1b. Has .read callable
    try:
        rotation = Net.get(ROTATION_NET, type=NetType.Rotation)
        passed = callable(getattr(rotation, "read", None))
        _record("has .read()", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("has .read()", False, str(e))
        ok = False

    return ok


# ===================================================================
# 2. Properties
# ===================================================================
def test_properties():
    """Verify .name and .pin properties."""
    print("\n" + "=" * 60)
    print("TEST GROUP 2: Properties")
    print("=" * 60)

    from lager import Net, NetType
    rotation = Net.get(ROTATION_NET, type=NetType.Rotation)
    ok = True

    # 2a. .name matches net name
    try:
        passed = rotation.name == ROTATION_NET
        _record(".name == ROTATION_NET", passed, f"name={rotation.name!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record(".name == ROTATION_NET", False, str(e))
        ok = False

    # 2b. .pin is an int
    try:
        pin = rotation.pin
        passed = isinstance(pin, int)
        _record(".pin is int", passed, f"pin={pin!r}, type={type(pin).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record(".pin is int", False, str(e))
        ok = False

    return ok


# ===================================================================
# 3. String Representation
# ===================================================================
def test_string_repr():
    """Verify str(rotation) contains 'lager.Rotation'."""
    print("\n" + "=" * 60)
    print("TEST GROUP 3: String Representation")
    print("=" * 60)

    from lager import Net, NetType
    rotation = Net.get(ROTATION_NET, type=NetType.Rotation)
    ok = True

    try:
        s = str(rotation)
        passed = "lager.Rotation" in s
        _record("str() contains 'lager.Rotation'", passed, f"str={s!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("str() contains 'lager.Rotation'", False, str(e))
        ok = False

    return ok


# ===================================================================
# 4. Single Read
# ===================================================================
def test_single_read():
    """Verify read() returns a dict with 'position' key (int value)."""
    print("\n" + "=" * 60)
    print("TEST GROUP 4: Single Read")
    print("=" * 60)

    from lager import Net, NetType
    rotation = Net.get(ROTATION_NET, type=NetType.Rotation)
    ok = True

    # 4a. read() returns a dict
    try:
        result = rotation.read()
        passed = isinstance(result, dict)
        _record("read() returns dict", passed, f"type={type(result).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read() returns dict", False, str(e))
        ok = False

    # 4b. dict contains 'position' key
    try:
        result = rotation.read()
        passed = "position" in result
        _record("result has 'position' key", passed, f"keys={list(result.keys())}")
        if not passed:
            ok = False
    except Exception as e:
        _record("result has 'position' key", False, str(e))
        ok = False

    # 4c. position value is int
    try:
        result = rotation.read()
        pos = result["position"]
        passed = isinstance(pos, int)
        _record("position value is int", passed,
                f"position={pos}, type={type(pos).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("position value is int", False, str(e))
        ok = False

    return ok


# ===================================================================
# 5. Multiple Reads
# ===================================================================
def test_multiple_reads():
    """Read 10 times with 0.1s interval, all valid dicts with 'position'."""
    print("\n" + "=" * 60)
    print("TEST GROUP 5: Multiple Reads")
    print("=" * 60)

    from lager import Net, NetType
    rotation = Net.get(ROTATION_NET, type=NetType.Rotation)
    ok = True

    try:
        readings = []
        for i in range(10):
            result = rotation.read()
            readings.append(result)
            time.sleep(0.1)

        # 5a. All results are dicts
        all_dicts = all(isinstance(r, dict) for r in readings)
        _record("10 reads all dicts", all_dicts,
                f"types={[type(r).__name__ for r in readings[:3]]}...")
        if not all_dicts:
            ok = False

        # 5b. All results have 'position' key
        all_have_key = all("position" in r for r in readings)
        _record("10 reads all have 'position'", all_have_key)
        if not all_have_key:
            ok = False

        # 5c. All position values are ints
        all_ints = all(isinstance(r["position"], int) for r in readings)
        positions = [r["position"] for r in readings]
        _record("10 reads all positions are int", all_ints,
                f"positions={positions}")
        if not all_ints:
            ok = False

    except Exception as e:
        _record("multiple reads", False, str(e))
        ok = False

    return ok


# ===================================================================
# 6. Read Consistency
# ===================================================================
def test_read_consistency():
    """Two consecutive reads on a stationary encoder should be close."""
    print("\n" + "=" * 60)
    print("TEST GROUP 6: Read Consistency")
    print("=" * 60)

    from lager import Net, NetType
    rotation = Net.get(ROTATION_NET, type=NetType.Rotation)
    ok = True

    try:
        r1 = rotation.read()
        r2 = rotation.read()
        pos1 = r1["position"]
        pos2 = r2["position"]
        diff = abs(pos2 - pos1)
        passed = diff < 10
        _record("consecutive reads close (diff < 10)", passed,
                f"pos1={pos1}, pos2={pos2}, diff={diff}")
        if not passed:
            ok = False
    except Exception as e:
        _record("consecutive reads close", False, str(e))
        ok = False

    return ok


# ===================================================================
# 7. Read Return Type Verification
# ===================================================================
def test_read_return_type():
    """Verify position is int and dict has no unexpected extra keys."""
    print("\n" + "=" * 60)
    print("TEST GROUP 7: Read Return Type Verification")
    print("=" * 60)

    from lager import Net, NetType
    rotation = Net.get(ROTATION_NET, type=NetType.Rotation)
    ok = True

    try:
        result = rotation.read()

        # 7a. position is strictly int (not bool subclass)
        pos = result["position"]
        passed = type(pos) is int
        _record("type(position) is int, not bool", passed,
                f"type={type(pos).__name__}")
        if not passed:
            ok = False

        # 7b. No unexpected keys beyond 'position'
        expected_keys = {"position"}
        extra_keys = set(result.keys()) - expected_keys
        passed = len(extra_keys) == 0
        _record("no unexpected extra keys", passed,
                f"keys={list(result.keys())}, extra={extra_keys or 'none'}")
        if not passed:
            ok = False

    except Exception as e:
        _record("read return type verification", False, str(e))
        ok = False

    return ok


# ===================================================================
# Main
# ===================================================================
def main():
    print("Rotation Encoder API Test Suite")
    print(f"Net: {ROTATION_NET}")
    print(f"Set ROTATION_NET env var to change")
    print("=" * 60)

    tests = [
        ("1. Net.get Factory",              test_net_get),
        ("2. Properties",                   test_properties),
        ("3. String Representation",        test_string_repr),
        ("4. Single Read",                  test_single_read),
        ("5. Multiple Reads",               test_multiple_reads),
        ("6. Read Consistency",             test_read_consistency),
        ("7. Read Return Type",             test_read_return_type),
    ]

    test_results = []
    for name, test_fn in tests:
        try:
            passed = test_fn()
            test_results.append((name, passed))
        except Exception as e:
            print(f"\nUNEXPECTED ERROR in {name}: {e}")
            traceback.print_exc()
            test_results.append((name, False))

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    passed_count = sum(1 for _, p in test_results if p)
    total_count = len(test_results)

    for name, p in test_results:
        status = "PASS" if p else "FAIL"
        print(f"  [{status}] {name}")

    print(f"\nGroups: {passed_count}/{total_count} passed")

    sub_passed = sum(1 for _, p, _ in _results if p)
    sub_total = len(_results)
    sub_failed = sub_total - sub_passed
    print(f"Sub-tests: {sub_passed}/{sub_total} passed", end="")
    if sub_failed > 0:
        print(f" ({sub_failed} failed)")
        print("\nFailed sub-tests:")
        for name, p, detail in _results:
            if not p:
                print(f"  FAIL: {name} -- {detail}")
    else:
        print()

    return 0 if passed_count == total_count else 1


if __name__ == "__main__":
    sys.exit(main())
