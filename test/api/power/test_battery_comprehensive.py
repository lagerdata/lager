#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive battery simulator tests targeting Keithley 2281S via lager Python API.

Run with: lager python test/api/power/test_battery_comprehensive.py --box <YOUR-BOX>

Prerequisites:
- A battery net configured in /etc/lager/saved_nets.json with instrument "Keithley_2281S"
- Example net configuration:
  {
    "name": "battery1",
    "role": "battery",
    "instrument": "keithley_2281s",
    "address": "TCPIP0::192.168.0.100::inst0::INSTR"
  }

Adjust BATTERY_NET environment variable or default below to match your box.
"""
import sys
import os
import time
import traceback

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BATTERY_NET = os.environ.get("BATTERY_NET", "battery1")

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


# ---------------------------------------------------------------------------
# 1. Imports
# ---------------------------------------------------------------------------
def test_imports():
    """Verify battery-related imports work."""
    print("\n" + "=" * 60)
    print("TEST: Imports")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        assert hasattr(NetType, "Battery"), "NetType.Battery not found"
        _record("import Net, NetType", True)
    except Exception as e:
        _record("import Net, NetType", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 2. Net.get
# ---------------------------------------------------------------------------
def test_net_get():
    """Verify Net.get returns a battery net object."""
    print("\n" + "=" * 60)
    print("TEST: Net.get")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        passed = batt is not None
        _record("Net.get returns object", passed, type(batt).__name__)
        if not passed:
            ok = False
    except Exception as e:
        _record("Net.get returns object", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 3. String Repr
# ---------------------------------------------------------------------------
def test_string_repr():
    """Verify str(batt) contains meaningful info."""
    print("\n" + "=" * 60)
    print("TEST: String Repr")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        s = str(batt)
        passed = isinstance(s, str) and len(s) > 0
        _record("str(batt) non-empty", passed, repr(s[:80]))
    except Exception as e:
        _record("str(batt) non-empty", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 4. Initialize Mode
# ---------------------------------------------------------------------------
def test_initialize_mode():
    """Verify set_mode_battery() enters battery simulation mode."""
    print("\n" + "=" * 60)
    print("TEST: Initialize Mode")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        _record("set_mode_battery()", True)
    except Exception as e:
        _record("set_mode_battery()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 5. Mode Static
# ---------------------------------------------------------------------------
def test_mode_static():
    """Verify mode('static') sets static simulation mode."""
    print("\n" + "=" * 60)
    print("TEST: Mode Static")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')
        _record("mode('static')", True)
    except Exception as e:
        _record("mode('static')", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 6. Mode Dynamic
# ---------------------------------------------------------------------------
def test_mode_dynamic():
    """Verify mode('dynamic') and switching back to 'static'."""
    print("\n" + "=" * 60)
    print("TEST: Mode Dynamic")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()

        batt.mode('dynamic')
        _record("mode('dynamic')", True)

        batt.mode('static')
        _record("mode('static') after dynamic", True)
    except Exception as e:
        _record("mode dynamic/static switch", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 7. SOC Set/Read
# ---------------------------------------------------------------------------
def test_soc_set_read():
    """Verify soc(value) sets and soc() reads state of charge."""
    print("\n" + "=" * 60)
    print("TEST: SOC Set/Read")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        batt.soc(80)
        _record("soc(80) set", True)

        # Read back (prints to stdout)
        batt.soc()
        _record("soc() readback", True)
    except Exception as e:
        _record("soc set/read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 8. SOC Edge Cases
# ---------------------------------------------------------------------------
def test_soc_edge_cases():
    """Verify SOC boundary values: 0% and 100%."""
    print("\n" + "=" * 60)
    print("TEST: SOC Edge Cases")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        batt.soc(0)
        _record("soc(0) minimum", True)

        batt.soc(100)
        _record("soc(100) maximum", True)

        # Restore to a mid-range value
        batt.soc(50)
        _record("soc(50) restored", True)
    except Exception as e:
        _record("soc edge cases", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 9. VOC Set
# ---------------------------------------------------------------------------
def test_voc_set():
    """Verify voc(value) sets open-circuit voltage."""
    print("\n" + "=" * 60)
    print("TEST: VOC Set")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        batt.voc(12.6)
        _record("voc(12.6)", True)

        # Read back
        batt.voc()
        _record("voc() readback", True)
    except Exception as e:
        _record("voc set", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 10. Voltage Limits
# ---------------------------------------------------------------------------
def test_voltage_limits():
    """Verify voltage_full() and voltage_empty() set voltage bounds."""
    print("\n" + "=" * 60)
    print("TEST: Voltage Limits")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        batt.voltage_full(13.0)
        _record("voltage_full(13.0)", True)

        batt.voltage_empty(10.0)
        _record("voltage_empty(10.0)", True)
    except Exception as e:
        _record("voltage limits", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 11. Capacity
# ---------------------------------------------------------------------------
def test_capacity():
    """Verify capacity(value) sets battery capacity."""
    print("\n" + "=" * 60)
    print("TEST: Capacity")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        batt.capacity(5.0)  # 5 Ah (Keithley uses Ah, not mAh)
        _record("capacity(5.0)", True)

        # Read back
        batt.capacity()
        _record("capacity() readback", True)
    except Exception as e:
        _record("capacity", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 12. ESR Set/Read
# ---------------------------------------------------------------------------
def test_esr():
    """Verify esr() reads equivalent series resistance."""
    print("\n" + "=" * 60)
    print("TEST: ESR Set/Read")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        esr_val = batt.esr()
        passed = isinstance(esr_val, float) and esr_val >= 0
        _record("esr() readback", passed, f"{esr_val:.4f} ohms")
        if not passed:
            ok = False
    except Exception as e:
        _record("esr() readback", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 13. Current Limit
# ---------------------------------------------------------------------------
def test_current_limit():
    """Verify current_limit(value) sets charge/discharge current limit."""
    print("\n" + "=" * 60)
    print("TEST: Current Limit")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        batt.current_limit(2.0)
        _record("current_limit(2.0)", True)

        # Read back
        batt.current_limit()
        _record("current_limit() readback", True)
    except Exception as e:
        _record("current limit", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 14. OVP Set/Read
# ---------------------------------------------------------------------------
def test_ovp():
    """Verify ovp(value) sets and ovp() reads over-voltage protection."""
    print("\n" + "=" * 60)
    print("TEST: OVP Set/Read")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        batt.ovp(15.0)
        _record("ovp(15.0) set", True)

        batt.ovp()
        _record("ovp() readback", True)
    except Exception as e:
        _record("ovp set/read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 15. OCP Set/Read
# ---------------------------------------------------------------------------
def test_ocp():
    """Verify ocp(value) sets and ocp() reads over-current protection."""
    print("\n" + "=" * 60)
    print("TEST: OCP Set/Read")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        batt.ocp(6.0)
        _record("ocp(6.0) set", True)

        batt.ocp()
        _record("ocp() readback", True)
    except Exception as e:
        _record("ocp set/read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 16. Model Set/Read
# ---------------------------------------------------------------------------
def test_model():
    """Verify model(name) sets and model() reads battery model."""
    print("\n" + "=" * 60)
    print("TEST: Model Set/Read")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        batt.model('18650')
        _record("model('18650') set", True)

        batt.model()
        _record("model() readback", True)
    except Exception as e:
        _record("model set/read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 17. Model Aliases
# ---------------------------------------------------------------------------
def test_model_aliases():
    """Verify numeric slot model selection works."""
    print("\n" + "=" * 60)
    print("TEST: Model Aliases")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        # Slot 0 = DISCHARGE mode (always available)
        batt.model('0')
        _record("model('0') numeric slot", True)

        batt.model()
        _record("model() after slot 0", True)
    except Exception as e:
        _record("model aliases", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 18. Enable/Measure
# ---------------------------------------------------------------------------
def test_enable_measure():
    """Verify enable(), terminal_voltage(), and current() measurements."""
    print("\n" + "=" * 60)
    print("TEST: Enable/Measure")
    print("=" * 60)

    ok = True
    batt = None

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')
        batt.soc(80)
        batt.voc(12.6)

        batt.enable()
        time.sleep(1)
        _record("enable()", True)

        v = batt.terminal_voltage()
        passed = isinstance(v, (int, float))
        _record("terminal_voltage()", passed, f"{v:.3f} V")
        if not passed:
            ok = False

        i = batt.current()
        passed = isinstance(i, (int, float))
        _record("current()", passed, f"{i:.3f} A")
        if not passed:
            ok = False

        batt.disable()
    except Exception as e:
        _record("enable/measure", False, str(e))
        ok = False
        if batt:
            try:
                batt.disable()
            except Exception:
                pass

    return ok


# ---------------------------------------------------------------------------
# 19. Discharge Simulation
# ---------------------------------------------------------------------------
def test_discharge_simulation():
    """Sweep SOC from 100 to 10 and measure terminal voltage at each step."""
    print("\n" + "=" * 60)
    print("TEST: Discharge Simulation")
    print("=" * 60)

    ok = True
    batt = None

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')
        batt.voc(12.6)
        batt.voltage_full(13.0)
        batt.voltage_empty(10.0)

        batt.enable()
        time.sleep(0.5)

        voltages = []
        for soc_level in [100, 75, 50, 25, 10]:
            batt.soc(soc_level)
            time.sleep(0.5)
            v = batt.terminal_voltage()
            voltages.append((soc_level, v))
            passed = isinstance(v, (int, float))
            _record(f"SOC {soc_level:3d}% terminal_voltage", passed,
                    f"{v:.3f} V")
            if not passed:
                ok = False

        # Verify monotonic decrease (higher SOC should give higher or equal voltage)
        if len(voltages) >= 2:
            monotonic = all(
                voltages[i][1] >= voltages[i + 1][1] - 0.5  # allow 0.5V tolerance
                for i in range(len(voltages) - 1)
            )
            _record("voltage roughly monotonic with SOC", monotonic,
                    " | ".join(f"{s}%={v:.2f}V" for s, v in voltages))
            if not monotonic:
                ok = False

        batt.disable()
    except Exception as e:
        _record("discharge simulation", False, str(e))
        ok = False
        if batt:
            try:
                batt.disable()
            except Exception:
                pass

    return ok


# ---------------------------------------------------------------------------
# 20. State Persistence
# ---------------------------------------------------------------------------
def test_state_persistence():
    """Verify disable -> enable preserves a reasonable terminal voltage."""
    print("\n" + "=" * 60)
    print("TEST: State Persistence")
    print("=" * 60)

    ok = True
    batt = None

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')
        batt.soc(50)
        batt.voc(12.0)

        batt.enable()
        time.sleep(1)
        v_before = batt.terminal_voltage()
        _record("voltage before disable", True, f"{v_before:.3f} V")

        batt.disable()
        time.sleep(0.5)

        batt.enable()
        time.sleep(1)
        v_after = batt.terminal_voltage()
        _record("voltage after re-enable", True, f"{v_after:.3f} V")

        # Voltages should be in the same ballpark (within 2V tolerance)
        diff = abs(v_before - v_after)
        passed = diff < 2.0
        _record("voltage stable across disable/enable", passed,
                f"delta={diff:.3f} V")
        if not passed:
            ok = False

        batt.disable()
    except Exception as e:
        _record("state persistence", False, str(e))
        ok = False
        if batt:
            try:
                batt.disable()
            except Exception:
                pass

    return ok


# ---------------------------------------------------------------------------
# 21. Clear Faults
# ---------------------------------------------------------------------------
def test_clear_faults():
    """Verify clear_ovp() and clear_ocp() execute without error."""
    print("\n" + "=" * 60)
    print("TEST: Clear Faults")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()

        batt.clear_ovp()
        _record("clear_ovp()", True)

        batt.clear_ocp()
        _record("clear_ocp()", True)
    except Exception as e:
        _record("clear faults", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 22. State Query
# ---------------------------------------------------------------------------
def test_state_query():
    """Verify print_state() executes and prints battery state."""
    print("\n" + "=" * 60)
    print("TEST: State Query")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()

        batt.print_state()
        _record("print_state()", True)
    except Exception as e:
        _record("print_state()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 23. Disable Safety
# ---------------------------------------------------------------------------
def test_disable_safety():
    """Verify disable() works cleanly as a safety teardown."""
    print("\n" + "=" * 60)
    print("TEST: Disable Safety")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()

        # Enable then disable to confirm clean shutdown
        batt.enable()
        time.sleep(0.5)
        batt.disable()
        _record("disable() after enable", True)

        # Double-disable should not raise
        batt.disable()
        _record("disable() idempotent", True)
    except Exception as e:
        _record("disable safety", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 24. Negative SOC
# ---------------------------------------------------------------------------
def test_negative_soc():
    """Verify soc(-50) raises an exception."""
    print("\n" + "=" * 60)
    print("TEST: Negative SOC")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        try:
            batt.soc(-50)
            _record("soc(-50) raises exception", False,
                    "no exception raised for negative SOC")
            ok = False
        except Exception as e:
            _record("soc(-50) raises exception", True,
                    f"{type(e).__name__}: {e}")
    except Exception as e:
        _record("negative soc test setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 25. SOC Over 100
# ---------------------------------------------------------------------------
def test_soc_over_100():
    """Verify soc(150) raises an exception."""
    print("\n" + "=" * 60)
    print("TEST: SOC Over 100")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        try:
            batt.soc(150)
            _record("soc(150) raises exception", False,
                    "no exception raised for SOC > 100")
            ok = False
        except Exception as e:
            _record("soc(150) raises exception", True,
                    f"{type(e).__name__}: {e}")
    except Exception as e:
        _record("soc over 100 test setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 26. Negative VOC
# ---------------------------------------------------------------------------
def test_negative_voc():
    """Verify voc(-1.0) raises an exception."""
    print("\n" + "=" * 60)
    print("TEST: Negative VOC")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        try:
            batt.voc(-1.0)
            _record("voc(-1.0) raises exception", False,
                    "no exception raised for negative VOC")
            ok = False
        except Exception as e:
            _record("voc(-1.0) raises exception", True,
                    f"{type(e).__name__}: {e}")
    except Exception as e:
        _record("negative voc test setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 27. Negative Capacity
# ---------------------------------------------------------------------------
def test_negative_capacity():
    """Verify capacity(-5.0) raises an exception."""
    print("\n" + "=" * 60)
    print("TEST: Negative Capacity")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        try:
            batt.capacity(-5.0)
            _record("capacity(-5.0) raises exception", False,
                    "no exception raised for negative capacity")
            ok = False
        except Exception as e:
            _record("capacity(-5.0) raises exception", True,
                    f"{type(e).__name__}: {e}")
    except Exception as e:
        _record("negative capacity test setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 28. Invalid Mode
# ---------------------------------------------------------------------------
def test_invalid_mode():
    """Verify mode('invalid_mode') raises an exception."""
    print("\n" + "=" * 60)
    print("TEST: Invalid Mode")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()

        try:
            batt.mode('invalid_mode')
            _record("mode('invalid_mode') raises exception", False,
                    "no exception raised for invalid mode")
            ok = False
        except Exception as e:
            _record("mode('invalid_mode') raises exception", True,
                    f"{type(e).__name__}: {e}")
    except Exception as e:
        _record("invalid mode test setup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 29. Combined Clear
# ---------------------------------------------------------------------------
def test_combined_clear():
    """Verify clear() (combined OVP+OCP) runs without error."""
    print("\n" + "=" * 60)
    print("TEST: Combined Clear")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()

        batt.clear()
        _record("clear() combined OVP+OCP", True)
    except Exception as e:
        _record("clear()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 30. SOC Readback Value
# ---------------------------------------------------------------------------
def test_soc_readback_value():
    """Set soc(75), read back, verify return value is numeric and close to 75."""
    print("\n" + "=" * 60)
    print("TEST: SOC Readback Value")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        batt = Net.get(BATTERY_NET, type=NetType.Battery)
        batt.set_mode_battery()
        batt.mode('static')

        batt.soc(75)
        _record("soc(75) set", True)

        readback = batt.soc()
        passed = isinstance(readback, (int, float))
        _record("soc() returns numeric", passed, f"type={type(readback).__name__}, value={readback}")
        if not passed:
            ok = False
        else:
            close = abs(readback - 75) < 10  # within 10% tolerance
            _record("soc() readback close to 75", close,
                    f"readback={readback}, delta={abs(readback - 75)}")
            if not close:
                ok = False
    except Exception as e:
        _record("soc readback value", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    """Run all tests."""
    print("Battery Simulator Comprehensive Test Suite")
    print(f"Testing net: {BATTERY_NET}")
    print(f"Set BATTERY_NET env var to change")
    print("=" * 60)

    tests = [
        ("Imports",                 test_imports),
        ("Net.get",                 test_net_get),
        ("String Repr",             test_string_repr),
        ("Initialize Mode",         test_initialize_mode),
        ("Mode Static",             test_mode_static),
        ("Mode Dynamic",            test_mode_dynamic),
        ("SOC Set/Read",            test_soc_set_read),
        ("SOC Edge Cases",          test_soc_edge_cases),
        ("VOC Set",                 test_voc_set),
        ("Voltage Limits",          test_voltage_limits),
        ("Capacity",                test_capacity),
        ("ESR Set/Read",            test_esr),
        ("Current Limit",           test_current_limit),
        ("OVP Set/Read",            test_ovp),
        ("OCP Set/Read",            test_ocp),
        ("Model Set/Read",          test_model),
        ("Model Aliases",           test_model_aliases),
        ("Enable/Measure",          test_enable_measure),
        ("Discharge Simulation",    test_discharge_simulation),
        ("State Persistence",       test_state_persistence),
        ("Clear Faults",            test_clear_faults),
        ("State Query",             test_state_query),
        ("Disable Safety",          test_disable_safety),
        ("Negative SOC",            test_negative_soc),
        ("SOC Over 100",            test_soc_over_100),
        ("Negative VOC",            test_negative_voc),
        ("Negative Capacity",       test_negative_capacity),
        ("Invalid Mode",            test_invalid_mode),
        ("Combined Clear",          test_combined_clear),
        ("SOC Readback Value",      test_soc_readback_value),
    ]

    test_results = []
    batt = None
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
            batt = Net.get(BATTERY_NET, type=NetType.Battery)
            batt.disable()
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
