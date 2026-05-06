# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_thermocouple_multiple.py
# Run with: lager python test_thermocouple_multiple.py --box <YOUR-BOX>

import sys
import math
from lager import Net, NetType

_results = []

def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def main():
    print("=== Thermocouple Multiple Channels Test ===\n")

    tc_nets = ['thermocouple1', 'thermocouple2', 'thermocouple3', 'thermocouple4']
    temps = {}

    print(f"Reading {len(tc_nets)} thermocouple channels...\n")

    for net_name in tc_nets:
        try:
            tc = Net.get(net_name, type=NetType.Thermocouple)
            temp_c = tc.read()
            temp_f = temp_c * 9 / 5 + 32
            print(f"  {net_name}: {temp_c:.2f} C ({temp_f:.2f} F)")

            # Check that result is a float
            is_float = isinstance(temp_c, (int, float))
            _record(f"{net_name}_is_numeric", is_float,
                    f"type={type(temp_c).__name__}")

            if is_float:
                is_finite = not math.isnan(temp_c) and not math.isinf(temp_c)
                _record(f"{net_name}_not_nan_inf", is_finite,
                        f"value={temp_c}")

                in_range = -40 <= temp_c <= 125
                _record(f"{net_name}_in_range_-40_to_125C", in_range,
                        f"temp={temp_c:.2f} C")

                if is_finite:
                    temps[net_name] = temp_c
            else:
                _record(f"{net_name}_not_nan_inf", False, "skipped -- not numeric")
                _record(f"{net_name}_in_range_-40_to_125C", False, "skipped -- not numeric")

        except Exception as e:
            _record(f"{net_name}_read", False, f"exception: {e}")

    # Cross-channel consistency: all readings within 20 C of each other
    if len(temps) >= 2:
        all_temps = list(temps.values())
        spread = max(all_temps) - min(all_temps)
        within_20 = spread <= 20.0
        _record("cross_channel_within_20C", within_20,
                f"spread={spread:.2f} C (min={min(all_temps):.2f}, max={max(all_temps):.2f})")
    else:
        _record("cross_channel_within_20C", False,
                f"need >= 2 valid readings, got {len(temps)}")

    # --- Summary ---
    print("\n=== Thermocouple Multiple Channels Test Complete ===")
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nTotal: {total}  Passed: {passed}  Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
