# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_usb_comprehensive.py
# Run with: lager python test_usb_comprehensive.py --box <YOUR-BOX>

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

    print("=== Comprehensive USB Test ===\n")

    # Test 1: list_saved returns USB nets
    all_nets = Net.list_saved()
    _record("list_saved_returns_list", isinstance(all_nets, list),
            f"type={type(all_nets).__name__}")

    usb_nets = sorted([n['name'] for n in all_nets if n.get('role') == 'usb'])
    _record("usb_nets_found", len(usb_nets) > 0,
            f"found {len(usb_nets)}: {', '.join(usb_nets)}")

    if not usb_nets:
        total = len(_results)
        passed = sum(1 for _, p, _ in _results if p)
        print(f"\nRESULTS: {passed}/{total} passed, {total - passed} failed")
        return 1

    # Test 2: Per-port disable/enable cycle
    for port in usb_nets:
        try:
            usb = Net.get(port, type=NetType.Usb)
            _record(f"{port}_net_get", usb is not None, f"type={type(usb).__name__}")
            usb.disable()
            time.sleep(0.1)
            usb.enable()
            time.sleep(0.1)
            usb.disable()
            time.sleep(0.1)
            _record(f"{port}_disable_enable_cycle", True)
        except Exception as e:
            _record(f"{port}_disable_enable_cycle", False, str(e))

    # Safety: enable all ports
    print("\n  Enabling all ports (safety)...")
    for port in usb_nets:
        try:
            usb = Net.get(port, type=NetType.Usb)
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
