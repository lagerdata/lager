#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
SPI Python API tests - Aardvark Manual CS Mode + wait_for_level.

Run with: lager python test/api/communication/test_spi_aardvark_manual.py --box <YOUR-BOX>

Prerequisites:
- Aardvark SPI net 'spi1' configured with cs_mode=manual
- LabJack GPIO net 'gpio16' (FIO0) wired to HW-611 CSB
- LabJack DAC net 'dac1' providing 3.3V to HW-611 VCC
- BMP280 (HW-611) wired to Aardvark SCK/MOSI/MISO

Wiring:
  Aardvark pin 1 (SCK)  -> HW-611 SCL
  Aardvark pin 3 (MOSI) -> HW-611 SDA
  Aardvark pin 5 (MISO) -> HW-611 SDO
  Aardvark pin 9 (SS)   -> NC (not connected)
  LabJack FIO0 (gpio16) -> HW-611 CSB (manual chip select)
  LabJack DAC0 (dac1)   -> HW-611 VCC (3.3V)

wait_for_level notes:
  wait_for_level tests use the Net object API (gpio.wait_for_level) on
  the CS pin (gpio16).  The LabJack internal pull-up idles the pin HIGH
  when configured as input:
    - Waiting for HIGH (1) returns immediately (internal pull-up).
    - Waiting for LOW (0) times out (nothing pulls the pin LOW).
