#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive battery simulator tests targeting the Keithley 2281S-20-6 via the lager Python API.
Covers mode switching, parameter configuration, output control, measurements, protection limits,
and monitor state.

Run with: lager python test/api/power/test_battery_Keithley_2281S.py --box <YOUR-BOX>

Prerequisites:
- A battery net configured on the box pointing to a Keithley 2281S
  (default net name 'battery1')

Override the net with:  KEITHLEY_BATTERY_NET=my-battery lager python ...
"""
import sys
import os
import time
import traceback

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
KEITHLEY_BATTERY_NET = os.environ.get("KEITHLEY_BATTERY_NET", "battery1")

_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


# ---------------------------------------------------------------------------
# 1. Battery Mode Entry
# ---------------------------------------------------------------------------
def test_battery_mode_entry():
    """set_to_battery_mode() switches instrument to battery entry function."""
    print("\n" + "=" * 60)
    print("TEST: Battery Mode Entry")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()
        _record("set_to_battery_mode() completed without error", True)

        try:
            batt.print_state()
            _record("print_state() runs after set_to_battery_mode()", True)
        except Exception as e:
            _record("print_state() after set_to_battery_mode()", False, str(e))
            ok = False

    except Exception as e:
        _record("set_to_battery_mode()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 2. Static Mode
# ---------------------------------------------------------------------------
def test_static_mode():
    """set_mode('static') is accepted without error."""
    print("\n" + "=" * 60)
    print("TEST: Static Simulation Mode")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()
        batt.set_mode("static")
        _record("set_mode('static') completed without error", True)

        try:
            batt.print_state()
            _record("print_state() runs in static mode", True)
        except Exception as e:
            _record("print_state() in static mode", False, str(e))
            ok = False

    except Exception as e:
        _record("set_mode('static')", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 3. Dynamic Mode
# ---------------------------------------------------------------------------
def test_dynamic_mode():
    """set_mode('dynamic') is accepted; skip gracefully if instrument requires a model."""
    print("\n" + "=" * 60)
    print("TEST: Dynamic Simulation Mode")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()

        try:
            batt.set_mode("dynamic")
            _record("set_mode('dynamic') completed without error", True)
        except Exception as e:
            msg = str(e)
            # Some instruments require a loaded battery model to enter dynamic mode
            if "model" in msg.lower() or "704" in msg or "not permitted" in msg.lower():
                _record(
                    "set_mode('dynamic') skipped — requires battery model loaded",
                    True,
                    msg[:80],
                )
            else:
                _record("set_mode('dynamic')", False, msg)
                ok = False
            return ok

        # Switch back to static before leaving
        try:
            batt.set_mode("static")
        except Exception:
            pass

    except Exception as e:
        _record("dynamic mode setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 4. SOC Setting
# ---------------------------------------------------------------------------
def test_soc_setting():
    """set_soc() accepts 0, 50, 100; rejects 101."""
    print("\n" + "=" * 60)
    print("TEST: State of Charge Setting")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()

        for soc_val in [0, 50, 100]:
            try:
                batt.set_soc(soc_val)
                _record(f"set_soc({soc_val}) accepted", True)
            except Exception as e:
                _record(f"set_soc({soc_val})", False, str(e))
                ok = False

        try:
            batt.set_soc(101)
            _record("set_soc(101) should raise but did not", False)
            ok = False
        except Exception:
            _record("set_soc(101) raises as expected", True)

        # Reset to safe value
        try:
            batt.set_soc(100)
        except Exception:
            pass

    except Exception as e:
        _record("SOC setting setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 5. VOC Setting
# ---------------------------------------------------------------------------
def test_voc_setting():
    """set_voc() accepts a valid voltage; rejects negative."""
    print("\n" + "=" * 60)
    print("TEST: Open-Circuit Voltage Setting")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()

        try:
            batt.set_voc(3.7)
            _record("set_voc(3.7) accepted", True)
        except Exception as e:
            _record("set_voc(3.7)", False, str(e))
            ok = False

        try:
            batt.set_voc(-0.1)
            _record("set_voc(-0.1) should raise but did not", False)
            ok = False
        except Exception:
            _record("set_voc(-0.1) raises as expected", True)

    except Exception as e:
        _record("VOC setting setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 6. Voltage Full / Empty
# ---------------------------------------------------------------------------
def test_voltage_full_empty():
    """set_volt_full() and set_volt_empty() are accepted."""
    print("\n" + "=" * 60)
    print("TEST: Voltage Full / Empty Setting")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()

        try:
            batt.set_volt_full(4.2)
            _record("set_volt_full(4.2) accepted", True)
        except Exception as e:
            _record("set_volt_full(4.2)", False, str(e))
            ok = False

        try:
            batt.set_volt_empty(3.0)
            _record("set_volt_empty(3.0) accepted", True)
        except Exception as e:
            _record("set_volt_empty(3.0)", False, str(e))
            ok = False

        try:
            batt.set_volt_full(-1.0)
            _record("set_volt_full(-1.0) should raise but did not", False)
            ok = False
        except Exception:
            _record("set_volt_full(-1.0) raises as expected", True)

    except Exception as e:
        _record("voltage full/empty setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 7. Capacity Setting
# ---------------------------------------------------------------------------
def test_capacity_setting():
    """set_capacity() accepts a positive value; rejects zero."""
    print("\n" + "=" * 60)
    print("TEST: Capacity Setting")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()

        try:
            batt.set_capacity(2.0)
            _record("set_capacity(2.0) accepted", True)
        except Exception as e:
            _record("set_capacity(2.0)", False, str(e))
            ok = False

        try:
            batt.set_capacity(0)
            _record("set_capacity(0) should raise but did not", False)
            ok = False
        except Exception:
            _record("set_capacity(0) raises as expected", True)

    except Exception as e:
        _record("capacity setting setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 8. Current Limit Setting
# ---------------------------------------------------------------------------
def test_current_limit_setting():
    """set_current_limit() accepts a valid value; rejects zero."""
    print("\n" + "=" * 60)
    print("TEST: Current Limit Setting")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()

        try:
            batt.set_current_limit(1.0)
            _record("set_current_limit(1.0) accepted", True)
        except Exception as e:
            _record("set_current_limit(1.0)", False, str(e))
            ok = False

        try:
            batt.set_current_limit(0)
            _record("set_current_limit(0) should raise but did not", False)
            ok = False
        except Exception:
            _record("set_current_limit(0) raises as expected", True)

        try:
            batt.set_current_limit(7.0)
            _record("set_current_limit(7.0) should raise but did not", False)
            ok = False
        except Exception:
            _record("set_current_limit(7.0) raises (exceeds 6A max)", True)

    except Exception as e:
        _record("current limit setting setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 9. Battery Model Loading
# ---------------------------------------------------------------------------
def test_battery_model_loading():
    """set_model('discharge') and set_model(0) succeed; an invalid name raises."""
    print("\n" + "=" * 60)
    print("TEST: Battery Model Loading")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()

        for model_id in ["discharge", 0]:
            try:
                batt.set_model(model_id)
                _record(f"set_model({model_id!r}) accepted", True)
            except Exception as e:
                _record(f"set_model({model_id!r})", False, str(e))
                ok = False

        try:
            batt.set_model("not_a_real_battery_model_xyz")
            _record("set_model('not_a_real_battery_model_xyz') should raise but did not", False)
            ok = False
        except Exception:
            _record("set_model with invalid name raises as expected", True)

    except Exception as e:
        _record("battery model loading setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 10. Enable / Disable Output
# ---------------------------------------------------------------------------
def test_enable_disable_output():
    """enable_battery() / disable_battery() toggle output state."""
    print("\n" + "=" * 60)
    print("TEST: Enable / Disable Output")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()
        batt.set_voc(3.7)
        batt.set_current_limit(1.0)

        batt.enable_battery()
        time.sleep(0.5)
        state = batt.get_monitor_state()
        passed_on = bool(state.get("enabled", False))
        _record("enabled=True after enable_battery()", passed_on, f"enabled={state.get('enabled')!r}")
        if not passed_on:
            ok = False

        batt.disable_battery()
        time.sleep(0.3)
        state = batt.get_monitor_state()
        passed_off = not bool(state.get("enabled", True))
        _record("enabled=False after disable_battery()", passed_off, f"enabled={state.get('enabled')!r}")
        if not passed_off:
            ok = False

    except Exception as e:
        _record("enable/disable output", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery).disable_battery()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 11. Terminal Voltage Measurement
# ---------------------------------------------------------------------------
def test_terminal_voltage():
    """terminal_voltage() returns a numeric value after output is enabled."""
    print("\n" + "=" * 60)
    print("TEST: Terminal Voltage Measurement")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()
        batt.set_voc(3.7)
        batt.set_current_limit(1.0)
        batt.enable_battery()
        time.sleep(0.5)

        tv = batt.terminal_voltage()
        passed = isinstance(tv, (int, float))
        _record("terminal_voltage() returns numeric", passed, f"value={tv}")
        if not passed:
            ok = False

        passed_pos = passed and float(tv) >= 0
        _record("terminal_voltage() >= 0", passed_pos, f"value={tv}")
        if not passed_pos:
            ok = False

    except Exception as e:
        _record("terminal_voltage()", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery).disable_battery()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 12. Current Measurement
# ---------------------------------------------------------------------------
def test_current_measurement():
    """current() returns a numeric value."""
    print("\n" + "=" * 60)
    print("TEST: Current Measurement")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()
        batt.set_voc(3.7)
        batt.set_current_limit(1.0)
        batt.enable_battery()
        time.sleep(0.5)

        ci = batt.current()
        passed = isinstance(ci, (int, float))
        _record("current() returns numeric", passed, f"value={ci}")
        if not passed:
            ok = False

    except Exception as e:
        _record("current()", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery).disable_battery()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 13. ESR Measurement
# ---------------------------------------------------------------------------
def test_esr_measurement():
    """esr() returns a positive numeric value."""
    print("\n" + "=" * 60)
    print("TEST: ESR Measurement")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()

        esr_val = batt.esr()
        passed_type = isinstance(esr_val, (int, float))
        _record("esr() returns numeric", passed_type, f"value={esr_val}")
        if not passed_type:
            ok = False

        passed_pos = passed_type and float(esr_val) >= 0
        _record("esr() >= 0", passed_pos, f"value={esr_val}")
        if not passed_pos:
            ok = False

    except Exception as e:
        _record("esr()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 14. Protection Limits
# ---------------------------------------------------------------------------
def test_protection_limits():
    """set_ovp() and set_ocp() are accepted; monitor state reflects the values."""
    print("\n" + "=" * 60)
    print("TEST: Protection Limits")
    print("=" * 60)

    ok = True

    OVP_TARGET = 5.0
    OCP_TARGET = 2.0

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()

        try:
            batt.set_ovp(OVP_TARGET)
            _record(f"set_ovp({OVP_TARGET}) accepted", True)
        except Exception as e:
            _record(f"set_ovp({OVP_TARGET})", False, str(e))
            ok = False

        try:
            batt.set_ocp(OCP_TARGET)
            _record(f"set_ocp({OCP_TARGET}) accepted", True)
        except Exception as e:
            _record(f"set_ocp({OCP_TARGET})", False, str(e))
            ok = False

        try:
            state = batt.get_monitor_state()
            ovp_rb = state.get("ovp_limit")
            passed_ovp = isinstance(ovp_rb, (int, float)) and abs(float(ovp_rb) - OVP_TARGET) < 0.1
            _record(
                f"ovp_limit readback ≈ {OVP_TARGET}",
                passed_ovp,
                f"readback={ovp_rb}",
            )
            if not passed_ovp:
                ok = False

            ocp_rb = state.get("ocp_limit")
            passed_ocp = isinstance(ocp_rb, (int, float)) and abs(float(ocp_rb) - OCP_TARGET) < 0.1
            _record(
                f"ocp_limit readback ≈ {OCP_TARGET}",
                passed_ocp,
                f"readback={ocp_rb}",
            )
            if not passed_ocp:
                ok = False

        except Exception as e:
            _record("protection limits readback", False, str(e))
            ok = False

    except Exception as e:
        _record("protection limits setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 15. Protection Clearing
# ---------------------------------------------------------------------------
def test_protection_clearing():
    """clear(), clear_ovp(), and clear_ocp() run without raising."""
    print("\n" + "=" * 60)
    print("TEST: Protection Clearing")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()

        for method_name in ("clear", "clear_ovp", "clear_ocp"):
            try:
                getattr(batt, method_name)()
                _record(f"{method_name}() completed without error", True)
            except Exception as e:
                _record(f"{method_name}()", False, str(e))
                ok = False

    except Exception as e:
        _record("protection clearing setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 16. Monitor State Structure
# ---------------------------------------------------------------------------
def test_monitor_state():
    """get_monitor_state() returns a dict with all expected keys."""
    print("\n" + "=" * 60)
    print("TEST: Monitor State Structure")
    print("=" * 60)

    ok = True

    EXPECTED_KEYS = [
        "terminal_voltage", "current", "esr", "soc", "voc",
        "enabled", "mode", "model", "capacity", "current_limit",
        "ocp_limit", "ovp_limit", "volt_full", "volt_empty",
        "ocp_tripped", "ovp_tripped",
    ]

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()

        state = batt.get_monitor_state()

        passed_type = isinstance(state, dict)
        _record("get_monitor_state() returns dict", passed_type, f"type={type(state).__name__}")
        if not passed_type:
            return False

        for key in EXPECTED_KEYS:
            present = key in state
            _record(f"key '{key}' present", present, "" if present else f"missing from {list(state.keys())[:6]}")
            if not present:
                ok = False

        numeric_keys = ["terminal_voltage", "current", "esr", "soc", "voc",
                        "capacity", "current_limit", "ocp_limit", "ovp_limit",
                        "volt_full", "volt_empty"]
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

    return ok


# ---------------------------------------------------------------------------
# 17. print_state() Runs Without Error
# ---------------------------------------------------------------------------
def test_print_state():
    """print_state() completes without raising an exception."""
    print("\n" + "=" * 60)
    print("TEST: print_state() Completeness")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()
        batt.print_state()
        _record("print_state() completed without error", True)
    except Exception as e:
        _record("print_state()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 18. Rapid Output Cycling
# ---------------------------------------------------------------------------
def test_rapid_output_cycling():
    """Enable/disable battery output 5 times without error; final state is OFF."""
    print("\n" + "=" * 60)
    print("TEST: Rapid Output Cycling")
    print("=" * 60)

    ok = True
    CYCLES = 5

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
        batt.set_to_battery_mode()
        batt.set_voc(3.7)
        batt.set_current_limit(1.0)

        for _ in range(CYCLES):
            batt.enable_battery()
            time.sleep(0.2)
            batt.disable_battery()
            time.sleep(0.2)

        _record(f"{CYCLES} enable/disable cycles completed", True)

        state = batt.get_monitor_state()
        final_enabled = bool(state.get("enabled", True))
        passed_off = not final_enabled
        _record(
            "output disabled after final cycle",
            passed_off,
            f"enabled={state.get('enabled')!r}",
        )
        if not passed_off:
            ok = False

    except Exception as e:
        _record("rapid output cycling", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery).disable_battery()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    print("Keithley 2281S Battery Simulator Test Suite")
    print(f"Testing net: {KEITHLEY_BATTERY_NET}")
    print(f"Set KEITHLEY_BATTERY_NET env var to override")
    print("=" * 60)

    try:
        from lager import Net, NetType
        batt = Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery)
    except Exception as e:
        print(f"\nERROR: Failed to load net '{KEITHLEY_BATTERY_NET}': {e}")
        print("Fix: check the net's instrument type in saved_nets.json (e.g. 'Keithley_2281S').")
        print(f"  lager nets list --box <box>")
        print(f"  lager nets tui --box <box>")
        sys.exit(1)

    try:
        batt.print_state()
    except Exception as e:
        print(f"\nERROR: Cannot connect to net '{KEITHLEY_BATTERY_NET}' — device not reachable: {e}")
        print("\nDiagnose the hardware issue with:")
        print(f"  lager instruments --box <box>")
        print(f"  lager diagnose {KEITHLEY_BATTERY_NET} --box <box>")
        print(f"  lager hello --box <box>")
        print("\nCommon fixes:")
        print("  - Check the Keithley is powered on and USB cable is connected")
        print("  - Verify the net is configured in /etc/lager/saved_nets.json")
        print("  - If busy: check lsof output in 'lager diagnose' for competing processes")
        sys.exit(1)

    tests = [
        ("Battery Mode Entry",           test_battery_mode_entry),
        ("Static Simulation Mode",       test_static_mode),
        ("Dynamic Simulation Mode",      test_dynamic_mode),
        ("SOC Setting",                  test_soc_setting),
        ("VOC Setting",                  test_voc_setting),
        ("Voltage Full / Empty",         test_voltage_full_empty),
        ("Capacity Setting",             test_capacity_setting),
        ("Current Limit Setting",        test_current_limit_setting),
        ("Battery Model Loading",        test_battery_model_loading),
        ("Enable / Disable Output",      test_enable_disable_output),
        ("Terminal Voltage Measurement", test_terminal_voltage),
        ("Current Measurement",          test_current_measurement),
        ("ESR Measurement",              test_esr_measurement),
        ("Protection Limits",            test_protection_limits),
        ("Protection Clearing",          test_protection_clearing),
        ("Monitor State Structure",      test_monitor_state),
        ("print_state() Completeness",   test_print_state),
        ("Rapid Output Cycling",         test_rapid_output_cycling),
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
            Net.get(KEITHLEY_BATTERY_NET, type=NetType.Battery).disable_battery()
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
