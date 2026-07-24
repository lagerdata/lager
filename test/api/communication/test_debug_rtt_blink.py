#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Flash-to-RTT round trip for a Debug net (J-Link).

Proves the loop a user actually runs end to end: flash firmware, reset the
target, then read the running firmware's output back over RTT.

test_debug_comprehensive.py exercises flash(), reset() and rtt() individually,
but its RTT check records a PASS on a zero-byte read ("no data (expected
without RTT firmware)"), and its flash checks self-skip when no firmware was
uploaded. Firmware that flashes but never boots therefore looks green there.
This test asserts that real output actually appeared, and fails -- rather than
skipping -- when the firmware is missing.

Hardware Required:
  - J-Link probe connected to the box
  - Target MCU powered and wired (SWD/JTAG)
  - A net of type=NetType.Debug
  - RTT-capable firmware printing `blink <N> period=<MS>ms` on RTT channel 0

Run with:
  lager python test/api/communication/test_debug_rtt_blink.py --box <YOUR-BOX> \\
      --add-file path/to/firmware.hex

Environment variables:
  DEBUG_NET          - net name (default: debug1)
  FIRMWARE_PATH      - firmware file name (default: firmware.hex)
  RTT_WINDOW_S       - seconds to capture RTT for (default: 8)
  EXPECTED_PERIOD_MS - if set, additionally assert this exact period value
"""
import os
import re
import sys
import time

DEBUG_NET = os.environ.get("DEBUG_NET", "debug1")
FIRMWARE_PATH = os.environ.get("FIRMWARE_PATH", "firmware.hex")
RTT_WINDOW_S = float(os.environ.get("RTT_WINDOW_S", "8"))
_expected = os.environ.get("EXPECTED_PERIOD_MS")
EXPECTED_PERIOD_MS = int(_expected) if _expected else None

# Matches the firmware's plain-text RTT line, e.g. "blink 12 period=700ms".
BLINK_RE = re.compile(r"blink\s+(\d+)\s+period=(\d+)ms")


def check_rtt_output(text, expected_period_ms=None):
    """Validate captured RTT text. Pure -- unit-testable without hardware.

    Asserts the SHAPE `blink <N> period=<MS>ms` rather than a hardcoded period,
    so this repo is not coupled to one firmware build. Set EXPECTED_PERIOD_MS to
    pin the value.

    Returns (ok: bool, detail: str).
    """
    match = BLINK_RE.search(text)
    if not match:
        preview = text[:200].replace("\n", "\\n") if text else "<empty>"
        return False, (
            f"no 'blink <N> period=<MS>ms' line in {len(text)}B of RTT output; "
            f"got: {preview!r}"
        )
    count, period = int(match.group(1)), int(match.group(2))
    if expected_period_ms is not None and period != expected_period_ms:
        return False, f"expected period={expected_period_ms}ms, got period={period}ms"
    return True, f"matched 'blink {count} period={period}ms'"


def main():
    print("Debug Net RTT Blink Round-Trip")
    print(f"Net:       {DEBUG_NET}")
    print(f"Firmware:  {FIRMWARE_PATH}")
    print(f"Window:    {RTT_WINDOW_S}s")
    print(f"Expected:  {EXPECTED_PERIOD_MS if EXPECTED_PERIOD_MS else 'any period'}")
    print("=" * 60)

    # Deliberately fatal, not a skip: this test exists to prove the flash->RTT
    # loop, so a missing upload is a failure to report, not a reason to pass.
    if not os.path.exists(FIRMWARE_PATH):
        print(f"FAIL: firmware '{FIRMWARE_PATH}' not found -- pass it with --add-file")
        return 1

    from lager import Net, NetType

    debug = Net.get(DEBUG_NET, type=NetType.Debug)

    # session() connects on entry and guarantees teardown on exit; flash/reset
    # self-heal across the post-flash settling window.
    with debug.session() as session:
        print("Flashing...")
        session.flash(FIRMWARE_PATH)
        print("Resetting...")
        session.reset()

        print(f"Capturing RTT channel 0 for {RTT_WINDOW_S}s...")
        buf = b""
        with session.rtt(channel=0) as rtt:
            deadline = time.time() + RTT_WINDOW_S
            while time.time() < deadline:
                chunk = rtt.read_some(timeout=0.5)
                if chunk:
                    buf += chunk
                    if BLINK_RE.search(buf.decode("utf-8", errors="replace")):
                        break  # got what we need; no reason to burn the window

    text = buf.decode("utf-8", errors="replace")
    print("-" * 60)
    print(text if text else "<no RTT output captured>")
    print("-" * 60)

    ok, detail = check_rtt_output(text, EXPECTED_PERIOD_MS)
    print(f"{'PASS' if ok else 'FAIL'}: {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
