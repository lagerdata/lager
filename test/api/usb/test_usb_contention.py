# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_usb_contention.py
# Run with: lager python test/api/usb/test_usb_contention.py --box <YOUR-BOX>
#
# Regression test for the USB-hub "OSError: open failed" bug. This script runs
# in its OWN process on the box (separate from box_http_server, the MCP server,
# etc.). Before the fix, if box_http_server had opened the hub (e.g. from a
# `lager usb ...` command) it pinned the exclusive libusb claim and this fresh
# process could not open the same hub. After the fix each op opens→operates→
# releases under a shared lock, so this always works.
#
# HOW TO USE (see the driver test plan):
#   1) lager usb <NET> enable --box <BOX>     # make box_http_server touch the hub
#   2) lager python test/api/usb/test_usb_contention.py --box <BOX>
#
# Edit NET below to a real USB net on your box (`lager nets --box <BOX>`):
#   YKUSH example:    "CLI_USB"
#   Acroname example: whatever you named the Acroname port net

NET = "CLI_USB"        # <-- EDIT to your USB net name
CYCLES = 20
DELAY = 0.1

import sys
import time


def main():
    from lager import Net, NetType

    print(f"=== USB contention test on net '{NET}' ===\n")

    # 1) Fresh open in THIS process must succeed even if box_http_server just
    #    used the hub. This single line is the exact thing that used to raise
    #    "OSError: open failed".
    try:
        usb = Net.get(NET, type=NetType.Usb)
        start = usb.state()
    except Exception as e:
        print(f"[FAIL] could not open the hub from a fresh process: "
              f"{type(e).__name__}: {e}")
        return 1
    print(f"[PASS] opened hub from a fresh process; '{NET}' is "
          f"{'on' if start else 'off'}\n")

    # 2) Stress: many open→operate→release cycles, each a fresh handle under the
    #    cross-process lock. Any residual contention shows up as an error here.
    errors = 0
    for i in range(CYCLES):
        try:
            new_state = usb.toggle()
            print(f"  cycle {i + 1}/{CYCLES}: toggled -> "
                  f"{'on' if new_state else 'off'}")
        except Exception as e:
            errors += 1
            print(f"  cycle {i + 1}/{CYCLES}: FAIL {type(e).__name__}: {e}")
        time.sleep(DELAY)

    # Restore original state (best effort).
    try:
        if usb.state() != start:
            usb.toggle()
    except Exception:
        pass

    if errors:
        print(f"\n[FAIL] {errors}/{CYCLES} cycles errored")
        return 1
    print(f"\n[PASS] {CYCLES}/{CYCLES} open/operate/release cycles succeeded "
          f"— no contention errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
