#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# test_dac_output.py
# Run with: lager python test/api/io/test_dac_output.py --box MY-BOX

import sys
import time
import traceback

from lager import Net, NetType

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DAC_NET = 'dac1'
TEST_VOLTAGES = [0.0, 1.0, 2.5, 3.3, 5.0]
TOLERANCE_MV = 50  # 50 mV tolerance for set vs readback

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
# 1. DAC Output and Readback
# ---------------------------------------------------------------------------
def test_dac_output():
    """Set DAC to various voltages and verify readback is within tolerance."""
    print("\n" + "=" * 60)
    print("TEST: DAC Output and Readback")
    print("=" * 60)

    ok = True
    dac = None

    try:
        dac = Net.get(DAC_NET, type=NetType.DAC)

        for target_v in TEST_VOLTAGES:
            dac.output(target_v)
            time.sleep(0.1)
            readback = dac.get_voltage()

            # Check readback is float
            is_float = isinstance(readback, (int, float))
            _record(f"readback at {target_v:.1f}V is numeric", is_float,
                    f"type={type(readback).__name__}, value={readback}")
            if not is_float:
                ok = False
                continue

            # Check set vs readback within tolerance
            error_mv = abs(target_v - readback) * 1000
            within_tol = error_mv <= TOLERANCE_MV
            _record(f"set {target_v:.1f}V vs readback {readback:.3f}V", within_tol,
                    f"error={error_mv:.1f} mV (limit {TOLERANCE_MV} mV)")
            if not within_tol:
                ok = False

    except Exception as e:
        _record("DAC output", False, str(e))
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
    print("DAC Output Test")
    print(f"Testing net: {DAC_NET}")
    print(f"Test voltages: {TEST_VOLTAGES}")
    print(f"Tolerance: {TOLERANCE_MV} mV")
    print("=" * 60)

    tests = [
        ("DAC Output and Readback", test_dac_output),
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
