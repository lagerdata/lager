#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# test_adc_single.py
# Run with: lager python test/api/io/test_adc_single.py --box MY-BOX

import sys
import traceback

from lager import Net, NetType

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ADC_NET = 'adc1'

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
# 1. Net.get
# ---------------------------------------------------------------------------
def test_net_get():
    """Verify Net.get returns an ADC net object."""
    print("\n" + "=" * 60)
    print("TEST: Net.get")
    print("=" * 60)

    try:
        adc = Net.get(ADC_NET, type=NetType.ADC)
        passed = adc is not None
        _record("Net.get returns object", passed, type(adc).__name__)
        return passed
    except Exception as e:
        _record("Net.get returns object", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 2. ADC Read
# ---------------------------------------------------------------------------
def test_adc_read():
    """Read a single ADC channel and validate the result."""
    print("\n" + "=" * 60)
    print("TEST: ADC Single Read")
    print("=" * 60)

    ok = True

    try:
        adc = Net.get(ADC_NET, type=NetType.ADC)
        voltage = adc.input()

        # Check voltage is a float
        is_float = isinstance(voltage, (int, float))
        _record("voltage is numeric", is_float,
                f"type={type(voltage).__name__}, value={voltage}")
        if not is_float:
            ok = False

        # Check voltage in LabJack T7 range: -0.5 to 11V
        if is_float:
            in_range = -0.5 <= voltage <= 11.0
            _record("voltage in range [-0.5, 11.0] V", in_range,
                    f"{voltage:.4f} V")
            if not in_range:
                ok = False
    except Exception as e:
        _record("ADC read", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    """Run all tests."""
    print("ADC Single Channel Test")
    print(f"Testing net: {ADC_NET}")
    print("=" * 60)

    tests = [
        ("Net.get",         test_net_get),
        ("ADC Single Read", test_adc_read),
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
