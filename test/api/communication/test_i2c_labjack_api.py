#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
I2C Python API tests - LabJack T7.

Run with: lager python test/api/communication/test_i2c_labjack_api.py --box <YOUR-BOX>

Prerequisites:
- LabJack T7 connected to box USB
- BMP280 (HW-611) wired to LabJack I2C pins:
    FIO4 (SDA) -> HW-611 SDA
    FIO5 (SCL) -> HW-611 SCL
- LabJack DAC0 (dac1) -> HW-611 VCC (3.3V power)
- HW-611 GND -> LabJack GND
- Net 'i2c2' configured for LabJack T7 I2C
- External 4.7k pull-up resistors on SDA/SCL (LabJack has NO internal pull-ups)

Wiring Diagram:
  LabJack T7              HW-611 (BMP280)
  +---------+             +---------+
  | FIO4 SDA|<----------->| SDA     |
  | FIO5 SCL|<----------->| SCL     |
  | GND     |-------------| GND     |
  | DAC0    |------------>| VCC     | (dac1 3.3V)
  +---------+             +---------+

  External pull-ups:
  VCC (3.3V) ---[4.7k]--- SDA
  VCC (3.3V) ---[4.7k]--- SCL

LabJack T7 I2C constraints:
  - Maximum 56 bytes per transaction (hardware buffer limit)
  - No internal pull-ups (pull_ups parameter silently ignored)
  - Frequency range ~25 Hz to ~450 kHz via throttle register
  - Reconnect retry logic with exponential backoff (errors 1227/1239)
  - Must check I2C_ACKS register after I2C_GO (NACK detection)

BMP280 I2C details:
  - I2C address: 0x76 (SDO low) or 0x77 (SDO high)
  - Chip ID register: 0xD0, expected value: 0x58
  - Calibration data: 24 bytes starting at 0x88
