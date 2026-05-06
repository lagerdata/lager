#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive edge-case tests for FT232H SPI auto (hardware) CS mode.

Run with: lager python test/api/communication/test_spi_ft232h_auto.py --box <YOUR-BOX>

Prerequisites:
- FT232H wired to HW-611 (BMP280) via SPI:
    Orange (AD0) -> SCL, Yellow (AD1) -> SDA, Green (AD2) -> SDO,
    Brown (AD3) -> CSB, Red -> VCC, Black -> GND
- SPI net 'spi1' configured on the box (FTDI_FT232H, channel SPI0)

BMP280 SPI reference:
- Chip ID register: 0xD0 (bit 7 set = read), expected value: 0x58
- ctrl_meas register: 0xF4 (read) / 0x74 (write, bit 7 cleared)
- Supports SPI modes 0 and 3, MSB-first, max 10 MHz
"""
import sys
import os
import traceback

SPI_NET = os.environ.get("SPI_NET", "spi1")

_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


# ===================================================================
# 1. BMP280 chip ID (baseline sanity check)
# ===================================================================
def test_chip_id_baseline():
    """Verify BMP280 responds with 0x58 before running edge cases."""
    print("\n" + "=" * 60)
    print("TEST: Chip ID baseline")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8,
               bit_order="msb", cs_active="low")

    result = spi.read_write(data=[0xD0, 0x00])
    chip_id = result[1]
    passed = chip_id == 0x58
    _record("BMP280 chip ID = 0x58", passed,
            f"got 0x{chip_id:02X}")
    return passed


# ===================================================================
# 2. Register write then readback (single process)
# ===================================================================
def test_register_write_readback():
    """Write to ctrl_meas, read back, verify value persists."""
    print("\n" + "=" * 60)
    print("TEST: Register write/readback")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8,
               bit_order="msb", cs_active="low")
    ok = True

    # Ensure BMP280 is in sleep mode before writing
    spi.write(data=[0x74, 0x00])

    # Write 0x27 to ctrl_meas (addr 0x74 = 0xF4 with bit 7 cleared)
    spi.write(data=[0x74, 0x27])
    result = spi.read_write(data=[0xF4, 0x00])
    val = result[1]
    passed = val == 0x27
    _record("write 0x27, readback ctrl_meas", passed,
            f"expected 0x27, got 0x{val:02X}")
    if not passed:
        ok = False

    # Put back to sleep before writing a different value
    spi.write(data=[0x74, 0x00])

    # Write a different value to confirm it changed
    spi.write(data=[0x74, 0x4B])
    result = spi.read_write(data=[0xF4, 0x00])
    val = result[1]
    passed = val == 0x4B
    _record("write 0x4B, readback ctrl_meas", passed,
            f"expected 0x4B, got 0x{val:02X}")
    if not passed:
        ok = False

    # Reset to sleep mode
    spi.write(data=[0x74, 0x00])
    return ok


# ===================================================================
# 3. keep_cs chained transfers
# ===================================================================
def test_keep_cs_chained():
    """Use keep_cs=True across multiple transfers, then release."""
    print("\n" + "=" * 60)
    print("TEST: keep_cs chained transfers")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8)
    ok = True

    # Transfer 1: send chip ID command, keep CS held
    try:
        r1 = spi.read_write(data=[0xD0], keep_cs=True)
        passed = isinstance(r1, list) and len(r1) == 1
        _record("keep_cs=True transfer 1", passed, f"data={r1}")
        if not passed:
            ok = False
    except Exception as e:
        _record("keep_cs=True transfer 1", False, str(e))
        ok = False

    # Transfer 2: clock out response byte, still keep CS
    try:
        r2 = spi.read(n_words=1, keep_cs=True)
        passed = isinstance(r2, list) and len(r2) == 1
        _record("keep_cs=True transfer 2 (read)", passed, f"data={r2}")
        if not passed:
            ok = False
    except Exception as e:
        _record("keep_cs=True transfer 2 (read)", False, str(e))
        ok = False

    # Transfer 3: final read, release CS
    try:
        r3 = spi.read(n_words=1, keep_cs=False)
        passed = isinstance(r3, list) and len(r3) == 1
        _record("keep_cs=False transfer 3 (release)", passed, f"data={r3}")
        if not passed:
            ok = False
    except Exception as e:
        _record("keep_cs=False transfer 3 (release)", False, str(e))
        ok = False

    return ok


# ===================================================================
# 4. Empty data transfers
# ===================================================================
def test_empty_data():
    """Empty lists and zero-length reads should return empty without error."""
    print("\n" + "=" * 60)
    print("TEST: Empty data transfers")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8)
    ok = True

    # read_write([])
    try:
        result = spi.read_write(data=[])
        passed = isinstance(result, list) and len(result) == 0
        _record("read_write([])", passed, f"result={result}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read_write([])", False, str(e))
        ok = False

    # read(0)
    try:
        result = spi.read(n_words=0)
        passed = isinstance(result, list) and len(result) == 0
        _record("read(0)", passed, f"result={result}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read(0)", False, str(e))
        ok = False

    # write([])
    try:
        result = spi.write(data=[])
        passed = result is None
        _record("write([])", passed, f"result={result!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("write([])", False, str(e))
        ok = False

    return ok


# ===================================================================
# 5. Single-byte transfers
# ===================================================================
def test_single_byte():
    """Single-byte read, write, and read_write."""
    print("\n" + "=" * 60)
    print("TEST: Single-byte transfers")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8)
    ok = True

    # read_write 1 byte
    try:
        result = spi.read_write(data=[0xD0])
        passed = isinstance(result, list) and len(result) == 1
        _record("read_write([0xD0])", passed, f"data={result}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read_write([0xD0])", False, str(e))
        ok = False

    # read 1 word
    try:
        result = spi.read(n_words=1)
        passed = isinstance(result, list) and len(result) == 1
        _record("read(1)", passed, f"data={result}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read(1)", False, str(e))
        ok = False

    # write 1 byte
    try:
        result = spi.write(data=[0x00])
        passed = result is None
        _record("write([0x00])", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("write([0x00])", False, str(e))
        ok = False

    return ok


# ===================================================================
# 6. Boundary byte values
# ===================================================================
def test_boundary_values():
    """Transfer boundary values: 0x00, 0xFF, 0xAA, 0x55."""
    print("\n" + "=" * 60)
    print("TEST: Boundary byte values")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8)
    ok = True

    for val in (0x00, 0xFF, 0xAA, 0x55, 0x01, 0x80):
        try:
            result = spi.read_write(data=[val])
            passed = isinstance(result, list) and len(result) == 1
            all_valid = all(0 <= b <= 0xFF for b in result)
            _record(f"read_write([0x{val:02X}])", passed and all_valid,
                    f"rx=0x{result[0]:02X}")
            if not (passed and all_valid):
                ok = False
        except Exception as e:
            _record(f"read_write([0x{val:02X}])", False, str(e))
            ok = False

    return ok


# ===================================================================
# 7. All four SPI modes with BMP280 verification
# ===================================================================
def test_all_modes_with_device():
    """Read chip ID in modes 0 and 3 (BMP280 supported), verify modes 1 and 2 don't crash."""
    print("\n" + "=" * 60)
    print("TEST: All SPI modes with device")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # Modes 0 and 3 are supported by BMP280 -- chip ID should be 0x58
    for mode in (0, 3):
        try:
            spi.config(mode=mode, frequency_hz=1_000_000)
            result = spi.read_write(data=[0xD0, 0x00])
            chip_id = result[1]
            passed = chip_id == 0x58
            _record(f"mode {mode}: chip ID", passed,
                    f"0x{chip_id:02X}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"mode {mode}: chip ID", False, str(e))
            ok = False

    # Modes 1 and 2 -- BMP280 won't respond correctly, but transfer should not crash
    for mode in (1, 2):
        try:
            spi.config(mode=mode, frequency_hz=1_000_000)
            result = spi.read_write(data=[0xD0, 0x00])
            passed = isinstance(result, list) and len(result) == 2
            _record(f"mode {mode}: no crash", passed,
                    f"rx=[0x{result[0]:02X}, 0x{result[1]:02X}]")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"mode {mode}: no crash", False, str(e))
            ok = False

    spi.config(mode=0)
    return ok


