#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive power supply tests targeting the Keithley 2281S-20-6 via the lager Python API.
Covers live measurements, setpoint accuracy, protection limits, output state, monitor state,
and rapid cycling. Complements test_supply_comprehensive.py — run both for full coverage.

Run with: lager python test/api/power/test_supply_Keithley_2281S.py --box <YOUR-BOX>

Prerequisites:
- A power supply net configured on the box pointing to a Keithley 2281S
  (default net name 'keithley_supply1')
- No load attached; unloaded measurements are expected (< 100 mA draw)

Override the net with:      KEITHLEY_SUPPLY_NET=my-supply lager python ...
Override channel limits:    KEITHLEY_MAX_VOLTAGE=20 KEITHLEY_MAX_CURRENT=6 lager python ...
"""
import sys
import os
import time
import traceback

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
KEITHLEY_SUPPLY_NET  = os.environ.get("KEITHLEY_SUPPLY_NET", "supply1")
TOLERANCE            = 0.05   # 5 % — setpoint / live-measurement agreement
TIGHT_TOLERANCE      = 0.02   # 2 % — setpoint vs. measured voltage (same device)
STABILITY_TOL        = 0.01   # 1 % — max spread across repeated reads
POWER_TOL            = 0.20   # 20 % — P vs V×I consistency
MAX_UNLOADED_CURRENT = 0.1    # 100 mA max expected with no load
KEITHLEY_MAX_VOLTAGE = 20.0   # 2281S-20-6 hardware ceiling
KEITHLEY_MAX_CURRENT = 6.0    # 2281S-20-6 hardware ceiling
CHANNEL_MAX_VOLTAGE  = float(os.environ.get("KEITHLEY_MAX_VOLTAGE", "20.0"))
CHANNEL_MAX_CURRENT  = float(os.environ.get("KEITHLEY_MAX_CURRENT", "6.0"))

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
        psu = Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply)
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
            Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 2. Measured Voltage vs. Setpoint Agreement
# ---------------------------------------------------------------------------
def test_setpoint_vs_measured():
    """voltage() should track the set_voltage() target within tight tolerance."""
    print("\n" + "=" * 60)
    print("TEST: Measured Voltage vs. Setpoint")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply)
        target = min(5.0, CHANNEL_MAX_VOLTAGE)
        psu.set_voltage(target)
        psu.set_current(min(1.0, CHANNEL_MAX_CURRENT))
        psu.enable()
        time.sleep(0.5)

        measured = psu.voltage()

        passed = isinstance(measured, (int, float)) and _close_enough(
            float(measured), target, TIGHT_TOLERANCE
        )
        _record(
            f"voltage() within {int(TIGHT_TOLERANCE * 100)}% of {target} V target",
            passed,
            f"measured={measured:.4f} V",
        )
        if not passed:
            ok = False

    except Exception as e:
        _record("setpoint vs. measured", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply).disable()
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
        psu = Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply)
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
            f"P ~= V*I (abs<0.15 W or within {int(POWER_TOL * 100)}%)",
            passed,
            f"P={mp:.4f} W, V*I={vi_product:.4f} W",
        )
        if not passed:
            ok = False

    except Exception as e:
        _record("power consistency", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply).disable()
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
        psu = Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply)
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
            Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 5. Output Mode Detection
# ---------------------------------------------------------------------------
def test_output_mode():
    """get_output_mode() returns a string (CV expected with no load)."""
    print("\n" + "=" * 60)
    print("TEST: Output Mode Detection")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply)
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
            Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 6. Common Embedded Voltages
# ---------------------------------------------------------------------------
def test_embedded_voltages():
    """Live-measured voltage matches setpoint for common embedded voltages."""
    print("\n" + "=" * 60)
    print("TEST: Common Embedded Voltages")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply)
        psu.set_current(min(1.0, CHANNEL_MAX_CURRENT))
        psu.set_ovp(CHANNEL_MAX_VOLTAGE)
        psu.enable()
        time.sleep(0.5)

        _all_voltages = [1.8, 2.5, 3.3, 5.0]
        targets = [v for v in _all_voltages if v <= CHANNEL_MAX_VOLTAGE]
        if not targets:
            _record(
                "embedded voltages skipped — channel max too low",
                True,
                f"max={CHANNEL_MAX_VOLTAGE} V, candidates={_all_voltages}",
            )
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
            Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 7. Measurement Stability
# ---------------------------------------------------------------------------
def test_measurement_stability():
    """Five repeated voltage() readings agree within 1%."""
    print("\n" + "=" * 60)
    print("TEST: Measurement Stability")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply)
        psu.set_voltage(min(5.0, CHANNEL_MAX_VOLTAGE))
        psu.set_current(min(1.0, CHANNEL_MAX_CURRENT))
        psu.enable()
        time.sleep(1.5)

        readings = []
        for _ in range(5):
            readings.append(float(psu.voltage()))
            time.sleep(0.4)

        _record("collected 5 voltage readings", True, f"{[f'{r:.4f}' for r in readings]}")

        mean = sum(readings) / len(readings)
        spread = max(readings) - min(readings)
        cv = spread / max(abs(mean), 0.001)
        passed = cv < STABILITY_TOL
        _record(
            f"readings spread < {int(STABILITY_TOL * 100)}% (coefficient of variation)",
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
            Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 8. Current Measurement (Unloaded)
# ---------------------------------------------------------------------------
def test_current_limit_readback():
    """current() returns near-zero measurement when output is enabled unloaded."""
    print("\n" + "=" * 60)
    print("TEST: Current Measurement (Unloaded)")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply)
        psu.set_voltage(min(5.0, CHANNEL_MAX_VOLTAGE))
        psu.enable()
        time.sleep(0.3)

        for limit_a in [0.5, 1.0]:
            psu.set_current(min(limit_a, CHANNEL_MAX_CURRENT))
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
            Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 9. OVP / OCP Pre-Enable Configuration
# ---------------------------------------------------------------------------
def test_protection_pre_enable():
    """Configuring voltage + OVP + OCP before enable produces coherent setpoints."""
    print("\n" + "=" * 60)
    print("TEST: OVP/OCP Pre-Enable Configuration")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply)

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
            psu = Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply)
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
        psu = Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply)
        psu.set_voltage(min(5.0, CHANNEL_MAX_VOLTAGE))
        psu.set_current(min(1.0, CHANNEL_MAX_CURRENT))

        for _ in range(CYCLES):
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
            Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 11. Channel Limits
# ---------------------------------------------------------------------------
def test_channel_limits():
    """get_channel_limits() returns a dict with the 2281S-20-6 hardware limits."""
    print("\n" + "=" * 60)
    print("TEST: Channel Limits")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        psu = Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply)

        limits = psu.get_channel_limits()

        passed_type = isinstance(limits, dict)
        _record("get_channel_limits() returns dict", passed_type, f"type={type(limits).__name__}")
        if not passed_type:
            return False

        v_max = limits.get("voltage_max")
        passed_vmax = isinstance(v_max, (int, float)) and float(v_max) == KEITHLEY_MAX_VOLTAGE
        _record(
            f"voltage_max == {KEITHLEY_MAX_VOLTAGE} V",
            passed_vmax,
            f"voltage_max={v_max}",
        )
        if not passed_vmax:
            ok = False

        i_max = limits.get("current_max")
        passed_imax = isinstance(i_max, (int, float)) and float(i_max) == KEITHLEY_MAX_CURRENT
        _record(
            f"current_max == {KEITHLEY_MAX_CURRENT} A",
            passed_imax,
            f"current_max={i_max}",
        )
        if not passed_imax:
            ok = False

    except Exception as e:
        _record("get_channel_limits()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 12. Monitor State Structure
# ---------------------------------------------------------------------------
def test_monitor_state():
    """get_monitor_state() returns a dict with all expected keys and numeric values."""
    print("\n" + "=" * 60)
    print("TEST: Monitor State Structure")
    print("=" * 60)

    ok = True

    EXPECTED_KEYS = [
        "voltage", "current", "power", "enabled", "mode",
        "voltage_set", "current_set", "voltage_max", "current_max",
        "ocp_limit", "ocp_tripped", "ovp_limit", "ovp_tripped",
    ]

    try:
        from lager import Net, NetType
        psu = Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply)
        psu.set_voltage(min(5.0, CHANNEL_MAX_VOLTAGE))
        psu.enable()
        time.sleep(0.3)

        state = psu.get_monitor_state()

        passed_type = isinstance(state, dict)
        _record("get_monitor_state() returns dict", passed_type, f"type={type(state).__name__}")
        if not passed_type:
            return False

        for key in EXPECTED_KEYS:
            present = key in state
            _record(f"key '{key}' present", present, "" if present else f"missing from {list(state.keys())[:6]}")
            if not present:
                ok = False

        numeric_keys = ["voltage", "current", "power", "voltage_set", "current_set",
                        "voltage_max", "current_max", "ocp_limit", "ovp_limit"]
        for key in numeric_keys:
            if key in state:
                passed_num = isinstance(state[key], (int, float))
                _record(f"state['{key}'] is numeric", passed_num, f"value={state[key]!r}")
                if not passed_num:
                    ok = False

        bool_keys = ["enabled", "ocp_tripped", "ovp_tripped"]
        for key in bool_keys:
            if key in state:
                passed_bool = isinstance(state[key], bool)
                _record(f"state['{key}'] is bool", passed_bool, f"value={state[key]!r}")
                if not passed_bool:
                    ok = False

    except Exception as e:
        _record("get_monitor_state()", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply).disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    print("Keithley 2281S Power Supply Test Suite")
    print(f"Testing net: {KEITHLEY_SUPPLY_NET}")
    print(f"Set KEITHLEY_SUPPLY_NET env var to override")
    print(f"Keithley 2281S limits: {KEITHLEY_MAX_VOLTAGE} V / {KEITHLEY_MAX_CURRENT} A")
    print("=" * 60)

    try:
        from lager import Net, NetType
        psu = Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply)
    except Exception as e:
        print(f"\nERROR: Failed to load net '{KEITHLEY_SUPPLY_NET}': {e}")
        print("Fix: check the net's instrument type in saved_nets.json (e.g. 'Keithley_2281S').")
        print(f"  lager nets list --box <box>")
        print(f"  lager nets tui --box <box>")
        sys.exit(1)

    if psu is None:
        print(f"\nSKIP: Net '{KEITHLEY_SUPPLY_NET}' not found in net configuration.")
        print("Skipping all tests for this device.")
        sys.exit(0)

    try:
        psu.state()
    except Exception as e:
        print(f"\nSKIP: Cannot connect to net '{KEITHLEY_SUPPLY_NET}' — device not reachable: {e}")
        print("\nDiagnose the hardware issue with:")
        print(f"  lager instruments --box <box>")
        print(f"  lager diagnose {KEITHLEY_SUPPLY_NET} --box <box>")
        print(f"  lager hello --box <box>")
        print("\nCommon fixes:")
        print("  - Check the Keithley is powered on and USB cable is connected")
        print("  - Verify the net is configured in /etc/lager/saved_nets.json")
        print("  - If busy: check lsof output in 'lager diagnose' for competing processes")
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
        ("Channel Limits",                 test_channel_limits),
        ("Monitor State Structure",        test_monitor_state),
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
            Net.get(KEITHLEY_SUPPLY_NET, type=NetType.PowerSupply).disable()
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