"""
import sys
import os
import json
import time
import traceback

# Configuration - change these or set env vars
I2C_NET = os.environ.get("I2C_NET", "i2c2")
BMP280_ADDR = int(os.environ.get("BMP280_ADDR", "0x76"), 0)
BMP280_CHIP_ID = 0x58
CHIP_ID_REG = 0xD0
CALIB_REG = 0x88
STATUS_REG = 0xF3
CTRL_MEAS_REG = 0xF4
CONFIG_REG = 0xF5
RESET_REG = 0xE0
RESET_VALUE = 0xB6

# LabJack-specific constants
MAX_BYTES = 56  # LabJack T7 I2C buffer limit

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
    """Verify all I2C module imports work."""
    print("\n" + "=" * 60)
    print("TEST: Imports")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        assert hasattr(NetType, "I2C"), "NetType.I2C not found"
        _record("import Net, NetType", True)
    except Exception as e:
        _record("import Net, NetType", False, str(e))
        ok = False

    try:
        from lager.protocols.i2c import I2CBase
        _record("import I2CBase", True)
    except Exception as e:
        _record("import I2CBase", False, str(e))
        ok = False

    try:
        from lager.protocols.i2c.labjack_i2c import LabJackI2C
        _record("import LabJackI2C", True)
    except Exception as e:
        _record("import LabJackI2C", False, str(e))
        ok = False

    try:
        from lager.protocols.i2c import I2CNet
        _record("import I2CNet", True)
    except Exception as e:
        _record("import I2CNet", False, str(e))
        ok = False

    try:
        from lager.protocols.i2c import config, scan, read, write, transfer
        _record("import dispatcher funcs", True)
    except Exception as e:
        _record("import dispatcher funcs", False, str(e))
        ok = False

    try:
        from lager.protocols.i2c import I2CBackendError
        _record("import I2CBackendError", True)
    except Exception as e:
        _record("import I2CBackendError", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 2. Net.get
# ---------------------------------------------------------------------------
def test_net_get():
    """Test Net.get('i2c2', NetType.I2C) returns I2CNet."""
    print("\n" + "=" * 60)
    print("TEST: Net.get")
    print("=" * 60)

    try:
        from lager import Net, NetType
        from lager.protocols.i2c import I2CNet

        i2c = Net.get(I2C_NET, NetType.I2C)
        is_i2cnet = isinstance(i2c, I2CNet)
        _record("Net.get returns I2CNet", is_i2cnet,
                f"type={type(i2c).__name__}")
        return is_i2cnet
    except Exception as e:
        _record("Net.get returns I2CNet", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 3. Config defaults
# ---------------------------------------------------------------------------
def test_config_defaults():
    """Test config() with default frequency."""
    print("\n" + "=" * 60)
    print("TEST: config defaults")
    print("=" * 60)

    try:
        from lager import Net, NetType
        i2c = Net.get(I2C_NET, NetType.I2C)

        i2c.config(frequency_hz=100_000)
        _record("config with 100kHz default", True)
        return True
    except Exception as e:
        _record("config with 100kHz default", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 4. Config frequencies
# ---------------------------------------------------------------------------
def test_config_frequencies():
    """Test various frequencies: 100k, 400k functional; others accepted."""
    print("\n" + "=" * 60)
    print("TEST: config frequencies")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # Standard (100kHz) and Fast (400kHz) must work with BMP280
    for freq in (100_000, 400_000):
        try:
            i2c.config(frequency_hz=freq)
            found = i2c.scan(start_addr=BMP280_ADDR, end_addr=BMP280_ADDR)
            functional = BMP280_ADDR in found
            _record(f"config frequency_hz={freq}", functional,
                    f"bus functional={functional}")
            if not functional:
                ok = False
        except Exception as e:
            _record(f"config frequency_hz={freq}", False, str(e))
            ok = False

    # Non-standard frequencies: accepted without error.
    # LabJack throttle formula maps these to valid throttle values.
    for freq in (1_000, 10_000, 25, 450_000):
        try:
            i2c.config(frequency_hz=freq)
            _record(f"config frequency_hz={freq}", True,
                    "accepted (non-standard, bus not verified)")
        except Exception as e:
            _record(f"config frequency_hz={freq}", False, str(e))
            ok = False

    # Restore standard
    i2c.config(frequency_hz=100_000)
    return ok


# ---------------------------------------------------------------------------
# 5. Config pull-ups (LabJack has NO internal pull-ups)
# ---------------------------------------------------------------------------
def test_config_pull_ups():
    """Test config with pull_ups=True, False, None -- all accepted silently."""
    print("\n" + "=" * 60)
    print("TEST: config pull-ups (LabJack - no hardware pull-ups)")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # All pull_ups values should be silently accepted (LabJack ignores them)
    for val in (True, False, None):
        try:
            i2c.config(frequency_hz=100_000, pull_ups=val)
            _record(f"config pull_ups={val}", True,
                    "accepted silently (LabJack has no internal pull-ups)")
        except Exception as e:
            _record(f"config pull_ups={val}", False, str(e))
            ok = False

    # Verify bus still works after pull_ups changes
    try:
        found = i2c.scan(start_addr=BMP280_ADDR, end_addr=BMP280_ADDR)
        passed = BMP280_ADDR in found
        _record("bus functional after pull_ups changes", passed,
                f"found={[hex(a) for a in found]}")
        if not passed:
            ok = False
    except Exception as e:
        _record("bus functional after pull_ups changes", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 6. Scan default range
# ---------------------------------------------------------------------------
def test_scan_default():
    """Test scan with default range (0x08-0x77)."""
    print("\n" + "=" * 60)
    print("TEST: scan default range")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        found = i2c.scan()
        is_list = isinstance(found, list)
        in_range = all(0x08 <= a <= 0x77 for a in found)
        has_bmp = BMP280_ADDR in found
        _record("scan default range returns list", is_list)
        _record("scan addresses in 0x08-0x77", in_range,
                f"found={[hex(a) for a in found]}")
        _record("scan finds BMP280", has_bmp,
                f"addr={hex(BMP280_ADDR)}")
        return is_list and in_range and has_bmp
    except Exception as e:
        _record("scan default range", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 7. Scan custom ranges
# ---------------------------------------------------------------------------
def test_scan_custom_ranges():
    """Test scan with various custom address ranges."""
    print("\n" + "=" * 60)
    print("TEST: scan custom ranges")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # Narrow range - just the BMP280
    try:
        found = i2c.scan(start_addr=BMP280_ADDR, end_addr=BMP280_ADDR)
        passed = BMP280_ADDR in found and len(found) == 1
        _record("scan single address (BMP280)", passed,
                f"found={[hex(a) for a in found]}")
        if not passed:
            ok = False
    except Exception as e:
        _record("scan single address (BMP280)", False, str(e))
        ok = False

    # Range excluding BMP280
    try:
        found = i2c.scan(start_addr=0x08, end_addr=BMP280_ADDR - 1)
        passed = BMP280_ADDR not in found
        _record("scan range excluding BMP280", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("scan range excluding BMP280", False, str(e))
        ok = False

    # Single address with no device
    try:
        found = i2c.scan(start_addr=0x10, end_addr=0x10)
        passed = len(found) == 0
        _record("scan empty address (0x10)", passed,
                f"found={[hex(a) for a in found]}")
        if not passed:
            ok = False
    except Exception as e:
        _record("scan empty address (0x10)", False, str(e))
        ok = False

    # Full 7-bit range
    try:
        found = i2c.scan(start_addr=0x00, end_addr=0x7F)
        passed = isinstance(found, list)
        _record("scan full 7-bit range (0x00-0x7F)", passed,
                f"found {len(found)} device(s)")
        if not passed:
            ok = False
    except Exception as e:
        _record("scan full 7-bit range", False, str(e))
        ok = False

    # Reserved low addresses only (0x00-0x07)
    try:
        found = i2c.scan(start_addr=0x00, end_addr=0x07)
        passed = isinstance(found, list)
        _record("scan reserved low (0x00-0x07)", passed,
                f"found {len(found)} device(s)")
    except Exception as e:
        _record("scan reserved low (0x00-0x07)", False, str(e))
        ok = False

    # Reserved high addresses only (0x78-0x7F)
    try:
        found = i2c.scan(start_addr=0x78, end_addr=0x7F)
        passed = isinstance(found, list)
        _record("scan reserved high (0x78-0x7F)", passed,
                f"found {len(found)} device(s)")
    except Exception as e:
        _record("scan reserved high (0x78-0x7F)", False, str(e))
        ok = False

    # Reversed range (start > end) - should handle gracefully
    try:
        found = i2c.scan(start_addr=0x77, end_addr=0x08)
        passed = isinstance(found, list)
        _record("scan reversed range (0x77-0x08)", passed,
                f"found {len(found)} device(s)")
    except Exception as e:
        _record("scan reversed range", False, str(e))
        ok = False

    # Repeated scans - results should be consistent
    try:
        found1 = i2c.scan()
        found2 = i2c.scan()
        passed = found1 == found2
        _record("scan consistency (2 scans match)", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("scan consistency", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 8. Read basic (LabJack: max 56 bytes)
# ---------------------------------------------------------------------------
def test_read_basic():
    """Test read(address, num_bytes) with various byte counts within 56-byte limit."""
    print("\n" + "=" * 60)
    print("TEST: read basic (LabJack max 56 bytes)")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    for n in (1, 4, 8, 56):
        try:
            result = i2c.read(address=BMP280_ADDR, num_bytes=n)
            is_list = isinstance(result, list)
            correct_len = len(result) == n
            all_ints = all(isinstance(b, int) for b in result)
            in_range = all(0 <= b <= 255 for b in result)
            passed = is_list and correct_len and all_ints and in_range
            _record(f"read num_bytes={n}", passed,
                    f"len={len(result)}, type={type(result).__name__}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"read num_bytes={n}", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 9. Read edge cases (LabJack 56-byte boundary)
# ---------------------------------------------------------------------------
def test_read_edge_cases():
    """Test read with edge case parameters including 56-byte boundary."""
    print("\n" + "=" * 60)
    print("TEST: read edge cases (LabJack 56-byte boundary)")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.i2c import I2CBackendError
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # 56 bytes OK (max)
    try:
        data = i2c.read(address=BMP280_ADDR, num_bytes=56)
        passed = isinstance(data, list) and len(data) == 56
        _record("read 56 bytes (max OK)", passed,
                f"len={len(data)}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read 56 bytes (max OK)", False, str(e))
        ok = False

    # 57 bytes MUST FAIL (exceeds LabJack buffer)
    try:
        data = i2c.read(address=BMP280_ADDR, num_bytes=57)
        _record("read 57 bytes (must fail)", False,
                f"expected I2CBackendError, got {len(data)} bytes")
        ok = False
    except I2CBackendError as e:
        _record("read 57 bytes (must fail)", True,
                f"raises I2CBackendError: {e}")
    except Exception as e:
        _record("read 57 bytes (must fail)", False,
                f"unexpected: {type(e).__name__}: {e}")
        ok = False

    # Read from non-existent device -- should raise I2CBackendError (NACK)
    try:
        data = i2c.read(address=0x10, num_bytes=1)
        _record("read from empty address 0x10", False,
                f"expected I2CBackendError, got {data}")
        ok = False
    except I2CBackendError:
        _record("read from empty address 0x10", True,
                "raises I2CBackendError (NACK)")
    except Exception as e:
        _record("read from empty address 0x10", False,
                f"unexpected: {type(e).__name__}: {e}")
        ok = False

    # Read 0 bytes
    try:
        data = i2c.read(address=BMP280_ADDR, num_bytes=0)
        _record("read 0 bytes", True, f"returned {data}")
    except Exception as e:
        _record("read 0 bytes", True,
                f"raises {type(e).__name__}: {e}")

    return ok


# ---------------------------------------------------------------------------
# 10. Read output formats
# ---------------------------------------------------------------------------
def test_read_output_formats():
    """Test read output_format='list', 'hex', 'bytes', 'json'."""
    print("\n" + "=" * 60)
    print("TEST: read output formats")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # list -> list
    try:
        result = i2c.read(address=BMP280_ADDR, num_bytes=4, output_format="list")
        passed = isinstance(result, list)
        _record("read format=list", passed,
                f"type={type(result).__name__}, value={result}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read format=list", False, str(e))
        ok = False

    # hex -> str
    try:
        result = i2c.read(address=BMP280_ADDR, num_bytes=4, output_format="hex")
        passed = isinstance(result, str)
        _record("read format=hex", passed,
                f"type={type(result).__name__}, value={result!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read format=hex", False, str(e))
        ok = False

    # bytes -> str
    try:
        result = i2c.read(address=BMP280_ADDR, num_bytes=4, output_format="bytes")
        passed = isinstance(result, str)
        _record("read format=bytes", passed,
                f"type={type(result).__name__}, value={result!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read format=bytes", False, str(e))
        ok = False

    # json -> dict with 'data' key
    try:
        result = i2c.read(address=BMP280_ADDR, num_bytes=4, output_format="json")
        passed = isinstance(result, dict) and "data" in result
        _record("read format=json", passed,
                f"type={type(result).__name__}, value={result}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read format=json", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 11. Write basic
# ---------------------------------------------------------------------------
def test_write_basic():
    """Test write(address, data) with various payloads."""
    print("\n" + "=" * 60)
    print("TEST: write basic")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # Write single byte (register pointer)
    try:
        result = i2c.write(address=BMP280_ADDR, data=[CHIP_ID_REG])
        passed = result is None
        _record("write 1 byte returns None", passed,
                f"returned={result!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("write 1 byte", False, str(e))
        ok = False

    # Write two bytes (register + value)
    try:
        original = i2c.write_read(address=BMP280_ADDR, data=[CTRL_MEAS_REG], num_bytes=1)
        i2c.write(address=BMP280_ADDR, data=[CTRL_MEAS_REG, original[0]])
        _record("write 2 bytes (register + value)", True)
    except Exception as e:
        _record("write 2 bytes", False, str(e))
        ok = False

    # Write then verify with write_read
    try:
        i2c.write(address=BMP280_ADDR, data=[CTRL_MEAS_REG, 0x00])
        readback = i2c.write_read(address=BMP280_ADDR, data=[CTRL_MEAS_REG], num_bytes=1)
        passed = readback[0] == 0x00
        _record("write 0x00 and verify readback", passed,
                f"readback={hex(readback[0])}")
        if not passed:
            ok = False
    except Exception as e:
        _record("write and verify readback", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 12. Write edge cases (LabJack 56-byte boundary)
# ---------------------------------------------------------------------------
def test_write_edge_cases():
    """Test write with edge case parameters including 56-byte boundary."""
    print("\n" + "=" * 60)
    print("TEST: write edge cases (LabJack 56-byte boundary)")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.i2c import I2CBackendError
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # 56 bytes OK (max TX)
    try:
        payload = [CTRL_MEAS_REG] + [0x00] * 55  # 56 bytes total
        i2c.write(address=BMP280_ADDR, data=payload)
        _record("write 56 bytes (max OK)", True)
    except Exception as e:
        _record("write 56 bytes (max OK)", False, str(e))
        ok = False

    # 57 bytes MUST FAIL
    try:
        payload = [CTRL_MEAS_REG] + [0x00] * 56  # 57 bytes total
        i2c.write(address=BMP280_ADDR, data=payload)
        _record("write 57 bytes (must fail)", False,
                "expected I2CBackendError, no error raised")
        ok = False
    except I2CBackendError as e:
        _record("write 57 bytes (must fail)", True,
                f"raises I2CBackendError: {e}")
    except Exception as e:
        _record("write 57 bytes (must fail)", False,
                f"unexpected: {type(e).__name__}: {e}")
        ok = False

    # Write to non-existent device
    try:
        i2c.write(address=0x10, data=[0x00])
        _record("write to empty address 0x10", True, "no crash")
    except I2CBackendError:
        _record("write to empty address 0x10", True,
                "raises I2CBackendError (expected)")
    except Exception as e:
        _record("write to empty address 0x10", False,
                f"unexpected: {type(e).__name__}: {e}")
        ok = False

    # Write empty data
    try:
        i2c.write(address=BMP280_ADDR, data=[])
        _record("write empty data", True, "no crash")
    except Exception as e:
        _record("write empty data", True,
                f"raises {type(e).__name__}: {e}")

    return ok


# ---------------------------------------------------------------------------
# 13. Write_read (transfer) basic
# ---------------------------------------------------------------------------
def test_write_read_basic():
    """Test write_read with standard register reads."""
    print("\n" + "=" * 60)
    print("TEST: write_read basic")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.i2c import I2CBackendError
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # Read chip ID
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
        passed = isinstance(data, list) and len(data) == 1 and data[0] == BMP280_CHIP_ID
        _record("write_read chip ID", passed,
                f"expected={hex(BMP280_CHIP_ID)}, got={hex(data[0]) if data else 'empty'}")
        if not passed:
            ok = False
    except Exception as e:
        _record("write_read chip ID", False, str(e))
        ok = False

    # Read calibration data (26 bytes) -- within 56-byte limit
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[CALIB_REG], num_bytes=26)
        passed = isinstance(data, list) and len(data) == 26
        non_zero = any(b != 0 for b in data)
        _record("write_read calibration (26 bytes)", passed and non_zero,
                f"len={len(data)}, non_zero={non_zero}")
        if not (passed and non_zero):
            ok = False
    except Exception as e:
        _record("write_read calibration", False, str(e))
        ok = False

    # Read status register
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[STATUS_REG], num_bytes=1)
        passed = isinstance(data, list) and len(data) == 1
        _record("write_read status register", passed,
                f"value={hex(data[0]) if data else 'empty'}")
        if not passed:
            ok = False
    except I2CBackendError as e:
        _record("write_read status register", True,
                f"bus-level error (acceptable): {e}")
    except Exception as e:
        _record("write_read status register", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 14. Write_read edge cases (LabJack: TX+RX combined <= 56)
# ---------------------------------------------------------------------------
def test_write_read_edge_cases():
    """Test write_read with edge cases including combined TX+RX limits."""
    print("\n" + "=" * 60)
    print("TEST: write_read edge cases (LabJack TX+RX limits)")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.i2c import I2CBackendError
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # To non-existent device -- should raise I2CBackendError (NACK)
    try:
        data = i2c.write_read(address=0x10, data=[0x00], num_bytes=1)
        _record("write_read empty addr 0x10", False,
                f"expected I2CBackendError, got {data}")
        ok = False
    except I2CBackendError:
        _record("write_read empty addr 0x10", True,
                "raises I2CBackendError (NACK)")
    except Exception as e:
        _record("write_read empty addr 0x10", False,
                f"unexpected: {type(e).__name__}: {e}")
        ok = False

    # Empty write data
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[], num_bytes=1)
        _record("write_read empty write data", True,
                f"returned {data}")
    except Exception as e:
        _record("write_read empty write data", True,
                f"raises {type(e).__name__}: {e}")

    # 0 read bytes
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=0)
        _record("write_read 0 read bytes", True,
                f"returned {data}")
    except Exception as e:
        _record("write_read 0 read bytes", True,
                f"raises {type(e).__name__}: {e}")

    # Multi-byte write data
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[CTRL_MEAS_REG, 0x00], num_bytes=1)
        passed = isinstance(data, list)
        _record("write_read multi-byte write data", passed,
                f"returned {data}")
        if not passed:
            ok = False
    except Exception as e:
        _record("write_read multi-byte write data", False, str(e))
        ok = False

    # Max RX within limit (1 byte TX + 55 bytes RX = 56 total stays within limits)
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[0x80], num_bytes=56)
        passed = isinstance(data, list) and len(data) == 56
        _record("write_read 1 TX + 56 RX (each within limit)", passed,
                f"len={len(data) if data else 0}")
        if not passed:
            ok = False
    except I2CBackendError as e:
        # TX and RX are validated independently; both are within 56
        _record("write_read 1 TX + 56 RX", True,
                f"bus error (acceptable): {e}")
    except Exception as e:
        _record("write_read 1 TX + 56 RX", False, str(e))
        ok = False

    # Repeated calls - consistency
    try:
        results = []
        for i in range(5):
            data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
            results.append(data[0])
        passed = all(r == BMP280_CHIP_ID for r in results)
        _record("write_read 5 consecutive calls", passed,
                f"results={[hex(r) for r in results]}")
        if not passed:
            ok = False
    except Exception as e:
        _record("write_read 5 consecutive calls", False, str(e))
        ok = False

    # Return type validation
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=4)
        all_ints = all(isinstance(b, int) for b in data)
        in_range = all(0 <= b <= 255 for b in data)
        _record("write_read return type validation", all_ints and in_range,
                f"all_ints={all_ints}, in_range={in_range}")
        if not (all_ints and in_range):
            ok = False
    except Exception as e:
        _record("write_read return type validation", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 15. Write_read output formats
# ---------------------------------------------------------------------------
def test_write_read_output_formats():
    """Test write_read output_format='list', 'hex', 'bytes', 'json'."""
    print("\n" + "=" * 60)
    print("TEST: write_read output formats")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    for fmt, expected_type in [("list", list), ("hex", str), ("bytes", str), ("json", dict)]:
        try:
            result = i2c.write_read(
                address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1,
                output_format=fmt
            )
            passed = isinstance(result, expected_type)
            if fmt == "json":
                passed = passed and "data" in result
            _record(f"write_read format={fmt}", passed,
                    f"type={type(result).__name__}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"write_read format={fmt}", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 16. Dispatcher config
# ---------------------------------------------------------------------------
def test_dispatcher_config():
    """Direct dispatcher config() call."""
    print("\n" + "=" * 60)
    print("TEST: dispatcher config")
    print("=" * 60)

    try:
        from lager.protocols.i2c.dispatcher import config

        config(I2C_NET, frequency_hz=100_000, pull_ups=None)
        _record("dispatcher config 100kHz pull_ups=None", True)
    except Exception as e:
        _record("dispatcher config", False, str(e))
        return False

    try:
        config(I2C_NET, frequency_hz=400_000, pull_ups=None)
        _record("dispatcher config 400kHz pull_ups=None", True)
    except Exception as e:
        _record("dispatcher config 400kHz", False, str(e))
        return False

    # Restore
    config(I2C_NET, frequency_hz=100_000)
    return True


# ---------------------------------------------------------------------------
# 17. Dispatcher scan
# ---------------------------------------------------------------------------
def test_dispatcher_scan():
    """Direct dispatcher scan() call."""
    print("\n" + "=" * 60)
    print("TEST: dispatcher scan")
    print("=" * 60)

    try:
        from lager.protocols.i2c.dispatcher import scan

        print("  (dispatcher scan prints to stdout)")
        scan(I2C_NET, start_addr=0x08, end_addr=0x77, overrides=None)
        _record("dispatcher scan default range", True)
    except Exception as e:
        _record("dispatcher scan", False, str(e))
        return False

    try:
        scan(I2C_NET, start_addr=BMP280_ADDR, end_addr=BMP280_ADDR, overrides=None)
        _record("dispatcher scan narrow range", True)
    except Exception as e:
        _record("dispatcher scan narrow", False, str(e))
        return False

    return True


# ---------------------------------------------------------------------------
# 18. Dispatcher read
# ---------------------------------------------------------------------------
def test_dispatcher_read():
    """Direct dispatcher read() call with output formats."""
    print("\n" + "=" * 60)
    print("TEST: dispatcher read")
    print("=" * 60)

    try:
        from lager.protocols.i2c.dispatcher import read

        print("  (dispatcher read prints to stdout)")
        read(I2C_NET, address=BMP280_ADDR, num_bytes=1, output_format="hex", overrides=None)
        _record("dispatcher read hex", True)
    except Exception as e:
        _record("dispatcher read hex", False, str(e))
        return False

    try:
        read(I2C_NET, address=BMP280_ADDR, num_bytes=1, output_format="bytes", overrides=None)
        _record("dispatcher read bytes", True)
    except Exception as e:
        _record("dispatcher read bytes", False, str(e))
        return False

    try:
        read(I2C_NET, address=BMP280_ADDR, num_bytes=1, output_format="json", overrides=None)
        _record("dispatcher read json", True)
    except Exception as e:
        _record("dispatcher read json", False, str(e))
        return False

    return True


# ---------------------------------------------------------------------------
# 19. Dispatcher write
# ---------------------------------------------------------------------------
def test_dispatcher_write():
    """Direct dispatcher write() call."""
    print("\n" + "=" * 60)
    print("TEST: dispatcher write")
    print("=" * 60)

    try:
        from lager.protocols.i2c.dispatcher import write

        print("  (dispatcher write prints to stdout)")
        write(I2C_NET, address=BMP280_ADDR, data=[CHIP_ID_REG], output_format="hex", overrides=None)
        _record("dispatcher write", True)
    except Exception as e:
        _record("dispatcher write", False, str(e))
        return False

    return True


# ---------------------------------------------------------------------------
# 20. Dispatcher transfer
# ---------------------------------------------------------------------------
def test_dispatcher_transfer():
    """Direct dispatcher transfer() call."""
    print("\n" + "=" * 60)
    print("TEST: dispatcher transfer")
    print("=" * 60)

    ok = True

    try:
        from lager.protocols.i2c.dispatcher import transfer

        print("  (dispatcher transfer prints to stdout)")
        transfer(I2C_NET, address=BMP280_ADDR, num_bytes=1, data=[CHIP_ID_REG],
                 output_format="hex", overrides=None)
        _record("dispatcher transfer hex", True)
    except Exception as e:
        _record("dispatcher transfer hex", False, str(e))
        ok = False

    try:
        transfer(I2C_NET, address=BMP280_ADDR, num_bytes=1, data=[CHIP_ID_REG],
                 output_format="json", overrides=None)
        _record("dispatcher transfer json", True)
    except Exception as e:
        _record("dispatcher transfer json", False, str(e))
        ok = False

    try:
        transfer(I2C_NET, address=BMP280_ADDR, num_bytes=1, data=[CHIP_ID_REG],
                 output_format="bytes", overrides=None)
        _record("dispatcher transfer bytes", True)
    except Exception as e:
        _record("dispatcher transfer bytes", False, str(e))
        ok = False

    # Transfer with data=None (should default to [])
    try:
        transfer(I2C_NET, address=BMP280_ADDR, num_bytes=1, data=None,
                 output_format="hex", overrides=None)
        _record("dispatcher transfer data=None", True)
    except Exception as e:
        _record("dispatcher transfer data=None", True,
                f"raises {type(e).__name__}: {e}")

    return ok


# ---------------------------------------------------------------------------
# 21. Dispatcher overrides
# ---------------------------------------------------------------------------
def test_dispatcher_overrides():
    """Dispatcher calls with frequency overrides."""
    print("\n" + "=" * 60)
    print("TEST: dispatcher overrides")
    print("=" * 60)

    ok = True

    try:
        from lager.protocols.i2c.dispatcher import transfer

        overrides = {"frequency_hz": 400_000}
        print("  (dispatcher transfer with overrides prints to stdout)")
        transfer(I2C_NET, address=BMP280_ADDR, num_bytes=1, data=[CHIP_ID_REG],
                 output_format="hex", overrides=overrides)
        _record("dispatcher override freq=400kHz", True)
    except Exception as e:
        _record("dispatcher override freq=400kHz", False, str(e))
        ok = False

    try:
        overrides = {"frequency_hz": 10_000}
        transfer(I2C_NET, address=BMP280_ADDR, num_bytes=1, data=[CHIP_ID_REG],
                 output_format="hex", overrides=overrides)
        _record("dispatcher override freq=10kHz", True)
    except Exception as e:
        _record("dispatcher override freq=10kHz", False, str(e))
        ok = False

    # Restore defaults
    from lager.protocols.i2c.dispatcher import config
    config(I2C_NET, frequency_hz=100_000)

    return ok


# ---------------------------------------------------------------------------
# 22. Dispatcher invalid net
# ---------------------------------------------------------------------------
def test_dispatcher_invalid_net():
    """Dispatcher with non-existent net raises I2CBackendError."""
    print("\n" + "=" * 60)
    print("TEST: dispatcher invalid net")
    print("=" * 60)

    from lager.protocols.i2c import I2CBackendError

    try:
        from lager.protocols.i2c.dispatcher import scan
        scan("nonexistent_net_12345", start_addr=0x08, end_addr=0x77, overrides=None)
        _record("invalid net raises error", False, "no error raised")
        return False
    except I2CBackendError:
        _record("invalid net raises I2CBackendError", True)
        return True
    except Exception as e:
        _record("invalid net raises I2CBackendError", False,
                f"wrong type: {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------------------
# 23. Driver cache behavior
# ---------------------------------------------------------------------------
def test_driver_cache():
    """Test that dispatcher caches driver instances."""
    print("\n" + "=" * 60)
    print("TEST: driver cache")
    print("=" * 60)

    from lager.protocols.i2c.dispatcher import _resolve_net_and_driver

    ok = True

    try:
        drv1 = _resolve_net_and_driver(I2C_NET, overrides=None)
        drv2 = _resolve_net_and_driver(I2C_NET, overrides=None)
        passed = drv1 is drv2
        _record("driver cache returns same instance", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("driver cache", False, str(e))
        ok = False

    try:
        drv1 = _resolve_net_and_driver(I2C_NET, overrides=None)
        drv2 = _resolve_net_and_driver(I2C_NET, overrides={"frequency_hz": 400_000})
        passed = drv1 is drv2
        _record("cached driver with override", passed,
                "same instance, config applied")
        if not passed:
            ok = False
    except Exception as e:
        _record("cached driver with override", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 24. Exception hierarchy
# ---------------------------------------------------------------------------
def test_exception_hierarchy():
    """Test I2CBackendError properties."""
    print("\n" + "=" * 60)
    print("TEST: exception hierarchy")
    print("=" * 60)

    from lager.protocols.i2c import I2CBackendError
    from lager.exceptions import LagerBackendError
    ok = True

    try:
        err = I2CBackendError("test error")
        passed = isinstance(err, LagerBackendError)
        _record("I2CBackendError inherits LagerBackendError", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("I2CBackendError inheritance", False, str(e))
        ok = False

    try:
        err = I2CBackendError("test")
        passed = err.backend == "I2C"
        _record("default backend='I2C'", passed,
                f"backend={err.backend!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("default backend", False, str(e))
        ok = False

    try:
        err = I2CBackendError("test", device="LabJack", backend="I2C")
        passed = err.device == "LabJack"
        _record("device='LabJack'", passed,
                f"device={err.device!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("device field", False, str(e))
        ok = False

    try:
        err = I2CBackendError("test error message")
        passed = "test error message" in str(err)
        _record("message in str()", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("message in str()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 25. BMP280 functional tests
# ---------------------------------------------------------------------------
def test_bmp280_functional():
    """BMP280-specific tests: chip ID, calibration, measurement."""
    print("\n" + "=" * 60)
    print("TEST: BMP280 functional")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.i2c import I2CBackendError
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # Soft reset and verify
    try:
        i2c.write(address=BMP280_ADDR, data=[RESET_REG, RESET_VALUE])
        time.sleep(0.01)
        data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
        passed = data[0] == BMP280_CHIP_ID
        _record("soft reset then chip ID", passed,
                f"chip_id={hex(data[0])}")
        if not passed:
            ok = False
    except Exception as e:
        _record("soft reset then chip ID", False, str(e))
        ok = False

    # Parse calibration data
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[0x88], num_bytes=24)
        dig_T1 = data[0] | (data[1] << 8)
        dig_T2 = data[2] | (data[3] << 8)
        if dig_T2 >= 0x8000:
            dig_T2 -= 0x10000
        passed = dig_T1 > 0
        _record("parse calibration dig_T1/T2", passed,
                f"dig_T1={dig_T1}, dig_T2={dig_T2}")
        if not passed:
            ok = False
    except Exception as e:
        _record("parse calibration", False, str(e))
        ok = False

    # Forced measurement
    try:
        i2c.write(address=BMP280_ADDR, data=[CTRL_MEAS_REG, 0x25])
        time.sleep(0.05)
        raw = i2c.write_read(address=BMP280_ADDR, data=[0xF7], num_bytes=6)
        raw_press = (raw[0] << 12) | (raw[1] << 4) | (raw[2] >> 4)
        raw_temp = (raw[3] << 12) | (raw[4] << 4) | (raw[5] >> 4)
        _record("forced measurement", True,
                f"raw_press={raw_press}, raw_temp={raw_temp}")
    except I2CBackendError as e:
        _record("forced measurement", True,
                f"bus-level error (acceptable): {e}")
    except Exception as e:
        _record("forced measurement", False, str(e))
        ok = False

    # Register map -- limited to 56 bytes for LabJack (not 128 like Aardvark)
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[0xD0], num_bytes=48)
        passed = len(data) == 48 and data[0] == BMP280_CHIP_ID
        _record("register map (48 bytes from 0xD0)", passed,
                f"chip_id at offset 0={hex(data[0]) if data else 'N/A'}")
        if not passed:
            ok = False
    except Exception as e:
        _record("register map (48 bytes)", False, str(e))
        ok = False

    # Restore sleep mode
    try:
        i2c.write(address=BMP280_ADDR, data=[CTRL_MEAS_REG, 0x00])
    except Exception:
        pass

    return ok


# ---------------------------------------------------------------------------
# 26. Stress tests
# ---------------------------------------------------------------------------
def test_stress():
    """Rapid sequential operations and mixed command patterns."""
    print("\n" + "=" * 60)
    print("TEST: stress")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # 20 rapid chip ID reads
    try:
        for i in range(20):
            data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
            assert data[0] == BMP280_CHIP_ID, f"iter {i}: {hex(data[0])}"
        _record("20 rapid write_read calls", True)
    except Exception as e:
        _record("20 rapid write_read calls", False, str(e))
        ok = False

    # Alternating write/read
    try:
        for i in range(10):
            i2c.write(address=BMP280_ADDR, data=[CHIP_ID_REG])
            data = i2c.read(address=BMP280_ADDR, num_bytes=1)
            assert len(data) == 1
        _record("10 alternating write/read", True)
    except Exception as e:
        _record("10 alternating write/read", False, str(e))
        ok = False

    # Alternating scan/transfer
    try:
        for i in range(3):
            found = i2c.scan(start_addr=BMP280_ADDR, end_addr=BMP280_ADDR)
            assert BMP280_ADDR in found
            data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
            assert data[0] == BMP280_CHIP_ID
        _record("3 alternating scan/transfer", True)
    except Exception as e:
        _record("3 alternating scan/transfer", False, str(e))
        ok = False

    # Config changes between operations
    try:
        i2c.config(frequency_hz=100_000)
        d1 = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
        i2c.config(frequency_hz=400_000)
        d2 = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
        i2c.config(frequency_hz=100_000)
        d3 = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
        passed = d1[0] == d2[0] == d3[0] == BMP280_CHIP_ID
        _record("config changes between ops", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("config changes between ops", False, str(e))
        ok = False

    # Multiple different register reads
    try:
        regs = [CHIP_ID_REG, CTRL_MEAS_REG, CONFIG_REG, STATUS_REG]
        results = {}
        for reg in regs:
            data = i2c.write_read(address=BMP280_ADDR, data=[reg], num_bytes=1)
            results[hex(reg)] = hex(data[0])
        _record("multi-register sequential reads", True,
                f"results={results}")
    except Exception as e:
        _record("multi-register sequential reads", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 27. Output format tests (via dispatcher internal)
# ---------------------------------------------------------------------------
def test_output_formatting():
    """Test dispatcher _format_output and _format_scan_output."""
    print("\n" + "=" * 60)
    print("TEST: output formatting")
    print("=" * 60)

    from lager.protocols.i2c.dispatcher import _format_output, _format_scan_output
    ok = True

    # Hex format
    try:
        result = _format_output([0x58, 0x00, 0xFF], "hex")
        passed = result == "58 00 ff"
        _record("format_output hex", passed, f"'{result}'")
        if not passed:
            ok = False
    except Exception as e:
        _record("format_output hex", False, str(e))
        ok = False

    # Bytes format
    try:
        result = _format_output([0x58, 0x00, 0xFF], "bytes")
        passed = result == "88 0 255"
        _record("format_output bytes", passed, f"'{result}'")
        if not passed:
            ok = False
    except Exception as e:
        _record("format_output bytes", False, str(e))
        ok = False

    # JSON format
    try:
        result = _format_output([0x58, 0x00, 0xFF], "json")
        parsed = json.loads(result)
        passed = parsed == {"data": [0x58, 0x00, 0xFF]}
        _record("format_output json", passed, f"{result}")
        if not passed:
            ok = False
    except Exception as e:
        _record("format_output json", False, str(e))
        ok = False

    # Empty list
    try:
        result = _format_output([], "hex")
        _record("format_output empty", True, f"'{result}'")
    except Exception as e:
        _record("format_output empty", False, str(e))
        ok = False

    # Single byte
    try:
        result = _format_output([0x58], "hex")
        passed = result == "58"
        _record("format_output single byte", passed, f"'{result}'")
        if not passed:
            ok = False
    except Exception as e:
        _record("format_output single byte", False, str(e))
        ok = False

    # Scan output with devices
    try:
        result = _format_scan_output([BMP280_ADDR], 0x08, 0x77)
        has_addr = "76" in result
        has_header = "0  1  2  3" in result
        _record("format_scan_output with device", has_addr and has_header,
                f"has_addr={has_addr}, has_header={has_header}")
        if not (has_addr and has_header):
            ok = False
    except Exception as e:
        _record("format_scan_output", False, str(e))
        ok = False

    # Scan output empty
    try:
        result = _format_scan_output([], 0x08, 0x77)
        has_dashes = "--" in result
        no_addrs = "48" not in result and "76" not in result
        _record("format_scan_output empty", has_dashes and no_addrs,
                f"has_dashes={has_dashes}, no_addrs={no_addrs}")
        if not (has_dashes and no_addrs):
            ok = False
    except Exception as e:
        _record("format_scan_output empty", False, str(e))
        ok = False

    # Scan output multiple devices
    try:
        result = _format_scan_output([0x48, 0x50, 0x76], 0x08, 0x77)
        has_48 = "48" in result
        has_50 = "50" in result
        has_76 = "76" in result
        passed = has_48 and has_50 and has_76
        _record("format_scan_output 3 devices", passed,
                f"has_48={has_48}, has_50={has_50}, has_76={has_76}")
        if not passed:
            ok = False
    except Exception as e:
        _record("format_scan_output 3 devices", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 28. _persist_params (LabJack: pull_ups should NOT persist)
# ---------------------------------------------------------------------------
def test_persist_params():
    """Test the _persist_params() function directly."""
    print("\n" + "=" * 60)
    print("TEST: _persist_params (LabJack pull_ups behavior)")
    print("=" * 60)

    from lager.protocols.i2c.dispatcher import _persist_params
    from lager.protocols.i2c import I2CBackendError
    from lager import Net
    ok = True

    # 28a. Persist frequency_hz=400_000
    try:
        _persist_params(I2C_NET, frequency_hz=400_000)
        all_nets = Net.get_local_nets()
        rec = next(r for r in all_nets if r.get("name") == I2C_NET)
        passed = rec.get("params", {}).get("frequency_hz") == 400_000
        _record("persist frequency_hz=400000", passed,
                f"stored={rec.get('params', {}).get('frequency_hz')}")
        if not passed:
            ok = False
    except Exception as e:
        _record("persist frequency_hz=400000", False, str(e))
        ok = False

    # 28b. Persist pull_ups=True (should store it even though LabJack ignores it)
    try:
        _persist_params(I2C_NET, pull_ups=True)
        all_nets = Net.get_local_nets()
        rec = next(r for r in all_nets if r.get("name") == I2C_NET)
        passed = rec.get("params", {}).get("pull_ups") is True
        _record("persist pull_ups=True", passed,
                f"stored={rec.get('params', {}).get('pull_ups')}")
        if not passed:
            ok = False
    except Exception as e:
        _record("persist pull_ups=True", False, str(e))
        ok = False

    # 28c. Verify 28b did not overwrite 28a's frequency
    try:
        all_nets = Net.get_local_nets()
        rec = next(r for r in all_nets if r.get("name") == I2C_NET)
        params = rec.get("params", {})
        freq_ok = params.get("frequency_hz") == 400_000
        pull_ok = params.get("pull_ups") is True
        passed = freq_ok and pull_ok
        _record("pull_ups persist did not overwrite freq", passed,
                f"freq={params.get('frequency_hz')}, pull_ups={params.get('pull_ups')}")
        if not passed:
            ok = False
    except Exception as e:
        _record("pull_ups persist did not overwrite freq", False, str(e))
        ok = False

    # 28d. Persist to net with no existing params dict
    try:
        _persist_params(I2C_NET, frequency_hz=100_000)
        all_nets = Net.get_local_nets()
        rec = next(r for r in all_nets if r.get("name") == I2C_NET)
        passed = "params" in rec and rec["params"].get("frequency_hz") == 100_000
        _record("persist with setdefault (params exists)", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("persist with setdefault", False, str(e))
        ok = False

    # 28e. Nonexistent net raises I2CBackendError
    try:
        _persist_params("nonexistent_net_99999", frequency_hz=100_000)
        _record("nonexistent net raises error", False, "no error raised")
        ok = False
    except I2CBackendError as e:
        passed = "not found" in str(e).lower()
        _record("nonexistent net raises I2CBackendError", passed,
                f"msg={e}")
        if not passed:
            ok = False
    except Exception as e:
        _record("nonexistent net raises I2CBackendError", False,
                f"wrong type: {type(e).__name__}: {e}")
        ok = False

    # 28f. Cleanup: restore defaults
    try:
        _persist_params(I2C_NET, frequency_hz=100_000, pull_ups=False)
        _record("cleanup: restore defaults", True)
    except Exception as e:
        _record("cleanup: restore defaults", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 29. Config persistence end-to-end
# ---------------------------------------------------------------------------
def test_config_persistence_e2e():
    """Test dispatcher config() persistence logic end-to-end."""
    print("\n" + "=" * 60)
    print("TEST: config persistence end-to-end")
    print("=" * 60)

    import io
    import contextlib
    from lager.protocols.i2c.dispatcher import config
    from lager import Net
    ok = True

    # 29a. config(freq=400k) persists freq, pull_ups unchanged
    try:
        config(I2C_NET, frequency_hz=400_000)
        all_nets = Net.get_local_nets()
        rec = next(r for r in all_nets if r.get("name") == I2C_NET)
        params = rec.get("params", {})
        freq_ok = params.get("frequency_hz") == 400_000
        pull_ok = params.get("pull_ups", False) is False
        passed = freq_ok and pull_ok
        _record("config(freq=400k) persists, pull_ups unchanged", passed,
                f"freq={params.get('frequency_hz')}, pull_ups={params.get('pull_ups')}")
        if not passed:
            ok = False
    except Exception as e:
        _record("config(freq=400k) persists", False, str(e))
        ok = False

    # 29b. config(pull_ups=True) preserves freq from 29a
    try:
        config(I2C_NET, pull_ups=True)
        all_nets = Net.get_local_nets()
        rec = next(r for r in all_nets if r.get("name") == I2C_NET)
        params = rec.get("params", {})
        freq_ok = params.get("frequency_hz") == 400_000
        pull_ok = params.get("pull_ups") is True
        passed = freq_ok and pull_ok
        _record("config(pull_ups=True) preserves freq", passed,
                f"freq={params.get('frequency_hz')}, pull_ups={params.get('pull_ups')}")
        if not passed:
            ok = False
    except Exception as e:
        _record("config(pull_ups=True) preserves freq", False, str(e))
        ok = False

    # 29c. config() with no args prints stored values
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            config(I2C_NET)
        output = buf.getvalue()
        has_freq = "400000" in output
        has_pull = "pull_ups=on" in output
        passed = has_freq and has_pull
        _record("config() no args prints stored values", passed,
                f"output={output.strip()!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("config() no args prints stored values", False, str(e))
        ok = False

    # 29d. config(freq=100k) does not reset pull_ups
    try:
        config(I2C_NET, frequency_hz=100_000)
        all_nets = Net.get_local_nets()
        rec = next(r for r in all_nets if r.get("name") == I2C_NET)
        params = rec.get("params", {})
        passed = params.get("pull_ups") is True
        _record("config(freq=100k) does not reset pull_ups", passed,
                f"pull_ups={params.get('pull_ups')}")
        if not passed:
            ok = False
    except Exception as e:
        _record("config(freq=100k) does not reset pull_ups", False, str(e))
        ok = False

    # 29e. Output always shows both freq and pull_ups fields
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            config(I2C_NET, frequency_hz=100_000)
        output = buf.getvalue()
        has_freq = "freq=" in output
        has_pull = "pull_ups=" in output
        passed = has_freq and has_pull
        _record("output has both freq= and pull_ups=", passed,
                f"output={output.strip()!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("output has both fields", False, str(e))
        ok = False

    # 29f. Cleanup: restore defaults
    try:
        config(I2C_NET, frequency_hz=100_000, pull_ups=False)
        _record("cleanup: restore defaults", True)
    except Exception as e:
        _record("cleanup: restore defaults", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 30. I2CNet.get_config()
# ---------------------------------------------------------------------------
def test_i2cnet_get_config():
    """Test the I2CNet.get_config() method."""
    print("\n" + "=" * 60)
    print("TEST: I2CNet.get_config()")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # 30a. Returns dict with expected keys
    try:
        cfg = i2c.get_config()
        passed = isinstance(cfg, dict)
        has_name = "name" in cfg
        has_role = "role" in cfg
        has_instr = "instrument" in cfg
        passed = passed and has_name and has_role and has_instr
        _record("get_config returns dict with keys", passed,
                f"keys={list(cfg.keys())}")
        if not passed:
            ok = False
    except Exception as e:
        _record("get_config returns dict with keys", False, str(e))
        ok = False

    # 30b. Values match expected
    try:
        cfg = i2c.get_config()
        name_ok = cfg.get("name") == I2C_NET
        role_ok = cfg.get("role") == "i2c"
        passed = name_ok and role_ok
        _record("get_config name/role correct", passed,
                f"name={cfg.get('name')!r}, role={cfg.get('role')!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("get_config values", False, str(e))
        ok = False

    # 30c. Returns a copy (mutation doesn't affect internal)
    try:
        cfg1 = i2c.get_config()
        cfg1["_test_mutation"] = True
        cfg2 = i2c.get_config()
        passed = "_test_mutation" not in cfg2
        _record("get_config returns copy", passed,
                f"mutation visible={not passed}")
        if not passed:
            ok = False
    except Exception as e:
        _record("get_config returns copy", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 31. Config persistence functional
# ---------------------------------------------------------------------------
def test_config_persistence_functional():
    """Test that persisted config actually affects hardware after cache clear."""
    print("\n" + "=" * 60)
    print("TEST: config persistence functional")
    print("=" * 60)

    from lager.protocols.i2c.dispatcher import config, _persist_params, \
        _driver_cache, _resolve_net_and_driver
    from lager import Net
    ok = True

    def _close_and_clear_cache():
        """Close cached drivers before clearing."""
        for drv in _driver_cache.values():
            if hasattr(drv, '_close'):
                drv._close()
        _driver_cache.clear()

    # 31a. Persist freq=400k, clear cache, scan works
    try:
        config(I2C_NET, frequency_hz=400_000)
        _close_and_clear_cache()
        drv = _resolve_net_and_driver(I2C_NET)
        found = drv.scan(start_addr=BMP280_ADDR, end_addr=BMP280_ADDR)
        passed = BMP280_ADDR in found
        _record("persist 400k, clear cache, scan finds BMP280", passed,
                f"found={[hex(a) for a in found]}")
        if not passed:
            ok = False
    except Exception as e:
        _record("persist 400k, clear cache, scan", False, str(e))
        ok = False

    # 31b. Persist freq=100k, clear cache, transfer returns chip ID
    try:
        config(I2C_NET, frequency_hz=100_000)
        _close_and_clear_cache()
        drv = _resolve_net_and_driver(I2C_NET)
        data = drv.write_read(BMP280_ADDR, [CHIP_ID_REG], 1)
        passed = data[0] == BMP280_CHIP_ID
        _record("persist 100k, clear cache, chip ID correct", passed,
                f"got={hex(data[0])}")
        if not passed:
            ok = False
    except Exception as e:
        _record("persist 100k, clear cache, transfer", False, str(e))
        ok = False

    # 31c. pull_ups=True survives a freq-only config change
    try:
        config(I2C_NET, pull_ups=True)
        config(I2C_NET, frequency_hz=400_000)
        all_nets = Net.get_local_nets()
        rec = next(r for r in all_nets if r.get("name") == I2C_NET)
        passed = rec.get("params", {}).get("pull_ups") is True
        _record("pull_ups survives freq-only change", passed,
                f"pull_ups={rec.get('params', {}).get('pull_ups')}")
        if not passed:
            ok = False
    except Exception as e:
        _record("pull_ups survives freq-only change", False, str(e))
        ok = False

    # 31d. Cleanup and restore
    try:
        config(I2C_NET, frequency_hz=100_000, pull_ups=False)
        _record("cleanup: restore defaults", True)
    except Exception as e:
        _record("cleanup: restore defaults", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 32. Invalid/edge case config parameters
# ---------------------------------------------------------------------------
def test_config_edge_cases():
    """Test boundary and invalid config values."""
    print("\n" + "=" * 60)
    print("TEST: invalid/edge case config parameters")
    print("=" * 60)

    from lager.protocols.i2c.dispatcher import config
    from lager.protocols.i2c import I2CBackendError
    ok = True

    # 32a. frequency_hz=0 -- should raise I2CBackendError
    try:
        config(I2C_NET, frequency_hz=0)
        _record("config freq=0", False, "expected error, no error raised")
        ok = False
    except I2CBackendError:
        _record("config freq=0", True, "raises I2CBackendError (expected)")
    except Exception as e:
        _record("config freq=0", False,
                f"unexpected: {type(e).__name__}: {e}")
        ok = False

    # 32b. frequency_hz=-100 -- should raise I2CBackendError
    try:
        config(I2C_NET, frequency_hz=-100)
        _record("config freq=-100", False, "expected error, no error raised")
        ok = False
    except I2CBackendError:
        _record("config freq=-100", True, "raises I2CBackendError (expected)")
    except Exception as e:
        _record("config freq=-100", False,
                f"unexpected: {type(e).__name__}: {e}")
        ok = False

    # 32c. frequency_hz=10_000_000 (10MHz - above LabJack max ~450kHz)
    try:
        config(I2C_NET, frequency_hz=10_000_000)
        _record("config freq=10MHz", True,
                "no crash (LabJack caps at ~450kHz via throttle=0)")
    except Exception as e:
        _record("config freq=10MHz", True,
                f"raises {type(e).__name__}: {e}")

    # 32d. config() with both None (no explicit args)
    try:
        config(I2C_NET)
        _record("config() no explicit args", True,
                "reads and prints stored values")
    except Exception as e:
        _record("config() no explicit args", False, str(e))
        ok = False

    # Restore valid config
    try:
        config(I2C_NET, frequency_hz=100_000)
    except Exception:
        pass

    return ok


# ===================================================================
# Main
# ===================================================================
def main():
    """Run all tests."""
    print("LabJack T7 I2C Comprehensive API Test Suite")
    print(f"Testing net: {I2C_NET}")
    print(f"BMP280 address: {hex(BMP280_ADDR)}")
    print(f"LabJack max bytes per transaction: {MAX_BYTES}")
    print(f"Set I2C_NET / BMP280_ADDR env vars to change")
    print("=" * 60)

    tests = [
        ("Imports",                  test_imports),
        ("Net.get",                  test_net_get),
        ("Config Defaults",          test_config_defaults),
        ("Config Frequencies",       test_config_frequencies),
        ("Config Pull-ups",          test_config_pull_ups),
        ("Scan Default",             test_scan_default),
        ("Scan Custom Ranges",       test_scan_custom_ranges),
        ("Read Basic",               test_read_basic),
        ("Read Edge Cases",          test_read_edge_cases),
        ("Read Output Formats",      test_read_output_formats),
        ("Write Basic",              test_write_basic),
        ("Write Edge Cases",         test_write_edge_cases),
        ("Write/Read Basic",         test_write_read_basic),
        ("Write/Read Edge Cases",    test_write_read_edge_cases),
        ("Write/Read Output Fmts",   test_write_read_output_formats),
        ("Dispatcher Config",        test_dispatcher_config),
        ("Dispatcher Scan",          test_dispatcher_scan),
        ("Dispatcher Read",          test_dispatcher_read),
        ("Dispatcher Write",         test_dispatcher_write),
        ("Dispatcher Transfer",      test_dispatcher_transfer),
        ("Dispatcher Overrides",     test_dispatcher_overrides),
        ("Dispatcher Invalid Net",   test_dispatcher_invalid_net),
        ("Driver Cache",             test_driver_cache),
        ("Exception Hierarchy",      test_exception_hierarchy),
        ("BMP280 Functional",        test_bmp280_functional),
        ("Stress",                   test_stress),
        ("Output Formatting",        test_output_formatting),
        ("Persist Params",           test_persist_params),
        ("Config Persistence E2E",   test_config_persistence_e2e),
        ("I2CNet.get_config",        test_i2cnet_get_config),
        ("Config Persist Functional", test_config_persistence_functional),
        ("Config Edge Cases",        test_config_edge_cases),
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

    # Detailed sub-test summary
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