# ===================================================================
# 8. Frequency sweep
# ===================================================================
def test_frequency_sweep():
    """Read chip ID at various frequencies, verify correct response."""
    print("\n" + "=" * 60)
    print("TEST: Frequency sweep")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # BMP280 max is 10 MHz, but test FT232H at higher frequencies too
    freqs = [100_000, 500_000, 1_000_000, 5_000_000, 10_000_000]

    for freq in freqs:
        try:
            spi.config(mode=0, frequency_hz=freq)
            result = spi.read_write(data=[0xD0, 0x00])
            chip_id = result[1]
            label = f"{freq // 1000}kHz" if freq < 1_000_000 else f"{freq // 1_000_000}MHz"
            passed = chip_id == 0x58
            _record(f"freq {label}: chip ID", passed,
                    f"0x{chip_id:02X}")
            if not passed:
                ok = False
        except Exception as e:
            label = f"{freq // 1000}kHz" if freq < 1_000_000 else f"{freq // 1_000_000}MHz"
            _record(f"freq {label}: chip ID", False, str(e))
            ok = False

    spi.config(frequency_hz=1_000_000)
    return ok


# ===================================================================
# 9. Read fill values
# ===================================================================
def test_fill_values():
    """Read with different fill bytes: 0xFF, 0x00, 0xAA."""
    print("\n" + "=" * 60)
    print("TEST: Read fill values")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8)
    ok = True

    for fill in (0xFF, 0x00, 0xAA):
        try:
            result = spi.read(n_words=4, fill=fill)
            passed = isinstance(result, list) and len(result) == 4
            _record(f"read fill=0x{fill:02X}", passed,
                    f"len={len(result)}, data={[f'0x{b:02X}' for b in result]}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"read fill=0x{fill:02X}", False, str(e))
            ok = False

    return ok


# ===================================================================
# 10. Word size 16-bit
# ===================================================================
def test_word_size_16():
    """16-bit word transfers and boundary values."""
    print("\n" + "=" * 60)
    print("TEST: Word size 16-bit")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=16, bit_order="msb")
    ok = True

    # Read 2 words (4 bytes on wire)
    try:
        result = spi.read(n_words=2)
        passed = isinstance(result, list) and len(result) == 2
        all_valid = all(0 <= w <= 0xFFFF for w in result)
        _record("read 2x16-bit", passed and all_valid,
                f"data={[f'0x{w:04X}' for w in result]}")
        if not (passed and all_valid):
            ok = False
    except Exception as e:
        _record("read 2x16-bit", False, str(e))
        ok = False

    # read_write with 16-bit words
    try:
        result = spi.read_write(data=[0xD000, 0x0000])
        passed = isinstance(result, list) and len(result) == 2
        all_valid = all(0 <= w <= 0xFFFF for w in result)
        _record("read_write 2x16-bit", passed and all_valid,
                f"data={[f'0x{w:04X}' for w in result]}")
        if not (passed and all_valid):
            ok = False
    except Exception as e:
        _record("read_write 2x16-bit", False, str(e))
        ok = False

    # Boundary values
    for val in (0x0000, 0xFFFF, 0xAAAA, 0x5555, 0x0001, 0x8000):
        try:
            result = spi.read_write(data=[val])
            passed = isinstance(result, list) and len(result) == 1
            _record(f"16-bit 0x{val:04X}", passed,
                    f"rx=0x{result[0]:04X}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"16-bit 0x{val:04X}", False, str(e))
            ok = False

    spi.config(word_size=8)
    return ok


# ===================================================================
# 11. Word size 32-bit
# ===================================================================
def test_word_size_32():
    """32-bit word transfers and boundary values."""
    print("\n" + "=" * 60)
    print("TEST: Word size 32-bit")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=32, bit_order="msb")
    ok = True

    # Read 1 word (4 bytes on wire)
    try:
        result = spi.read(n_words=1)
        passed = isinstance(result, list) and len(result) == 1
        all_valid = all(0 <= w <= 0xFFFFFFFF for w in result)
        _record("read 1x32-bit", passed and all_valid,
                f"data=0x{result[0]:08X}")
        if not (passed and all_valid):
            ok = False
    except Exception as e:
        _record("read 1x32-bit", False, str(e))
        ok = False

    # Boundary values
    for val in (0x00000000, 0xFFFFFFFF, 0xAAAAAAAA, 0x00000001, 0x80000000):
        try:
            result = spi.read_write(data=[val])
            passed = isinstance(result, list) and len(result) == 1
            _record(f"32-bit 0x{val:08X}", passed,
                    f"rx=0x{result[0]:08X}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"32-bit 0x{val:08X}", False, str(e))
            ok = False

    spi.config(word_size=8)
    return ok


# ===================================================================
# 12. LSB bit order
# ===================================================================
def test_lsb_bit_order():
    """LSB-first transfers should not crash; verify round-trip config."""
    print("\n" + "=" * 60)
    print("TEST: LSB bit order")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # 8-bit LSB
    try:
        spi.config(mode=0, frequency_hz=1_000_000, word_size=8, bit_order="lsb")
        result = spi.read_write(data=[0xD0, 0x00])
        passed = isinstance(result, list) and len(result) == 2
        _record("8-bit LSB read_write", passed,
                f"data={[f'0x{b:02X}' for b in result]}")
        if not passed:
            ok = False
    except Exception as e:
        _record("8-bit LSB read_write", False, str(e))
        ok = False

    # 16-bit LSB
    try:
        spi.config(word_size=16, bit_order="lsb")
        result = spi.read_write(data=[0xD000])
        passed = isinstance(result, list) and len(result) == 1
        _record("16-bit LSB read_write", passed,
                f"data=0x{result[0]:04X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("16-bit LSB read_write", False, str(e))
        ok = False

    # 32-bit LSB
    try:
        spi.config(word_size=32, bit_order="lsb")
        result = spi.read_write(data=[0xD0000000])
        passed = isinstance(result, list) and len(result) == 1
        _record("32-bit LSB read_write", passed,
                f"data=0x{result[0]:08X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("32-bit LSB read_write", False, str(e))
        ok = False

    spi.config(word_size=8, bit_order="msb")
    return ok


# ===================================================================
# 13. CS polarity
# ===================================================================
def test_cs_polarity():
    """cs_active='low' should work with BMP280; 'high' should not crash."""
    print("\n" + "=" * 60)
    print("TEST: CS polarity")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # cs_active=low: BMP280 should respond
    try:
        spi.config(mode=0, frequency_hz=1_000_000, cs_active="low")
        result = spi.read_write(data=[0xD0, 0x00])
        chip_id = result[1]
        passed = chip_id == 0x58
        _record("cs_active=low: chip ID", passed,
                f"0x{chip_id:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("cs_active=low: chip ID", False, str(e))
        ok = False

    # cs_active=high: BMP280 won't respond (CS inverted), but shouldn't crash
    try:
        spi.config(cs_active="high")
        result = spi.read_write(data=[0xD0, 0x00])
        passed = isinstance(result, list) and len(result) == 2
        _record("cs_active=high: no crash", passed,
                f"rx=[0x{result[0]:02X}, 0x{result[1]:02X}]")
        if not passed:
            ok = False
    except Exception as e:
        _record("cs_active=high: no crash", False, str(e))
        ok = False

    spi.config(cs_active="low")
    return ok


# ===================================================================
# 14. Invalid config parameters
# ===================================================================
def test_invalid_config():
    """Invalid parameters should raise SPIBackendError."""
    print("\n" + "=" * 60)
    print("TEST: Invalid config parameters")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.spi import SPIBackendError
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    cases = [
        ("mode=5", {"mode": 5}),
        ("mode=-1", {"mode": -1}),
        ("bit_order='xyz'", {"bit_order": "xyz"}),
        ("word_size=12", {"word_size": 12}),
        ("word_size=4", {"word_size": 4}),
        ("cs_active='x'", {"cs_active": "x"}),
    ]

    for label, kwargs in cases:
        try:
            spi.config(**kwargs)
            _record(f"invalid {label} raises error", False, "no error raised")
            ok = False
        except SPIBackendError:
            _record(f"invalid {label} raises SPIBackendError", True)
        except Exception as e:
            _record(f"invalid {label} raises SPIBackendError", False,
                    f"wrong exception: {type(e).__name__}: {e}")
            ok = False

    return ok


# ===================================================================
# 15. Config with None values (no-op)
# ===================================================================
def test_config_none_noop():
    """Config with all None should not change anything."""
    print("\n" + "=" * 60)
    print("TEST: Config with None (no-op)")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)

    # Set known config
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8,
               bit_order="msb", cs_active="low")

    # Call config with no args -- should be a no-op
    try:
        spi.config()
        # Verify device still works
        result = spi.read_write(data=[0xD0, 0x00])
        chip_id = result[1]
        passed = chip_id == 0x58
        _record("config() no-op, device still works", passed,
                f"chip_id=0x{chip_id:02X}")
        return passed
    except Exception as e:
        _record("config() no-op", False, str(e))
        return False


# ===================================================================
# 16. Incremental config changes
# ===================================================================
def test_incremental_config():
    """Change one parameter at a time, verify device works after each."""
    print("\n" + "=" * 60)
    print("TEST: Incremental config changes")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8,
               bit_order="msb", cs_active="low")
    ok = True

    # Change only frequency
    try:
        spi.config(frequency_hz=500_000)
        result = spi.read_write(data=[0xD0, 0x00])
        passed = result[1] == 0x58
        _record("change freq only", passed, f"chip_id=0x{result[1]:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("change freq only", False, str(e))
        ok = False

    # Change only mode (to 3, BMP280-compatible)
    try:
        spi.config(mode=3)
        result = spi.read_write(data=[0xD0, 0x00])
        passed = result[1] == 0x58
        _record("change mode only (3)", passed, f"chip_id=0x{result[1]:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("change mode only (3)", False, str(e))
        ok = False

    # Change back to mode 0
    try:
        spi.config(mode=0)
        result = spi.read_write(data=[0xD0, 0x00])
        passed = result[1] == 0x58
        _record("change mode only (0)", passed, f"chip_id=0x{result[1]:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("change mode only (0)", False, str(e))
        ok = False

    spi.config(frequency_hz=1_000_000)
    return ok


# ===================================================================
# 17. Transfer padding and truncation
# ===================================================================
def test_transfer_pad_truncate():
    """SPINet.transfer() padding and truncation behavior."""
    print("\n" + "=" * 60)
    print("TEST: Transfer padding/truncation")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8)
    ok = True

    # Padding: 1 byte data -> 4 word transfer
    try:
        result = spi.transfer(n_words=4, data=[0xD0])
        passed = isinstance(result, list) and len(result) == 4
        _record("pad 1->4", passed, f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        _record("pad 1->4", False, str(e))
        ok = False

    # Truncation: 8 byte data -> 2 word transfer
    try:
        result = spi.transfer(n_words=2, data=[1, 2, 3, 4, 5, 6, 7, 8])
        passed = isinstance(result, list) and len(result) == 2
        _record("truncate 8->2", passed, f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        _record("truncate 8->2", False, str(e))
        ok = False

    # Exact: 4 byte data -> 4 word transfer
    try:
        result = spi.transfer(n_words=4, data=[0xD0, 0x00, 0x00, 0x00])
        passed = isinstance(result, list) and len(result) == 4
        _record("exact 4=4", passed, f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        _record("exact 4=4", False, str(e))
        ok = False

    # No data (all fill)
    try:
        result = spi.transfer(n_words=4)
        passed = isinstance(result, list) and len(result) == 4
        _record("no data (all fill)", passed, f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        _record("no data (all fill)", False, str(e))
        ok = False

    # Custom fill
    try:
        result = spi.transfer(n_words=4, data=[0xD0], fill=0x00)
        passed = isinstance(result, list) and len(result) == 4
        _record("custom fill=0x00", passed, f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        _record("custom fill=0x00", False, str(e))
        ok = False

    return ok


# ===================================================================
# 18. Output formats
# ===================================================================
def test_output_formats():
    """All output_format options: list, hex, bytes, json."""
    print("\n" + "=" * 60)
    print("TEST: Output formats")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8)
    ok = True

    # list
    try:
        result = spi.read(n_words=4, output_format="list")
        passed = isinstance(result, list) and len(result) == 4
        _record("format=list", passed, f"type={type(result).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("format=list", False, str(e))
        ok = False

    # hex
    try:
        result = spi.read(n_words=4, output_format="hex")
        passed = isinstance(result, str)
        _record("format=hex", passed, f"value={result!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("format=hex", False, str(e))
        ok = False

    # bytes
    try:
        result = spi.read(n_words=4, output_format="bytes")
        passed = isinstance(result, str)
        _record("format=bytes", passed, f"value={result!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("format=bytes", False, str(e))
        ok = False

    # json
    try:
        result = spi.read(n_words=4, output_format="json")
        passed = isinstance(result, dict) and "data" in result
        _record("format=json", passed, f"value={result}")
        if not passed:
            ok = False
    except Exception as e:
        _record("format=json", False, str(e))
        ok = False

    return ok


# ===================================================================
# 19. Large transfers
# ===================================================================
def test_large_transfers():
    """Large transfers: 256, 1024, 4096 bytes."""
    print("\n" + "=" * 60)
    print("TEST: Large transfers")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8)
    ok = True

    for n in (256, 512, 1024):
        try:
            result = spi.read(n_words=n)
            passed = isinstance(result, list) and len(result) == n
            _record(f"read {n} bytes", passed, f"len={len(result)}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"read {n} bytes", False, str(e))
            ok = False

    # Large read_write
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


# ===================================================================
# 20. Multi-register BMP280 read (calibration data)
# ===================================================================
def test_bmp280_calibration_read():
    """Read BMP280 calibration registers (0x88-0x9F), verify non-zero data."""
    print("\n" + "=" * 60)
    print("TEST: BMP280 calibration data read")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8)

    try:
        # Read 24 calibration bytes starting at 0x88 (bit 7 already set)
        cmd = [0x88] + [0x00] * 24
        result = spi.read_write(data=cmd)
        cal_data = result[1:]  # Skip first byte (sent during address)

        # Calibration data should not be all zeros or all 0xFF
        all_zero = all(b == 0x00 for b in cal_data)
        all_ff = all(b == 0xFF for b in cal_data)
        passed = not all_zero and not all_ff and len(cal_data) == 24
        _record("calibration 24 bytes", passed,
                f"first 6: {[f'0x{b:02X}' for b in cal_data[:6]]}")
        return passed
    except Exception as e:
        _record("calibration 24 bytes", False, str(e))
        return False


# ===================================================================
# 21. Rapid config changes (stress test)
# ===================================================================
def test_rapid_config_changes():
    """Rapidly cycle through configs and verify device still works."""
    print("\n" + "=" * 60)
    print("TEST: Rapid config changes")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    configs = [
        {"mode": 0, "frequency_hz": 100_000},
        {"mode": 3, "frequency_hz": 5_000_000},
        {"mode": 0, "frequency_hz": 1_000_000},
        {"mode": 3, "frequency_hz": 500_000},
        {"mode": 0, "frequency_hz": 10_000_000},
    ]

    for i, cfg in enumerate(configs):
        try:
            spi.config(**cfg)
            result = spi.read_write(data=[0xD0, 0x00])
            chip_id = result[1]
            passed = chip_id == 0x58
            _record(f"rapid config #{i+1} mode={cfg['mode']} "
                    f"freq={cfg['frequency_hz']}", passed,
                    f"chip_id=0x{chip_id:02X}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"rapid config #{i+1}", False, str(e))
            ok = False

    spi.config(mode=0, frequency_hz=1_000_000)
    return ok


# ===================================================================
# 22. Write returns None
# ===================================================================
def test_write_returns_none():
    """write() should always return None for various data sizes."""
    print("\n" + "=" * 60)
    print("TEST: Write returns None")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8)
    ok = True

    cases = [
        ("1 byte", [0x06]),
        ("4 bytes", [0x02, 0x00, 0x00, 0x00]),
        ("256 bytes", [i & 0xFF for i in range(256)]),
    ]

    for label, data in cases:
        try:
            result = spi.write(data=data)
            passed = result is None
            _record(f"write {label} -> None", passed, f"returned={result!r}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"write {label} -> None", False, str(e))
            ok = False

    return ok


# ===================================================================
# 23. BMP280 full read/write/verify cycle
# ===================================================================
def test_bmp280_full_cycle():
    """Full BMP280 cycle: config, trigger measurement, read temperature."""
    print("\n" + "=" * 60)
    print("TEST: BMP280 full read/write/verify cycle")
    print("=" * 60)

    import time
    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000, word_size=8, cs_active="low")
    ok = True

    # Step 1: Read chip ID
    result = spi.read_write(data=[0xD0, 0x00])
    chip_id = result[1]
    passed = chip_id == 0x58
    _record("step 1: chip ID", passed, f"0x{chip_id:02X}")
    if not passed:
        ok = False
        return ok

    # Step 2: Write config register (0xF5 -> write addr 0x75)
    # t_sb=000 (0.5ms), filter=000 (off), spi3w_en=0
    spi.write(data=[0x75, 0x00])
    result = spi.read_write(data=[0xF5, 0x00])
    _record("step 2: write config reg", result[1] == 0x00,
            f"readback=0x{result[1]:02X}")

    # Step 3: Write ctrl_meas (0xF4 -> write addr 0x74)
    # osrs_t=001 (x1), osrs_p=001 (x1), mode=01 (forced)
    spi.write(data=[0x74, 0x25])
    result = spi.read_write(data=[0xF4, 0x00])
    # In forced mode, mode bits may clear to 00 after measurement
    ctrl_val = result[1]
    passed = (ctrl_val & 0xFC) == 0x24  # Check osrs bits, ignore mode
    _record("step 3: write ctrl_meas", passed,
            f"readback=0x{ctrl_val:02X}")
    if not passed:
        ok = False

    # Step 4: Wait for measurement and read raw temp
    time.sleep(0.1)
    result = spi.read_write(data=[0xFA, 0x00, 0x00, 0x00])
    temp_msb = result[1]
    temp_lsb = result[2]
    temp_xlsb = result[3]
    raw_temp = (temp_msb << 12) | (temp_lsb << 4) | (temp_xlsb >> 4)
    # Raw temp should be non-zero if measurement happened
    passed = raw_temp != 0 and raw_temp != 0xFFFFF
    _record("step 4: raw temperature", passed,
            f"raw=0x{raw_temp:05X} ({raw_temp})")
    if not passed:
        ok = False

    # Reset to sleep
    spi.write(data=[0x74, 0x00])
    return ok


# ===================================================================
# Main
# ===================================================================
def main():
    print("FT232H SPI Auto CS -- Comprehensive Edge Case Tests")
    print(f"Net: {SPI_NET}")
    print("=" * 60)

    tests = [
        ("Chip ID Baseline",           test_chip_id_baseline),
        ("Register Write/Readback",    test_register_write_readback),
        ("keep_cs Chained Transfers",  test_keep_cs_chained),
        ("Empty Data Transfers",       test_empty_data),
        ("Single-Byte Transfers",      test_single_byte),
        ("Boundary Byte Values",       test_boundary_values),
        ("All SPI Modes",             test_all_modes_with_device),
        ("Frequency Sweep",           test_frequency_sweep),
        ("Read Fill Values",          test_fill_values),
        ("Word Size 16-bit",          test_word_size_16),
        ("Word Size 32-bit",          test_word_size_32),
        ("LSB Bit Order",            test_lsb_bit_order),
        ("CS Polarity",              test_cs_polarity),
        ("Invalid Config",           test_invalid_config),
        ("Config None No-op",        test_config_none_noop),
        ("Incremental Config",       test_incremental_config),
        ("Transfer Pad/Truncate",    test_transfer_pad_truncate),
        ("Output Formats",           test_output_formats),
        ("Large Transfers",          test_large_transfers),
        ("BMP280 Calibration Read",  test_bmp280_calibration_read),
        ("Rapid Config Changes",     test_rapid_config_changes),
        ("Write Returns None",       test_write_returns_none),
        ("BMP280 Full Cycle",        test_bmp280_full_cycle),
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

    print(f"\nGroups: {passed_count}/{total_count} passed")

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
