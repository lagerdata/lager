# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_usb_stress.py
# Run with: lager python test_usb_stress.py --box <YOUR-BOX>

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

    print("=== USB Stress Test ===\n")

    port = 'usb1'
    cycles = 20
    delay = 0.1
    usb = None

    print(f"Port: {port}")
    print(f"Cycles: {cycles}\n")

    try:
        usb = Net.get(port, type=NetType.Usb)
        _record("net_get", usb is not None, f"type={type(usb).__name__}")

        errors = 0
        for i in range(cycles):
            try:
                usb.disable()
                time.sleep(delay)
                usb.enable()
                time.sleep(delay)
                _record(f"cycle_{i+1}", True)
            except Exception as e:
                errors += 1
                _record(f"cycle_{i+1}", False, str(e))

        _record("zero_errors", errors == 0,
                f"{errors} error(s) in {cycles} cycles")

    except Exception as e:
        _record("stress_init", False, str(e))
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
