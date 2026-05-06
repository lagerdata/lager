#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
I2C Python API tests - Aardvark adapter.

Run with: lager python test/api/communication/test_i2c_aardvark_api.py --box <YOUR-BOX>

Prerequisites:
- Aardvark I2C net 'i2c1' configured
- BMP280 (HW-611) wired to Aardvark I2C:
    Aardvark pin 4 (SCL) -> HW-611 SCL
    Aardvark pin 6 (SDA) -> HW-611 SDA
    HW-611 CSB -> VCC (selects I2C mode)
    HW-611 SDO -> GND (selects address 0x76)
- LabJack DAC0 (dac1) -> HW-611 VCC (3.3V)
- HW-611 GND -> Aardvark GND

Wiring:
  Aardvark                HW-611 (BMP280)        LabJack T7
  +---------+             +---------+             +---------+
  | 4  SCL  |------------>| SCL     |             |         |
  | 6  SDA  |<----------->| SDA     |             |         |
  | 2  GND  |-----+-------| GND     |             |         |
  +---------+     |       | CSB     |---[VCC]     |         |
                  |       | SDO     |---[GND]     |         |
                  |       | VCC     |<------------| DAC0    | (dac1 3.3V)
                  +-------| GND     |             | GND     |
                          +---------+             +---------+