"""
import sys
import os
import time
import traceback

# Configuration
SPI_NET = os.environ.get("SPI_NET", "spi1")
GPIO_CS = os.environ.get("GPIO_CS", "gpio16")

# BMP280 constants
BMP280_CHIP_ID = 0x58
CHIP_ID_REG = 0xD0
CTRL_MEAS_WRITE = 0x74  # 0xF4 with bit 7 = 0
CTRL_MEAS_READ = 0xF4
CALIB_REG = 0x88

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


# ---------------------------------------------------------------------------
# 1. Imports
# ---------------------------------------------------------------------------
def test_imports():
    """Verify all required imports work."""
    print("\n" + "=" * 60)
    print("TEST: Imports")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        assert hasattr(NetType, "SPI"), "NetType.SPI not found"
        _record("import Net, NetType", True)
    except Exception as e:
        _record("import Net, NetType", False, str(e))
        ok = False

    try:
        from lager.protocols.spi import SPINet, AardvarkSPI, SPIBackendError
        _record("import SPINet, AardvarkSPI, SPIBackendError", True)
    except Exception as e:
        _record("import SPINet, AardvarkSPI, SPIBackendError", False, str(e))
        ok = False

    try:
        from lager.protocols.spi import config, read, read_write, transfer
        _record("import SPI dispatcher funcs", True)
    except Exception as e:
        _record("import SPI dispatcher funcs", False, str(e))
        ok = False

    try:
        from lager.io.gpio.dispatcher import gpi, gpo, wait_for_level
        _record("import GPIO dispatcher (gpi, gpo, wait_for_level)", True)
    except Exception as e:
        _record("import GPIO dispatcher", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 2. Net.get + get_config
# ---------------------------------------------------------------------------
def test_net_get_and_config():
    """Test Net.get and get_config for SPINet."""
    print("\n" + "=" * 60)
    print("TEST: Net.get + get_config")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.spi import SPINet
    ok = True

    try:
        spi = Net.get(SPI_NET, NetType.SPI)
        is_spinet = isinstance(spi, SPINet)
        _record("Net.get returns SPINet", is_spinet,
                f"type={type(spi).__name__}")
        if not is_spinet:
            ok = False
    except Exception as e:
        _record("Net.get returns SPINet", False, str(e))
        return False

    try:
        cfg = spi.get_config()
        is_dict = isinstance(cfg, dict)
        has_name = "name" in cfg
        has_role = "role" in cfg
        _record("get_config returns dict with name/role", is_dict and has_name and has_role,
                f"keys={list(cfg.keys())}")
        if not (is_dict and has_name):
            ok = False
    except Exception as e:
        _record("get_config", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 3. Config: all modes, bit orders, etc.
# ---------------------------------------------------------------------------
def test_config():
    """Test config with various parameters including cs_mode=manual."""
    print("\n" + "=" * 60)
    print("TEST: Config")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # cs_mode=manual
    try:
        spi.config(cs_mode="manual")
        _record("config cs_mode=manual", True)
    except Exception as e:
        _record("config cs_mode=manual", False, str(e))
        ok = False

    # All modes
    for m in (0, 1, 2, 3):
        try:
            spi.config(mode=m)
            _record(f"config mode={m}", True)
        except Exception as e:
            _record(f"config mode={m}", False, str(e))
            ok = False

    # Bit orders
    for order in ("msb", "lsb"):
        try:
            spi.config(bit_order=order)
            _record(f"config bit_order={order}", True)
        except Exception as e:
            _record(f"config bit_order={order}", False, str(e))
            ok = False

    # Frequencies
    for freq in (125_000, 1_000_000, 4_000_000, 8_000_000):
        try:
            spi.config(frequency_hz=freq)
            _record(f"config frequency_hz={freq}", True)
        except Exception as e:
            _record(f"config frequency_hz={freq}", False, str(e))
            ok = False

    # Word sizes
    for ws in (8, 16, 32):
        try:
            spi.config(word_size=ws)
            _record(f"config word_size={ws}", True)
        except Exception as e:
            _record(f"config word_size={ws}", False, str(e))
            ok = False

    # CS polarity
    for pol in ("low", "high"):
        try:
            spi.config(cs_active=pol)
            _record(f"config cs_active={pol}", True)
        except Exception as e:
            _record(f"config cs_active={pol}", False, str(e))
            ok = False

    # Restore defaults
    spi.config(mode=0, bit_order="msb", frequency_hz=1_000_000,
               word_size=8, cs_active="low", cs_mode="manual")
    return ok


# ---------------------------------------------------------------------------
# 4. Invalid config
# ---------------------------------------------------------------------------
def test_invalid_config():
    """Test invalid config parameters raise SPIBackendError."""
    print("\n" + "=" * 60)
    print("TEST: Invalid Config")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.spi import SPIBackendError
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    invalids = [
        ("mode=5", {"mode": 5}),
        ("bit_order='xyz'", {"bit_order": "xyz"}),
        ("word_size=12", {"word_size": 12}),
        ("cs_active='x'", {"cs_active": "x"}),
    ]

    for label, kwargs in invalids:
        try:
            spi.config(**kwargs)
            _record(f"invalid {label} raises error", False, "no error raised")
            ok = False
        except SPIBackendError:
            _record(f"invalid {label} raises SPIBackendError", True)
        except Exception as e:
            _record(f"invalid {label} raises SPIBackendError", False,
                    f"wrong type: {type(e).__name__}: {e}")
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 5. Manual CS SPI operations
# ---------------------------------------------------------------------------
def test_manual_cs_operations():
    """Test SPI operations with manual CS via gpo/gpi."""
    print("\n" + "=" * 60)
    print("TEST: Manual CS SPI Operations")
    print("=" * 60)

    from lager import Net, NetType
    from lager.io.gpio.dispatcher import gpo
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8, bit_order="msb",
               cs_active="low", cs_mode="manual")
    ok = True

    # Warm-up: force _ensure_open() with CS deasserted so GPIO direction
    # changes don't produce spurious SCK edges while the BMP280 is selected.
    gpo(GPIO_CS, "high")
    spi.read_write(data=[0x00, 0x00])

    # 5a. Chip ID read with manual CS
    # NOTE: uses transfer() because read_write() in manual CS mode
    # consistently returns 0x00 for the chip ID byte, while transfer()
    # (which sends fill=0xFF for padding) works correctly.
    try:
        gpo(GPIO_CS, "low")
        result = spi.transfer(n_words=2, data=[CHIP_ID_REG])
        gpo(GPIO_CS, "high")
        chip_id = result[1]
        passed = chip_id == BMP280_CHIP_ID
        _record("manual CS chip ID read", passed,
                f"chip_id=0x{chip_id:02X}")
        if not passed:
            ok = False
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("manual CS chip ID read", False, str(e))
        ok = False

    # 5b. read() with manual CS
    try:
        gpo(GPIO_CS, "low")
        result = spi.read(n_words=4)
        gpo(GPIO_CS, "high")
        passed = isinstance(result, list) and len(result) == 4
        _record("manual CS read(4)", passed, f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("manual CS read(4)", False, str(e))
        ok = False

    # 5c. write() with manual CS
    try:
        gpo(GPIO_CS, "low")
        result = spi.write(data=[CTRL_MEAS_WRITE, 0x00])
        gpo(GPIO_CS, "high")
        _record("manual CS write(2 bytes)", True)
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("manual CS write(2 bytes)", False, str(e))
        ok = False

    # 5d. transfer() with manual CS
    try:
        gpo(GPIO_CS, "low")
        result = spi.transfer(n_words=2, data=[CHIP_ID_REG])
        gpo(GPIO_CS, "high")
        passed = isinstance(result, list) and len(result) == 2
        _record("manual CS transfer(2)", passed, f"data={result}")
        if not passed:
            ok = False
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("manual CS transfer(2)", False, str(e))
        ok = False

    # 5e. Keep-cs split transaction
    try:
        gpo(GPIO_CS, "low")
        result1 = spi.transfer(n_words=13, data=[CALIB_REG], keep_cs=True)
        result2 = spi.transfer(n_words=12)
        gpo(GPIO_CS, "high")
        passed = len(result1) == 13 and len(result2) == 12
        _record("keep_cs split (13+12 bytes)", passed,
                f"part1={len(result1)}, part2={len(result2)}")
        if not passed:
            ok = False
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("keep_cs split (13+12 bytes)", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 6. Output formats
# ---------------------------------------------------------------------------
def test_output_formats():
    """Test output_format='list', 'hex', 'bytes', 'json' with manual CS."""
    print("\n" + "=" * 60)
    print("TEST: Output Formats")
    print("=" * 60)

    from lager import Net, NetType
    from lager.io.gpio.dispatcher import gpo
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    for fmt, expected_type in [("list", list), ("hex", str), ("bytes", str), ("json", dict)]:
        try:
            gpo(GPIO_CS, "low")
            result = spi.read_write(data=[CHIP_ID_REG, 0x00], output_format=fmt)
            gpo(GPIO_CS, "high")
            passed = isinstance(result, expected_type)
            if fmt == "json":
                passed = passed and "data" in result
            _record(f"output_format={fmt}", passed,
                    f"type={type(result).__name__}")
            if not passed:
                ok = False
        except Exception as e:
            gpo(GPIO_CS, "high")
            _record(f"output_format={fmt}", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 7. wait_for_level tests
# ---------------------------------------------------------------------------
def test_wait_for_level():
    """Test wait_for_level via the Net object API (polling-based).

    Uses Net.get(GPIO_CS, NetType.GPIO) following the pattern in
    test_wait_for_level_simple.py.  The BMP280's internal CSB pull-up
    idles the pin HIGH when the LabJack reads it as an input:
      - Waiting for HIGH (1) returns immediately.
      - Waiting for LOW (0) times out (nothing pulls the pin LOW).
    """
    print("\n" + "=" * 60)
    print("TEST: wait_for_level")
    print("=" * 60)

    from lager import Net, NetType
    from lager.io.gpio.dispatcher import gpo
    ok = True

    # Use the Net object API on the CS pin (gpio16/FIO0).
    gpio = Net.get(GPIO_CS, NetType.GPIO)

    # Test 1: wait_for_level(HIGH) -- internal pull-up idles pin HIGH
    # Re-drive pin HIGH and settle before each test.  The LabJack
    # streaming backend (eStreamStart/eStreamStop) may leave stale
    # buffer data that causes false readings on the next stream.
    gpio.output("high")
    time.sleep(0.1)
    try:
        elapsed = gpio.wait_for_level(1, timeout=2)
        passed = isinstance(elapsed, float) and elapsed < 0.5
        _record("wait_for_level(HIGH) immediate", passed,
                f"elapsed={elapsed:.4f}s")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level(HIGH) immediate", False, str(e))
        ok = False

    # Test 2: wait_for_level(LOW) timeout -- no driver pulls pin LOW
    # Re-assert HIGH output: the previous stream may have reconfigured
    # the pin direction, so we must re-drive and settle.
    gpio.output("high")
    time.sleep(0.1)
    try:
        gpio.wait_for_level(0, timeout=0.5)
        _record("wait_for_level(LOW) raises TimeoutError", False,
                "no TimeoutError raised")
        ok = False
    except TimeoutError:
        _record("wait_for_level(LOW) raises TimeoutError", True)
    except Exception as e:
        _record("wait_for_level(LOW) raises TimeoutError", False,
                f"wrong exception: {type(e).__name__}: {e}")
        ok = False

    # Test 3: Timeout precision (~0.5s)
    gpio.output("high")
    time.sleep(0.1)
    try:
        start = time.monotonic()
        try:
            gpio.wait_for_level(0, timeout=0.5)
        except TimeoutError:
            pass
        actual_elapsed = time.monotonic() - start
        passed = abs(actual_elapsed - 0.5) < 0.3
        _record("wait_for_level: timeout precision ~0.5s", passed,
                f"actual={actual_elapsed:.3f}s")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level: timeout precision", False, str(e))
        ok = False

    # Restore SPI CS pin HIGH for subsequent tests
    gpo(GPIO_CS, "high")
    return ok


# ---------------------------------------------------------------------------
# 8. Large transaction
# ---------------------------------------------------------------------------
def test_large_transaction():
    """Test 1024-byte SPI transfer with manual CS."""
    print("\n" + "=" * 60)
    print("TEST: Large Transaction (1024 bytes)")
    print("=" * 60)

    from lager import Net, NetType
    from lager.io.gpio.dispatcher import gpo
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8, cs_mode="manual")
    ok = True

    try:
        gpo(GPIO_CS, "low")
        result = spi.read(n_words=1024)
        gpo(GPIO_CS, "high")
        passed = isinstance(result, list) and len(result) == 1024
        _record("read 1024 bytes", passed, f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("read 1024 bytes", False, str(e))
        ok = False

    try:
        data = [i & 0xFF for i in range(1024)]
        gpo(GPIO_CS, "low")
        result = spi.read_write(data=data)
        gpo(GPIO_CS, "high")
        passed = isinstance(result, list) and len(result) == 1024
        _record("read_write 1024 bytes", passed, f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("read_write 1024 bytes", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 9. Mode/frequency persistence
# ---------------------------------------------------------------------------
def test_persistence():
    """Test mode and frequency persistence.

    Also validates config() fix: cs_mode is set once and should persist
    across subsequent config() calls that don't specify cs_mode.
    """
    print("\n" + "=" * 60)
    print("TEST: Mode/Frequency Persistence")
    print("=" * 60)

    from lager import Net, NetType
    from lager.io.gpio.dispatcher import gpo
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # Set cs_mode once - should persist across all subsequent config calls
    spi.config(cs_mode="manual")

    for mode in (0, 3):
        try:
            spi.config(mode=mode)
            gpo(GPIO_CS, "low")
            result = spi.read(n_words=4)
            gpo(GPIO_CS, "high")
            passed = isinstance(result, list) and len(result) == 4
            _record(f"mode {mode} persists across read", passed)
            if not passed:
                ok = False
        except Exception as e:
            gpo(GPIO_CS, "high")
            _record(f"mode {mode} persists", False, str(e))
            ok = False

    for freq in (1_000_000, 4_000_000):
        try:
            spi.config(frequency_hz=freq)
            gpo(GPIO_CS, "low")
            result = spi.read(n_words=4)
            gpo(GPIO_CS, "high")
            passed = isinstance(result, list) and len(result) == 4
            _record(f"freq {freq} persists across read", passed)
            if not passed:
                ok = False
        except Exception as e:
            gpo(GPIO_CS, "high")
            _record(f"freq {freq} persists", False, str(e))
            ok = False

    spi.config(mode=0, frequency_hz=1_000_000)
    return ok


# ---------------------------------------------------------------------------
# 10. BMP280 functional
# ---------------------------------------------------------------------------
def test_bmp280_functional():
    """BMP280 chip ID, calibration, write + readback via manual CS."""
    print("\n" + "=" * 60)
    print("TEST: BMP280 Functional")
    print("=" * 60)

    from lager import Net, NetType
    from lager.io.gpio.dispatcher import gpo
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8, bit_order="msb",
               cs_active="low", cs_mode="manual")
    ok = True

    # Chip ID (use transfer -- see note in test_manual_cs_operations 5a)
    try:
        gpo(GPIO_CS, "low")
        result = spi.transfer(n_words=2, data=[CHIP_ID_REG])
        gpo(GPIO_CS, "high")
        passed = result[1] == BMP280_CHIP_ID
        _record("BMP280 chip ID", passed,
                f"got=0x{result[1]:02X}, expected=0x{BMP280_CHIP_ID:02X}")
        if not passed:
            ok = False
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("BMP280 chip ID", False, str(e))
        ok = False

    # Calibration data (24 bytes from 0x88)
    try:
        gpo(GPIO_CS, "low")
        result = spi.transfer(n_words=25, data=[CALIB_REG])
        gpo(GPIO_CS, "high")
        calib = result[1:]  # Skip first byte (register echo)
        non_zero = any(b != 0 for b in calib)
        passed = len(calib) == 24 and non_zero
        _record("BMP280 calibration (24 bytes)", passed,
                f"non_zero={non_zero}")
        if not passed:
            ok = False
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("BMP280 calibration", False, str(e))
        ok = False

    # Write ctrl_meas=0x00, readback
    try:
        gpo(GPIO_CS, "low")
        spi.write(data=[CTRL_MEAS_WRITE, 0x00])
        gpo(GPIO_CS, "high")

        gpo(GPIO_CS, "low")
        result = spi.read_write(data=[CTRL_MEAS_READ, 0x00])
        gpo(GPIO_CS, "high")
        passed = result[1] == 0x00
        _record("BMP280 write+readback ctrl_meas=0x00", passed,
                f"readback=0x{result[1]:02X}")
        if not passed:
            ok = False
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("BMP280 write+readback", False, str(e))
        ok = False

    return ok


# ===================================================================
# Main
# ===================================================================
def main():
    """Run all tests."""
    print("Aardvark SPI Manual CS + wait_for_level Test Suite")
    print(f"SPI net: {SPI_NET}, GPIO CS: {GPIO_CS}")
    print("=" * 60)

    tests = [
        ("Imports",                 test_imports),
        ("Net.get + get_config",    test_net_get_and_config),
        ("Config",                  test_config),
        ("Invalid Config",          test_invalid_config),
        ("Manual CS Operations",    test_manual_cs_operations),
        ("Output Formats",          test_output_formats),
        ("wait_for_level",          test_wait_for_level),
        ("Large Transaction",       test_large_transaction),
        ("Mode/Freq Persistence",   test_persistence),
        ("BMP280 Functional",       test_bmp280_functional),
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
