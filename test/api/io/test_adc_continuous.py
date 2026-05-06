#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# test_adc_continuous.py
# Run with: lager python test/api/io/test_adc_continuous.py --box MY-BOX

import sys
import math
import time
import traceback

from lager import Net, NetType

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ADC_NET = 'adc1'
NUM_SAMPLES = 10
SAMPLE_INTERVAL = 0.5

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
# 1. Continuous Read
# ---------------------------------------------------------------------------
def test_continuous_read():
    """Read ADC channel repeatedly and validate all samples."""
    print("\n" + "=" * 60)
    print("TEST: Continuous ADC Read")
    print("=" * 60)

    ok = True

    try:
        adc = Net.get(ADC_NET, type=NetType.ADC)
        samples = []

        print(f"\n  Collecting {NUM_SAMPLES} samples at {SAMPLE_INTERVAL}s intervals...")

        for i in range(NUM_SAMPLES):
            voltage = adc.input()
            samples.append(voltage)
            print(f"    Sample {i+1:2d}: {voltage:.4f} V")
            if i < NUM_SAMPLES - 1:
                time.sleep(SAMPLE_INTERVAL)

        # Check all samples are float
        all_float = all(isinstance(s, (int, float)) for s in samples)
        _record("all samples are numeric", all_float,
                f"{sum(1 for s in samples if isinstance(s, (int, float)))}/{len(samples)} numeric")
        if not all_float:
            ok = False

        # Check all samples in LabJack T7 range: -0.5 to 11V
        all_in_range = all(-0.5 <= s <= 11.0 for s in samples)
        _record("all samples in range [-0.5, 11.0] V", all_in_range,
                f"min={min(samples):.4f} V, max={max(samples):.4f} V")
        if not all_in_range:
            ok = False

        # Stability check: std deviation < 1V
        if len(samples) >= 2:
            mean = sum(samples) / len(samples)
            variance = sum((s - mean) ** 2 for s in samples) / (len(samples) - 1)
            stdev = math.sqrt(variance)
            stable = stdev < 1.0
            _record("std deviation < 1V (stability)", stable,
                    f"stdev={stdev:.4f} V, mean={mean:.4f} V")
            if not stable:
                ok = False

    except Exception as e:
        _record("continuous read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    """Run all tests."""
    print("ADC Continuous Monitoring Test")
    print(f"Testing net: {ADC_NET}")
    print(f"Samples: {NUM_SAMPLES}, Interval: {SAMPLE_INTERVAL}s")
    print("=" * 60)

    tests = [
        ("Continuous Read", test_continuous_read),
    ]

    for name, test_fn in tests:
        try:
            test_fn()
        except Exception as e:
            print(f"\nUNEXPECTED ERROR in {name}: {e}")
            traceback.print_exc()
            _record(name, False, str(e))

    # Summary
    failed = [r for r in _results if not r[1]]
    print(f"\n{'='*60}")
    print(f"Results: {len(_results)-len(failed)}/{len(_results)} passed")
    if failed:
        for name, _, detail in failed:
            print(f"  FAILED: {name} -- {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
