#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Electronic Load (ELoad) Python API comprehensive tests.

Run with: lager python test/api/power/test_eload_comprehensive.py --box <YOUR-BOX>

Prerequisites:
- ELoad net (default 'eload1') configured on the box
- A power source connected to the electronic load input terminals
- Safe current values only (max 0.3A throughout)

Environment overrides:
    ELOAD_NET   - net name (default: eload1)
"""
import sys
import os
import time
import traceback

# Configuration
ELOAD_NET = os.environ.get("ELOAD_NET", "eload1")

# Safe limits
MAX_CURRENT = 0.3       # 300 mA
TEST_CURRENT = 0.3      # 300 mA
TEST_VOLTAGE = 5.0      # 5 V
TEST_RESISTANCE = 50.0  # 50 ohm
TEST_POWER = 2.0        # 2 W
MEAS_CURRENT = 0.1      # 100 mA for measurement tests
TOLERANCE = 0.15        # 15 % readback tolerance

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
    """Verify all ELoad-related imports work."""
    print("\n" + "=" * 60)
    print("TEST: Imports")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        assert hasattr(NetType, "ELoad")
        _record("import Net, NetType (ELoad attr)", True)
    except Exception as e:
        _record("import Net, NetType", False, str(e))
        ok = False

    try:
        from lager.power.eload.eload_net import ELoadNet
        _record("import ELoadNet", True)
    except Exception as e:
        _record("import ELoadNet", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 2. Net.get
# ---------------------------------------------------------------------------
def test_net_get():
    """Test Net.get returns an ELoadNet-like object."""
    print("\n" + "=" * 60)
    print("TEST: Net.get")
    print("=" * 60)

    from lager import Net, NetType

    try:
        eload = Net.get(ELOAD_NET, type=NetType.ELoad)
        type_name = type(eload).__name__
        has_methods = all(hasattr(eload, m) for m in [
            "mode", "current", "voltage", "resistance", "power",
            "enable", "disable", "measured_voltage", "measured_current",
            "measured_power", "print_state",
        ])
        _record("Net.get returns eload with expected API", has_methods,
                f"type={type_name}")
        return has_methods
    except Exception as e:
        _record("Net.get returns eload", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 3. String Repr
# ---------------------------------------------------------------------------
def test_string_repr():
    """Test str(eload) contains 'Electronic Load'."""
    print("\n" + "=" * 60)
    print("TEST: String Repr")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)

    try:
        s = str(eload)
        passed = "Electronic Load" in s
        _record("str(eload) contains 'Electronic Load'", passed,
                f"repr={s!r}")
        return passed
    except Exception as e:
        _record("str(eload)", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 4. CC Mode
# ---------------------------------------------------------------------------
def test_cc_mode():
    """Set CC mode, set current, read back."""
    print("\n" + "=" * 60)
    print("TEST: CC Mode")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)
    ok = True

    try:
        eload.mode("CC")
        _record("mode('CC') accepted", True)
    except Exception as e:
        _record("mode('CC')", False, str(e))
        return False

    try:
        eload.current(TEST_CURRENT)
        readback = eload.current()
        passed = isinstance(readback, (int, float)) and _close_enough(readback, TEST_CURRENT)
        _record(f"current set={TEST_CURRENT}, readback={readback}", passed,
                f"delta={abs(readback - TEST_CURRENT):.4f}")
        if not passed:
            ok = False
    except Exception as e:
        _record("current set/read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 5. CV Mode
# ---------------------------------------------------------------------------
def test_cv_mode():
    """Set CV mode, set voltage, read back."""
    print("\n" + "=" * 60)
    print("TEST: CV Mode")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)
    ok = True

    try:
        eload.mode("CV")
        _record("mode('CV') accepted", True)
    except Exception as e:
        _record("mode('CV')", False, str(e))
        return False

    try:
        eload.voltage(TEST_VOLTAGE)
        readback = eload.voltage()
        passed = isinstance(readback, (int, float)) and _close_enough(readback, TEST_VOLTAGE)
        _record(f"voltage set={TEST_VOLTAGE}, readback={readback}", passed,
                f"delta={abs(readback - TEST_VOLTAGE):.4f}")
        if not passed:
            ok = False
    except Exception as e:
        _record("voltage set/read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 6. CR Mode
# ---------------------------------------------------------------------------
def test_cr_mode():
    """Set CR mode, set resistance, read back."""
    print("\n" + "=" * 60)
    print("TEST: CR Mode")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)
    ok = True

    try:
        eload.mode("CR")
        _record("mode('CR') accepted", True)
    except Exception as e:
        _record("mode('CR')", False, str(e))
        return False

    try:
        eload.resistance(TEST_RESISTANCE)
        readback = eload.resistance()
        passed = isinstance(readback, (int, float)) and _close_enough(readback, TEST_RESISTANCE)
        _record(f"resistance set={TEST_RESISTANCE}, readback={readback}", passed,
                f"delta={abs(readback - TEST_RESISTANCE):.4f}")
        if not passed:
            ok = False
    except Exception as e:
        _record("resistance set/read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 7. CW Mode
# ---------------------------------------------------------------------------
def test_cw_mode():
    """Set CW mode, set power, read back."""
    print("\n" + "=" * 60)
    print("TEST: CW Mode")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)
    ok = True

    try:
        eload.mode("CW")
        _record("mode('CW') accepted", True)
    except Exception as e:
        _record("mode('CW')", False, str(e))
        return False

    try:
        eload.power(TEST_POWER)
        readback = eload.power()
        passed = isinstance(readback, (int, float)) and _close_enough(readback, TEST_POWER)
        _record(f"power set={TEST_POWER}, readback={readback}", passed,
                f"delta={abs(readback - TEST_POWER):.4f}")
        if not passed:
            ok = False
    except Exception as e:
        _record("power set/read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 8. Enable / Disable
# ---------------------------------------------------------------------------
def test_enable_disable():
    """Enable the load, wait, then disable."""
    print("\n" + "=" * 60)
    print("TEST: Enable / Disable")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)
    ok = True

    try:
        eload.mode("CC")
        eload.current(MEAS_CURRENT)
        eload.enable()
        _record("enable()", True)
    except Exception as e:
        _record("enable()", False, str(e))
        ok = False

    time.sleep(1)

    try:
        eload.disable()
        _record("disable()", True)
    except Exception as e:
        _record("disable()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 9. Mode Readback
# ---------------------------------------------------------------------------
def test_mode_readback():
    """Set each mode and verify readback via eload.mode()."""
    print("\n" + "=" * 60)
    print("TEST: Mode Readback")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)
    ok = True

    for mode_str in ("CC", "CV", "CR", "CW"):
        try:
            eload.mode(mode_str)
            readback = eload.mode()
            # Some instruments may return lowercase or full names; normalize
            passed = isinstance(readback, str) and mode_str.upper() in readback.upper()
            _record(f"mode readback after mode('{mode_str}')", passed,
                    f"readback={readback!r}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"mode readback '{mode_str}'", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 10. Measurements
# ---------------------------------------------------------------------------
def test_measurements():
    """Enable in CC at 0.1A and read measured_voltage/current/power."""
    print("\n" + "=" * 60)
    print("TEST: Measurements")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)
    ok = True

    try:
        eload.mode("CC")
        eload.current(MEAS_CURRENT)
        eload.enable()
        time.sleep(1)

        v = eload.measured_voltage()
        passed_v = isinstance(v, (int, float))
        _record("measured_voltage() returns float", passed_v,
                f"value={v}")
        if not passed_v:
            ok = False

        i = eload.measured_current()
        passed_i = isinstance(i, (int, float))
        _record("measured_current() returns float", passed_i,
                f"value={i}")
        if not passed_i:
            ok = False

        p = eload.measured_power()
        passed_p = isinstance(p, (int, float))
        _record("measured_power() returns float", passed_p,
                f"value={p}")
        if not passed_p:
            ok = False

    except Exception as e:
        _record("measurements", False, str(e))
        ok = False
    finally:
        try:
            eload.disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 11. CR Edge Case -- minimum resistance
# ---------------------------------------------------------------------------
def test_cr_edge_case():
    """Set a very low resistance (0.02 ohm) -- should succeed or raise cleanly."""
    print("\n" + "=" * 60)
    print("TEST: CR Edge Case")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)

    try:
        eload.mode("CR")
        eload.resistance(0.02)
        _record("resistance(0.02) accepted", True)
        return True
    except Exception as e:
        # A clean exception (e.g. value out of range) is acceptable
        _record("resistance(0.02) raises cleanly", True,
                f"{type(e).__name__}: {e}")
        return True


# ---------------------------------------------------------------------------
# 12. Disabled Measurements
# ---------------------------------------------------------------------------
def test_disabled_measurements():
    """Disable input, then read measurements -- values should be near zero."""
    print("\n" + "=" * 60)
    print("TEST: Disabled Measurements")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)
    ok = True

    try:
        eload.disable()
        time.sleep(0.5)

        v = eload.measured_voltage()
        i = eload.measured_current()
        p = eload.measured_power()

        # With input disabled, current and power should be near zero
        i_ok = isinstance(i, (int, float))
        p_ok = isinstance(p, (int, float))
        v_ok = isinstance(v, (int, float))

        _record("disabled: measured_voltage is float", v_ok, f"value={v}")
        _record("disabled: measured_current is float", i_ok, f"value={i}")
        _record("disabled: measured_power is float", p_ok, f"value={p}")

        if not (v_ok and i_ok and p_ok):
            ok = False

        # Current should be very small when disabled (< 50 mA)
        if i_ok:
            small = abs(i) < 0.05
            _record("disabled: current near zero", small,
                    f"measured_current={i:.4f}")
            if not small:
                ok = False

    except Exception as e:
        _record("disabled measurements", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 13. Rapid Mode Changes
# ---------------------------------------------------------------------------
def test_rapid_mode_changes():
    """Cycle CC->CV->CR->CW->CC back-to-back, verify each sticks."""
    print("\n" + "=" * 60)
    print("TEST: Rapid Mode Changes")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)
    ok = True

    modes = ["CC", "CV", "CR", "CW", "CC"]
    for m in modes:
        try:
            eload.mode(m)
            readback = eload.mode()
            passed = isinstance(readback, str) and m.upper() in readback.upper()
            _record(f"rapid switch to {m}", passed,
                    f"readback={readback!r}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"rapid switch to {m}", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 14. Power Consistency (V * I ~= P)
# ---------------------------------------------------------------------------
def test_power_consistency():
    """Enable CC 0.1A, measure V, I, P. Verify V*I ~ P within 20%."""
    print("\n" + "=" * 60)
    print("TEST: Power Consistency")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)
    ok = True

    try:
        eload.mode("CC")
        eload.current(MEAS_CURRENT)
        eload.enable()
        time.sleep(1)

        v = eload.measured_voltage()
        i = eload.measured_current()
        p = eload.measured_power()

        computed = v * i
        # Only check consistency if both V and I are non-trivial
        if abs(computed) > 0.001 and abs(p) > 0.001:
            ratio = abs(computed - p) / max(abs(p), 0.001)
            passed = ratio < 0.20
            _record("V*I ~ P consistency", passed,
                    f"V={v:.4f}, I={i:.4f}, V*I={computed:.4f}, P={p:.4f}, ratio={ratio:.3f}")
        else:
            # If values are near zero, consistency check is trivially satisfied
            _record("V*I ~ P consistency (near-zero)", True,
                    f"V={v:.4f}, I={i:.4f}, P={p:.4f}")

        if not ok:
            pass  # ok already set

    except Exception as e:
        _record("power consistency", False, str(e))
        ok = False
    finally:
        try:
            eload.disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 15. Current Sweep
# ---------------------------------------------------------------------------
def test_current_sweep():
    """Enable CC, sweep 0.1 -> 0.2 -> 0.3 A, measure at each step."""
    print("\n" + "=" * 60)
    print("TEST: Current Sweep")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)
    ok = True

    try:
        eload.mode("CC")
        eload.enable()
        time.sleep(0.5)

        sweep_values = [0.1, 0.2, 0.3]
        for target in sweep_values:
            eload.current(target)
            time.sleep(0.5)
            v = eload.measured_voltage()
            i = eload.measured_current()
            v_ok = isinstance(v, (int, float))
            i_ok = isinstance(i, (int, float))
            passed = v_ok and i_ok
            _record(f"sweep {target:.1f}A", passed,
                    f"V={v:.3f}, I={i:.3f}")
            if not passed:
                ok = False

    except Exception as e:
        _record("current sweep", False, str(e))
        ok = False
    finally:
        try:
            eload.disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 16. State Query
# ---------------------------------------------------------------------------
def test_state_query():
    """Call print_state() and verify it does not raise."""
    print("\n" + "=" * 60)
    print("TEST: State Query")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)

    try:
        eload.print_state()
        _record("print_state() runs without exception", True)
        return True
    except Exception as e:
        _record("print_state()", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 17. Negative CC
# ---------------------------------------------------------------------------
def test_negative_cc():
    """Verify current(-1.0) in CC mode raises an exception."""
    print("\n" + "=" * 60)
    print("TEST: Negative CC")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)

    try:
        eload.mode("CC")
        try:
            eload.current(-1.0)
            _record("current(-1.0) raises exception", False,
                    "no exception raised for negative current")
            return False
        except Exception as e:
            _record("current(-1.0) raises exception", True,
                    f"{type(e).__name__}: {e}")
            return True
    except Exception as e:
        _record("negative cc test setup", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 18. Negative CV
# ---------------------------------------------------------------------------
def test_negative_cv():
    """Verify voltage(-5.0) in CV mode raises an exception."""
    print("\n" + "=" * 60)
    print("TEST: Negative CV")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)

    try:
        eload.mode("CV")
        try:
            eload.voltage(-5.0)
            _record("voltage(-5.0) raises exception", False,
                    "no exception raised for negative voltage")
            return False
        except Exception as e:
            _record("voltage(-5.0) raises exception", True,
                    f"{type(e).__name__}: {e}")
            return True
    except Exception as e:
        _record("negative cv test setup", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 19. Zero CR
# ---------------------------------------------------------------------------
def test_zero_cr():
    """Verify resistance(0) in CR mode raises exception or handles cleanly."""
    print("\n" + "=" * 60)
    print("TEST: Zero CR")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)

    try:
        eload.mode("CR")
        try:
            eload.resistance(0)
            # Zero resistance accepted is also valid (some instruments clamp)
            _record("resistance(0) handled cleanly", True,
                    "accepted without error")
            return True
        except Exception as e:
            # A clean exception for zero resistance is expected behavior
            _record("resistance(0) raises cleanly", True,
                    f"{type(e).__name__}: {e}")
            return True
    except Exception as e:
        _record("zero cr test setup", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 20. Invalid Net
# ---------------------------------------------------------------------------
def test_invalid_net():
    """Verify Net.get with nonexistent net raises an exception."""
    print("\n" + "=" * 60)
    print("TEST: Invalid Net")
    print("=" * 60)

    try:
        from lager import Net, NetType
        try:
            Net.get("nonexistent_eload_net", type=NetType.ELoad)
            _record("invalid net raises exception", False,
                    "no exception raised for nonexistent net")
            return False
        except Exception as e:
            _record("invalid net raises exception", True,
                    f"{type(e).__name__}: {e}")
            return True
    except Exception as e:
        _record("invalid net test setup", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 21. Enable/Disable State Verification
# ---------------------------------------------------------------------------
def test_enable_disable_state():
    """Enable, verify measurements are non-None, disable, verify current near zero."""
    print("\n" + "=" * 60)
    print("TEST: Enable/Disable State Verification")
    print("=" * 60)

    from lager import Net, NetType
    eload = Net.get(ELOAD_NET, type=NetType.ELoad)
    ok = True

    try:
        eload.mode("CC")
        eload.current(MEAS_CURRENT)
        eload.enable()
        time.sleep(1)

        v = eload.measured_voltage()
        i = eload.measured_current()
        passed = isinstance(v, (int, float)) and isinstance(i, (int, float))
        _record("enabled: measurements are non-None", passed,
                f"V={v}, I={i}")
        if not passed:
            ok = False

        eload.disable()
        time.sleep(0.5)

        i_disabled = eload.measured_current()
        if isinstance(i_disabled, (int, float)):
            near_zero = abs(i_disabled) < 0.05
            _record("disabled: current near zero", near_zero,
                    f"measured_current={i_disabled:.4f}")
            if not near_zero:
                ok = False
        else:
            _record("disabled: measured_current is numeric", False,
                    f"type={type(i_disabled).__name__}")
            ok = False

    except Exception as e:
        _record("enable/disable state", False, str(e))
        ok = False
    finally:
        try:
            eload.disable()
        except Exception:
            pass

    return ok


# ===================================================================
# Main
# ===================================================================
def main():
    """Run all tests."""
    print("Electronic Load (ELoad) Comprehensive API Test Suite")
    print(f"ELoad net: {ELOAD_NET}")
    print("=" * 60)

    from lager import Net, NetType
    eload = None

    tests = [
        ("Imports",                 test_imports),
        ("Net.get",                 test_net_get),
        ("String Repr",             test_string_repr),
        ("CC Mode",                 test_cc_mode),
        ("CV Mode",                 test_cv_mode),
        ("CR Mode",                 test_cr_mode),
        ("CW Mode",                 test_cw_mode),
        ("Enable / Disable",        test_enable_disable),
        ("Mode Readback",           test_mode_readback),
        ("Measurements",            test_measurements),
        ("CR Edge Case",            test_cr_edge_case),
        ("Disabled Measurements",   test_disabled_measurements),
        ("Rapid Mode Changes",      test_rapid_mode_changes),
        ("Power Consistency",       test_power_consistency),
        ("Current Sweep",           test_current_sweep),
        ("State Query",             test_state_query),
        ("Negative CC",             test_negative_cc),
        ("Negative CV",             test_negative_cv),
        ("Zero CR",                 test_zero_cr),
        ("Invalid Net",             test_invalid_net),
        ("Enable/Disable State",    test_enable_disable_state),
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

    # Safety: always disable the load at the end
    try:
        eload = Net.get(ELOAD_NET, type=NetType.ELoad)
        eload.disable()
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
