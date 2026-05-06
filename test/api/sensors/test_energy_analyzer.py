# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Test script for energy analyzer (Joulescope JS220) via lager Python API.

Run via:
    lager python test_energy_analyzer.py --box <box>

Requires a net named 'pwr' with role 'energy-analyzer' saved on the box.
Adjust NET_NAME to match your configuration.
"""

import sys
import math
from lager import Net, NetType

NET_NAME = "pwr"
DURATION = 5.0

_results = []

def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def main():
    print(f"=== Energy Analyzer Test ===\n")

    print(f"Getting energy-analyzer net '{NET_NAME}'...")
    net = Net.get(NET_NAME, type=NetType.EnergyAnalyzer)
    print(f"  OK: {type(net).__name__}")

    print(f"\nReading energy over {DURATION}s...")
    result = net.read_energy(duration=DURATION)

    # Check all required keys are present
    required_keys = {"energy_j", "energy_wh", "charge_c", "charge_ah", "duration_s"}
    missing = required_keys - set(result.keys())
    _record("all_keys_present", len(missing) == 0,
            f"missing={missing}" if missing else "all 5 keys found")

    if missing:
        # Cannot continue meaningful checks without keys
        print("\n=== Energy Analyzer Test Complete ===")
        total = len(_results)
        passed = sum(1 for _, p, _ in _results if p)
        failed = total - passed
        print(f"\nTotal: {total}  Passed: {passed}  Failed: {failed}")
        return 1

    # Print values
    print(f"  energy_j    = {result['energy_j']:.6f} J")
    print(f"  energy_wh   = {result['energy_wh']:.9f} Wh")
    print(f"  charge_c    = {result['charge_c']:.6f} C")
    print(f"  charge_ah   = {result['charge_ah']:.9f} Ah")
    print(f"  duration_s  = {result['duration_s']:.1f} s")

    # energy_j > 0
    ej = result['energy_j']
    _record("energy_j_positive", isinstance(ej, (int, float)) and ej > 0,
            f"energy_j={ej}")

    # energy_wh > 0
    ewh = result['energy_wh']
    _record("energy_wh_positive", isinstance(ewh, (int, float)) and ewh > 0,
            f"energy_wh={ewh}")

    # charge_c > 0
    cc = result['charge_c']
    _record("charge_c_positive", isinstance(cc, (int, float)) and cc > 0,
            f"charge_c={cc}")

    # charge_ah > 0
    cah = result['charge_ah']
    _record("charge_ah_positive", isinstance(cah, (int, float)) and cah > 0,
            f"charge_ah={cah}")

    # duration within 10% of requested DURATION
    dur = result['duration_s']
    dur_is_float = isinstance(dur, (int, float))
    if dur_is_float:
        tolerance = DURATION * 0.10
        dur_ok = abs(dur - DURATION) <= tolerance
        _record("duration_within_10pct", dur_ok,
                f"expected={DURATION}s, got={dur:.2f}s, tolerance={tolerance:.2f}s")
    else:
        _record("duration_within_10pct", False,
                f"duration_s is not numeric: {dur}")

    # Cross-check: energy_wh should equal energy_j / 3600
    if isinstance(ej, (int, float)) and isinstance(ewh, (int, float)) and ej > 0:
        expected_wh = ej / 3600.0
        # Allow 1% relative tolerance for floating-point
        if expected_wh > 0:
            relative_err = abs(ewh - expected_wh) / expected_wh
            crosscheck_ok = relative_err < 0.01
            _record("wh_equals_j_div_3600", crosscheck_ok,
                    f"energy_wh={ewh:.9f}, J/3600={expected_wh:.9f}, err={relative_err:.4%}")
        else:
            _record("wh_equals_j_div_3600", False, "expected_wh is zero")
    else:
        _record("wh_equals_j_div_3600", False, "cannot cross-check -- invalid values")

    # --- Summary ---
    print("\n=== Energy Analyzer Test Complete ===")
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nTotal: {total}  Passed: {passed}  Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
