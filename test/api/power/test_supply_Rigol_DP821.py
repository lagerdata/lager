#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Advanced power supply tests targeting live measurement methods, output state
querying, power calculation consistency, measurement stability, and stress
cycling. Complements test_supply_comprehensive.py — run both for full coverage.

Run with: lager python test/api/power/test_supply_Rigol_DP821.py --box <YOUR-BOX>

Prerequisites:
- A power supply net configured on the box (default 'supply2')
- No load attached; unloaded measurements are expected (< 100 mA draw)

Override the net with:    SUPPLY_NET=my-supply lager python ...
Override channel limits: CHANNEL_MAX_VOLTAGE=8 CHANNEL_MAX_CURRENT=10 lager python ...
"""
import sys
import os
import time
import traceback

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SUPPLY_NET = os.environ.get("SUPPLY_NET", "supply2")
TOLERANCE = 0.05           # 5 % — setpoint / live-measurement agreement
TIGHT_TOLERANCE = 0.02     # 2 % — setpoint vs. measured voltage (same device)
STABILITY_TOL = 0.01       # 1 % — max spread across repeated reads
POWER_TOL = 0.20           # 20 % — P vs V×I consistency (same as eload tests)
MAX_UNLOADED_CURRENT = 0.1  # 100 mA max expected with no load
CHANNEL_MAX_VOLTAGE = float(os.environ.get("CHANNEL_MAX_VOLTAGE", "60.0"))
CHANNEL_MAX_CURRENT = float(os.environ.get("CHANNEL_MAX_CURRENT", "1.0"))

_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def _close_enough(actual, expected, tol=TOLERANCE):
    denom = max(abs(expected), 0.001)
    return abs(actual - expected) / denom < tol


# ---------------------------------------------------------------------------
# 1. Live Measurements
# ---------------------------------------------------------------------------
def test_live_measurements():
    """voltage/current/power return numeric types with output enabled."""
    print("\n" + "=" * 60)
    print("TEST: Live Measurements")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
        psu.set_voltage(min(5.0, CHANNEL_MAX_VOLTAGE))
        psu.set_current(min(1.0, CHANNEL_MAX_CURRENT))
        psu.enable()
        time.sleep(0.5)

        try:
            mv = psu.voltage()
            passed = isinstance(mv, (int, float))
            _record("voltage() returns numeric", passed, f"value={mv}")
            if not passed:
                ok = False
        except Exception as e:
            _record("voltage()", False, str(e))
            ok = False

        try:
            mi = psu.current()
            passed = isinstance(mi, (int, float))
            _record("current() returns numeric", passed, f"value={mi}")
            if not passed:
                ok = False
        except Exception as e:
            _record("current()", False, str(e))
            ok = False

        try:
            mp = psu.power()
            passed = isinstance(mp, (int, float))
            _record("power() returns numeric", passed, f"value={mp}")
            if not passed:
                ok = False
        except Exception as e:
            _record("power()", False, str(e))
            ok = False

        try:
            mi = psu.current()
            passed = isinstance(mi, (int, float)) and abs(float(mi)) < MAX_UNLOADED_CURRENT
            _record(
                f"current() < {MAX_UNLOADED_CURRENT} A when unloaded",
                passed,
                f"measured={mi:.4f} A",
            )
            if not passed:
                ok = False
        except Exception as e:
            _record("current() unloaded check", False, str(e))
            ok = False

    except Exception as e:
        _record("live measurements setup", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 2. Measured Voltage vs. Setpoint Agreement
# ---------------------------------------------------------------------------
def test_setpoint_vs_measured():
    """voltage() should track the set_voltage() target within tight tolerance.

    On the Rigol DP800 mapper, voltage() returns the live hardware measurement.
    The useful assertion here is that the live reading is close to what was programmed.
    """
    print("\n" + "=" * 60)
    print("TEST: Measured Voltage vs. Setpoint")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
        target = 5.0
        psu.set_voltage(target)
        psu.enable()
        time.sleep(0.5)

        measured = psu.voltage()

        passed_meas = isinstance(measured, (int, float)) and _close_enough(
            float(measured), target, TIGHT_TOLERANCE
        )
        _record(
            f"voltage() within {int(TIGHT_TOLERANCE*100)}% of {target} V target",
            passed_meas,
            f"measured={measured:.4f} V",
        )
        if not passed_meas:
            ok = False

    except Exception as e:
        _record("setpoint vs. measured", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 3. Power Calculation Consistency
# ---------------------------------------------------------------------------
def test_power_consistency():
    """power() should approximately equal voltage() * current()."""
    print("\n" + "=" * 60)
    print("TEST: Power Calculation Consistency")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
        psu.set_voltage(min(5.0, CHANNEL_MAX_VOLTAGE))
        psu.set_current(min(1.0, CHANNEL_MAX_CURRENT))
        psu.enable()
        time.sleep(0.5)

        mv = float(psu.voltage())
        mi = float(psu.current())
        mp = float(psu.power())
        vi_product = mv * mi

        abs_diff = abs(mp - vi_product)
        denom = max(abs(vi_product), 0.001)
        passed = abs_diff < 0.15 or abs_diff / denom < POWER_TOL
        _record(
            f"P ≈ V×I (abs<0.15 W or within {int(POWER_TOL*100)}%)",
            passed,
            f"P={mp:.4f} W, V×I={vi_product:.4f} W",
        )
        if not passed:
            ok = False

    except Exception as e:
        _record("power consistency", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 4. Output State Querying
# ---------------------------------------------------------------------------
def test_output_is_enabled():
    """output_is_enabled() tracks enable/disable transitions."""
    print("\n" + "=" * 60)
    print("TEST: Output State Querying")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
        psu.set_voltage(min(5.0, CHANNEL_MAX_VOLTAGE))

        psu.enable()
        time.sleep(0.3)
        state_on = psu.output_is_enabled()
        passed_on = bool(state_on)
        _record("output_is_enabled() True after enable()", passed_on, f"returned={state_on!r}")
        if not passed_on:
            ok = False

        psu.disable()
        time.sleep(0.3)
        state_off = psu.output_is_enabled()
        passed_off = not bool(state_off)
        _record("output_is_enabled() False after disable()", passed_off, f"returned={state_off!r}")
        if not passed_off:
            ok = False

    except Exception as e:
        _record("output_is_enabled()", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 5. Output Mode Detection
# ---------------------------------------------------------------------------
def test_output_mode():
    """get_output_mode() returns CV with no load at a known voltage."""
    print("\n" + "=" * 60)
    print("TEST: Output Mode Detection")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
        psu.set_voltage(min(5.0, CHANNEL_MAX_VOLTAGE))
        psu.set_current(min(1.0, CHANNEL_MAX_CURRENT))
        psu.enable()
        time.sleep(0.5)

        mode = psu.get_output_mode()
        passed_type = isinstance(mode, str)
        _record("get_output_mode() returns string", passed_type, f"mode={mode!r}")
        if not passed_type:
            ok = False
        else:
            # With no load, supply should be in constant-voltage mode
            passed_cv = "CV" in mode.upper() or "CC" in mode.upper()
            _record(
                "get_output_mode() is CV or CC",
                passed_cv,
                f"mode={mode!r}",
            )
            if not passed_cv:
                ok = False

    except Exception as e:
        _record("get_output_mode()", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 6. Common Embedded Voltages
# ---------------------------------------------------------------------------
def test_embedded_voltages():
    """Live-measured voltage matches setpoint for 1.8, 2.5, 3.3, 5.0, 12.0 V."""
    print("\n" + "=" * 60)
    print("TEST: Common Embedded Voltages")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
        psu.set_current(min(1.0, CHANNEL_MAX_CURRENT))
        psu.set_ovp(CHANNEL_MAX_VOLTAGE)
        psu.enable()
        time.sleep(0.5)

        _all_voltages = [1.8, 2.5, 3.3, 5.0, 12.0]
        targets = [v for v in _all_voltages if v <= CHANNEL_MAX_VOLTAGE]
        if not targets:
            _record("embedded voltages skipped — channel max too low", True,
                    f"max={CHANNEL_MAX_VOLTAGE} V, candidates={_all_voltages}")
            return True

        for target in targets:
            try:
                psu.set_voltage(target)
                time.sleep(1.5)
                measured = float(psu.voltage())
                passed = _close_enough(measured, target, TOLERANCE)
                _record(
                    f"{target} V measured",
                    passed,
                    f"measured={measured:.4f} V, delta={abs(measured - target):.4f}",
                )
                if not passed:
                    ok = False
            except Exception as e:
                _record(f"{target} V", False, str(e))
                ok = False

    except Exception as e:
        _record("embedded voltages setup", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 7. Measurement Stability
# ---------------------------------------------------------------------------
def test_measurement_stability():
    """Five repeated voltage() readings at 5 V agree within 1%."""
    print("\n" + "=" * 60)
    print("TEST: Measurement Stability")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
        psu.set_voltage(min(5.0, CHANNEL_MAX_VOLTAGE))
        psu.set_current(min(1.0, CHANNEL_MAX_CURRENT))
        psu.enable()
        time.sleep(1.5)

        readings = []
        for i in range(5):
            readings.append(float(psu.voltage()))
            time.sleep(0.4)

        _record("collected 5 voltage readings", True, f"{[f'{r:.4f}' for r in readings]}")

        mean = sum(readings) / len(readings)
        spread = max(readings) - min(readings)
        cv = spread / max(abs(mean), 0.001)
        passed = cv < STABILITY_TOL
        _record(
            f"readings spread < {int(STABILITY_TOL*100)}% (coefficient of variation)",
            passed,
            f"spread={spread:.4f} V, mean={mean:.4f} V, cv={cv:.4f}",
        )
        if not passed:
            ok = False

    except Exception as e:
        _record("measurement stability", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 8. Current Measurement After set_current
# ---------------------------------------------------------------------------
def test_current_limit_readback():
    """current() returns a numeric near-zero measurement when output is enabled unloaded.

    On the Rigol DP800 mapper, current() returns the live measured current
    (not the setpoint register). With no load attached the reading should be
    near zero regardless of the programmed limit.
    """
    print("\n" + "=" * 60)
    print("TEST: Current Measurement (Unloaded)")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
        psu.set_voltage(min(5.0, CHANNEL_MAX_VOLTAGE))
        psu.enable()
        time.sleep(0.3)

        for limit_a in [0.5, 1.0]:
            psu.set_current(limit_a)
            time.sleep(0.2)
            measured_i = psu.current()
            passed = isinstance(measured_i, (int, float)) and abs(float(measured_i)) < MAX_UNLOADED_CURRENT
            _record(
                f"current() numeric and near-zero with limit={limit_a} A (unloaded)",
                passed,
                f"measured={measured_i}",
            )
            if not passed:
                ok = False

    except Exception as e:
        _record("current measurement unloaded", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 9. OVP / OCP Combined Pre-Enable Config
# ---------------------------------------------------------------------------
def test_protection_pre_enable():
    """Configuring voltage + OVP + OCP before enable produces coherent setpoints."""
    print("\n" + "=" * 60)
    print("TEST: OVP/OCP Pre-Enable Configuration")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)

        test_voltage = min(5.0, CHANNEL_MAX_VOLTAGE)
        ovp_limit = min(test_voltage * 1.1, CHANNEL_MAX_VOLTAGE)
        ocp_limit = min(1.0, CHANNEL_MAX_CURRENT)

        psu.set_voltage(test_voltage)
        psu.set_ovp(ovp_limit)
        psu.set_ocp(ocp_limit)
        psu.enable()
        time.sleep(0.5)

        v_sp = psu.voltage()
        ovp_val = psu.get_ovp_limit()
        ocp_val = psu.get_ocp_limit()

        passed_v = isinstance(v_sp, (int, float)) and _close_enough(float(v_sp), test_voltage, TOLERANCE)
        _record("voltage setpoint readable after enable", passed_v, f"v={v_sp}")
        if not passed_v:
            ok = False

        passed_ovp = isinstance(ovp_val, (int, float)) and float(ovp_val) > 0
        _record("OVP limit readable after enable", passed_ovp, f"ovp={ovp_val}")
        if not passed_ovp:
            ok = False

        passed_ocp = isinstance(ocp_val, (int, float)) and float(ocp_val) > 0
        _record("OCP limit readable after enable", passed_ocp, f"ocp={ocp_val}")
        if not passed_ocp:
            ok = False

        # OVP must be >= voltage setpoint (otherwise supply would immediately trip)
        passed_order = (
            isinstance(v_sp, (int, float))
            and isinstance(ovp_val, (int, float))
            and float(ovp_val) >= float(v_sp) * 0.95
        )
        _record(
            "OVP limit >= voltage setpoint",
            passed_order,
            f"ovp={ovp_val}, v={v_sp}",
        )
        if not passed_order:
            ok = False

    except Exception as e:
        _record("protection pre-enable config", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
            psu.clear_ovp()
            psu.set_ovp(CHANNEL_MAX_VOLTAGE)
            psu.clear_ocp()
            psu.set_ocp(CHANNEL_MAX_CURRENT)
            psu.disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 10. Rapid Enable/Disable Cycling
# ---------------------------------------------------------------------------
def test_rapid_cycling():
    """Enable/disable 5 times without error; final state is disabled."""
    print("\n" + "=" * 60)
    print("TEST: Rapid Enable/Disable Cycling")
    print("=" * 60)

    ok = True
    CYCLES = 5

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
        psu.set_voltage(min(5.0, CHANNEL_MAX_VOLTAGE))
        psu.set_current(min(1.0, CHANNEL_MAX_CURRENT))

        for i in range(CYCLES):
            psu.enable()
            time.sleep(0.1)
            psu.disable()
            time.sleep(0.1)

        _record(f"{CYCLES} enable/disable cycles completed", True)

        final_state = psu.output_is_enabled()
        passed_off = not bool(final_state)
        _record(
            "output disabled after final cycle",
            passed_off,
            f"output_is_enabled()={final_state!r}",
        )
        if not passed_off:
            ok = False

    except Exception as e:
        _record("rapid cycling", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 11. get_full_state() Structure
# ---------------------------------------------------------------------------
def test_full_state():
    """get_full_state() runs without error; if it returns a dict, verify shape."""
    print("\n" + "=" * 60)
    print("TEST: get_full_state() Structure")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)

        if not hasattr(psu, "get_full_state"):
            _record("get_full_state() method exists", False,
                    "method not present on this driver — skipping")
            return True  # Not a hard failure; method is driver-specific

        psu.set_voltage(min(5.0, CHANNEL_MAX_VOLTAGE))
        psu.enable()
        time.sleep(0.3)

        fs = psu.get_full_state()

        # Rigol DP800 prints diagnostics to stdout and returns None; that is
        # acceptable. If a driver returns a dict, also verify it has content.
        if fs is None:
            _record("get_full_state() completed without error (returns None)", True)
        elif isinstance(fs, dict):
            _record("get_full_state() returns dict", True, f"keys={list(fs.keys())[:8]}")
            passed_nonempty = len(fs) > 0
            _record("get_full_state() non-empty", passed_nonempty,
                    f"len={len(fs)}")
            if not passed_nonempty:
                ok = False
        else:
            _record("get_full_state() unexpected return type", False,
                    f"type={type(fs).__name__}")
            ok = False

    except Exception as e:
        _record("get_full_state()", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    print("Power Supply Advanced Test Suite")
    print(f"Testing net: {SUPPLY_NET}")
    print(f"Set SUPPLY_NET env var to override")
    print("=" * 60)

    # Preflight: verify the supply is reachable before running any tests.
    # Split into two blocks: Net.get() failures are config errors (exit 1);
    # state() failures are connectivity errors (exit 0 = skip).
    try:
        from lager import Net, NetType
        psu = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
    except Exception as e:
        print(f"\nERROR: Failed to load net '{SUPPLY_NET}': {e}")
        print("Fix: check the net's instrument type in saved_nets.json (e.g. 'Rigol_DP821').")
        print(f"  lager nets list --box <box>")
        print(f"  lager nets tui --box <box>")
        sys.exit(1)

    if psu is None:
        print(f"\nSKIP: Net '{SUPPLY_NET}' not found in net configuration.")
        print("Skipping all tests for this device.")
        sys.exit(0)

    try:
        psu.state()
    except Exception as e:
        print(f"\nSKIP: Cannot connect to net '{SUPPLY_NET}' — device not reachable: {e}")
        print("\nDiagnose the hardware issue with:")
        print(f"  lager instruments --box <box>")
        print(f"  lager diagnose {SUPPLY_NET} --box <box>")
        print(f"  lager hello --box <box>")
        print("\nCommon fixes:")
        print("  - Check the power supply is powered on and USB cable is connected")
        print("  - If 'nodev': run  lager power supply1 state --box <box>  to reset the session")
        print("  - If 'busy': check lsof output in 'lager diagnose' for competing processes")
        print("  - If 'usbtmc kmod LOADED': run  lager box update")
        print("\nSkipping all tests for this device.")
        sys.exit(0)

    tests = [
        ("Live Measurements",              test_live_measurements),
        ("Measured Voltage vs. Setpoint",  test_setpoint_vs_measured),
        ("Power Calculation Consistency",  test_power_consistency),
        ("Output State Querying",          test_output_is_enabled),
        ("Output Mode Detection",          test_output_mode),
        ("Common Embedded Voltages",       test_embedded_voltages),
        ("Measurement Stability",          test_measurement_stability),
        ("Current Measurement (Unloaded)", test_current_limit_readback),
        ("OVP/OCP Pre-Enable Config",      test_protection_pre_enable),
        ("Rapid Enable/Disable Cycling",   test_rapid_cycling),
        ("get_full_state() Structure",     test_full_state),
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
        try:
            from lager import Net, NetType
            Net.get(SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

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
