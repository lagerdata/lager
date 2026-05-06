#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Simple wait_for_level test with LabJack manual SPI + GPIO.

Run with: lager python test/api/communication/test_wait_for_level_simple.py --box <YOUR-BOX>

Validates wait_for_level using only the Net/NetType API alongside a
manual-CS SPI transaction (BMP280 chip ID read on spi2/gpio16).
"""
import sys

SPI_NET = "spi2"
GPIO_CS = "gpio22"
BMP280_CHIP_ID = 0x58
CHIP_ID_REG = 0xD0

passed = 0
failed = 0


def run(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  PASS: {name}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {name} -- {type(e).__name__}: {e}")
        failed += 1


def main():
    from lager import Net, NetType

    spi = Net.get(SPI_NET, NetType.SPI)
    gpio = Net.get(GPIO_CS, NetType.GPIO)

    spi.config(mode=0, frequency_hz=800_000, word_size=8, bit_order="msb",
               cs_active="low", cs_mode="manual")

    # Force the LabJack SPI warm-up sequence BEFORE asserting CS.
    # The first _execute_transaction() runs 14 one-byte warm-up SPI
    # transactions to stabilize the hardware.  If CS is already LOW,
    # those bytes corrupt the BMP280's SPI state machine.
    gpio.output("high")
    spi.read_write(data=[0x00, 0x00])

    print("=" * 60)
    print("wait_for_level simple test (Net/NetType API only)")
    print("=" * 60)

    # 1. Manual CS SPI read -- BMP280 chip ID
    def test_manual_cs_chip_id():
        gpio.output("low")
        result = spi.read_write(data=[CHIP_ID_REG, 0x00])
        gpio.output("high")
        assert result[1] == BMP280_CHIP_ID, \
            f"expected 0x{BMP280_CHIP_ID:02X}, got 0x{result[1]:02X}"

    run("manual CS SPI chip ID = 0x58", test_manual_cs_chip_id)

    # 2. wait_for_level(HIGH) -- pin idles HIGH via internal pull-up
    def test_wait_high():
        elapsed = gpio.wait_for_level(1, timeout=2)
        assert isinstance(elapsed, float) and elapsed < 0.5, \
            f"expected < 0.5s, got {elapsed:.4f}s"

    run("wait_for_level(HIGH) returns in < 0.5s", test_wait_high)

    # 3. wait_for_level(LOW) timeout -- no driver pulls pin LOW
    def test_wait_low_timeout():
        try:
            gpio.wait_for_level(0, timeout=0.5)
            assert False, "should have raised TimeoutError"
        except TimeoutError:
            pass  # expected

    run("wait_for_level(LOW) raises TimeoutError", test_wait_low_timeout)

    # Restore CS high
    gpio.output("high")

    # Summary
    total = passed + failed
    print("\n" + "=" * 60)
    print(f"RESULTS: {passed}/{total} passed, {failed} failed")
    print("=" * 60)
    if failed == 0:
        print("\nAll tests passed!")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
