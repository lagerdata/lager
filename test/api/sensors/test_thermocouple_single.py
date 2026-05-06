# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_thermocouple_single.py
# Run with: lager python test_thermocouple_single.py --box <YOUR-BOX>

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
    print("=== Thermocouple Single Reading Test ===\n")

    net_name = 'thermocouple1'

    try:
        print(f"Reading temperature from {net_name}...")
        tc = Net.get(net_name, type=NetType.Thermocouple)
        temp_c = tc.read()

        print(f"\n  Temperature: {temp_c} C")

        # Check that result is a float
        is_float = isinstance(temp_c, (int, float))
        _record("read_is_numeric", is_float,
                f"type={type(temp_c).__name__}")

        if is_float:
            # Check not NaN or Inf
            is_finite = not math.isnan(temp_c) and not math.isinf(temp_c)
            _record("read_not_nan_inf", is_finite,
                    f"value={temp_c}")

            # Check reasonable range -40 to 125 C
            in_range = -40 <= temp_c <= 125
            _record("read_in_range_-40_to_125C", in_range,
                    f"temp={temp_c:.2f} C")

            # Print Fahrenheit for reference
            temp_f = temp_c * 9 / 5 + 32
            print(f"  Temperature: {temp_f:.2f} F")
        else:
            _record("read_not_nan_inf", False, "skipped -- not numeric")
            _record("read_in_range_-40_to_125C", False, "skipped -- not numeric")

    except Exception as e:
        _record("read_thermocouple", False, f"exception: {e}")

    # --- Summary ---
    print("\n=== Thermocouple Single Reading Test Complete ===")
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nTotal: {total}  Passed: {passed}  Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
