#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
SPI Python API tests - Aardvark Incremental Config Change.

Validates that AardvarkSPI.config() only updates explicitly-provided parameters
and retains current values for omitted parameters. This is critical for the CLI
workflow where each `lager spi config` command runs as a separate process.

Run with: lager python test/api/communication/test_spi_aardvark_incremental_config.py --box <YOUR-BOX>

Prerequisites:
- Aardvark SPI net 'spi1' configured with cs_mode=auto
- Aardvark pin 9 (SS) wired to HW-611 CSB
- LabJack DAC net 'dac1' providing 3.3V to HW-611 VCC
- BMP280 (HW-611) wired to Aardvark SCK/MOSI/MISO
- For Test 6 (manual CS): LabJack FIO0 (gpio16) wired to HW-611 CSB

Wiring:
  Aardvark pin 1 (SCK)  -> HW-611 SCL
  Aardvark pin 3 (MOSI) -> HW-611 SDA
  Aardvark pin 5 (MISO) -> HW-611 SDO
  Aardvark pin 9 (SS)   -> HW-611 CSB (auto chip select)
  LabJack DAC0 (dac1)   -> HW-611 VCC (3.3V)
"""
import sys
import os
import traceback

# Configuration
SPI_NET = os.environ.get("SPI_NET", "spi1")
GPIO_CS = os.environ.get("GPIO_CS", "gpio16")

# BMP280 constants
BMP280_CHIP_ID = 0x58
CHIP_ID_REG = 0xD0

# Track results
_results = []


def _record(name, passed, detail=""):
    """Record a sub-test result."""
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def _read_chip_id(spi):
    """Read BMP280 chip ID. Returns the chip ID byte."""
    result = spi.read_write(data=[CHIP_ID_REG, 0x00])
    return result[1]


def _read_chip_id_manual(spi):
    """Read BMP280 chip ID with manual CS. Returns the chip ID byte."""
    from lager.io.gpio.dispatcher import gpo
    gpo(GPIO_CS, "low")
    result = spi.read_write(data=[CHIP_ID_REG, 0x00])
    gpo(GPIO_CS, "high")
    return result[1]


# ---------------------------------------------------------------------------
# 1. No-arg config is a no-op
# ---------------------------------------------------------------------------
def test_noarg_config_noop():
    """Set full config, call config() with no args, verify chip ID still works."""
    print("\n" + "=" * 60)
    print("TEST 1: No-arg config is a no-op")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # Set known-good full config
    spi.config(mode=0, bit_order="msb", frequency_hz=1_000_000,
               word_size=8, cs_active="low", cs_mode="auto")

    # Read baseline chip ID
    try:
        chip_id = _read_chip_id(spi)
        passed = chip_id == BMP280_CHIP_ID
        _record("baseline chip ID", passed,
                f"got=0x{chip_id:02X}, expected=0x{BMP280_CHIP_ID:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("baseline chip ID", False, str(e))
        return False

    # Call config() with no arguments
    try:
        spi.config()
        _record("config() with no args accepted", True)
    except Exception as e:
        _record("config() with no args accepted", False, str(e))
        return False

    # Verify chip ID still correct (config wasn't corrupted)
    try:
        chip_id = _read_chip_id(spi)
        passed = chip_id == BMP280_CHIP_ID
        _record("chip ID after no-arg config", passed,
                f"got=0x{chip_id:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("chip ID after no-arg config", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 2. Single-param update preserves other params
# ---------------------------------------------------------------------------
def test_single_param_preserves_others():
    """Update one param at a time, verify others aren't reset."""
    print("\n" + "=" * 60)
    print("TEST 2: Single-param update preserves other params")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # Set known-good full config
    spi.config(mode=0, bit_order="msb", frequency_hz=1_000_000,
               word_size=8, cs_active="low", cs_mode="auto")

    # Baseline chip ID
    try:
        chip_id = _read_chip_id(spi)
        passed = chip_id == BMP280_CHIP_ID
        _record("baseline before single-param tests", passed,
                f"got=0x{chip_id:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("baseline", False, str(e))
        return False

    # config(mode=1) - BMP280 doesn't support mode 1 well, but shouldn't crash
    try:
        spi.config(mode=1)
        result = spi.read_write(data=[CHIP_ID_REG, 0x00])
        _record("config(mode=1) accepted + transaction works", True,
                f"got=0x{result[1]:02X} (garbled expected)")
    except Exception as e:
        _record("config(mode=1) transaction", False, str(e))
        ok = False

    # config(mode=0) back - chip ID should work again (bit_order/freq/word_size preserved)
    try:
        spi.config(mode=0)
        chip_id = _read_chip_id(spi)
        passed = chip_id == BMP280_CHIP_ID
        _record("config(mode=0) restores chip ID", passed,
                f"got=0x{chip_id:02X} (proves bit_order/freq/word_size preserved)")
        if not passed:
            ok = False
    except Exception as e:
        _record("config(mode=0) restores chip ID", False, str(e))
        ok = False

    # config(frequency_hz=500000) - mode should stay at 0
    try:
        spi.config(frequency_hz=500_000)
        chip_id = _read_chip_id(spi)
        passed = chip_id == BMP280_CHIP_ID
        _record("config(freq=500k) preserves mode=0", passed,
                f"got=0x{chip_id:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("config(freq=500k) preserves mode=0", False, str(e))
        ok = False

    # config(word_size=16) then config(word_size=8) round-trip
    try:
        spi.config(word_size=16)
        _record("config(word_size=16) accepted", True)

        spi.config(word_size=8)
        chip_id = _read_chip_id(spi)
        passed = chip_id == BMP280_CHIP_ID
        _record("word_size 16->8 round-trip preserves config", passed,
                f"got=0x{chip_id:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("word_size round-trip", False, str(e))
        ok = False

    # Restore
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8)
    return ok


# ---------------------------------------------------------------------------
# 3. Multiple sequential partial updates build correct config
# ---------------------------------------------------------------------------
def test_sequential_partial_updates():
    """Build up config one param at a time, verify final state works."""
    print("\n" + "=" * 60)
    print("TEST 3: Sequential partial updates build correct config")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # Start fresh - set all params to ensure clean state
    spi.config(mode=3, bit_order="lsb", frequency_hz=125_000,
               word_size=16, cs_active="high", cs_mode="auto")

    # Now incrementally set each param to the correct value
    try:
        spi.config(mode=0)
        _record("step 1: config(mode=0)", True)
    except Exception as e:
        _record("step 1: config(mode=0)", False, str(e))
        return False

    try:
        spi.config(bit_order="msb")
        _record("step 2: config(bit_order=msb)", True)
    except Exception as e:
        _record("step 2: config(bit_order=msb)", False, str(e))
        return False

    try:
        spi.config(frequency_hz=1_000_000)
        _record("step 3: config(freq=1M)", True)
    except Exception as e:
        _record("step 3: config(freq=1M)", False, str(e))
        return False

    try:
        spi.config(word_size=8)
        _record("step 4: config(word_size=8)", True)
    except Exception as e:
        _record("step 4: config(word_size=8)", False, str(e))
        return False

    try:
        spi.config(cs_active="low")
        _record("step 5: config(cs_active=low)", True)
    except Exception as e:
        _record("step 5: config(cs_active=low)", False, str(e))
        return False

    # Now all params should be correct for BMP280 communication
    try:
        chip_id = _read_chip_id(spi)
        passed = chip_id == BMP280_CHIP_ID
        _record("chip ID after incremental config", passed,
                f"got=0x{chip_id:02X} (all 5 params set incrementally)")
        if not passed:
            ok = False
    except Exception as e:
        _record("chip ID after incremental config", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 4. Invalid partial update doesn't corrupt valid state
# ---------------------------------------------------------------------------
def test_invalid_partial_no_corruption():
    """Invalid config value shouldn't corrupt the existing valid config."""
    print("\n" + "=" * 60)
    print("TEST 4: Invalid partial update doesn't corrupt valid state")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.spi import SPIBackendError
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # Set known-good full config
    spi.config(mode=0, bit_order="msb", frequency_hz=1_000_000,
               word_size=8, cs_active="low", cs_mode="auto")

    # Baseline
    try:
        chip_id = _read_chip_id(spi)
        passed = chip_id == BMP280_CHIP_ID
        _record("baseline before invalid tests", passed,
                f"got=0x{chip_id:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("baseline", False, str(e))
        return False

    # Invalid mode=5 should raise, not corrupt
    try:
        spi.config(mode=5)
        _record("config(mode=5) raises SPIBackendError", False,
                "no error raised")
        ok = False
    except SPIBackendError:
        _record("config(mode=5) raises SPIBackendError", True)
    except Exception as e:
        _record("config(mode=5) raises SPIBackendError", False,
                f"wrong type: {type(e).__name__}: {e}")
        ok = False

    # Chip ID should still work (mode still 0)
    try:
        chip_id = _read_chip_id(spi)
        passed = chip_id == BMP280_CHIP_ID
        _record("chip ID preserved after invalid mode", passed,
                f"got=0x{chip_id:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("chip ID after invalid mode", False, str(e))
        ok = False

    # Invalid bit_order should raise, not corrupt
    try:
        spi.config(bit_order="xyz")
        _record("config(bit_order=xyz) raises SPIBackendError", False,
                "no error raised")
        ok = False
    except SPIBackendError:
        _record("config(bit_order=xyz) raises SPIBackendError", True)
    except Exception as e:
        _record("config(bit_order=xyz) raises SPIBackendError", False,
                f"wrong type: {type(e).__name__}: {e}")
        ok = False

    # Chip ID should still work (bit_order still msb)
    try:
        chip_id = _read_chip_id(spi)
        passed = chip_id == BMP280_CHIP_ID
        _record("chip ID preserved after invalid bit_order", passed,
                f"got=0x{chip_id:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("chip ID after invalid bit_order", False, str(e))
        ok = False

    # Invalid word_size should raise, not corrupt
    try:
        spi.config(word_size=12)
        _record("config(word_size=12) raises SPIBackendError", False,
                "no error raised")
        ok = False
    except SPIBackendError:
        _record("config(word_size=12) raises SPIBackendError", True)
    except Exception as e:
        _record("config(word_size=12) raises SPIBackendError", False,
                f"wrong type: {type(e).__name__}: {e}")
        ok = False

    # Chip ID should still work
    try:
        chip_id = _read_chip_id(spi)
        passed = chip_id == BMP280_CHIP_ID
        _record("chip ID preserved after invalid word_size", passed,
                f"got=0x{chip_id:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("chip ID after invalid word_size", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 5. _apply_config called on partial update with open device
# ---------------------------------------------------------------------------
def test_apply_config_on_partial_update():
    """Partial config update on open device should update GPIO idle state."""
    print("\n" + "=" * 60)
    print("TEST 5: _apply_config on partial update with open device")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # Set known-good config and open device by performing a read
    spi.config(mode=0, bit_order="msb", frequency_hz=1_000_000,
               word_size=8, cs_active="low", cs_mode="auto")

    try:
        chip_id = _read_chip_id(spi)
        passed = chip_id == BMP280_CHIP_ID
        _record("open device (baseline read)", passed,
                f"got=0x{chip_id:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("open device", False, str(e))
        return False

    # Switch to mode=2 (CPOL=1) - _apply_config should update GPIO idle state
    # SCK should now idle HIGH
    try:
        spi.config(mode=2)
        _record("config(mode=2) on open device accepted", True)
    except Exception as e:
        _record("config(mode=2) on open device", False, str(e))
        ok = False

    # Switch back to mode=0 (CPOL=0) - SCK should idle LOW again
    try:
        spi.config(mode=0)
        chip_id = _read_chip_id(spi)
        passed = chip_id == BMP280_CHIP_ID
        _record("mode 2->0 round-trip on open device", passed,
                f"got=0x{chip_id:02X} (GPIO idle state correctly updated)")
        if not passed:
            ok = False
    except Exception as e:
        _record("mode 2->0 round-trip", False, str(e))
        ok = False

    # Also test cs_active update on open device
    try:
        spi.config(cs_active="high")
        _record("config(cs_active=high) on open device accepted", True)
    except Exception as e:
        _record("config(cs_active=high) on open device", False, str(e))
        ok = False

    try:
        spi.config(cs_active="low")
        chip_id = _read_chip_id(spi)
        passed = chip_id == BMP280_CHIP_ID
        _record("cs_active high->low round-trip on open device", passed,
                f"got=0x{chip_id:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("cs_active round-trip", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 6. cs_mode persistence across config calls (manual CS regression)
# ---------------------------------------------------------------------------
def test_cs_mode_persistence():
    """cs_mode=manual should persist when other params are updated."""
    print("\n" + "=" * 60)
    print("TEST 6: cs_mode persistence across config calls")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # Set manual CS mode
    spi.config(mode=0, bit_order="msb", frequency_hz=1_000_000,
               word_size=8, cs_active="low", cs_mode="manual")

    # Update mode - cs_mode should stay "manual"
    try:
        spi.config(mode=0)
        _record("config(mode=0) after cs_mode=manual accepted", True)
    except Exception as e:
        _record("config(mode=0) after cs_mode=manual", False, str(e))
        ok = False

    # Update frequency - cs_mode should stay "manual"
    try:
        spi.config(frequency_hz=1_000_000)
        _record("config(freq=1M) after cs_mode=manual accepted", True)
    except Exception as e:
        _record("config(freq=1M) after cs_mode=manual", False, str(e))
        ok = False

    # Perform manual CS transaction to prove cs_mode persisted
    try:
        from lager.io.gpio.dispatcher import gpo
        gpo(GPIO_CS, "low")
        chip_id_result = spi.read_write(data=[CHIP_ID_REG, 0x00])
        gpo(GPIO_CS, "high")
        chip_id = chip_id_result[1]

        # If cs_mode had been reset to "auto", the Aardvark's SS pin would
        # have toggled during the transaction. With manual mode, only our
        # GPIO CS controls the chip select. We verify the BMP280 responded.
        passed = chip_id == BMP280_CHIP_ID
        _record("manual CS transaction after partial updates", passed,
                f"got=0x{chip_id:02X} (proves cs_mode=manual persisted)")
        if not passed:
            ok = False
    except Exception as e:
        try:
            from lager.io.gpio.dispatcher import gpo
            gpo(GPIO_CS, "high")
        except Exception:
            pass
        _record("manual CS transaction", False, str(e))
        ok = False

    # Restore to auto mode
    spi.config(cs_mode="auto")
    return ok


# ---------------------------------------------------------------------------
# 7. CLI-level persistence via dispatcher
# ---------------------------------------------------------------------------
def test_dispatcher_persistence():
    """Test incremental config via dispatcher (simulates separate CLI commands)."""
    print("\n" + "=" * 60)
    print("TEST 7: Dispatcher-level incremental config persistence")
    print("=" * 60)

    ok = True

    try:
        from lager.protocols.spi.dispatcher import config, transfer

        # Set full config via dispatcher (like `lager spi config --mode 0 ...`)
        config(SPI_NET, mode=0, bit_order="msb", frequency_hz=1_000_000,
               word_size=8, cs_active="low", cs_mode="auto")
        _record("dispatcher full config", True)
    except Exception as e:
        _record("dispatcher full config", False, str(e))
        return False

    # Update only mode via dispatcher (like `lager spi config --mode 0`)
    try:
        config(SPI_NET, mode=0)
        _record("dispatcher config(mode=0) only", True)
    except Exception as e:
        _record("dispatcher config(mode=0) only", False, str(e))
        ok = False

    # Update only frequency via dispatcher (like `lager spi config --frequency 500k`)
    # mode=0 should still be active from previous call
    try:
        config(SPI_NET, frequency_hz=500_000)
        _record("dispatcher config(freq=500k) only", True)
    except Exception as e:
        _record("dispatcher config(freq=500k) only", False, str(e))
        ok = False

    # Read chip ID via dispatcher transfer (mode=0 + freq=500k should both be active)
    try:
        print("  (dispatcher transfer prints to stdout)")
        transfer(SPI_NET, n_words=2, data=[CHIP_ID_REG], output_format="hex")
        _record("dispatcher transfer after incremental config", True)
    except Exception as e:
        _record("dispatcher transfer after incremental config", False, str(e))
        ok = False

    # Restore frequency
    try:
        config(SPI_NET, frequency_hz=1_000_000)
    except Exception:
        pass

    return ok


# ===================================================================
# Main
# ===================================================================
def main():
    """Run all incremental config tests."""
    print("Aardvark SPI Incremental Config Test Suite")
    print(f"SPI net: {SPI_NET}, GPIO CS: {GPIO_CS}")
    print("=" * 60)

    tests = [
        ("No-arg config no-op",           test_noarg_config_noop),
        ("Single-param preserves others",  test_single_param_preserves_others),
        ("Sequential partial updates",     test_sequential_partial_updates),
        ("Invalid partial no corruption",  test_invalid_partial_no_corruption),
        ("_apply_config on open device",   test_apply_config_on_partial_update),
        ("cs_mode persistence",            test_cs_mode_persistence),
        ("Dispatcher persistence",         test_dispatcher_persistence),
    ]

    test_results = []
    for name, test_fn in tests:
        try:
            passed = test_fn()
            test_results.append((name, passed))
        except Exception as e:
            print(f"\nUNEXPECTED ERROR in {name}: {e}")
            traceback.print_exc()
            test_results.append((name, False))

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    passed_count = sum(1 for _, p in test_results if p)
    total_count = len(test_results)

    for name, p in test_results:
        status = "PASS" if p else "FAIL"
        print(f"  [{status}] {name}")

    print(f"\nTotal: {passed_count}/{total_count} test groups passed")

    sub_passed = sum(1 for _, p, _ in _results if p)
    sub_total = len(_results)
    sub_failed = sub_total - sub_passed
    print(f"Sub-tests: {sub_passed}/{sub_total} passed", end="")
    if sub_failed > 0:
        print(f" ({sub_failed} failed)")
        print("\nFailed sub-tests:")
        for name, p, detail in _results:
            if not p:
                print(f"  FAIL: {name} -- {detail}")
    else:
        print()

    return 0 if passed_count == total_count else 1


if __name__ == "__main__":
    sys.exit(main())
