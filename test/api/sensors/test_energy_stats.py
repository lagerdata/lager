# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Test script for energy analyzer statistics (Joulescope JS220) via lager Python API.

Run via:
    lager python test_energy_stats.py --box <box>

Requires a net named 'pwr' with role 'energy-analyzer' saved on the box.
Adjust NET_NAME to match your configuration.
"""

import sys
from lager import Net, NetType

NET_NAME = "pwr"
DURATION = 1.0

_results = []

def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def main():
    print("=== Energy Stats Test ===\n")

    print(f"Getting energy-analyzer net '{NET_NAME}'...")
    net = Net.get(NET_NAME, type=NetType.EnergyAnalyzer)
    print(f"  OK: {type(net).__name__}")

    print(f"\nReading stats over {DURATION}s...")
    result = net.read_stats(duration=DURATION)

    # Check required top-level keys
    required_top = {"current", "voltage", "power", "duration_s"}
    required_sub = {"mean", "min", "max", "std"}

    missing_top = required_top - set(result.keys())
    _record("top_level_keys_present", len(missing_top) == 0,
            f"missing={missing_top}" if missing_top else "all 4 keys found")

    if missing_top:
        print("\n=== Energy Stats Test Complete ===")
        total = len(_results)
        passed = sum(1 for _, p, _ in _results if p)
        failed = total - passed
        print(f"\nTotal: {total}  Passed: {passed}  Failed: {failed}")
        return 1

    # Check sub-keys for each section
    for section in ("current", "voltage", "power"):
        missing_sub = required_sub - set(result[section].keys())
        _record(f"{section}_sub_keys_present", len(missing_sub) == 0,
                f"missing={missing_sub}" if missing_sub else "mean/min/max/std found")

    # Print values
    print(f"\n  Current: mean={result['current']['mean']*1000:.2f} mA  "
          f"min={result['current']['min']*1000:.2f} mA  "
          f"max={result['current']['max']*1000:.2f} mA  "
          f"std={result['current']['std']*1000:.2f} mA")
    print(f"  Voltage: mean={result['voltage']['mean']:.3f} V  "
          f"min={result['voltage']['min']:.3f} V  "
          f"max={result['voltage']['max']:.3f} V  "
          f"std={result['voltage']['std']:.4f} V")
    print(f"  Power:   mean={result['power']['mean']*1000:.2f} mW  "
          f"min={result['power']['min']*1000:.2f} mW  "
          f"max={result['power']['max']*1000:.2f} mW  "
          f"std={result['power']['std']*1000:.2f} mW")
    print(f"  duration_s = {result['duration_s']:.1f} s")

    # Validate each section
    for section in ("current", "voltage", "power"):
        data = result[section]

        # Type checks -- all values must be numeric
        all_numeric = all(isinstance(data.get(k), (int, float)) for k in required_sub)
        _record(f"{section}_all_values_numeric", all_numeric,
                f"types: mean={type(data.get('mean')).__name__}, "
                f"min={type(data.get('min')).__name__}, "
                f"max={type(data.get('max')).__name__}, "
                f"std={type(data.get('std')).__name__}")

        if not all_numeric:
            continue

        mean_val = data['mean']
        min_val = data['min']
        max_val = data['max']
        std_val = data['std']

        # min <= mean <= max
        ordering_ok = min_val <= mean_val <= max_val
        _record(f"{section}_min_le_mean_le_max", ordering_ok,
                f"min={min_val}, mean={mean_val}, max={max_val}")

        # std >= 0
        std_ok = std_val >= 0
        _record(f"{section}_std_non_negative", std_ok,
                f"std={std_val}")

    # duration_s type check
    dur = result['duration_s']
    _record("duration_s_is_numeric", isinstance(dur, (int, float)),
            f"type={type(dur).__name__}, value={dur}")

    # --- Summary ---
    print("\n=== Energy Stats Test Complete ===")
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nTotal: {total}  Passed: {passed}  Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
