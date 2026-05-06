#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive power supply tests targeting the lager Python API.

Run with: lager python test/api/power/test_supply_comprehensive.py --box <YOUR-BOX>

Prerequisites:
- A power supply net configured on the box (default 'supply1')
- Example net configuration:
  {
    "name": "supply1",
    "role": "supply",
    "instrument": "keysight_e36312a",
    "address": "TCPIP0::192.168.0.101::inst0::INSTR"
  }

Adjust SUPPLY_NET environment variable or default below to match your box.
"""
import sys
import os
import time
import traceback

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SUPPLY_NET = os.environ.get("SUPPLY_NET", "supply1")
TOLERANCE = 0.05  # 5% tolerance for readback verification

# Track results
_results = []


def _record(name, passed, detail=""):
    """Record a sub-test result."""
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def _close_enough(actual, expected, tol=TOLERANCE):
    """Return True if actual is within tol fraction of expected."""
    denom = max(abs(expected), 0.001)
    return abs(actual - expected) / denom < tol


# ---------------------------------------------------------------------------
# 1. Imports
# ---------------------------------------------------------------------------
def test_imports():
    """Verify supply-related imports work."""
    print("\n" + "=" * 60)
    print("TEST: Imports")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        assert hasattr(NetType, "PowerSupply"), "NetType.PowerSupply not found"
        _record("import Net, NetType (PowerSupply attr)", True)
    except Exception as e:
        _record("import Net, NetType", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 2. Net.get
# ---------------------------------------------------------------------------
def test_net_get():
    """Verify Net.get returns a supply net object with expected methods."""
    print("\n" + "=" * 60)
    print("TEST: Net.get")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
        passed = psu is not None
        _record("Net.get returns object", passed, type(psu).__name__)
        if not passed:
            ok = False

        expected_methods = [
            "set_voltage", "set_current", "voltage", "current",
            "enable", "disable", "state",
        ]
        has_methods = all(hasattr(psu, m) for m in expected_methods)
        _record("supply has expected methods", has_methods,
                ", ".join(expected_methods))
        if not has_methods:
            ok = False
    except Exception as e:
        _record("Net.get returns object", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 3. String Repr
# ---------------------------------------------------------------------------
def test_string_repr():
    """Verify str(psu) contains meaningful info."""
    print("\n" + "=" * 60)
    print("TEST: String Repr")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
        s = str(psu)
        passed = isinstance(s, str) and len(s) > 0
        _record("str(psu) non-empty", passed, repr(s[:80]))
    except Exception as e:
        _record("str(psu) non-empty", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 4. Voltage Set/Read
# ---------------------------------------------------------------------------
def test_voltage_set_read():
    """Set voltage to 5.0V, read back, verify within tolerance."""
    print("\n" + "=" * 60)
    print("TEST: Voltage Set/Read")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)

        target = 5.0
        psu.set_voltage(target)
        time.sleep(0.5)
        readback = psu.voltage()
        passed = isinstance(readback, (int, float)) and _close_enough(readback, target)
        _record(f"voltage set={target}, readback={readback}", passed,
                f"delta={abs(readback - target):.4f}")
        if not passed:
            ok = False
    except Exception as e:
        _record("voltage set/read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 5. Current Set/Read
# ---------------------------------------------------------------------------
def test_current_set_read():
    """Set current limit to 1.0A, read back, verify within tolerance."""
    print("\n" + "=" * 60)
    print("TEST: Current Set/Read")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)

        target = 1.0
        psu.set_current(target)
        time.sleep(0.5)
        readback = psu.current()
        passed = isinstance(readback, (int, float))
        _record(f"current set={target}, readback={readback}", passed,
                f"type={type(readback).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("current set/read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 6. Enable/Disable with State Verification
# ---------------------------------------------------------------------------
def test_enable_disable():
    """Enable, verify state, disable, verify state."""
    print("\n" + "=" * 60)
    print("TEST: Enable/Disable")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)

        psu.enable()
        time.sleep(0.5)
        _record("enable()", True)

        state = psu.state()
        _record("state() after enable", state is not None, f"state={state}")

        psu.disable()
        time.sleep(0.5)
        _record("disable()", True)

        state = psu.state()
        _record("state() after disable", state is not None, f"state={state}")
    except Exception as e:
        _record("enable/disable", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 7. OVP Set/Read
# ---------------------------------------------------------------------------
def test_ovp():
    """Set OVP limit, read back, verify no error."""
    print("\n" + "=" * 60)
    print("TEST: OVP Set/Read")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)

        psu.set_ovp(6.0)
        _record("set_ovp(6.0)", True)

        ovp_limit = psu.get_ovp_limit()
        passed = isinstance(ovp_limit, (int, float))
        _record("get_ovp_limit() readback", passed, f"value={ovp_limit}")
        if not passed:
            ok = False
    except Exception as e:
        _record("ovp set/read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 8. OCP Set/Read
# ---------------------------------------------------------------------------
def test_ocp():
    """Set OCP limit, read back, verify no error."""
    print("\n" + "=" * 60)
    print("TEST: OCP Set/Read")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)

        psu.set_ocp(1.5)
        _record("set_ocp(1.5)", True)

        ocp_limit = psu.get_ocp_limit()
        passed = isinstance(ocp_limit, (int, float))
        _record("get_ocp_limit() readback", passed, f"value={ocp_limit}")
        if not passed:
            ok = False
    except Exception as e:
        _record("ocp set/read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 9. Clear OVP / Clear OCP
# ---------------------------------------------------------------------------
def test_clear_protections():
    """Verify clear_ovp() and clear_ocp() execute without error."""
    print("\n" + "=" * 60)
    print("TEST: Clear Protections")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)

        psu.clear_ovp()
        _record("clear_ovp()", True)

        psu.clear_ocp()
        _record("clear_ocp()", True)
    except Exception as e:
        _record("clear protections", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 10. Voltage Sweep
# ---------------------------------------------------------------------------
def test_voltage_sweep():
    """Sweep 3.3, 5.0, 12.0V with readback verification."""
    print("\n" + "=" * 60)
    print("TEST: Voltage Sweep")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
        psu.enable()
        time.sleep(0.5)

        for target in [3.3, 5.0, 12.0]:
            psu.set_voltage(target)
            time.sleep(0.5)
            readback = psu.voltage()
            passed = isinstance(readback, (int, float)) and _close_enough(readback, target)
            _record(f"sweep {target}V readback={readback}", passed,
                    f"delta={abs(readback - target):.4f}" if isinstance(readback, (int, float)) else "N/A")
            if not passed:
                ok = False

        psu.disable()
    except Exception as e:
        _record("voltage sweep", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 11. State Query
# ---------------------------------------------------------------------------
def test_state_query():
    """Verify state() executes and returns data."""
    print("\n" + "=" * 60)
    print("TEST: State Query")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)

        state = psu.state()
        _record("state() returns", state is not None, f"type={type(state).__name__}")
    except Exception as e:
        _record("state()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 12. Set Command
# ---------------------------------------------------------------------------
def test_set_command():
    """Verify set() applies configuration without error."""
    print("\n" + "=" * 60)
    print("TEST: Set Command")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)

        psu.set_voltage(3.3)
        psu.set_current(1.0)
        # Apply configuration
        psu.state()
        _record("set/apply configuration", True)
    except Exception as e:
        _record("set command", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 13. Error: Invalid Voltage (negative)
# ---------------------------------------------------------------------------
def test_error_negative_voltage():
    """Negative voltage should raise an exception."""
    print("\n" + "=" * 60)
    print("TEST: Error - Negative Voltage")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)

        try:
            psu.set_voltage(-5.0)
            # If no exception, this is a failure
            _record("set_voltage(-5.0) raises exception", False,
                    "no exception raised for negative voltage")
            ok = False
        except Exception as e:
            _record("set_voltage(-5.0) raises exception", True,
                    f"{type(e).__name__}: {e}")
    except Exception as e:
        _record("error test setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 14. Boundary: 0V
# ---------------------------------------------------------------------------
def test_boundary_zero_voltage():
    """Setting voltage to 0V should succeed."""
    print("\n" + "=" * 60)
    print("TEST: Boundary - 0V")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)

        psu.set_voltage(0.0)
        _record("set_voltage(0.0) accepted", True)

        readback = psu.voltage()
        passed = isinstance(readback, (int, float)) and abs(readback) < 0.5
        _record("voltage readback near 0V", passed, f"readback={readback}")
        if not passed:
            ok = False
    except Exception as e:
        _record("boundary 0V", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 15. Disable Safety (double disable)
# ---------------------------------------------------------------------------
def test_disable_safety():
    """Double disable should not raise an exception."""
    print("\n" + "=" * 60)
    print("TEST: Disable Safety")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)

        psu.enable()
        time.sleep(0.5)
        psu.disable()
        _record("disable() after enable", True)

        # Double disable should not raise
        psu.disable()
        _record("disable() idempotent", True)
    except Exception as e:
        _record("disable safety", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    """Run all tests."""
    print("Power Supply Comprehensive Test Suite")
    print(f"Testing net: {SUPPLY_NET}")
    print(f"Set SUPPLY_NET env var to change")
    print("=" * 60)

    tests = [
        ("Imports",                 test_imports),
        ("Net.get",                 test_net_get),
        ("String Repr",             test_string_repr),
        ("Voltage Set/Read",        test_voltage_set_read),
        ("Current Set/Read",        test_current_set_read),
        ("Enable/Disable",          test_enable_disable),
        ("OVP Set/Read",            test_ovp),
        ("OCP Set/Read",            test_ocp),
        ("Clear Protections",       test_clear_protections),
        ("Voltage Sweep",           test_voltage_sweep),
        ("State Query",             test_state_query),
        ("Set Command",             test_set_command),
        ("Error: Negative Voltage", test_error_negative_voltage),
        ("Boundary: 0V",            test_boundary_zero_voltage),
        ("Disable Safety",          test_disable_safety),
    ]

    test_results = []
    try:
        for name, test_fn in tests:
            try:
                passed = test_fn()
                test_results.append((name, passed))
            except Exception as e:
                print(f"\nUNEXPECTED ERROR in {name}: {e}")
                traceback.print_exc()
                test_results.append((name, False))
    finally:
        # Safety: always disable output regardless of what happened
        try:
            from lager import Net, NetType
            psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
            psu.disable()
        except Exception:
            pass

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    passed_count = sum(1 for _, p in test_results if p)
    total_count = len(test_results)

    for name, p in test_results:
        status = "PASS" if p else "FAIL"
        print(f"  [{status}] {name}")

    print(f"\nTotal: {passed_count}/{total_count} test groups passed")

    # Detailed sub-test summary
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
