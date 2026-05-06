#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# test_adc_multiple.py
# Run with: lager python test/api/io/test_adc_multiple.py --box MY-BOX

import sys
import traceback

from lager import Net, NetType

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ADC_NETS = ['adc1', 'adc2', 'adc3', 'adc4', 'adc5', 'adc6', 'adc7', 'adc8']

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
# 1. Read All Channels
# ---------------------------------------------------------------------------
def test_read_all_channels():
    """Read all 8 ADC channels and validate each result."""
    print("\n" + "=" * 60)
    print("TEST: Read All ADC Channels")
    print("=" * 60)

    ok = True

    for net_name in ADC_NETS:
        try:
            adc = Net.get(net_name, type=NetType.ADC)
            voltage = adc.input()

            # Check voltage is a float
            is_float = isinstance(voltage, (int, float))
            _record(f"{net_name} is numeric", is_float,
                    f"type={type(voltage).__name__}, value={voltage}")
            if not is_float:
                ok = False
                continue

            # Check voltage in LabJack T7 range: -0.5 to 11V
            in_range = -0.5 <= voltage <= 11.0
            _record(f"{net_name} in range [-0.5, 11.0] V", in_range,
                    f"{voltage:.4f} V")
            if not in_range:
                ok = False

        except Exception as e:
            _record(f"{net_name} read", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    """Run all tests."""
    print("ADC Multiple Channel Test")
    print(f"Testing nets: {', '.join(ADC_NETS)}")
    print("=" * 60)

    tests = [
        ("Read All Channels", test_read_all_channels),
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
