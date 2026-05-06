# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_usb_power_cycle.py
# Run with: lager python test_usb_power_cycle.py --box <YOUR-BOX>

import sys
import time

_results = []

def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)

def main():
    from lager import Net, NetType

    print("=== USB Power Cycling Test ===\n")

    net_name = 'usb1'
    off_duration = 2.0
    usb = None

    try:
        # Test 1: Net.get
        usb = Net.get(net_name, type=NetType.Usb)
        _record("net_get", usb is not None, f"type={type(usb).__name__}")

        # Test 2: Disable
        usb.disable()
        _record("disable", True)

        # Test 3: Wait and re-enable (power cycle timing)
        t0 = time.time()
        time.sleep(off_duration)
        usb.enable()
        elapsed = time.time() - t0
        _record("power_cycle_timing", elapsed >= off_duration,
                f"off for {elapsed:.2f}s (target {off_duration}s)")

    except Exception as e:
        _record("power_cycle", False, str(e))
    finally:
        # Safety: leave port enabled
        if usb is not None:
            try:
                usb.enable()
            except Exception:
                pass

    # Summary
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nRESULTS: {passed}/{total} passed, {failed} failed")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
