# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_watt_profile.py
# Run with: lager python test_watt_profile.py --box MY-BOX

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
    print("=== Power Profiling Test ===\n")

    net_name = 'watt1'
    duration = 5
    sample_rate = 2

    try:
        print(f"Profiling power on {net_name} for {duration} seconds...")
        watt = Net.get(net_name, type=NetType.WattMeter)

        interval = 1.0 / sample_rate
        total_samples = duration * sample_rate
        readings = []

        print("\nTime (s)    Power (mW)")
        print("-" * 25)

        start_time = time.time()
        all_valid = True
        for i in range(total_samples):
            power = watt.read()
            timestamp = time.time() - start_time
            print(f"{timestamp:6.1f}      {power*1000:.2f}")

            is_numeric = isinstance(power, (int, float))
            if is_numeric and not math.isnan(power) and not math.isinf(power):
                readings.append(power)
                if power < 0:
                    all_valid = False
            else:
                all_valid = False

            time.sleep(interval)

        # All readings must be valid floats
        _record("all_readings_valid_float", len(readings) == total_samples,
                f"{len(readings)}/{total_samples} valid")

        # All readings non-negative and not NaN
        _record("all_readings_non_negative", all_valid,
                f"{len(readings)} readings checked")

        if readings:
            mean_val = sum(readings) / len(readings)
            min_val = min(readings)
            max_val = max(readings)

            print(f"\n  Summary: min={min_val*1000:.2f} mW, "
                  f"max={max_val*1000:.2f} mW, mean={mean_val*1000:.2f} mW")

            # Summary stats sanity: min <= mean <= max
            stats_ok = min_val <= mean_val <= max_val
            _record("summary_stats_consistent", stats_ok,
                    f"min={min_val:.6f} <= mean={mean_val:.6f} <= max={max_val:.6f}")

    except Exception as e:
        _record("profile_watt_meter", False, f"exception: {e}")

    # --- Summary ---
    print("\n=== Power Profiling Test Complete ===")
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nTotal: {total}  Passed: {passed}  Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
