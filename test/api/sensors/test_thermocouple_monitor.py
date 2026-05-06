# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_thermocouple_monitor.py
# Run with: lager python test_thermocouple_monitor.py --box <YOUR-BOX>

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


def _stdev(values):
    """Compute population standard deviation without statistics module."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    return math.sqrt(variance)


def main():
    print("=== Thermocouple Continuous Monitoring Test ===\n")

    net_name = 'thermocouple1'
    num_samples = 10
    sample_interval = 1.0
    samples = []

    try:
        print(f"Monitoring {net_name} for {num_samples} samples...")
        tc = Net.get(net_name, type=NetType.Thermocouple)
        start_time = time.time()

        print("\nTime (s)    Temp (C)")
        print("-" * 25)

        for i in range(num_samples):
            temp_c = tc.read()
            elapsed = time.time() - start_time
            print(f"{elapsed:8.1f}    {temp_c:8.2f}")

            is_numeric = isinstance(temp_c, (int, float))
            if is_numeric:
                is_finite = not math.isnan(temp_c) and not math.isinf(temp_c)
                if is_finite:
                    samples.append(temp_c)

            if i < num_samples - 1:
                time.sleep(sample_interval)

        # All samples must be valid floats
        all_valid = len(samples) == num_samples
        _record("all_samples_valid_float", all_valid,
                f"{len(samples)}/{num_samples} valid")

        # No NaN or Inf (already filtered above -- check count matches)
        _record("no_nan_or_inf", all_valid,
                f"{len(samples)}/{num_samples} finite")

        if len(samples) >= 2:
            # Standard deviation should be < 5 C for stable readings
            sd = _stdev(samples)
            sd_ok = sd < 5.0
            _record("std_dev_below_5C", sd_ok,
                    f"stdev={sd:.3f} C")

            mean_val = sum(samples) / len(samples)
            print(f"\n  Stats: mean={mean_val:.2f} C, stdev={sd:.3f} C, "
                  f"min={min(samples):.2f} C, max={max(samples):.2f} C")
        else:
            _record("std_dev_below_5C", False,
                    f"not enough valid samples ({len(samples)})")

    except Exception as e:
        _record("monitor_thermocouple", False, f"exception: {e}")

    # --- Summary ---
    print("\n=== Thermocouple Continuous Monitoring Test Complete ===")
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nTotal: {total}  Passed: {passed}  Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
