#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# test_dac_ramp.py
# Run with: lager python test/api/io/test_dac_ramp.py --box MY-BOX

import sys
import time
import traceback

from lager import Net, NetType

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DAC_NET = 'dac1'
START_V = 0.0
END_V = 3.3
STEPS = 10

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
# 1. DAC Ramp
# ---------------------------------------------------------------------------
def test_dac_ramp():
    """Ramp DAC from 0V to 3.3V and verify each step succeeds."""
    print("\n" + "=" * 60)
    print("TEST: DAC Voltage Ramp")
    print("=" * 60)

    ok = True
    dac = None
    step_size = (END_V - START_V) / STEPS

    try:
        dac = Net.get(DAC_NET, type=NetType.DAC)
        readbacks = []

        print(f"\n  Ramp: {START_V}V -> {END_V}V in {STEPS} steps")

        for i in range(STEPS + 1):
            target_v = START_V + (i * step_size)
            try:
                dac.output(target_v)
                time.sleep(0.1)
                _record(f"step {i:2d}: output {target_v:.3f}V", True)

                # Attempt readback for monotonic check
                try:
                    rb = dac.get_voltage()
                    if isinstance(rb, (int, float)):
                        readbacks.append(rb)
                except Exception:
                    pass

            except Exception as e:
                _record(f"step {i:2d}: output {target_v:.3f}V", False, str(e))
                ok = False

        # Monotonic increase check (if readbacks available)
        if len(readbacks) >= 2:
            monotonic = all(
                readbacks[j] <= readbacks[j + 1] + 0.05  # 50mV tolerance
                for j in range(len(readbacks) - 1)
            )
            _record("readback monotonically increasing", monotonic,
                    f"min={min(readbacks):.3f}V, max={max(readbacks):.3f}V")
            if not monotonic:
                ok = False

    except Exception as e:
        _record("DAC ramp", False, str(e))
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
    print("DAC Voltage Ramp Test")
    print(f"Testing net: {DAC_NET}")
    print(f"Ramp: {START_V}V -> {END_V}V in {STEPS} steps")
    print("=" * 60)

    tests = [
        ("DAC Voltage Ramp", test_dac_ramp),
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