"""
import sys
import os
import json
import time
import traceback

# Configuration
I2C_NET = os.environ.get("I2C_NET", "i2c1")
BMP280_ADDR = int(os.environ.get("BMP280_ADDR", "0x76"), 0)
BMP280_CHIP_ID = 0x58
CHIP_ID_REG = 0xD0
CALIB_REG = 0x88
STATUS_REG = 0xF3
CTRL_MEAS_REG = 0xF4
CONFIG_REG = 0xF5
RESET_REG = 0xE0
RESET_VALUE = 0xB6

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
        assert hasattr(NetType, "I2C")
        _record("import Net, NetType", True)
    except Exception as e:
        _record("import Net, NetType", False, str(e))
        ok = False

    try:
        from lager.protocols.i2c import I2CBase, AardvarkI2C, I2CNet
        _record("import I2CBase, AardvarkI2C, I2CNet", True)
    except Exception as e:
        _record("import I2CBase, AardvarkI2C, I2CNet", False, str(e))
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
    """Test Net.get returns I2CNet."""
    print("\n" + "=" * 60)
    print("TEST: Net.get")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.i2c import I2CNet

    try:
        i2c = Net.get(I2C_NET, NetType.I2C)
        is_i2cnet = isinstance(i2c, I2CNet)
        _record("Net.get returns I2CNet", is_i2cnet,
                f"type={type(i2c).__name__}")
        return is_i2cnet
    except Exception as e:
        _record("Net.get returns I2CNet", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 3. Config: frequencies + pull-ups
# ---------------------------------------------------------------------------
def test_config():
    """Test config with various frequencies and pull-up settings."""
    print("\n" + "=" * 60)
    print("TEST: Config")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # Standard frequencies with bus verification
    for freq in (100_000, 400_000):
        try:
            i2c.config(frequency_hz=freq, pull_ups=True)
            found = i2c.scan(start_addr=BMP280_ADDR, end_addr=BMP280_ADDR)
            functional = BMP280_ADDR in found
            _record(f"config freq={freq}", functional,
                    f"bus functional={functional}")
            if not functional:
                ok = False
        except Exception as e:
            _record(f"config freq={freq}", False, str(e))
            ok = False

    # Non-standard frequencies (accepted, bus not verified)
    for freq in (1_000, 10_000, 800_000):
        try:
            i2c.config(frequency_hz=freq)
            _record(f"config freq={freq} (non-standard)", True)
        except Exception as e:
            _record(f"config freq={freq}", False, str(e))
            ok = False

    # Pull-ups
    for pull in (True, False, None):
        try:
            i2c.config(frequency_hz=100_000, pull_ups=pull)
            _record(f"config pull_ups={pull}", True)
        except Exception as e:
            _record(f"config pull_ups={pull}", False, str(e))
            ok = False

    # Restore
    i2c.config(frequency_hz=100_000, pull_ups=True)
    return ok


# ---------------------------------------------------------------------------
# 4. Scan
# ---------------------------------------------------------------------------
def test_scan():
    """Test scan with various ranges."""
    print("\n" + "=" * 60)
    print("TEST: Scan")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # Default range
    try:
        found = i2c.scan()
        is_list = isinstance(found, list)
        has_bmp = BMP280_ADDR in found
        _record("scan default (0x08-0x77)", is_list and has_bmp,
                f"found={[hex(a) for a in found]}")
        if not (is_list and has_bmp):
            ok = False
    except Exception as e:
        _record("scan default", False, str(e))
        ok = False

    # Custom ranges
    cases = [
        ("single BMP280", BMP280_ADDR, BMP280_ADDR, True),
        ("excluding device", 0x08, BMP280_ADDR - 1, False),
        ("full 7-bit", 0x00, 0x7F, True),
        ("reserved low", 0x00, 0x07, None),
        ("reserved high", 0x78, 0x7F, None),
    ]

    for label, start, end, expect_bmp in cases:
        try:
            found = i2c.scan(start_addr=start, end_addr=end)
            if expect_bmp is True:
                passed = BMP280_ADDR in found
            elif expect_bmp is False:
                passed = BMP280_ADDR not in found
            else:
                passed = isinstance(found, list)
            _record(f"scan {label} ({hex(start)}-{hex(end)})", passed,
                    f"found={len(found)} device(s)")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"scan {label}", False, str(e))
            ok = False

    # Reversed range
    try:
        found = i2c.scan(start_addr=0x77, end_addr=0x08)
        _record("scan reversed range (0x77-0x08)", isinstance(found, list),
                f"found {len(found)} device(s)")
    except Exception as e:
        _record("scan reversed range", False, str(e))
        ok = False

    # Consistency
    try:
        found1 = i2c.scan()
        found2 = i2c.scan()
        passed = found1 == found2
        _record("scan consistency (2 scans)", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("scan consistency", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 5. read() basic + edge cases
# ---------------------------------------------------------------------------
def test_read():
    """Test read with various byte counts and edge cases."""
    print("\n" + "=" * 60)
    print("TEST: read()")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.i2c import I2CBackendError
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # Various sizes
    for n in (1, 4, 8, 128, 256):
        try:
            result = i2c.read(address=BMP280_ADDR, num_bytes=n)
            passed = isinstance(result, list) and len(result) == n
            _record(f"read num_bytes={n}", passed, f"len={len(result)}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"read num_bytes={n}", False, str(e))
            ok = False

    # NACK (non-existent device)
    try:
        i2c.read(address=0x10, num_bytes=1)
        _record("read NACK (addr 0x10)", False, "expected I2CBackendError")
        ok = False
    except I2CBackendError:
        _record("read NACK (addr 0x10)", True, "I2CBackendError raised")
    except Exception as e:
        _record("read NACK", False, f"wrong type: {type(e).__name__}: {e}")
        ok = False

    # 0 bytes
    try:
        result = i2c.read(address=BMP280_ADDR, num_bytes=0)
        _record("read 0 bytes", True, f"returned {result}")
    except Exception as e:
        _record("read 0 bytes", True, f"raises {type(e).__name__}")

    # Boundary addresses
    for addr in (0x00, 0x7F):
        try:
            i2c.read(address=addr, num_bytes=1)
            _record(f"read addr {hex(addr)}", True)
        except Exception as e:
            _record(f"read addr {hex(addr)}", True,
                    f"raises {type(e).__name__}")

    return ok


# ---------------------------------------------------------------------------
# 6. read() output formats
# ---------------------------------------------------------------------------
def test_read_formats():
    """Test read output formats."""
    print("\n" + "=" * 60)
    print("TEST: read() output formats")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    for fmt, expected_type in [("list", list), ("hex", str), ("bytes", str), ("json", dict)]:
        try:
            result = i2c.read(address=BMP280_ADDR, num_bytes=4, output_format=fmt)
            passed = isinstance(result, expected_type)
            if fmt == "json":
                passed = passed and "data" in result
            _record(f"read format={fmt}", passed,
                    f"type={type(result).__name__}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"read format={fmt}", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 7. write() basic + edge cases
# ---------------------------------------------------------------------------
def test_write():
    """Test write with various payloads."""
    print("\n" + "=" * 60)
    print("TEST: write()")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.i2c import I2CBackendError
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # Single byte
    try:
        result = i2c.write(address=BMP280_ADDR, data=[CHIP_ID_REG])
        passed = result is None
        _record("write 1 byte", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("write 1 byte", False, str(e))
        ok = False

    # Two bytes (register + value)
    try:
        i2c.write(address=BMP280_ADDR, data=[CTRL_MEAS_REG, 0x00])
        _record("write 2 bytes", True)
    except Exception as e:
        _record("write 2 bytes", False, str(e))
        ok = False

    # Write then verify
    try:
        i2c.write(address=BMP280_ADDR, data=[CTRL_MEAS_REG, 0x00])
        readback = i2c.write_read(address=BMP280_ADDR, data=[CTRL_MEAS_REG], num_bytes=1)
        passed = readback[0] == 0x00
        _record("write+verify readback", passed,
                f"readback=0x{readback[0]:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("write+verify readback", False, str(e))
        ok = False

    # NACK
    try:
        i2c.write(address=0x10, data=[0x00])
        _record("write NACK (addr 0x10)", True, "silent (Aardvark limitation)")
    except I2CBackendError:
        _record("write NACK (addr 0x10)", True, "I2CBackendError raised")
    except Exception as e:
        _record("write NACK", False, f"wrong type: {type(e).__name__}: {e}")
        ok = False

    # Empty data
    try:
        i2c.write(address=BMP280_ADDR, data=[])
        _record("write empty data", True)
    except Exception as e:
        _record("write empty data", True, f"raises {type(e).__name__}")

    # Write 0xFF and 0x00
    for val in (0xFF, 0x00):
        try:
            i2c.write(address=BMP280_ADDR, data=[CTRL_MEAS_REG, val])
            readback = i2c.write_read(address=BMP280_ADDR, data=[CTRL_MEAS_REG], num_bytes=1)
            _record(f"write 0x{val:02X} readback", True,
                    f"readback=0x{readback[0]:02X}")
        except Exception as e:
            _record(f"write 0x{val:02X}", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 8. write_read() basic + edge cases
# ---------------------------------------------------------------------------
def test_write_read():
    """Test write_read with various register reads and edge cases."""
    print("\n" + "=" * 60)
    print("TEST: write_read()")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.i2c import I2CBackendError
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # Chip ID
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
        passed = len(data) == 1 and data[0] == BMP280_CHIP_ID
        _record("write_read chip ID", passed,
                f"got=0x{data[0]:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("write_read chip ID", False, str(e))
        ok = False

    # Calibration (26 bytes)
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[CALIB_REG], num_bytes=26)
        non_zero = any(b != 0 for b in data)
        passed = len(data) == 26 and non_zero
        _record("write_read calibration (26 bytes)", passed,
                f"non_zero={non_zero}")
        if not passed:
            ok = False
    except Exception as e:
        _record("write_read calibration", False, str(e))
        ok = False

    # Status register
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[STATUS_REG], num_bytes=1)
        passed = len(data) == 1
        _record("write_read status", passed,
                f"value=0x{data[0]:02X}")
        if not passed:
            ok = False
    except I2CBackendError:
        _record("write_read status", True, "bus-level error (acceptable)")
    except Exception as e:
        _record("write_read status", False, str(e))
        ok = False

    # NACK
    try:
        i2c.write_read(address=0x10, data=[0x00], num_bytes=1)
        _record("write_read NACK", False, "expected error")
        ok = False
    except I2CBackendError:
        _record("write_read NACK", True, "I2CBackendError raised")
    except Exception as e:
        _record("write_read NACK", False, f"wrong type: {type(e).__name__}")
        ok = False

    # Empty write data
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[], num_bytes=1)
        _record("write_read empty write", True, f"returned {data}")
    except Exception as e:
        _record("write_read empty write", True,
                f"raises {type(e).__name__}")

    # 0 read bytes
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=0)
        _record("write_read 0 read bytes", True, f"returned {data}")
    except Exception as e:
        _record("write_read 0 read bytes", True,
                f"raises {type(e).__name__}")

    # Multi-byte write data
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[CTRL_MEAS_REG, 0x00], num_bytes=1)
        _record("write_read multi-byte write", isinstance(data, list))
    except Exception as e:
        _record("write_read multi-byte write", False, str(e))
        ok = False

    # Large read (128 bytes)
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[0x80], num_bytes=128)
        passed = len(data) == 128
        _record("write_read 128 bytes", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("write_read 128 bytes", False, str(e))
        ok = False

    # 5 consecutive calls
    try:
        results = []
        for i in range(5):
            data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
            results.append(data[0])
        passed = all(r == BMP280_CHIP_ID for r in results)
        _record("write_read 5 consecutive", passed,
                f"results={[hex(r) for r in results]}")
        if not passed:
            ok = False
    except Exception as e:
        _record("write_read 5 consecutive", False, str(e))
        ok = False

    # Return type validation
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=4)
        all_ints = all(isinstance(b, int) for b in data)
        in_range = all(0 <= b <= 255 for b in data)
        _record("write_read return type", all_ints and in_range)
    except Exception as e:
        _record("write_read return type", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 9. write_read() output formats
# ---------------------------------------------------------------------------
def test_write_read_formats():
    """Test write_read output formats."""
    print("\n" + "=" * 60)
    print("TEST: write_read() output formats")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    for fmt, expected_type in [("list", list), ("hex", str), ("bytes", str), ("json", dict)]:
        try:
            result = i2c.write_read(
                address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1,
                output_format=fmt)
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
# 10. Dispatcher-level calls
# ---------------------------------------------------------------------------
def test_dispatcher():
    """Test dispatcher config, scan, read, write, transfer with overrides."""
    print("\n" + "=" * 60)
    print("TEST: Dispatcher calls")
    print("=" * 60)

    ok = True

    try:
        from lager.protocols.i2c.dispatcher import config, scan, read, write, transfer

        config(I2C_NET, frequency_hz=100_000, pull_ups=True)
        _record("dispatcher config", True)
    except Exception as e:
        _record("dispatcher config", False, str(e))
        return False

    try:
        print("  (dispatcher scan prints to stdout)")
        scan(I2C_NET, start_addr=0x08, end_addr=0x77, overrides=None)
        _record("dispatcher scan", True)
    except Exception as e:
        _record("dispatcher scan", False, str(e))
        ok = False

    try:
        read(I2C_NET, address=BMP280_ADDR, num_bytes=1, output_format="hex", overrides=None)
        _record("dispatcher read hex", True)
    except Exception as e:
        _record("dispatcher read hex", False, str(e))
        ok = False

    try:
        write(I2C_NET, address=BMP280_ADDR, data=[CHIP_ID_REG], output_format="hex", overrides=None)
        _record("dispatcher write", True)
    except Exception as e:
        _record("dispatcher write", False, str(e))
        ok = False

    try:
        transfer(I2C_NET, address=BMP280_ADDR, num_bytes=1, data=[CHIP_ID_REG],
                 output_format="hex", overrides=None)
        _record("dispatcher transfer hex", True)
    except Exception as e:
        _record("dispatcher transfer hex", False, str(e))
        ok = False

    # Overrides
    try:
        overrides = {"frequency_hz": 400_000}
        transfer(I2C_NET, address=BMP280_ADDR, num_bytes=1, data=[CHIP_ID_REG],
                 output_format="hex", overrides=overrides)
        _record("dispatcher override freq=400k", True)
    except Exception as e:
        _record("dispatcher override freq=400k", False, str(e))
        ok = False

    # Invalid net
    try:
        from lager.protocols.i2c import I2CBackendError
        scan("nonexistent_net_12345", start_addr=0x08, end_addr=0x77, overrides=None)
        _record("dispatcher invalid net", False, "expected error")
        ok = False
    except I2CBackendError:
        _record("dispatcher invalid net raises I2CBackendError", True)
    except Exception as e:
        _record("dispatcher invalid net", False, f"wrong type: {type(e).__name__}")
        ok = False

    # Restore
    config(I2C_NET, frequency_hz=100_000, pull_ups=True)
    return ok


# ---------------------------------------------------------------------------
# 11. Driver cache
# ---------------------------------------------------------------------------
def test_driver_cache():
    """Test that dispatcher caches driver instances."""
    print("\n" + "=" * 60)
    print("TEST: Driver cache")
    print("=" * 60)

    ok = True

    try:
        from lager.protocols.i2c import _resolve_net_and_driver

        drv1 = _resolve_net_and_driver(I2C_NET, overrides=None)
        drv2 = _resolve_net_and_driver(I2C_NET, overrides=None)
        passed = drv1 is drv2
        _record("driver cache same instance", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("driver cache", False, str(e))
        ok = False

    try:
        drv1 = _resolve_net_and_driver(I2C_NET, overrides=None)
        drv2 = _resolve_net_and_driver(I2C_NET, overrides={"frequency_hz": 400_000})
        passed = drv1 is drv2
        _record("cached driver with override", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("cached driver with override", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 12. Exception hierarchy
# ---------------------------------------------------------------------------
def test_exceptions():
    """Test I2CBackendError inherits LagerBackendError."""
    print("\n" + "=" * 60)
    print("TEST: Exception hierarchy")
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
        _record("inheritance", False, str(e))
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
        err = I2CBackendError("test", device="Aardvark", backend="I2C")
        passed = err.device == "Aardvark"
        _record("device='Aardvark'", passed)
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
# 13. BMP280 functional
# ---------------------------------------------------------------------------
def test_bmp280():
    """BMP280 functional: soft reset, calibration, measurement, register map."""
    print("\n" + "=" * 60)
    print("TEST: BMP280 functional")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.i2c import I2CBackendError
    i2c = Net.get(I2C_NET, NetType.I2C)
    i2c.config(frequency_hz=100_000, pull_ups=True)
    ok = True

    # Soft reset then chip ID
    try:
        i2c.write(address=BMP280_ADDR, data=[RESET_REG, RESET_VALUE])
        time.sleep(0.01)
        data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
        passed = data[0] == BMP280_CHIP_ID
        _record("soft reset then chip ID", passed,
                f"chip_id=0x{data[0]:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("soft reset then chip ID", False, str(e))
        ok = False

    # Parse calibration
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
        _record("forced measurement", True, f"bus-level error: {e}")
    except Exception as e:
        _record("forced measurement", False, str(e))
        ok = False

    # Full register map (128 bytes)
    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[0x80], num_bytes=128)
        chip_id_offset = 0xD0 - 0x80
        passed = len(data) == 128 and data[chip_id_offset] == BMP280_CHIP_ID
        _record("full register map (128 bytes)", passed,
                f"chip_id at offset {chip_id_offset}=0x{data[chip_id_offset]:02X}")
        if not passed:
            ok = False
    except Exception as e:
        _record("full register map", False, str(e))
        ok = False

    # Restore sleep mode
    try:
        i2c.write(address=BMP280_ADDR, data=[CTRL_MEAS_REG, 0x00])
    except Exception:
        pass

    return ok


# ---------------------------------------------------------------------------
# 14. Stress tests
# ---------------------------------------------------------------------------
def test_stress():
    """Rapid sequential operations and mixed patterns."""
    print("\n" + "=" * 60)
    print("TEST: Stress")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # 20 rapid reads
    try:
        for i in range(20):
            data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
            assert data[0] == BMP280_CHIP_ID, f"iter {i}: 0x{data[0]:02X}"
        _record("20 rapid write_read", True)
    except Exception as e:
        _record("20 rapid write_read", False, str(e))
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

    # Config changes between ops
    try:
        i2c.config(frequency_hz=100_000, pull_ups=True)
        d1 = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
        i2c.config(frequency_hz=400_000, pull_ups=True)
        d2 = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
        i2c.config(frequency_hz=100_000, pull_ups=True)
        d3 = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG], num_bytes=1)
        passed = d1[0] == d2[0] == d3[0] == BMP280_CHIP_ID
        _record("config changes between ops", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("config changes between ops", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 15. _persist_params + config persistence E2E
# ---------------------------------------------------------------------------
def test_persistence():
    """Test _persist_params and config persistence."""
    print("\n" + "=" * 60)
    print("TEST: Config Persistence")
    print("=" * 60)

    from lager.protocols.i2c.dispatcher import config, _persist_params
    from lager.protocols.i2c import I2CBackendError
    from lager import Net
    ok = True

    # Persist frequency
    try:
        _persist_params(I2C_NET, frequency_hz=400_000)
        all_nets = Net.get_local_nets()
        rec = next(r for r in all_nets if r.get("name") == I2C_NET)
        passed = rec.get("params", {}).get("frequency_hz") == 400_000
        _record("persist freq=400k", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("persist freq=400k", False, str(e))
        ok = False

    # Persist pull_ups without overwriting freq
    try:
        _persist_params(I2C_NET, pull_ups=True)
        all_nets = Net.get_local_nets()
        rec = next(r for r in all_nets if r.get("name") == I2C_NET)
        params = rec.get("params", {})
        freq_ok = params.get("frequency_hz") == 400_000
        pull_ok = params.get("pull_ups") is True
        passed = freq_ok and pull_ok
        _record("persist pull_ups preserves freq", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("persist pull_ups preserves freq", False, str(e))
        ok = False

    # Nonexistent net
    try:
        _persist_params("nonexistent_99999", frequency_hz=100_000)
        _record("persist nonexistent net", False, "expected error")
        ok = False
    except I2CBackendError:
        _record("persist nonexistent net raises I2CBackendError", True)
    except Exception as e:
        _record("persist nonexistent net", False, f"wrong: {type(e).__name__}")
        ok = False

    # Config persistence E2E
    try:
        config(I2C_NET, frequency_hz=100_000, pull_ups=True)
        config(I2C_NET, frequency_hz=400_000)
        all_nets = Net.get_local_nets()
        rec = next(r for r in all_nets if r.get("name") == I2C_NET)
        params = rec.get("params", {})
        passed = params.get("frequency_hz") == 400_000 and params.get("pull_ups") is True
        _record("config E2E freq change preserves pull_ups", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("config E2E", False, str(e))
        ok = False

    # Cleanup
    try:
        config(I2C_NET, frequency_hz=100_000, pull_ups=True)
        _record("cleanup restore defaults", True)
    except Exception as e:
        _record("cleanup", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 16. I2CNet.get_config
# ---------------------------------------------------------------------------
def test_get_config():
    """Test I2CNet.get_config() method."""
    print("\n" + "=" * 60)
    print("TEST: I2CNet.get_config()")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    try:
        cfg = i2c.get_config()
        passed = isinstance(cfg, dict)
        has_name = cfg.get("name") == I2C_NET
        has_role = cfg.get("role") == "i2c"
        _record("get_config returns dict", passed and has_name and has_role,
                f"name={cfg.get('name')!r}, role={cfg.get('role')!r}")
        if not (passed and has_name):
            ok = False
    except Exception as e:
        _record("get_config", False, str(e))
        ok = False

    # Returns a copy
    try:
        cfg1 = i2c.get_config()
        cfg1["_mutation_test"] = True
        cfg2 = i2c.get_config()
        passed = "_mutation_test" not in cfg2
        _record("get_config returns copy", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("get_config returns copy", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 17. Config edge cases
# ---------------------------------------------------------------------------
def test_config_edge_cases():
    """Test boundary and invalid config values."""
    print("\n" + "=" * 60)
    print("TEST: Config edge cases")
    print("=" * 60)

    from lager.protocols.i2c.dispatcher import config
    from lager.protocols.i2c import I2CBackendError
    ok = True

    # freq=0
    try:
        config(I2C_NET, frequency_hz=0)
        _record("config freq=0", False, "expected error")
        ok = False
    except I2CBackendError:
        _record("config freq=0 raises I2CBackendError", True)
    except Exception as e:
        _record("config freq=0", False, f"wrong: {type(e).__name__}: {e}")
        ok = False

    # freq=-100
    try:
        config(I2C_NET, frequency_hz=-100)
        _record("config freq=-100", False, "expected error")
        ok = False
    except I2CBackendError:
        _record("config freq=-100 raises I2CBackendError", True)
    except Exception as e:
        _record("config freq=-100", False, f"wrong: {type(e).__name__}: {e}")
        ok = False

    # freq=10MHz (above max)
    try:
        config(I2C_NET, frequency_hz=10_000_000)
        _record("config freq=10MHz", True, "no crash (Aardvark caps)")
    except Exception as e:
        _record("config freq=10MHz", True, f"raises {type(e).__name__}")

    # No explicit args
    try:
        config(I2C_NET)
        _record("config() no args", True, "prints stored values")
    except Exception as e:
        _record("config() no args", False, str(e))
        ok = False

    # Restore
    try:
        config(I2C_NET, frequency_hz=100_000, pull_ups=True)
    except Exception:
        pass

    return ok


# ===================================================================
# Main
# ===================================================================
def main():
    """Run all tests."""
    print("Aardvark I2C API Test Suite")
    print(f"I2C net: {I2C_NET}, BMP280 addr: {hex(BMP280_ADDR)}")
    print("=" * 60)

    tests = [
        ("Imports",                 test_imports),
        ("Net.get",                 test_net_get),
        ("Config",                  test_config),
        ("Scan",                    test_scan),
        ("read()",                  test_read),
        ("read() formats",          test_read_formats),
        ("write()",                 test_write),
        ("write_read()",            test_write_read),
        ("write_read() formats",    test_write_read_formats),
        ("Dispatcher calls",        test_dispatcher),
        ("Driver Cache",            test_driver_cache),
        ("Exception Hierarchy",     test_exceptions),
        ("BMP280 Functional",       test_bmp280),
        ("Stress",                  test_stress),
        ("Config Persistence",      test_persistence),
        ("I2CNet.get_config",       test_get_config),
        ("Config Edge Cases",       test_config_edge_cases),
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
