# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_usb_net_api.py
# Run with: lager python test_usb_net_api.py --box <YOUR-BOX>

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
    from lager import Net, NetType

    print("=== USB Net-Based API Test ===\n")

    net_name = 'usb1'
    usb = None

    try:
        # Test 1: Net.get returns valid object
        usb = Net.get(net_name, type=NetType.Usb)
        _record("net_get", usb is not None, f"type={type(usb).__name__}")

        # Test 2: Enable
        usb.enable()
        _record("enable", True)

        # Test 3: Disable
        usb.disable()
        _record("disable", True)

        # Test 4: Re-enable
        usb.enable()
        _record("re_enable", True)

    except Exception as e:
        _record("net_api", False, str(e))
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
