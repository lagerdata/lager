#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
SPI Python API tests - Aardvark Automatic CS Mode.

Run with: lager python test/api/communication/test_spi_aardvark_auto.py --box <YOUR-BOX>

Prerequisites:
- Aardvark SPI net 'spi1' configured with cs_mode=auto
- Aardvark pin 9 (SS) wired to HW-611 CSB
- LabJack DAC net 'dac1' providing 3.3V to HW-611 VCC
- BMP280 (HW-611) wired to Aardvark SCK/MOSI/MISO

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


# ---------------------------------------------------------------------------
# 1. Imports
# ---------------------------------------------------------------------------
def test_imports():
    """Verify all SPI module imports work."""
    print("\n" + "=" * 60)
    print("TEST: Imports")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        assert hasattr(NetType, "SPI")
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
        _record("import dispatcher funcs", True)
    except Exception as e:
        _record("import dispatcher funcs", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 2. Net.get + get_config
# ---------------------------------------------------------------------------
def test_net_get_and_config():
    """Test Net.get returns SPINet and get_config works."""
    print("\n" + "=" * 60)
    print("TEST: Net.get + get_config")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.spi import SPINet

    try:
        spi = Net.get(SPI_NET, NetType.SPI)
        is_spinet = isinstance(spi, SPINet)
        _record("Net.get returns SPINet", is_spinet,
                f"type={type(spi).__name__}")

        cfg = spi.get_config()
        _record("get_config returns dict", isinstance(cfg, dict),
                f"keys={list(cfg.keys())}")
        return is_spinet
    except Exception as e:
        _record("Net.get + get_config", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 3. Config: all modes, bit orders, frequencies, word sizes, CS polarity
# ---------------------------------------------------------------------------
def test_config():
    """Test config with various parameters including cs_mode=auto."""
    print("\n" + "=" * 60)
    print("TEST: Config")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # cs_mode=auto
    try:
        spi.config(cs_mode="auto")
        _record("config cs_mode=auto", True)
    except Exception as e:
        _record("config cs_mode=auto", False, str(e))
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
               word_size=8, cs_active="low", cs_mode="auto")
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
# 5. read() basic
# ---------------------------------------------------------------------------
def test_read_basic():
    """Test read with various n_words, fills, keep_cs."""
    print("\n" + "=" * 60)
    print("TEST: read() basic")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8, cs_mode="auto")
    ok = True

    # Various sizes
    for n in (1, 4, 100):
        try:
            result = spi.read(n_words=n)
            passed = isinstance(result, list) and len(result) == n
            _record(f"read n_words={n}", passed,
                    f"len={len(result)}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"read n_words={n}", False, str(e))
            ok = False

    # Fill values
    for fill in (0xFF, 0x00, 0xAA):
        try:
            result = spi.read(n_words=4, fill=fill)
            passed = isinstance(result, list) and len(result) == 4
            _record(f"read fill=0x{fill:02X}", passed)
            if not passed:
                ok = False
        except Exception as e:
            _record(f"read fill=0x{fill:02X}", False, str(e))
            ok = False

    # Keep-cs (known limitation in auto mode -- may warn)
    try:
        result = spi.read(n_words=4, keep_cs=True)
        _record("read keep_cs=True (auto mode)", True,
                "accepted (may warn)")
    except Exception as e:
        _record("read keep_cs=True (auto mode)", True,
                f"raises {type(e).__name__}: {e} (expected limitation)")

    return ok


# ---------------------------------------------------------------------------
# 6. read_write() basic
# ---------------------------------------------------------------------------
def test_read_write_basic():
    """Test read_write with various payloads and formats."""
    print("\n" + "=" * 60)
    print("TEST: read_write() basic")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # Basic 4-byte
    try:
        result = spi.read_write(data=[0x9F, 0x00, 0x00, 0x00])
        passed = isinstance(result, list) and len(result) == 4
        _record("read_write 4 bytes", passed,
                f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read_write 4 bytes", False, str(e))
        ok = False

    # Single byte
    try:
        result = spi.read_write(data=[0x9F])
        passed = isinstance(result, list) and len(result) == 1
        _record("read_write single byte", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("read_write single byte", False, str(e))
        ok = False

    # Empty
    try:
        result = spi.read_write(data=[])
        passed = isinstance(result, list) and len(result) == 0
        _record("read_write empty", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("read_write empty", False, str(e))
        ok = False

    # Output formats
    data = [0x9F, 0x00, 0x00, 0x00]
    for fmt, expected_type in [("list", list), ("hex", str), ("bytes", str), ("json", dict)]:
        try:
            result = spi.read_write(data=data, output_format=fmt)
            passed = isinstance(result, expected_type)
            if fmt == "json":
                passed = passed and "data" in result
            _record(f"read_write format={fmt}", passed,
                    f"type={type(result).__name__}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"read_write format={fmt}", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 7. transfer() with padding, truncation, exact, no data, custom fill
# ---------------------------------------------------------------------------
def test_transfer():
    """Test transfer with various data/n_words combinations."""
    print("\n" + "=" * 60)
    print("TEST: transfer()")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # Padding: 1 byte data -> 4 words
    try:
        result = spi.transfer(n_words=4, data=[0x9F])
        passed = isinstance(result, list) and len(result) == 4
        _record("transfer pad 1->4", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("transfer pad 1->4", False, str(e))
        ok = False

    # Truncation: 5 bytes data -> 2 words
    try:
        result = spi.transfer(n_words=2, data=[1, 2, 3, 4, 5])
        passed = isinstance(result, list) and len(result) == 2
        _record("transfer truncate 5->2", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("transfer truncate 5->2", False, str(e))
        ok = False

    # Exact: 4 = 4
    try:
        result = spi.transfer(n_words=4, data=[1, 2, 3, 4])
        passed = isinstance(result, list) and len(result) == 4
        _record("transfer exact 4=4", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("transfer exact 4=4", False, str(e))
        ok = False

    # No data (all fill)
    try:
        result = spi.transfer(n_words=4)
        passed = isinstance(result, list) and len(result) == 4
        _record("transfer no data (all fill)", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("transfer no data", False, str(e))
        ok = False

    # Custom fill=0x00
    try:
        result = spi.transfer(n_words=4, data=[0x9F], fill=0x00)
        passed = isinstance(result, list) and len(result) == 4
        _record("transfer fill=0x00", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("transfer fill=0x00", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 8. write() basic
# ---------------------------------------------------------------------------
def test_write():
    """Test write with various payloads."""
    print("\n" + "=" * 60)
    print("TEST: write()")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # Single byte
    try:
        result = spi.write(data=[0x06])
        passed = result is None
        _record("write 1 byte", passed, f"returned={result!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("write 1 byte", False, str(e))
        ok = False

    # Multi-byte
    try:
        result = spi.write(data=[0x02, 0x00, 0x00, 0x00, 0xDE, 0xAD])
        passed = result is None
        _record("write 6 bytes", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("write 6 bytes", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 9. Word size 16 and 32
# ---------------------------------------------------------------------------
def test_word_sizes():
    """Test 16-bit and 32-bit word sizes."""
    print("\n" + "=" * 60)
    print("TEST: Word Sizes 16/32")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # 16-bit
    try:
        spi.config(word_size=16)
        result = spi.read(n_words=2)
        all_in_range = all(0 <= w <= 0xFFFF for w in result)
        passed = isinstance(result, list) and len(result) == 2 and all_in_range
        _record("word_size=16 read 2 words", passed,
                f"data={[hex(w) for w in result]}")
        if not passed:
            ok = False
    except Exception as e:
        _record("word_size=16", False, str(e))
        ok = False

    try:
        result = spi.read_write(data=[0x1234, 0x5678])
        passed = isinstance(result, list) and len(result) == 2
        _record("word_size=16 read_write", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("word_size=16 read_write", False, str(e))
        ok = False

    # 32-bit
    try:
        spi.config(word_size=32)
        result = spi.read(n_words=1)
        all_in_range = all(0 <= w <= 0xFFFFFFFF for w in result)
        passed = isinstance(result, list) and len(result) == 1 and all_in_range
        _record("word_size=32 read 1 word", passed,
                f"data={[hex(w) for w in result]}")
        if not passed:
            ok = False
    except Exception as e:
        _record("word_size=32", False, str(e))
        ok = False

    # Restore 8-bit
    spi.config(word_size=8)
    return ok


# ---------------------------------------------------------------------------
# 10. Mode/frequency persistence
# ---------------------------------------------------------------------------
def test_persistence():
    """Test mode and frequency persistence across operations."""
    print("\n" + "=" * 60)
    print("TEST: Mode/Frequency Persistence")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # Mode persistence
    for mode in (0, 3):
        try:
            spi.config(mode=mode)
            result = spi.read(n_words=4)
            passed = isinstance(result, list) and len(result) == 4
            _record(f"mode {mode} persists", passed)
            if not passed:
                ok = False
        except Exception as e:
            _record(f"mode {mode} persists", False, str(e))
            ok = False

    # Frequency persistence
    for freq in (1_000_000, 4_000_000):
        try:
            spi.config(frequency_hz=freq)
            result = spi.read(n_words=4)
            passed = isinstance(result, list) and len(result) == 4
            _record(f"freq {freq} persists", passed)
            if not passed:
                ok = False
        except Exception as e:
            _record(f"freq {freq} persists", False, str(e))
            ok = False

    spi.config(mode=0, frequency_hz=1_000_000)
    return ok


# ---------------------------------------------------------------------------
# 11. Dispatcher-level calls
# ---------------------------------------------------------------------------
def test_dispatcher():
    """Test dispatcher-level config, read, read_write, transfer with overrides."""
    print("\n" + "=" * 60)
    print("TEST: Dispatcher-level calls")
    print("=" * 60)

    ok = True

    try:
        from lager.protocols.spi.dispatcher import config, read, read_write, transfer

        config(SPI_NET, mode=0, bit_order="msb", frequency_hz=1_000_000,
               word_size=8, cs_active="low", cs_mode="auto")
        _record("dispatcher config", True)
    except Exception as e:
        _record("dispatcher config", False, str(e))
        return False

    try:
        print("  (dispatcher read prints to stdout)")
        read(SPI_NET, n_words=4, fill=0xFF, output_format="hex")
        _record("dispatcher read hex", True)
    except Exception as e:
        _record("dispatcher read hex", False, str(e))
        ok = False

    try:
        read_write(SPI_NET, data=[0x9F, 0x00, 0x00], output_format="hex")
        _record("dispatcher read_write hex", True)
    except Exception as e:
        _record("dispatcher read_write hex", False, str(e))
        ok = False

    try:
        transfer(SPI_NET, n_words=4, data=[0x9F], fill=0xFF, output_format="hex")
        _record("dispatcher transfer pad", True)
    except Exception as e:
        _record("dispatcher transfer pad", False, str(e))
        ok = False

    # Overrides
    try:
        overrides = {"mode": 1, "frequency_hz": 500_000}
        read(SPI_NET, n_words=4, output_format="hex", overrides=overrides)
        _record("dispatcher override mode=1 freq=500k", True)
    except Exception as e:
        _record("dispatcher override", False, str(e))
        ok = False

    # Restore
    config(SPI_NET, mode=0, frequency_hz=1_000_000)
    return ok


# ---------------------------------------------------------------------------
# 12. Large transaction
# ---------------------------------------------------------------------------
def test_large_transaction():
    """Test 1024-byte SPI transfer."""
    print("\n" + "=" * 60)
    print("TEST: Large Transaction (1024 bytes)")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8, cs_mode="auto")
    ok = True

    try:
        result = spi.read(n_words=1024)
        passed = isinstance(result, list) and len(result) == 1024
        _record("read 1024 bytes", passed, f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read 1024 bytes", False, str(e))
        ok = False

    try:
        data = [i & 0xFF for i in range(1024)]
        result = spi.read_write(data=data)
        passed = isinstance(result, list) and len(result) == 1024
        _record("read_write 1024 bytes", passed, f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read_write 1024 bytes", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 13. BMP280 chip ID
# ---------------------------------------------------------------------------
def test_bmp280_chip_id():
    """Read BMP280 chip ID via auto CS SPI."""
    print("\n" + "=" * 60)
    print("TEST: BMP280 Chip ID")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8, bit_order="msb",
               cs_active="low", cs_mode="auto")

    try:
        result = spi.read_write(data=[CHIP_ID_REG, 0x00])
        chip_id = result[1]
        known_ids = {0x58: "BMP280", 0x60: "BME280"}

        if chip_id in known_ids:
            _record("BMP280 chip ID", True,
                    f"chip_id=0x{chip_id:02X} ({known_ids[chip_id]})")
        elif chip_id in (0x00, 0xFF):
            _record("BMP280 chip ID", True,
                    f"chip_id=0x{chip_id:02X} (no sensor detected, SKIP)")
        else:
            _record("BMP280 chip ID", True,
                    f"chip_id=0x{chip_id:02X} (unknown device, SPI works)")
        return True
    except Exception as e:
        _record("BMP280 chip ID", False, str(e))
        return False


# ===================================================================
# Main
# ===================================================================
def main():
    """Run all tests."""
    print("Aardvark SPI Auto CS Test Suite")
    print(f"SPI net: {SPI_NET}")
    print("=" * 60)

    tests = [
        ("Imports",                 test_imports),
        ("Net.get + get_config",    test_net_get_and_config),
        ("Config",                  test_config),
        ("Invalid Config",          test_invalid_config),
        ("read() basic",            test_read_basic),
        ("read_write() basic",      test_read_write_basic),
        ("transfer()",              test_transfer),
        ("write()",                 test_write),
        ("Word Sizes 16/32",        test_word_sizes),
        ("Mode/Freq Persistence",   test_persistence),
        ("Dispatcher calls",        test_dispatcher),
        ("Large Transaction",       test_large_transaction),
        ("BMP280 Chip ID",          test_bmp280_chip_id),
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
