# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_ble_basic.py
# Run with: lager python test_ble_basic.py --box <YOUR-BOX>
# NOTE: Requires BLE adapter and nearby BLE devices

import sys

_results = []

def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)

def main():
    from lager.ble import Central

    print("=== BLE Basic Test ===\n")

    # Test 1: Central constructor
    try:
        central = Central()
        _record("central_constructor", True)
    except Exception as e:
        _record("central_constructor", False, str(e))
        return 1

    # Test 2: Scan returns a list
    try:
        devices = central.scan(scan_time=10.0)
        _record("scan_returns_list", isinstance(devices, list),
                f"type={type(devices).__name__}")
    except Exception as e:
        _record("scan_returns_list", False, str(e))
        devices = []

    # Test 3: Each device has name and address attributes
    if devices:
        all_have_attrs = all(
            hasattr(d, 'name') and hasattr(d, 'address') for d in devices
        )
        _record("devices_have_attrs", all_have_attrs,
                f"found {len(devices)} device(s)")
        for d in devices[:5]:
            name = d.name or "Unknown"
            print(f"    - {name}: {d.address}")
        if len(devices) > 5:
            print(f"    ... and {len(devices) - 5} more")
    else:
        # No devices found is acceptable (scan is environment-dependent)
        _record("devices_have_attrs", True, "no devices found (scan is read-only)")

    # Summary
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nRESULTS: {passed}/{total} passed, {failed} failed")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
