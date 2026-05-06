#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Test SPI register write-then-readback using BMP280 ctrl_meas register.

Run with: lager python test/api/communication/test_spi_write_readback.py --box <YOUR-BOX>

Verifies that a single-process write+read works (no driver reinitialization between ops).
"""
import sys
import os

SPI_NET = os.environ.get("SPI_NET", "spi1")


def main():
    from lager import Net, NetType

    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8, cs_active="low")

    # Write 0x27 to ctrl_meas (register 0xF4, write addr = 0x74)
    spi.write(data=[0x74, 0x27])

    # Read it back (register 0xF4, read addr = 0xF4)
    result = spi.read_write(data=[0xF4, 0x00])
    readback = result[1]

    print(f"Wrote: 0x27, Read back: 0x{readback:02X}")
    if readback == 0x27:
        print("PASS")
        return 0
    else:
        print("FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
