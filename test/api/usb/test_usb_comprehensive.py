# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_usb_comprehensive.py
# Run with: lager python test_usb_comprehensive.py --box <YOUR-BOX> [--env USB_TOGGLE_NETS=usb1]
#
# Lists the box's USB nets, then power-cycles ONLY the ones named in
# USB_TOGGLE_NETS (comma-separated, default usb1), reading each port's state
# back after every change. Other ports on a bench hub power probes and peers
# that must not lose power. Every port it touches is left enabled.

import os
import sys
import time

TOGGLE_NETS = [n.strip() for n in os.environ.get("USB_TOGGLE_NETS", "usb1").split(",") if n.strip()]

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

    # Test 2: disable / enable cycle on the allowed ports, each read back
    missing = [n for n in TOGGLE_NETS if n not in usb_nets]
    _record("toggle_nets_exist", not missing,
            f"allowed {', '.join(TOGGLE_NETS)}; missing {', '.join(missing) or 'none'}")
    toggled = [n for n in TOGGLE_NETS if n in usb_nets]
    try:
        for port in toggled:
            try:
                usb = Net.get(port, type=NetType.Usb)
                _record(f"{port}_net_get", usb is not None, f"type={type(usb).__name__}")
                usb.disable()
                time.sleep(0.1)
                off = usb.state()
                usb.enable()
                time.sleep(0.1)
                on = usb.state()
                _record(f"{port}_disable_reads_off", off is False, f"state()={off!r}")
                _record(f"{port}_enable_reads_on", on is True, f"state()={on!r}")
            except Exception as e:
                _record(f"{port}_disable_enable_cycle", False, str(e))
    finally:
        # Safety: leave every toggled port enabled
        for port in toggled:
            try:
                Net.get(port, type=NetType.Usb).enable()
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
