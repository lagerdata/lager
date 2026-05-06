# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_sensors_comprehensive.py
# Run with: lager python test_sensors_comprehensive.py --box <YOUR-BOX>
#
# Note: This test runs oscilloscope tests on the box (scope1-6).
# For thermocouple tests, use test_thermocouple_*.py on a box with thermocouples.

import sys
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
    print("=== Comprehensive Sensor Test ===\n")

    scope_channels = ['scope3', 'scope4', 'scope5', 'scope6']  # Rigol MSO5204 scopes
    enabled_scopes = []

    try:
        # Oscilloscope Test (requires box with scope1-6)
        print("--- Oscilloscope Test ---")
        for ch_name in scope_channels:
            try:
                scope = Net.get(ch_name, type=NetType.Analog)

                # Test enable
                scope.enable()
                enabled_scopes.append((ch_name, scope))
                _record(f"{ch_name}_enable", True, "enabled successfully")

                # Test disable
                scope.disable()
                enabled_scopes = [(n, s) for n, s in enabled_scopes if n != ch_name]
                _record(f"{ch_name}_disable", True, "disabled successfully")

            except Exception as e:
                _record(f"{ch_name}_enable_disable", False, f"exception: {e}")

        # Thermocouple tests skipped - run on a box with thermocouples
        print("\n--- Thermocouple Test ---")
        print("  (Skipped - thermocouples require a box with thermocouple hardware)")
        print("  Run: lager python test/api/sensors/test_thermocouple_single.py --box <YOUR-BOX>")

    finally:
        # Safety teardown: disable any scopes that were left enabled
        for ch_name, scope in enabled_scopes:
            try:
                scope.disable()
                print(f"  [teardown] Disabled {ch_name}")
            except Exception:
                pass

    # --- Summary ---
    print("\n=== Comprehensive Sensor Test Complete ===")
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nTotal: {total}  Passed: {passed}  Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
