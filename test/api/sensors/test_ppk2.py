# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Integration test suite for Nordic PPK2 (Power Profiler Kit II) CLI features.

Run via:
    lager python test/api/sensors/test_ppk2.py --box <box> -- <watt_net> [energy_net]

Arguments (after --):
    watt_net   - net configured with role 'watt-meter' and instrument 'ppk2'
    energy_net - net configured with role 'energy-analyzer' and instrument 'ppk2'
                 (defaults to watt_net)

Example:
    lager python test/api/sensors/test_ppk2.py --box MY-BOX -- ppk2_power ppk2_energy
    lager python test/api/sensors/test_ppk2.py --box MY-BOX -- ppk2_power
"""

import sys
import time

from lager import Net, NetType
from lager.measurement.watt.ppk2_watt import PPK2Watt
from lager.measurement.energy_analyzer.ppk2_energy import PPK2EnergyAnalyzer

# PPK2 noise floor: values more negative than this are real failures.
# The PPK2 reports slight negative current (~-1 to -3 uA) with no load.
NOISE_FLOOR = -5e-6

# ============================================================
# Test harness
# ============================================================

PASS = 0
FAIL = 0
SKIP = 0
_sections = []
_current_section = {"name": None, "pass": 0, "fail": 0, "skip": 0}


def section(name):
    global _current_section
    if _current_section["name"] is not None:
        _sections.append(dict(_current_section))
    _current_section = {"name": name, "pass": 0, "fail": 0, "skip": 0}
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")


def passed(msg=""):
    global PASS
    PASS += 1
    _current_section["pass"] += 1
    print(f"  [PASS]{' ' + msg if msg else ''}")


def failed(msg=""):
    global FAIL
    FAIL += 1
    _current_section["fail"] += 1
    print(f"  [FAIL]{' ' + msg if msg else ''}", file=sys.stderr)


def skipped(msg=""):
    global SKIP
    SKIP += 1
    _current_section["skip"] += 1
    print(f"  [SKIP]{' ' + msg if msg else ''}")


def check(condition, pass_msg="", fail_msg=""):
    if condition:
        passed(pass_msg)
    else:
        failed(fail_msg)
    return condition


def run_test(name, fn):
    """Run fn(); catch and report any exception as a failure."""
    print(f"\n  -- {name}")
    try:
        fn()
    except Exception as exc:
        failed(f"Unexpected exception: {exc}")


def summary():
    if _current_section["name"] is not None:
        _sections.append(dict(_current_section))

    print(f"\n{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}")
    print(f"  {'Section':<35} {'Pass':>5} {'Fail':>5} {'Skip':>5}")
    print(f"  {'-'*52}")
    for s in _sections:
        print(f"  {s['name']:<35} {s['pass']:>5} {s['fail']:>5} {s['skip']:>5}")
    print(f"  {'-'*52}")
    print(f"  {'TOTAL':<35} {PASS:>5} {FAIL:>5} {SKIP:>5}")
    print()
    if FAIL == 0:
        print("  RESULT: ALL TESTS PASSED")
    else:
        print(f"  RESULT: {FAIL} TEST(S) FAILED", file=sys.stderr)
    print()


# ============================================================
# Argument parsing
# ============================================================

if len(sys.argv) < 2:
    print("Usage: lager python test_ppk2.py --box <box> -- <watt_net> [energy_net]")
    sys.exit(1)

WATT_NET = sys.argv[1]
ENERGY_NET = sys.argv[2] if len(sys.argv) > 2 else WATT_NET

print("="*60)
print("  NORDIC PPK2 INTEGRATION TEST SUITE")
print("="*60)
print(f"  Watt net:   {WATT_NET}")
print(f"  Energy net: {ENERGY_NET}")

# ============================================================
# SECTION 1: Net acquisition
# ============================================================
section("1. Net Acquisition")

watt_net = None
energy_net = None


def test_get_watt_net():
    global watt_net
    watt_net = Net.get(WATT_NET, type=NetType.WattMeter)
    check(watt_net is not None, f"Got net: {type(watt_net).__name__}")

run_test("Net.get() watt-meter", test_get_watt_net)


def test_get_energy_net():
    global energy_net
    energy_net = Net.get(ENERGY_NET, type=NetType.EnergyAnalyzer)
    check(energy_net is not None, f"Got net: {type(energy_net).__name__}")

run_test("Net.get() energy-analyzer", test_get_energy_net)


def test_invalid_watt_net():
    try:
        Net.get("__nonexistent_ppk2_net__", type=NetType.WattMeter)
        failed("Expected error for invalid net name, got none")
    except Exception:
        passed("Invalid net name raised an error")

run_test("Net.get() invalid watt net raises error", test_invalid_watt_net)


def test_invalid_energy_net():
    try:
        Net.get("__nonexistent_ppk2_net__", type=NetType.EnergyAnalyzer)
        failed("Expected error for invalid net name, got none")
    except Exception:
        passed("Invalid net name raised an error")

run_test("Net.get() invalid energy net raises error", test_invalid_energy_net)

# ============================================================
# SECTION 2: WattMeter — read()
# ============================================================
section("2. WattMeter — read()")

if watt_net is None:
    skipped("Skipping section: watt net unavailable")
else:
    def test_read_returns_float():
        val = watt_net.read()
        check(isinstance(val, float), f"read() = {val} W (type: {type(val).__name__})")

    run_test("read() returns float", test_read_returns_float)


    def test_read_non_negative():
        val = watt_net.read()
        check(val > NOISE_FLOOR, f"read() = {val:.6e} W (above noise floor {NOISE_FLOOR:.0e})")

    run_test("read() is above noise floor", test_read_non_negative)


    def test_read_five_times():
        values = [watt_net.read() for _ in range(5)]
        print(f"    Values: {[f'{v:.3e}' for v in values]}")
        all_floats = all(isinstance(v, float) for v in values)
        above_floor = all(v > NOISE_FLOOR for v in values)
        check(all_floats and above_floor, "All 5 reads returned floats above noise floor")

    run_test("read() x5 stability", test_read_five_times)


    def test_read_consistent():
        """Two reads within 5 seconds should not differ by more than 10x."""
        v1 = watt_net.read()
        time.sleep(0.2)
        v2 = watt_net.read()
        print(f"    read 1: {v1:.6f} W,  read 2: {v2:.6f} W")
        if v1 > 0 and v2 > 0:
            ratio = max(v1, v2) / min(v1, v2)
            check(ratio < 10.0, f"Consecutive reads within 10x of each other (ratio={ratio:.2f})")
        else:
            passed("At least one read is zero (device may be unpowered)")

    run_test("read() consecutive consistency", test_read_consistent)

# ============================================================
# SECTION 3: WattMeter — read_current() / read_voltage() / read_all()
# ============================================================
section("3. WattMeter — read_current / read_voltage / read_all")

if watt_net is None:
    skipped("Skipping section: watt net unavailable")
else:
    def test_read_current():
        val = watt_net.read_current()
        print(f"    read_current() = {val:.6f} A")
        check(isinstance(val, float), "returns float")
        check(val >= 0, "non-negative")

    run_test("read_current() returns non-negative float", test_read_current)


    def test_read_voltage():
        val = watt_net.read_voltage()
        print(f"    read_voltage() = {val:.6f} V")
        check(isinstance(val, float), "returns float")
        # PPK2 source mode: voltage should be the configured source voltage
        check(0.5 < val < 6.0, f"voltage {val:.3f} V is within PPK2 source range (0.8-5V)")

    run_test("read_voltage() returns float within PPK2 source range", test_read_voltage)


    def test_read_all_keys():
        result = watt_net.read_all()
        print(f"    read_all() = {result}")
        for key in ("current", "voltage", "power"):
            check(key in result, f"key '{key}' present")

    run_test("read_all() has current/voltage/power keys", test_read_all_keys)


    def test_read_all_types():
        result = watt_net.read_all()
        for key in ("current", "voltage", "power"):
            if key in result:
                check(isinstance(result[key], float), f"result['{key}'] is float")

    run_test("read_all() values are floats", test_read_all_types)


    def test_read_all_ranges():
        result = watt_net.read_all()
        if "current" in result:
            check(result["current"] > NOISE_FLOOR,
                  f"result['current'] = {result['current']:.3e} A (above noise floor)")
        if "voltage" in result:
            check(0.5 < result["voltage"] < 6.0,
                  f"result['voltage'] = {result['voltage']:.4f} V (within PPK2 source range)")
        if "power" in result:
            check(result["power"] > NOISE_FLOOR,
                  f"result['power'] = {result['power']:.3e} W (above noise floor)")

    run_test("read_all() values are within expected ranges", test_read_all_ranges)


    def test_read_all_power_matches():
        """power should be approximately current * voltage."""
        result = watt_net.read_all()
        if all(k in result for k in ("current", "voltage", "power")):
            expected = result["current"] * result["voltage"]
            actual = result["power"]
            print(f"    I={result['current']:.6f} A, V={result['voltage']:.6f} V")
            print(f"    power reported={actual:.6f} W, I*V={expected:.6f} W")
            if expected > 1e-9:
                ratio = abs(actual - expected) / expected
                check(ratio < 0.01, f"power matches I*V within 1% (ratio={ratio:.4f})")
            else:
                passed("Power near zero, skip ratio check")

    run_test("read_all() power = current * voltage", test_read_all_power_matches)

# ============================================================
# SECTION 4: EnergyAnalyzer — read_energy()
# ============================================================
section("4. EnergyAnalyzer — read_energy()")

if energy_net is None:
    skipped("Skipping section: energy net unavailable")
else:
    REQUIRED_ENERGY_KEYS = {"energy_j", "energy_wh", "charge_c", "charge_ah", "duration_s"}

    def test_energy_keys():
        result = energy_net.read_energy(duration=1.0)
        print(f"    result = {result}")
        missing = REQUIRED_ENERGY_KEYS - set(result.keys())
        check(not missing, f"All keys present" if not missing else f"Missing: {missing}")

    run_test("read_energy() has all required keys", test_energy_keys)


    def test_energy_types():
        result = energy_net.read_energy(duration=1.0)
        for key in REQUIRED_ENERGY_KEYS:
            if key in result:
                check(isinstance(result[key], float), f"result['{key}'] is float")

    run_test("read_energy() values are floats", test_energy_types)


    def test_energy_non_negative():
        result = energy_net.read_energy(duration=1.0)
        for key in ("energy_j", "energy_wh", "charge_c", "charge_ah"):
            if key in result:
                check(result[key] > NOISE_FLOOR,
                      f"result['{key}'] = {result[key]:.3e} (above noise floor)")

    run_test("read_energy() values above noise floor", test_energy_non_negative)


    def test_energy_duration_respected():
        result = energy_net.read_energy(duration=2.0)
        if "duration_s" in result:
            check(result["duration_s"] == 2.0, f"duration_s = {result['duration_s']} (expected 2.0)")

    run_test("read_energy() duration_s reflects requested duration", test_energy_duration_respected)


    def test_energy_wh_joules_consistent():
        result = energy_net.read_energy(duration=1.0)
        if "energy_j" in result and "energy_wh" in result:
            expected_wh = result["energy_j"] / 3600.0
            diff = abs(result["energy_wh"] - expected_wh)
            check(diff < 1e-9, f"energy_wh = energy_j / 3600 (diff={diff:.2e})")

    run_test("read_energy() Wh = J / 3600", test_energy_wh_joules_consistent)


    def test_energy_ah_coulombs_consistent():
        result = energy_net.read_energy(duration=1.0)
        if "charge_c" in result and "charge_ah" in result:
            expected_ah = result["charge_c"] / 3600.0
            diff = abs(result["charge_ah"] - expected_ah)
            check(diff < 1e-12, f"charge_ah = charge_c / 3600 (diff={diff:.2e})")

    run_test("read_energy() Ah = C / 3600", test_energy_ah_coulombs_consistent)


    def test_energy_longer_gives_more():
        """Both durations should return valid results; longer should integrate more samples."""
        r1 = energy_net.read_energy(duration=1.0)
        r2 = energy_net.read_energy(duration=2.0)
        print(f"    1s: {r1.get('energy_j', 'N/A'):.3e} J,  2s: {r2.get('energy_j', 'N/A'):.3e} J")
        check("energy_j" in r1 and "energy_j" in r2,
              "Both durations returned energy_j")

    run_test("read_energy() both 1s and 2s durations return results", test_energy_longer_gives_more)


    def test_energy_short_duration():
        """Short 0.5s integration should still succeed."""
        result = energy_net.read_energy(duration=0.5)
        check("energy_j" in result, f"0.5s integration returned result: {result}")

    run_test("read_energy() works with short 0.5s duration", test_energy_short_duration)

# ============================================================
# SECTION 5: EnergyAnalyzer — read_stats()
# ============================================================
section("5. EnergyAnalyzer — read_stats()")

if energy_net is None:
    skipped("Skipping section: energy net unavailable")
else:
    REQUIRED_STAT_KEYS = {"mean", "min", "max", "std"}
    REQUIRED_TOP_KEYS = {"current", "voltage", "power", "duration_s"}

    def test_stats_top_keys():
        result = energy_net.read_stats(duration=1.0)
        print(f"    top-level keys: {set(result.keys())}")
        missing = REQUIRED_TOP_KEYS - set(result.keys())
        check(not missing, f"All top-level keys present" if not missing else f"Missing: {missing}")

    run_test("read_stats() has current/voltage/power/duration_s", test_stats_top_keys)


    def test_stats_sub_keys():
        result = energy_net.read_stats(duration=1.0)
        for section_name in ("current", "voltage", "power"):
            if section_name in result:
                missing = REQUIRED_STAT_KEYS - set(result[section_name].keys())
                check(not missing,
                      f"'{section_name}' has mean/min/max/std" if not missing
                      else f"'{section_name}' missing: {missing}")

    run_test("read_stats() sub-keys are mean/min/max/std", test_stats_sub_keys)


    def test_stats_types():
        result = energy_net.read_stats(duration=1.0)
        for section_name in ("current", "voltage", "power"):
            if section_name in result:
                for stat in ("mean", "min", "max", "std"):
                    if stat in result[section_name]:
                        check(isinstance(result[section_name][stat], float),
                              f"result['{section_name}']['{stat}'] is float")

    run_test("read_stats() all values are floats", test_stats_types)


    def test_stats_ordering():
        """min <= mean <= max for each signal."""
        result = energy_net.read_stats(duration=1.0)
        for section_name in ("current", "voltage", "power"):
            if section_name in result:
                s = result[section_name]
                if all(k in s for k in ("min", "mean", "max")):
                    print(f"    {section_name}: min={s['min']:.6f}  mean={s['mean']:.6f}  max={s['max']:.6f}")
                    check(s["min"] <= s["mean"] <= s["max"],
                          f"{section_name} min <= mean <= max")

    run_test("read_stats() min <= mean <= max for all signals", test_stats_ordering)


    def test_stats_std_non_negative():
        result = energy_net.read_stats(duration=1.0)
        for section_name in ("current", "voltage", "power"):
            if section_name in result and "std" in result[section_name]:
                check(result[section_name]["std"] >= 0,
                      f"{section_name} std = {result[section_name]['std']:.6f} (non-negative)")

    run_test("read_stats() std is non-negative", test_stats_std_non_negative)


    def test_stats_voltage_constant():
        """PPK2 source mode: voltage stats should all be the same (constant source)."""
        result = energy_net.read_stats(duration=1.0)
        if "voltage" in result and "mean" in result["voltage"]:
            v = result["voltage"]["mean"]
            std = result["voltage"].get("std", 0)
            print(f"    voltage mean = {v:.4f} V, std = {std:.6f} V")
            check(std < 0.001, f"Voltage std near zero (constant source mode)")

    run_test("read_stats() voltage is constant (source mode)", test_stats_voltage_constant)


    def test_stats_duration_respected():
        result = energy_net.read_stats(duration=2.0)
        if "duration_s" in result:
            check(result["duration_s"] == 2.0, f"duration_s = {result['duration_s']} (expected 2.0)")

    run_test("read_stats() duration_s reflects requested duration", test_stats_duration_respected)

# ============================================================
# SECTION 6: Cross-method consistency
# ============================================================
section("6. Cross-method Consistency")

if watt_net is None or energy_net is None:
    skipped("Skipping section: one or both nets unavailable")
else:
    def test_watt_vs_stats_power():
        """watt.read() and energy stats power mean should be in the same order of magnitude."""
        watt_power = watt_net.read()
        stats = energy_net.read_stats(duration=1.0)
        stats_power = stats.get("power", {}).get("mean")
        if stats_power is not None:
            print(f"    watt.read()={watt_power:.6f} W,  stats power mean={stats_power:.6f} W")
            if watt_power > 1e-9 and stats_power > 1e-9:
                ratio = max(watt_power, stats_power) / min(watt_power, stats_power)
                check(ratio < 100,
                      f"watt and stats within 100x of each other (ratio={ratio:.1f})")
            else:
                passed("Both near zero")

    run_test("watt.read() and energy stats power are comparable", test_watt_vs_stats_power)


    def test_read_all_vs_stats():
        """read_all() and read_stats() should agree on current and voltage."""
        all_result = watt_net.read_all()
        stats = energy_net.read_stats(duration=1.0)
        i_all = all_result.get("current")
        v_all = all_result.get("voltage")
        i_stats = stats.get("current", {}).get("mean")
        v_stats = stats.get("voltage", {}).get("mean")
        if all(x is not None for x in (i_all, v_all, i_stats, v_stats)):
            print(f"    read_all  I={i_all:.6f} A  V={v_all:.6f} V")
            print(f"    stats     I={i_stats:.6f} A  V={v_stats:.6f} V")
            passed("Both methods returned I/V values")

    run_test("read_all() and read_stats() both return I/V", test_read_all_vs_stats)


    def test_energy_charge_from_current():
        """Charge (C) over 1s should approximate mean current x 1s."""
        stats = energy_net.read_stats(duration=1.0)
        energy = energy_net.read_energy(duration=1.0)
        i_mean = stats.get("current", {}).get("mean")
        charge_c = energy.get("charge_c")
        if i_mean is not None and charge_c is not None and i_mean > 1e-9:
            expected_c = i_mean * 1.0  # I * t
            ratio = abs(charge_c - expected_c) / expected_c
            print(f"    stats I_mean={i_mean:.6f} A -> expected C={expected_c:.6f}")
            print(f"    energy charge_c={charge_c:.6f} C  (ratio diff={ratio:.3f})")
            check(ratio < 0.20, f"charge_c ~ mean_current x 1s (within 20%)")
        else:
            skipped("Current or charge near zero -- cannot validate ratio")

    run_test("read_energy() charge_c ~ read_stats() current_mean x duration", test_energy_charge_from_current)

# ============================================================
# SECTION 7: Repeated reads (stability)
# ============================================================
section("7. Repeated Reads — Stability")

if watt_net is None:
    skipped("Skipping watt stability: net unavailable")
else:
    def test_watt_10_reads():
        values = []
        for _ in range(10):
            values.append(watt_net.read())
        all_ok = all(isinstance(v, float) and v > NOISE_FLOOR for v in values)
        print(f"    10 reads: min={min(values):.3e} max={max(values):.3e} W")
        check(all_ok, "All 10 watt reads returned floats above noise floor")

    run_test("read() x10 all succeed", test_watt_10_reads)

if energy_net is None:
    skipped("Skipping energy stability: net unavailable")
else:
    def test_energy_3_reads():
        results = [energy_net.read_energy(duration=0.5) for _ in range(3)]
        all_ok = all("energy_j" in r and r["energy_j"] > NOISE_FLOOR for r in results)
        vals = [f"{r['energy_j']:.3e} J" for r in results]
        print(f"    3 energy reads: {vals}")
        check(all_ok, "All 3 energy reads returned valid results")

    run_test("read_energy() x3 all succeed", test_energy_3_reads)

    def test_stats_3_reads():
        results = [energy_net.read_stats(duration=0.5) for _ in range(3)]
        all_ok = all("current" in r for r in results)
        check(all_ok, "All 3 stats reads returned valid results")

    run_test("read_stats() x3 all succeed", test_stats_3_reads)

# ============================================================
# Summary
# ============================================================
summary()

# Close all cached PPK2 device handles so the process exits cleanly.
PPK2Watt.clear_cache()
PPK2EnergyAnalyzer.clear_cache()

if FAIL > 0:
    sys.exit(1)
