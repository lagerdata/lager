#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive LabJack T7 test suite covering ADC, DAC, and GPIO.
Complements the existing focused tests — run this for a full-device integration check.

Run with:
    lager python test/api/io/test_LabJack_T7.py --box <YOUR-BOX>

Override defaults:
    ADC_NET=adc2 DAC_NET=dac2 GPIO_NET=gpio4 lager python ...
    LOOPBACK_ADC_NET="" lager python ...   # disable loopback group

Bench wiring (STG-1):
    dac1 output    → adc1 input   (loopback wire; enables Group 10 by default)
    supply1 output → adc2 input   (Group 2 multi-channel sweep reads real supply voltage)

Prerequisites:
    - ADC net configured on the box (default 'adc1', wired to dac1 on STG-1)
    - DAC net configured on the box (default 'dac1', wired to adc1 on STG-1)
    - GPIO net configured on the box, output-capable (default 'gpio16')
    - Loopback (Group 10) runs by default when LOOPBACK_ADC_NET is set; set it to ""
      to skip on benches where dac1 and adc1 are not physically connected
"""
import sys
import os
import time
import math
import traceback

from lager import Net, NetType

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ADC_NET          = os.environ.get("ADC_NET",  "adc1")
DAC_NET          = os.environ.get("DAC_NET",  "dac1")
GPIO_NET         = os.environ.get("GPIO_NET", "gpio16")
ADC_NETS_CSV     = os.environ.get("ADC_NETS", "adc1,adc2,adc3")
LOOPBACK_ADC_NET = os.environ.get("LOOPBACK_ADC_NET", "adc1")

ADC_VALID_MIN   = -10.4   # V  (LabJack T7 ±10V AIN range + 4% headroom)
ADC_VALID_MAX   = 10.4    # V
ADC_STDEV_MAX   = 1.0     # V  — max acceptable std deviation across stability samples
DAC_TOL_MV      = 50      # mV — set vs. get_voltage() readback tolerance
LOOPBACK_TOL_MV = 100     # mV — DAC output vs. ADC loopback read tolerance
DAC_MAX_V       = 5.0     # V  — LabJack T7 DAC hardware ceiling

_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def _close_enough_mv(actual, expected, tol_mv):
    return abs(actual - expected) * 1000 <= tol_mv


# ---------------------------------------------------------------------------
# 1. ADC Single-Channel Read
# ---------------------------------------------------------------------------
def test_adc_single_channel():
    """input() returns a numeric value in the valid LabJack T7 ADC range."""
    print("\n" + "=" * 60)
    print("TEST: ADC Single-Channel Read")
    print("=" * 60)

    ok = True

    try:
        adc = Net.get(ADC_NET, type=NetType.ADC)
        voltage = adc.input()

        is_numeric = isinstance(voltage, (int, float))
        _record("input() returns numeric", is_numeric,
                f"type={type(voltage).__name__}, value={voltage}")
        if not is_numeric:
            return False

        in_range = ADC_VALID_MIN <= float(voltage) <= ADC_VALID_MAX
        _record(f"value in [{ADC_VALID_MIN}, {ADC_VALID_MAX}] V", in_range,
                f"{voltage:.4f} V")
        if not in_range:
            ok = False

    except Exception as e:
        _record("ADC single-channel read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 2. ADC Multi-Channel Sweep
# ---------------------------------------------------------------------------
def test_adc_multi_channel():
    """All configured ADC nets return numeric values in the valid range."""
    print("\n" + "=" * 60)
    print("TEST: ADC Multi-Channel Sweep")
    print("=" * 60)

    adc_nets = [n.strip() for n in ADC_NETS_CSV.split(",") if n.strip()]
    print(f"  Channels: {adc_nets}")

    ok = True

    for net_name in adc_nets:
        try:
            adc = Net.get(net_name, type=NetType.ADC)
            voltage = adc.input()

            is_numeric = isinstance(voltage, (int, float))
            _record(f"{net_name} returns numeric", is_numeric,
                    f"type={type(voltage).__name__}, value={voltage}")
            if not is_numeric:
                ok = False
                continue

            in_range = ADC_VALID_MIN <= float(voltage) <= ADC_VALID_MAX
            _record(f"{net_name} in [{ADC_VALID_MIN}, {ADC_VALID_MAX}] V", in_range,
                    f"{voltage:.4f} V")
            if not in_range:
                ok = False

        except Exception as e:
            _record(f"{net_name} read", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 3. ADC Measurement Stability
# ---------------------------------------------------------------------------
def test_adc_stability():
    """10 repeated reads on the primary ADC net show std deviation below threshold."""
    print("\n" + "=" * 60)
    print("TEST: ADC Measurement Stability")
    print("=" * 60)

    NUM_SAMPLES = 10
    INTERVAL    = 0.4   # seconds between samples

    ok = True

    try:
        adc = Net.get(ADC_NET, type=NetType.ADC)
        samples = []

        print(f"\n  Collecting {NUM_SAMPLES} samples at {INTERVAL}s intervals...")
        for i in range(NUM_SAMPLES):
            v = adc.input()
            samples.append(v)
            print(f"    Sample {i+1:2d}: {v:.4f} V")
            if i < NUM_SAMPLES - 1:
                time.sleep(INTERVAL)

        all_numeric = all(isinstance(s, (int, float)) for s in samples)
        _record("all samples numeric",
                all_numeric,
                f"{sum(1 for s in samples if isinstance(s, (int, float)))}/{len(samples)}")
        if not all_numeric:
            return False

        all_in_range = all(ADC_VALID_MIN <= s <= ADC_VALID_MAX for s in samples)
        _record("all samples in valid range", all_in_range,
                f"min={min(samples):.4f} V, max={max(samples):.4f} V")
        if not all_in_range:
            ok = False

        mean     = sum(samples) / len(samples)
        variance = sum((s - mean) ** 2 for s in samples) / (len(samples) - 1)
        stdev    = math.sqrt(variance)
        stable   = stdev < ADC_STDEV_MAX
        _record(f"std dev < {ADC_STDEV_MAX} V", stable,
                f"stdev={stdev:.4f} V, mean={mean:.4f} V")
        if not stable:
            ok = False

        spread = max(samples) - min(samples)
        _record("min/max spread", True, f"{spread:.4f} V")   # informational only

    except Exception as e:
        _record("ADC stability", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 4. DAC Output and Readback
# ---------------------------------------------------------------------------
def test_dac_output_readback():
    """output() at five voltages; get_voltage() readback within 50 mV."""
    print("\n" + "=" * 60)
    print("TEST: DAC Output and Readback")
    print("=" * 60)

    TEST_VOLTAGES = [0.5, 1.0, 2.0, 3.3, 4.5]
    ok  = True
    dac = None

    try:
        dac = Net.get(DAC_NET, type=NetType.DAC)

        for target_v in TEST_VOLTAGES:
            dac.output(target_v)
            time.sleep(0.1)
            readback = dac.get_voltage()

            is_numeric = isinstance(readback, (int, float))
            _record(f"readback at {target_v:.1f}V is numeric", is_numeric,
                    f"type={type(readback).__name__}, value={readback}")
            if not is_numeric:
                ok = False
                continue

            within_tol = _close_enough_mv(float(readback), target_v, DAC_TOL_MV)
            error_mv   = abs(target_v - float(readback)) * 1000
            _record(f"set {target_v:.1f}V → readback {readback:.3f}V", within_tol,
                    f"error={error_mv:.1f} mV (limit {DAC_TOL_MV} mV)")
            if not within_tol:
                ok = False

    except Exception as e:
        _record("DAC output readback", False, str(e))
        ok = False
    finally:
        try:
            if dac is None:
                dac = Net.get(DAC_NET, type=NetType.DAC)
            dac.output(0.0)
            print("\n  Safety: DAC reset to 0V")
        except Exception as e:
            print(f"\n  WARNING: Failed to reset DAC to 0V -- {e}")

    return ok


# ---------------------------------------------------------------------------
# 5. DAC Voltage Ramp
# ---------------------------------------------------------------------------
def test_dac_ramp():
    """Sweep 0 → 4.5 V in 10 steps; verify each step and monotonic ordering."""
    print("\n" + "=" * 60)
    print("TEST: DAC Voltage Ramp")
    print("=" * 60)

    NUM_STEPS = 10
    steps     = [round(i * 4.5 / (NUM_STEPS - 1), 4) for i in range(NUM_STEPS)]
    ok        = True
    dac       = None
    readbacks = []

    try:
        dac = Net.get(DAC_NET, type=NetType.DAC)

        for target_v in steps:
            dac.output(target_v)
            time.sleep(0.05)
            rb = dac.get_voltage()
            readbacks.append(float(rb))

            within_tol = _close_enough_mv(float(rb), target_v, DAC_TOL_MV)
            error_mv   = abs(target_v - float(rb)) * 1000
            _record(f"step {target_v:.3f}V → {rb:.3f}V", within_tol,
                    f"error={error_mv:.1f} mV")
            if not within_tol:
                ok = False

        mono = all(readbacks[i] >= readbacks[i - 1] - (DAC_TOL_MV / 1000)
                   for i in range(1, len(readbacks)))
        _record("ramp is monotonically non-decreasing", mono,
                f"readbacks: {[round(r, 3) for r in readbacks]}")
        if not mono:
            ok = False

    except Exception as e:
        _record("DAC ramp", False, str(e))
        ok = False
    finally:
        try:
            if dac is None:
                dac = Net.get(DAC_NET, type=NetType.DAC)
            dac.output(0.0)
            print("\n  Safety: DAC reset to 0V")
        except Exception as e:
            print(f"\n  WARNING: Failed to reset DAC to 0V -- {e}")

    return ok


# ---------------------------------------------------------------------------
# 6. DAC Boundary and Range Enforcement
# ---------------------------------------------------------------------------
def test_dac_boundary():
    """Edge voltages (0 V, 5 V) succeed; out-of-range raises an exception."""
    print("\n" + "=" * 60)
    print("TEST: DAC Boundary and Range Enforcement")
    print("=" * 60)

    ok  = True
    dac = None

    try:
        dac = Net.get(DAC_NET, type=NetType.DAC)

        for edge_v in [0.0, DAC_MAX_V]:
            try:
                dac.output(edge_v)
                time.sleep(0.05)
                _record(f"output({edge_v:.1f}V) accepted (edge)", True)
            except Exception as e:
                _record(f"output({edge_v:.1f}V) accepted (edge)", False, str(e))
                ok = False

        for bad_v in [-0.1, DAC_MAX_V + 0.1]:
            try:
                dac.output(bad_v)
                _record(f"output({bad_v:.1f}V) raises exception", False,
                        "no exception — out-of-range silently accepted")
                ok = False
            except Exception:
                _record(f"output({bad_v:.1f}V) raises exception", True,
                        "correctly rejected")

    except Exception as e:
        _record("DAC boundary setup", False, str(e))
        ok = False
    finally:
        try:
            if dac is None:
                dac = Net.get(DAC_NET, type=NetType.DAC)
            dac.output(0.0)
            print("\n  Safety: DAC reset to 0V")
        except Exception as e:
            print(f"\n  WARNING: Failed to reset DAC to 0V -- {e}")

    return ok


# ---------------------------------------------------------------------------
# 7. GPIO Output Set and Readback
# ---------------------------------------------------------------------------
def test_gpio_output():
    """output(1/0) followed by input() returns the expected integer level."""
    print("\n" + "=" * 60)
    print("TEST: GPIO Output Set and Readback")
    print("=" * 60)

    ok   = True
    gpio = None

    try:
        gpio = Net.get(GPIO_NET, type=NetType.GPIO)

        for level, expected in [(1, 1), (0, 0), (1, 1)]:
            gpio.output(level)
            time.sleep(0.1)
            val = gpio.input()

            is_int = isinstance(val, int)
            _record(f"output({level}) → input() is int", is_int,
                    f"type={type(val).__name__}, value={val!r}")
            if not is_int:
                ok = False
                continue

            correct = (val == expected)
            _record(f"output({level}) → input() == {expected}", correct,
                    f"got {val}")
            if not correct:
                ok = False

    except Exception as e:
        _record("GPIO output readback", False, str(e))
        ok = False
    finally:
        try:
            if gpio is None:
                gpio = Net.get(GPIO_NET, type=NetType.GPIO)
            gpio.output(0)
            print("\n  Safety: GPIO left LOW")
        except Exception as e:
            print(f"\n  WARNING: Failed to leave GPIO LOW -- {e}")

    return ok


# ---------------------------------------------------------------------------
# 8. GPIO Input Reads
# ---------------------------------------------------------------------------
def test_gpio_input():
    """Repeated input() reads return only integer 0 or 1 and are consistent."""
    print("\n" + "=" * 60)
    print("TEST: GPIO Input Reads")
    print("=" * 60)

    NUM_READS = 5
    ok = True

    try:
        gpio = Net.get(GPIO_NET, type=NetType.GPIO)

        # Drive LOW so reads are well-defined (not floating)
        gpio.output(0)
        time.sleep(0.1)

        reads = []
        for i in range(NUM_READS):
            val = gpio.input()
            reads.append(val)
            is_int    = isinstance(val, int)
            is_binary = val in (0, 1)
            _record(f"read {i + 1}: int and binary", is_int and is_binary,
                    f"value={val!r}, type={type(val).__name__}")
            if not (is_int and is_binary):
                ok = False

        all_same = len(set(reads)) == 1
        _record("all reads consistent (driven LOW)", all_same,
                f"values={reads}")
        if not all_same:
            ok = False

    except Exception as e:
        _record("GPIO input reads", False, str(e))
        ok = False
    finally:
        try:
            Net.get(GPIO_NET, type=NetType.GPIO).output(0)
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 9. GPIO Pulse Generation
# ---------------------------------------------------------------------------
def test_gpio_pulse():
    """5 HIGH/LOW pulses; each state verified; pin left LOW."""
    print("\n" + "=" * 60)
    print("TEST: GPIO Pulse Generation")
    print("=" * 60)

    NUM_PULSES  = 5
    PULSE_WIDTH = 0.05   # seconds
    ok          = True
    gpio        = None

    try:
        gpio = Net.get(GPIO_NET, type=NetType.GPIO)

        for pulse in range(1, NUM_PULSES + 1):
            gpio.output(1)
            time.sleep(PULSE_WIDTH)
            hi = gpio.input()
            passed_hi = isinstance(hi, int) and hi == 1
            _record(f"pulse {pulse} HIGH", passed_hi, f"read={hi!r}")
            if not passed_hi:
                ok = False

            gpio.output(0)
            time.sleep(PULSE_WIDTH)
            lo = gpio.input()
            passed_lo = isinstance(lo, int) and lo == 0
            _record(f"pulse {pulse} LOW", passed_lo, f"read={lo!r}")
            if not passed_lo:
                ok = False

    except Exception as e:
        _record("GPIO pulse", False, str(e))
        ok = False
    finally:
        try:
            if gpio is None:
                gpio = Net.get(GPIO_NET, type=NetType.GPIO)
            gpio.output(0)
            print("\n  Safety: GPIO left LOW")
        except Exception as e:
            print(f"\n  WARNING: Failed to leave GPIO LOW -- {e}")

    return ok


# ---------------------------------------------------------------------------
# 10. DAC → ADC Loopback (optional)
# ---------------------------------------------------------------------------
def test_loopback():
    """DAC output matches ADC read within 100 mV (requires physical connection)."""
    print("\n" + "=" * 60)
    print("TEST: DAC → ADC Loopback")
    print("=" * 60)

    if not LOOPBACK_ADC_NET:
        print("  SKIP: LOOPBACK_ADC_NET not set — set it to enable this group")
        print(f"  Example: LOOPBACK_ADC_NET=adc1 DAC_NET={DAC_NET} lager python ...")
        return True   # not a failure — hardware may not be wired

    print(f"  DAC net:      {DAC_NET}")
    print(f"  Loopback ADC: {LOOPBACK_ADC_NET}")
    print(f"  NOTE: Requires {DAC_NET} output physically wired to {LOOPBACK_ADC_NET} input")

    TEST_VOLTAGES = [0.0, 1.0, 2.0, 3.0, 3.3]
    ok  = True
    dac = None

    try:
        dac = Net.get(DAC_NET,          type=NetType.DAC)
        adc = Net.get(LOOPBACK_ADC_NET, type=NetType.ADC)

        for target_v in TEST_VOLTAGES:
            dac.output(target_v)
            time.sleep(0.1)
            measured = adc.input()

            is_numeric = isinstance(measured, (int, float))
            _record(f"ADC read at {target_v:.1f}V is numeric", is_numeric,
                    f"type={type(measured).__name__}, value={measured}")
            if not is_numeric:
                ok = False
                continue

            within_tol = _close_enough_mv(float(measured), target_v, LOOPBACK_TOL_MV)
            error_mv   = abs(target_v - float(measured)) * 1000
            _record(f"DAC {target_v:.1f}V → ADC {measured:.3f}V", within_tol,
                    f"error={error_mv:.1f} mV (limit {LOOPBACK_TOL_MV} mV)")
            if not within_tol:
                ok = False

    except Exception as e:
        _record("loopback", False, str(e))
        ok = False
    finally:
        try:
            if dac is None:
                dac = Net.get(DAC_NET, type=NetType.DAC)
            dac.output(0.0)
            print("\n  Safety: DAC reset to 0V")
        except Exception as e:
            print(f"\n  WARNING: Failed to reset DAC to 0V -- {e}")

    return ok


# ---------------------------------------------------------------------------
# 11. Rapid Operations Stress
# ---------------------------------------------------------------------------
def test_rapid_stress():
    """20 ADC reads, 10 DAC writes, 10 GPIO toggles — no sleep between operations."""
    print("\n" + "=" * 60)
    print("TEST: Rapid Operations Stress")
    print("=" * 60)

    ok   = True
    dac  = None
    gpio = None

    try:
        adc  = Net.get(ADC_NET,  type=NetType.ADC)
        dac  = Net.get(DAC_NET,  type=NetType.DAC)
        gpio = Net.get(GPIO_NET, type=NetType.GPIO)

        # 20 rapid ADC reads
        adc_readings = []
        adc_errors   = []
        for _ in range(20):
            try:
                adc_readings.append(float(adc.input()))
            except Exception as e:
                adc_errors.append(str(e))

        _record("20 rapid ADC reads: all succeed", len(adc_readings) == 20,
                f"{len(adc_readings)} ok, {len(adc_errors)} errors")
        if len(adc_readings) < 20:
            ok = False

        if adc_readings:
            all_in_range = all(ADC_VALID_MIN <= v <= ADC_VALID_MAX for v in adc_readings)
            _record("20 rapid ADC reads: all in range", all_in_range,
                    f"min={min(adc_readings):.3f} V, max={max(adc_readings):.3f} V")
            if not all_in_range:
                ok = False

        # 10 rapid DAC writes cycling 0.5 → 4.5 V
        dac_voltages = [round(0.5 + i * 0.4, 2) for i in range(10)]
        dac_errors   = []
        for v in dac_voltages:
            try:
                dac.output(v)
            except Exception as e:
                dac_errors.append(str(e))

        _record("10 rapid DAC writes: no errors", not dac_errors,
                f"{len(dac_errors)} errors" if dac_errors else "all ok")
        if dac_errors:
            ok = False

        # 10 rapid GPIO toggles (output-only, no readback)
        gpio_errors = []
        for level in ([1, 0] * 5):
            try:
                gpio.output(level)
            except Exception as e:
                gpio_errors.append(str(e))

        _record("10 rapid GPIO toggles: no errors", not gpio_errors,
                f"{len(gpio_errors)} errors" if gpio_errors else "all ok")
        if gpio_errors:
            ok = False

    except Exception as e:
        _record("rapid stress setup", False, str(e))
        ok = False
    finally:
        try:
            if dac is None:
                dac = Net.get(DAC_NET, type=NetType.DAC)
            dac.output(0.0)
            print("\n  Safety: DAC reset to 0V")
        except Exception as e:
            print(f"\n  WARNING: Failed to reset DAC to 0V -- {e}")
        try:
            if gpio is None:
                gpio = Net.get(GPIO_NET, type=NetType.GPIO)
            gpio.output(0)
            print("  Safety: GPIO left LOW")
        except Exception as e:
            print(f"\n  WARNING: Failed to leave GPIO LOW -- {e}")

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    print("LabJack T7 Comprehensive Test Suite")
    print(f"  ADC net:      {ADC_NET}  (ADC_NETS={ADC_NETS_CSV})")
    print(f"  DAC net:      {DAC_NET}")
    print(f"  GPIO net:     {GPIO_NET}")
    print(f"  Loopback ADC: {LOOPBACK_ADC_NET or '(disabled — set LOOPBACK_ADC_NET to enable)'}")
    print("=" * 60)

    # Preflight: open the LabJack handle and confirm the primary ADC responds.
    try:
        adc = Net.get(ADC_NET, type=NetType.ADC)
        adc.input()
    except Exception as e:
        print(f"\nSKIP: Cannot connect to net '{ADC_NET}' — device not reachable: {e}")
        print("\nDiagnose with:")
        print("  lager instruments --box <box>")
        print(f"  lager diagnose {ADC_NET} --box <box>")
        print("\nSkipping all tests for this device.")
        sys.exit(0)

    tests = [
        ("ADC Single-Channel Read",      test_adc_single_channel),
        ("ADC Multi-Channel Sweep",      test_adc_multi_channel),
        ("ADC Measurement Stability",    test_adc_stability),
        ("DAC Output and Readback",      test_dac_output_readback),
        ("DAC Voltage Ramp",             test_dac_ramp),
        ("DAC Boundary and Range",       test_dac_boundary),
        ("GPIO Output Set and Readback", test_gpio_output),
        ("GPIO Input Reads",             test_gpio_input),
        ("GPIO Pulse Generation",        test_gpio_pulse),
        ("DAC → ADC Loopback",      test_loopback),
        ("Rapid Operations Stress",      test_rapid_stress),
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
            Net.get(DAC_NET,  type=NetType.DAC).output(0.0)
            Net.get(GPIO_NET, type=NetType.GPIO).output(0)
        except Exception:
            pass

    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    passed_count = sum(1 for _, p in test_results if p)
    total_count  = len(test_results)

    for name, p in test_results:
        status = "PASS" if p else "FAIL"
        print(f"  [{status}] {name}")

    print(f"\nTotal: {passed_count}/{total_count} test groups passed")

    sub_passed = sum(1 for _, p, _ in _results if p)
    sub_total  = len(_results)
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
