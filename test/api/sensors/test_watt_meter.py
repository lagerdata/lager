# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_watt_meter.py
# Run with: lager python test_watt_meter.py --box MY-BOX

import sys
import math
import time
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
    print("=== Watt Meter Test ===\n")

    net_name = 'watt1'
    num_samples = 5

    try:
        print(f"Reading power from {net_name}...")
        watt = Net.get(net_name, type=NetType.WattMeter)

        print("\nPower readings:")
        for i in range(num_samples):
            power = watt.read()
            print(f"  Sample {i+1}: {power*1000:.2f} mW")

            # Each read must return a numeric type
            is_float = isinstance(power, (int, float))
            _record(f"sample_{i+1}_is_numeric", is_float,
                    f"type={type(power).__name__}")

            if is_float:
                # Must not be NaN
                not_nan = not math.isnan(power)
                _record(f"sample_{i+1}_not_nan", not_nan,
                        f"value={power}")

                # Must be non-negative
                non_neg = power >= 0
                _record(f"sample_{i+1}_non_negative", non_neg,
                        f"power={power:.6f} W")
            else:
                _record(f"sample_{i+1}_not_nan", False, "skipped -- not numeric")
                _record(f"sample_{i+1}_non_negative", False, "skipped -- not numeric")

            time.sleep(0.2)

    except Exception as e:
        _record("read_watt_meter", False, f"exception: {e}")

    # --- Summary ---
    print("\n=== Watt Meter Test Complete ===")
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nTotal: {total}  Passed: {passed}  Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
