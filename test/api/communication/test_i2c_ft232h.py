#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive I2C edge case tests targeting the FT232H adapter on net 'i2c2'.

Run with: lager python test/api/communication/test_i2c_ft232h.py --box <boxname>

Prerequisites:
- An I2C net configured in /etc/lager/saved_nets.json with instrument "FTDI_FT232H"
- Example net configuration:
  {
    "name": "i2c2",
    "role": "i2c",
    "instrument": "FTDI_FT232H",
    "address": "ftdi://ftdi:232h/1",
    "params": {
      "frequency_hz": 100000,
      "pull_ups": false
    }
  }

HW-611 (BMP280) wired to FT232H I2C:
- FT232H I2C pins: AD0=SCL, AD1+AD2=SDA (bridged)
- BMP280 CSB tied to VCC (I2C mode), SDO tied to GND (address 0x76)
- External 4.7k pull-ups on SDA and SCL (or onboard module pull-ups)
- Chip ID register: 0xD0, expected value: 0x58

FT232H I2C capabilities:
- Standard (100kHz), Fast (400kHz), Fast-mode Plus (~1MHz)
- No transfer size limit (unlike LabJack's 56-byte limit)
- No internal pull-ups (external required)
- USB disconnect recovery with retry logic
"""
import sys
import os
import json
import time
import traceback

# Configuration - change these or set env vars
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
        from lager.protocols.i2c import FT232HI2C
        _record("import FT232HI2C", True)
    except Exception as e:
        _record("import FT232HI2C", False, str(e))
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
# 3. get_config
# ---------------------------------------------------------------------------
def test_get_config():
    """Test i2c.get_config() returns dict with expected keys."""
    print("\n" + "=" * 60)
    print("TEST: get_config")
    print("=" * 60)

    try:
        from lager import Net, NetType
        i2c = Net.get(I2C_NET, NetType.I2C)

        cfg = i2c.get_config()
        is_dict = isinstance(cfg, dict)
        _record("get_config returns dict", is_dict, f"type={type(cfg).__name__}")

        has_name = "name" in cfg
        _record("config has 'name' key", has_name, f"keys={list(cfg.keys())}")

        has_role = "role" in cfg
        _record("config has 'role' key", has_role)

        # Check frequency_hz in params
        params = cfg.get("params", {})
        has_freq = "frequency_hz" in params
        _record("params has 'frequency_hz'", has_freq,
                f"value={params.get('frequency_hz')}" if has_freq else "missing")

        return is_dict and has_name and has_role
    except Exception as e:
        _record("get_config", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 4. Config frequency
# ---------------------------------------------------------------------------
def test_config_frequency():
    """Test various frequency settings."""
    print("\n" + "=" * 60)
    print("TEST: config frequency")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # Standard and Fast must work with BMP280
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

    # Non-standard frequencies accepted without error
    for freq in (50_000, 1_000_000):
        try:
            i2c.config(frequency_hz=freq)
            _record(f"config frequency_hz={freq}", True, "accepted")
        except Exception as e:
            _record(f"config frequency_hz={freq}", False, str(e))
            ok = False

    # Restore standard
    i2c.config(frequency_hz=100_000)
    return ok


# ---------------------------------------------------------------------------
# 5. Config pull-ups (FT232H has NO internal pull-ups)
# ---------------------------------------------------------------------------
def test_config_pull_ups():
    """Test config with pull_ups -- accepted but ignored for FT232H."""
    print("\n" + "=" * 60)
    print("TEST: config pull-ups (FT232H - no hardware pull-ups)")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    for val in (True, False):
        try:
            i2c.config(frequency_hz=100_000, pull_ups=val)
            _record(f"config pull_ups={val}", True,
                    "accepted (FT232H has no internal pull-ups)")
        except Exception as e:
            _record(f"config pull_ups={val}", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 6. Config invalid
# ---------------------------------------------------------------------------
def test_config_invalid():
    """Test invalid config values raise errors."""
    print("\n" + "=" * 60)
    print("TEST: config invalid values")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.i2c import I2CBackendError
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # Negative frequency
    try:
        i2c.config(frequency_hz=-100)
        _record("config freq=-100 raises error", False, "no error raised")
        ok = False
    except (I2CBackendError, ValueError):
        _record("config freq=-100 raises error", True)
    except Exception as e:
        _record("config freq=-100 raises error", False,
                f"unexpected: {type(e).__name__}: {e}")
        ok = False

    # Zero frequency
    try:
        i2c.config(frequency_hz=0)
        _record("config freq=0 raises error", False, "no error raised")
        ok = False
    except (I2CBackendError, ValueError):
        _record("config freq=0 raises error", True)
    except Exception as e:
        _record("config freq=0 raises error", False,
                f"unexpected: {type(e).__name__}: {e}")
        ok = False

    # Restore valid config
    i2c.config(frequency_hz=100_000)
    return ok


# ---------------------------------------------------------------------------
# 7. Scan default
# ---------------------------------------------------------------------------
def test_scan_default():
    """Scan default range, verify BMP280 at 0x76."""
    print("\n" + "=" * 60)
    print("TEST: scan default range")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        found = i2c.scan()
        has_bmp = BMP280_ADDR in found
        _record("scan default finds BMP280", has_bmp,
                f"found={[hex(a) for a in found]}")
        return has_bmp
    except Exception as e:
        _record("scan default", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 8. Scan narrow
# ---------------------------------------------------------------------------
def test_scan_narrow():
    """Scan only 0x76-0x76."""
    print("\n" + "=" * 60)
    print("TEST: scan narrow (0x76 only)")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        found = i2c.scan(start_addr=BMP280_ADDR, end_addr=BMP280_ADDR)
        passed = found == [BMP280_ADDR]
        _record("scan 0x76-0x76", passed,
                f"found={[hex(a) for a in found]}")
        return passed
    except Exception as e:
        _record("scan narrow", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 9. Scan miss
# ---------------------------------------------------------------------------
def test_scan_miss():
    """Scan range that excludes BMP280."""
    print("\n" + "=" * 60)
    print("TEST: scan miss (0x08-0x75)")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        found = i2c.scan(start_addr=0x08, end_addr=0x75)
        passed = BMP280_ADDR not in found
        _record("scan 0x08-0x75 excludes BMP280", passed,
                f"found={[hex(a) for a in found]}")
        return passed
    except Exception as e:
        _record("scan miss", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 10. Scan full range
# ---------------------------------------------------------------------------
def test_scan_full_range():
    """Scan 0x00-0x7F, verify BMP280 found."""
    print("\n" + "=" * 60)
    print("TEST: scan full range (0x00-0x7F)")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        found = i2c.scan(start_addr=0x00, end_addr=0x7F)
        has_bmp = BMP280_ADDR in found
        _record("scan 0x00-0x7F finds BMP280", has_bmp,
                f"found={[hex(a) for a in found]}")
        return has_bmp
    except Exception as e:
        _record("scan full range", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 11. Scan empty range
# ---------------------------------------------------------------------------
def test_scan_empty_range():
    """Scan range with no devices."""
    print("\n" + "=" * 60)
    print("TEST: scan empty range (0x50-0x55)")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        found = i2c.scan(start_addr=0x50, end_addr=0x55)
        passed = len(found) == 0
        _record("scan 0x50-0x55 empty", passed,
                f"found={[hex(a) for a in found]}")
        return passed
    except Exception as e:
        _record("scan empty range", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 12. Read basic
# ---------------------------------------------------------------------------
def test_read_basic():
    """Read various byte counts from BMP280."""
    print("\n" + "=" * 60)
    print("TEST: read basic")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    for count in (1, 4, 8, 26):
        try:
            data = i2c.read(address=BMP280_ADDR, num_bytes=count)
            passed = isinstance(data, list) and len(data) == count
            _record(f"read {count} bytes", passed,
                    f"len={len(data)}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"read {count} bytes", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 13. Read formats
# ---------------------------------------------------------------------------
def test_read_formats():
    """Test read output formats: hex, bytes, json."""
    print("\n" + "=" * 60)
    print("TEST: read output formats")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    for fmt in ("hex", "bytes", "json"):
        try:
            data = i2c.read(address=BMP280_ADDR, num_bytes=4, output_format=fmt)
            _record(f"read format={fmt}", True,
                    f"type={type(data).__name__}")
        except Exception as e:
            _record(f"read format={fmt}", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 14. Read frequency override
# ---------------------------------------------------------------------------
def test_read_frequency_override():
    """Read with frequency override."""
    print("\n" + "=" * 60)
    print("TEST: read frequency override")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        data = i2c.read(address=BMP280_ADDR, num_bytes=1,
                        overrides={"frequency_hz": 400_000})
        passed = isinstance(data, list) and len(data) == 1
        _record("read freq override 400k", passed, f"len={len(data)}")
        return passed
    except Exception as e:
        _record("read freq override 400k", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 15. Read NACK
# ---------------------------------------------------------------------------
def test_read_nack():
    """Read from non-existent device should raise error."""
    print("\n" + "=" * 60)
    print("TEST: read NACK")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.i2c import I2CBackendError
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        data = i2c.read(address=0x50, num_bytes=1)
        _record("read 0x50 raises error", False, f"got data={data}")
        return False
    except I2CBackendError:
        _record("read 0x50 raises I2CBackendError", True)
        return True
    except Exception as e:
        _record("read 0x50 raises error", True,
                f"{type(e).__name__}: {e}")
        return True


# ---------------------------------------------------------------------------
# 16. Write basic
# ---------------------------------------------------------------------------
def test_write_basic():
    """Write single byte and multi-byte to BMP280."""
    print("\n" + "=" * 60)
    print("TEST: write basic")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    # Single byte (register pointer)
    try:
        i2c.write(address=BMP280_ADDR, data=[CHIP_ID_REG])
        _record("write single byte (0xD0)", True)
    except Exception as e:
        _record("write single byte", False, str(e))
        ok = False

    # Multi-byte (register + value)
    try:
        i2c.write(address=BMP280_ADDR, data=[CTRL_MEAS_REG, 0x00])
        _record("write register+value (sleep mode)", True)
    except Exception as e:
        _record("write register+value", False, str(e))
        ok = False

    # Soft reset
    try:
        i2c.write(address=BMP280_ADDR, data=[RESET_REG, RESET_VALUE])
        time.sleep(0.01)
        _record("write soft reset", True)
    except Exception as e:
        _record("write soft reset", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 17. Write NACK
# ---------------------------------------------------------------------------
def test_write_nack():
    """Write to non-existent device should raise error."""
    print("\n" + "=" * 60)
    print("TEST: write NACK")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.i2c import I2CBackendError
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        i2c.write(address=0x50, data=[0x00])
        _record("write 0x50 raises error", False, "no error raised")
        return False
    except I2CBackendError:
        _record("write 0x50 raises I2CBackendError", True)
        return True
    except Exception as e:
        _record("write 0x50 raises error", True,
                f"{type(e).__name__}: {e}")
        return True


# ---------------------------------------------------------------------------
# 18. Transfer chip ID
# ---------------------------------------------------------------------------
def test_transfer_chip_id():
    """Transfer: write 0xD0, read 1 byte -- expect 0x58."""
    print("\n" + "=" * 60)
    print("TEST: transfer chip ID")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG],
                              num_bytes=1)
        passed = data[0] == BMP280_CHIP_ID
        _record("transfer chip ID == 0x58", passed,
                f"got=0x{data[0]:02x}")
        return passed
    except Exception as e:
        _record("transfer chip ID", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 19. Transfer calibration
# ---------------------------------------------------------------------------
def test_transfer_calibration():
    """Transfer: read 26 bytes of calibration data."""
    print("\n" + "=" * 60)
    print("TEST: transfer calibration data")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[CALIB_REG],
                              num_bytes=26)
        passed = len(data) == 26
        non_zero = any(b != 0 for b in data)
        _record("transfer 26 bytes calib", passed and non_zero,
                f"len={len(data)}, non_zero={non_zero}")
        return passed and non_zero
    except Exception as e:
        _record("transfer calibration", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 20. Transfer status
# ---------------------------------------------------------------------------
def test_transfer_status():
    """Transfer: read status register."""
    print("\n" + "=" * 60)
    print("TEST: transfer status register")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[STATUS_REG],
                              num_bytes=1)
        passed = len(data) == 1
        _record("transfer status reg", passed,
                f"value=0x{data[0]:02x}")
        return passed
    except Exception as e:
        _record("transfer status", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 21. Transfer formats
# ---------------------------------------------------------------------------
def test_transfer_formats():
    """Test transfer output formats: hex, bytes, json."""
    print("\n" + "=" * 60)
    print("TEST: transfer output formats")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    for fmt in ("hex", "bytes", "json"):
        try:
            data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG],
                                  num_bytes=1, output_format=fmt)
            _record(f"transfer format={fmt}", True,
                    f"type={type(data).__name__}")
        except Exception as e:
            _record(f"transfer format={fmt}", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 22. Transfer frequency override
# ---------------------------------------------------------------------------
def test_transfer_frequency_override():
    """Transfer with frequency override."""
    print("\n" + "=" * 60)
    print("TEST: transfer frequency override")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG],
                              num_bytes=1,
                              overrides={"frequency_hz": 400_000})
        passed = data[0] == BMP280_CHIP_ID
        _record("transfer freq override 400k", passed,
                f"got=0x{data[0]:02x}")
        return passed
    except Exception as e:
        _record("transfer freq override", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 23. Transfer NACK
# ---------------------------------------------------------------------------
def test_transfer_nack():
    """Transfer to non-existent device should raise error."""
    print("\n" + "=" * 60)
    print("TEST: transfer NACK")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.i2c import I2CBackendError
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        data = i2c.write_read(address=0x50, data=[0x00], num_bytes=1)
        _record("transfer 0x50 raises error", False, f"got data={data}")
        return False
    except I2CBackendError:
        _record("transfer 0x50 raises I2CBackendError", True)
        return True
    except Exception as e:
        _record("transfer 0x50 raises error", True,
                f"{type(e).__name__}: {e}")
        return True


# ---------------------------------------------------------------------------
# 24. BMP280 soft reset
# ---------------------------------------------------------------------------
def test_bmp280_soft_reset():
    """Write soft reset, then verify chip ID still returns 0x58."""
    print("\n" + "=" * 60)
    print("TEST: BMP280 soft reset")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    try:
        i2c.write(address=BMP280_ADDR, data=[RESET_REG, RESET_VALUE])
        time.sleep(0.01)
        data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG],
                              num_bytes=1)
        passed = data[0] == BMP280_CHIP_ID
        _record("soft reset -> chip ID", passed,
                f"got=0x{data[0]:02x}")
        if not passed:
            ok = False
    except Exception as e:
        _record("soft reset -> chip ID", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 25. BMP280 forced mode
# ---------------------------------------------------------------------------
def test_bmp280_forced_mode():
    """Write forced mode, read raw data."""
    print("\n" + "=" * 60)
    print("TEST: BMP280 forced mode measurement")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    try:
        # Set forced mode: osrs_t=1x, osrs_p=1x, mode=forced (0x25)
        i2c.write(address=BMP280_ADDR, data=[CTRL_MEAS_REG, 0x25])
        time.sleep(0.1)

        # Read 6 bytes of raw pressure + temperature data
        data = i2c.write_read(address=BMP280_ADDR, data=[0xF7],
                              num_bytes=6)
        passed = len(data) == 6
        non_trivial = any(b != 0x00 and b != 0x80 for b in data)
        _record("forced mode raw data", passed and non_trivial,
                f"data={[f'0x{b:02x}' for b in data]}")
        if not (passed and non_trivial):
            ok = False
    except Exception as e:
        _record("forced mode", False, str(e))
        ok = False

    # Put back to sleep
    try:
        i2c.write(address=BMP280_ADDR, data=[CTRL_MEAS_REG, 0x00])
    except Exception:
        pass

    return ok


# ---------------------------------------------------------------------------
# 26. BMP280 config readback
# ---------------------------------------------------------------------------
def test_bmp280_config_readback():
    """Write config register, read back, verify match."""
    print("\n" + "=" * 60)
    print("TEST: BMP280 config register readback")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        # Write config register (t_sb=0, filter=0, spi3w_en=0 -> 0x00)
        i2c.write(address=BMP280_ADDR, data=[CONFIG_REG, 0x00])
        data = i2c.write_read(address=BMP280_ADDR, data=[CONFIG_REG],
                              num_bytes=1)
        passed = data[0] == 0x00
        _record("config readback == 0x00", passed,
                f"got=0x{data[0]:02x}")
        return passed
    except Exception as e:
        _record("config readback", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 27. Frequency sweep
# ---------------------------------------------------------------------------
def test_frequency_sweep():
    """Read chip ID at 50k, 100k, 400k -- all should return 0x58."""
    print("\n" + "=" * 60)
    print("TEST: frequency sweep chip ID")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    for freq in (50_000, 100_000, 400_000):
        try:
            i2c.config(frequency_hz=freq)
            data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG],
                                  num_bytes=1)
            passed = data[0] == BMP280_CHIP_ID
            _record(f"chip ID at {freq}Hz", passed,
                    f"got=0x{data[0]:02x}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"chip ID at {freq}Hz", False, str(e))
            ok = False

    # Restore standard
    i2c.config(frequency_hz=100_000)
    return ok


# ---------------------------------------------------------------------------
# 28. Rapid operations
# ---------------------------------------------------------------------------
def test_rapid_operations():
    """20 consecutive chip ID reads, all must return 0x58."""
    print("\n" + "=" * 60)
    print("TEST: rapid operations (20x chip ID)")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)

    try:
        results = []
        for i in range(20):
            data = i2c.write_read(address=BMP280_ADDR, data=[CHIP_ID_REG],
                                  num_bytes=1)
            results.append(data[0])

        all_correct = all(r == BMP280_CHIP_ID for r in results)
        _record("20x chip ID all 0x58", all_correct,
                f"unique_values={set(hex(r) for r in results)}")
        return all_correct
    except Exception as e:
        _record("rapid operations", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 29. Config persistence
# ---------------------------------------------------------------------------
def test_config_persistence():
    """Set freq 400k, scan, verify freq still 400k."""
    print("\n" + "=" * 60)
    print("TEST: config persistence")
    print("=" * 60)

    from lager import Net, NetType
    i2c = Net.get(I2C_NET, NetType.I2C)
    ok = True

    try:
        i2c.config(frequency_hz=400_000)
        # Scan should use persisted config
        found = i2c.scan(start_addr=BMP280_ADDR, end_addr=BMP280_ADDR)
        passed = BMP280_ADDR in found
        _record("scan at persisted 400k", passed,
                f"found={[hex(a) for a in found]}")
        if not passed:
            ok = False
    except Exception as e:
        _record("scan at persisted 400k", False, str(e))
        ok = False

    # Verify config still shows 400k
    try:
        cfg = i2c.get_config()
        freq = cfg.get("params", {}).get("frequency_hz")
        passed = freq == 400_000
        _record("config still 400k after scan", passed,
                f"freq={freq}")
        if not passed:
            ok = False
    except Exception as e:
        _record("config still 400k", False, str(e))
        ok = False

    # Restore
    i2c.config(frequency_hz=100_000)
    return ok


# ---------------------------------------------------------------------------
# 30. Dispatcher direct
# ---------------------------------------------------------------------------
def test_dispatcher_direct():
    """Import and call dispatcher functions directly."""
    print("\n" + "=" * 60)
    print("TEST: dispatcher direct calls")
    print("=" * 60)

    import io
    import contextlib
    from lager.protocols.i2c import config, scan, read, write, transfer
    ok = True

    # config
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            config(I2C_NET, frequency_hz=100_000)
        _record("dispatcher config()", True, f"output={buf.getvalue().strip()!r}")
    except Exception as e:
        _record("dispatcher config()", False, str(e))
        ok = False

    # scan
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            scan(I2C_NET, start_addr=BMP280_ADDR, end_addr=BMP280_ADDR)
        has_76 = "76" in buf.getvalue()
        _record("dispatcher scan()", has_76, f"has_76={has_76}")
        if not has_76:
            ok = False
    except Exception as e:
        _record("dispatcher scan()", False, str(e))
        ok = False

    # read
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            read(I2C_NET, address=BMP280_ADDR, num_bytes=1)
        _record("dispatcher read()", True, f"output={buf.getvalue().strip()!r}")
    except Exception as e:
        _record("dispatcher read()", False, str(e))
        ok = False

    # write
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            write(I2C_NET, address=BMP280_ADDR, data=[CHIP_ID_REG])
        _record("dispatcher write()", True)
    except Exception as e:
        _record("dispatcher write()", False, str(e))
        ok = False

    # transfer
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            transfer(I2C_NET, address=BMP280_ADDR, data=[CHIP_ID_REG],
                     num_bytes=1)
        has_58 = "58" in buf.getvalue()
        _record("dispatcher transfer()", has_58,
                f"output={buf.getvalue().strip()!r}")
        if not has_58:
            ok = False
    except Exception as e:
        _record("dispatcher transfer()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 31. Dispatcher overrides
# ---------------------------------------------------------------------------
def test_dispatcher_overrides():
    """Pass overrides dict with frequency_hz."""
    print("\n" + "=" * 60)
    print("TEST: dispatcher overrides")
    print("=" * 60)

    import io
    import contextlib
    from lager.protocols.i2c import transfer
    ok = True

    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            transfer(I2C_NET, address=BMP280_ADDR, data=[CHIP_ID_REG],
                     num_bytes=1, overrides={"frequency_hz": 400_000})
        has_58 = "58" in buf.getvalue()
        _record("transfer with freq override", has_58,
                f"output={buf.getvalue().strip()!r}")
        if not has_58:
            ok = False
    except Exception as e:
        _record("transfer with freq override", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 32. Exception hierarchy
# ---------------------------------------------------------------------------
def test_exception_hierarchy():
    """Verify I2CBackendError inherits from LagerBackendError."""
    print("\n" + "=" * 60)
    print("TEST: exception hierarchy")
    print("=" * 60)

    ok = True

    try:
        from lager.protocols.i2c import I2CBackendError
        from lager.exceptions import LagerBackendError

        passed = issubclass(I2CBackendError, LagerBackendError)
        _record("I2CBackendError -> LagerBackendError", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("exception hierarchy", False, str(e))
        ok = False

    try:
        from lager.protocols.i2c import I2CBackendError
        err = I2CBackendError("test error")
        passed = isinstance(err, Exception)
        _record("I2CBackendError is Exception", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("I2CBackendError instantiation", False, str(e))
        ok = False

    return ok


# ===================================================================
# Main
# ===================================================================
def main():
    """Run all tests."""
    print("FT232H I2C Comprehensive Test Suite")
    print(f"Testing net: {I2C_NET}")
    print(f"BMP280 address: {hex(BMP280_ADDR)}")
    print(f"Set I2C_NET / BMP280_ADDR env vars to change")
    print("=" * 60)

    tests = [
        ("Imports",                  test_imports),
        ("Net.get",                  test_net_get),
        ("get_config",               test_get_config),
        ("Config Frequency",         test_config_frequency),
        ("Config Pull-ups",          test_config_pull_ups),
        ("Config Invalid",           test_config_invalid),
        ("Scan Default",             test_scan_default),
        ("Scan Narrow",              test_scan_narrow),
        ("Scan Miss",                test_scan_miss),
        ("Scan Full Range",          test_scan_full_range),
        ("Scan Empty Range",         test_scan_empty_range),
        ("Read Basic",               test_read_basic),
        ("Read Formats",             test_read_formats),
        ("Read Freq Override",       test_read_frequency_override),
        ("Read NACK",                test_read_nack),
        ("Write Basic",              test_write_basic),
        ("Write NACK",               test_write_nack),
        ("Transfer Chip ID",         test_transfer_chip_id),
        ("Transfer Calibration",     test_transfer_calibration),
        ("Transfer Status",          test_transfer_status),
        ("Transfer Formats",         test_transfer_formats),
        ("Transfer Freq Override",   test_transfer_frequency_override),
        ("Transfer NACK",            test_transfer_nack),
        ("BMP280 Soft Reset",        test_bmp280_soft_reset),
        ("BMP280 Forced Mode",       test_bmp280_forced_mode),
        ("BMP280 Config Readback",   test_bmp280_config_readback),
        ("Frequency Sweep",          test_frequency_sweep),
        ("Rapid Operations",         test_rapid_operations),
        ("Config Persistence",       test_config_persistence),
        ("Dispatcher Direct",        test_dispatcher_direct),
        ("Dispatcher Overrides",     test_dispatcher_overrides),
        ("Exception Hierarchy",      test_exception_hierarchy),
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
