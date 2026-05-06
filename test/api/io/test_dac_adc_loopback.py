#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# test_dac_adc_loopback.py
# Run with: lager python test/api/io/test_dac_adc_loopback.py --box MY-BOX
# NOTE: Requires DAC1 connected to ADC1 (or internal connection)

import sys
import time
import traceback

from lager import Net, NetType

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DAC_NET = 'dac1'
ADC_NET = 'adc1'
TEST_VOLTAGES = [0.0, 1.0, 2.0, 3.0, 3.3]
TOLERANCE_MV = 100  # 100 mV tolerance for DAC-ADC loopback

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
# 1. Loopback Test
# ---------------------------------------------------------------------------
def test_loopback():
    """Set DAC voltages and verify ADC reads within tolerance."""
    print("\n" + "=" * 60)
    print("TEST: DAC-ADC Loopback")
    print("=" * 60)
    print(f"\n  NOTE: Requires {DAC_NET} physically connected to {ADC_NET}")

    ok = True
    dac = None

    try:
        dac = Net.get(DAC_NET, type=NetType.DAC)
        adc = Net.get(ADC_NET, type=NetType.ADC)

        for target_v in TEST_VOLTAGES:
            dac.output(target_v)
            time.sleep(0.1)
            measured_v = adc.input()

            # Check DAC output is accepted (no exception = pass, already past)

            # Check ADC readback is float
            adc_is_float = isinstance(measured_v, (int, float))
            _record(f"ADC read at {target_v:.1f}V is numeric", adc_is_float,
                    f"type={type(measured_v).__name__}, value={measured_v}")
            if not adc_is_float:
                ok = False
                continue

            # Check DAC-ADC within tolerance
            error_mv = abs(target_v - measured_v) * 1000
            within_tol = error_mv <= TOLERANCE_MV
            _record(f"DAC {target_v:.1f}V vs ADC {measured_v:.3f}V", within_tol,
                    f"error={error_mv:.1f} mV (limit {TOLERANCE_MV} mV)")
            if not within_tol:
                ok = False

    except Exception as e:
        _record("loopback", False, str(e))
        ok = False
    finally:
        # Safety: always reset DAC to 0V
        try:
            if dac is None:
                dac = Net.get(DAC_NET, type=NetType.DAC)
            dac.output(0.0)
            print(f"\n  Safety: DAC reset to 0V")
        except Exception as e:
            print(f"\n  WARNING: Failed to reset DAC to 0V -- {e}")

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    """Run all tests."""
    print("DAC to ADC Loopback Test")
    print(f"DAC net: {DAC_NET}")
    print(f"ADC net: {ADC_NET}")
    print(f"Test voltages: {TEST_VOLTAGES}")
    print(f"Tolerance: {TOLERANCE_MV} mV")
    print("=" * 60)

    tests = [
        ("DAC-ADC Loopback", test_loopback),
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
