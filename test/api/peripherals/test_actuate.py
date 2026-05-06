#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Actuate (Dexarm) Python API test using only `from lager import Net, NetType`.

Run with:
    lager python test/api/peripherals/test_actuate.py --box <ARM_BOX>

Hardware:
    - Rotrix Dexarm robotic arm
    - Net configured as type=NetType.Actuate (default: actuate1)

NOTE: The arm physically moves during this test. Keep the workspace clear.
"""
import sys
import os
import time
import traceback

# ----- Configuration -----
ACTUATE_NET = os.environ.get("ACTUATE_NET", "actuate1")

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
    """Verify Net.get returns an Actuate object."""
    print("\n" + "=" * 60)
    print("TEST GROUP 1: Net.get Factory")
    print("=" * 60)

    from lager import Net, NetType
    ok = True

    try:
        actuate = Net.get(ACTUATE_NET, type=NetType.Actuate)
        passed = actuate is not None
        _record("Net.get returns object", passed, f"type={type(actuate).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("Net.get returns object", False, str(e))
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
    actuate = Net.get(ACTUATE_NET, type=NetType.Actuate)
    ok = True

    # 2a. .name matches the net name
    try:
        passed = actuate.name == ACTUATE_NET
        _record(".name == ACTUATE_NET", passed, f"name={actuate.name!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record(".name == ACTUATE_NET", False, str(e))
        ok = False

    # 2b. .pin is an int
    try:
        passed = isinstance(actuate.pin, int)
        _record(".pin is int", passed, f"pin={actuate.pin!r}, type={type(actuate.pin).__name__}")
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
    """Verify str(actuate) contains 'lager.Actuate'."""
    print("\n" + "=" * 60)
    print("TEST GROUP 3: String Representation")
    print("=" * 60)

    from lager import Net, NetType
    actuate = Net.get(ACTUATE_NET, type=NetType.Actuate)
    ok = True

    try:
        s = str(actuate)
        passed = "lager.Actuate" in s
        _record("str() contains 'lager.Actuate'", passed, f"str={s!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("str() contains 'lager.Actuate'", False, str(e))
        ok = False

    return ok


# ===================================================================
# 4. Single Actuation
# ===================================================================
def test_single_actuation():
    """Verify actuate() completes without error and returns None."""
    print("\n" + "=" * 60)
    print("TEST GROUP 4: Single Actuation")
    print("=" * 60)

    from lager import Net, NetType
    actuate = Net.get(ACTUATE_NET, type=NetType.Actuate)
    ok = True

    try:
        result = actuate.actuate()
        passed = result is None
        _record("actuate() returns None", passed, f"returned {result!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("actuate() completes", False, str(e))
        ok = False

    return ok


# ===================================================================
# 5. Repeated Actuation
# ===================================================================
def test_repeated_actuation():
    """Call actuate() three times with 2s pauses, all should complete."""
    print("\n" + "=" * 60)
    print("TEST GROUP 5: Repeated Actuation")
    print("=" * 60)

    from lager import Net, NetType
    actuate = Net.get(ACTUATE_NET, type=NetType.Actuate)
    ok = True

    for i in range(1, 4):
        try:
            result = actuate.actuate()
            passed = result is None
            _record(f"actuate() call {i}/3", passed, f"returned {result!r}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"actuate() call {i}/3", False, str(e))
            ok = False
        if i < 3:
            time.sleep(2)

    return ok


# ===================================================================
# Main
# ===================================================================
def main():
    print("Actuate (Dexarm) API Test Suite")
    print(f"Net: {ACTUATE_NET}")
    print(f"Set ACTUATE_NET env var to change")
    print("=" * 60)

    tests = [
        ("1. Net.get Factory",         test_net_get),
        ("2. Properties",              test_properties),
        ("3. String Representation",   test_string_repr),
        ("4. Single Actuation",        test_single_actuation),
        ("5. Repeated Actuation",      test_repeated_actuation),
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
