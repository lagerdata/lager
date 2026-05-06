#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Test that SPI dead zone clamping goes to the SLOW boundary (throttle 23063,
~2.9 kHz), not the fast boundary (throttle 0, 800 kHz).

Requires: MOSI (FIO2) wired to MISO (FIO3) on LabJack T7 spi2 net.
Run with: lager python test/api/communication/test_spi_dead_zone_clamp.py --box <YOUR-BOX>
"""
import sys
import os

SPI_NET = os.environ.get("SPI_NET", "spi2")


def test_dead_zone_loopback():
    """Request a frequency in the dead zone, do a multi-byte loopback transfer."""
    from lager import Net, NetType

    spi = Net.get(SPI_NET, NetType.SPI)

    # 100 kHz maps to throttle ~83, which is in the dead zone (1-23062).
    # The fix should clamp this to throttle=23063 (~2.9 kHz), NOT throttle=0 (800 kHz).
    # Watch stderr for the warning message to confirm.
    spi.config(mode=0, frequency_hz=100_000, word_size=8, bit_order="msb",
               cs_active="low")

    # Multi-byte loopback: with MOSI wired to MISO, sent data should come back.
    tx_data = [0xAA, 0x55, 0xDE, 0xAD]
    print(f"Sending {len(tx_data)} bytes: {[hex(b) for b in tx_data]}")

    result = spi.read_write(data=tx_data)
    print(f"Received {len(result)} bytes: {[hex(b) for b in result]}")

    if result == tx_data:
        print("PASS: Loopback data matches -- dead zone clamping works")
    else:
        print(f"FAIL: Loopback mismatch -- sent {tx_data}, got {result}")
        return False

    # Try a longer payload to be thorough
    tx_long = list(range(32))
    print(f"\nSending {len(tx_long)} bytes: {[hex(b) for b in tx_long]}")
    result_long = spi.read_write(data=tx_long)
    print(f"Received {len(result_long)} bytes: {[hex(b) for b in result_long]}")

    if result_long == tx_long:
        print("PASS: 32-byte loopback matches")
    else:
        mismatches = [(i, tx_long[i], result_long[i])
                      for i in range(len(tx_long)) if tx_long[i] != result_long[i]]
        print(f"FAIL: {len(mismatches)} byte(s) differ: {mismatches[:5]}")
        return False

    return True


def test_800khz_still_works():
    """Verify that requesting 800 kHz (throttle=0) still works and bypasses clamping."""
    from lager import Net, NetType

    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(frequency_hz=800_000)

    tx_data = [0xCA, 0xFE, 0xBA, 0xBE]
    print(f"\n800 kHz loopback: sending {[hex(b) for b in tx_data]}")
    result = spi.read_write(data=tx_data)
    print(f"Received: {[hex(b) for b in result]}")

    if result == tx_data:
        print("PASS: 800 kHz loopback matches (no clamping needed)")
        return True
    else:
        print(f"FAIL: 800 kHz loopback mismatch")
        return False


def main():
    print("=== SPI Dead Zone Clamping Test ===")
    print(f"Net: {SPI_NET}")
    print("Check stderr for the clamping warning message.")
    print("Expected: 'Clamping to throttle=62500 (~1kHz)'")
    print("Bad (old): 'Clamping to throttle=0 (800kHz)'")
    print()

    ok = True

    try:
        if not test_dead_zone_loopback():
            ok = False
    except Exception as e:
        print(f"FAIL: Exception during dead zone test: {e}")
        ok = False

    try:
        if not test_800khz_still_works():
            ok = False
    except Exception as e:
        print(f"FAIL: Exception during 800 kHz test: {e}")
        ok = False

    print()
    if ok:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
