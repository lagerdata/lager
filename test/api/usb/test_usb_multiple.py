# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_usb_multiple.py
# Run with: lager python test_usb_multiple.py --box <YOUR-BOX>

import os
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

    print("=== USB Multiple Port Control Test ===\n")

    # Only the ports named in USB_TOGGLE_NETS (default usb1): the other ports
    # on a bench hub power probes and peers that must not lose power.
    usb_ports = [n.strip() for n in os.environ.get("USB_TOGGLE_NETS", "usb1").split(",") if n.strip()]

    try:
        # Test 1: Disable all ports
        print("Disabling all ports...")
        for port in usb_ports:
            try:
                usb = Net.get(port, type=NetType.Usb)
                usb.disable()
                _record(f"{port}_disable", usb.state() is False, f"state()={usb.state()!r}")
            except Exception as e:
                _record(f"{port}_disable", False, str(e))

        time.sleep(0.5)

        # Test 2: Enable all ports
        print("\nEnabling all ports...")
        for port in usb_ports:
            try:
                usb = Net.get(port, type=NetType.Usb)
                usb.enable()
                _record(f"{port}_enable", usb.state() is True, f"state()={usb.state()!r}")
            except Exception as e:
                _record(f"{port}_enable", False, str(e))

    finally:
        # Safety: enable all ports
        for port in usb_ports:
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
