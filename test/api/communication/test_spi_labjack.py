#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive SPI edge case tests targeting the LabJack T7 adapter on net 'spi2'.

Run with: lager python test_spi_labjack.py --box <boxname>

Prerequisites:
- An SPI net configured in /etc/lager/saved_nets.json with instrument "labjack_t7"
- Example net configuration:
  {
    "name": "spi2",
    "role": "spi",
    "instrument": "labjack_t7",
    "pin": "FIO0-FIO3",
    "params": {
      "mode": 0,
      "bit_order": "msb",
      "frequency_hz": 800000,
      "word_size": 8,
      "cs_active": "low"
    }
  }

LabJack T7 SPI Limitations (vs Aardvark):
- Max transaction size: 56 bytes (vs 65535)
- Max frequency: ~800 kHz (vs ~8 MHz)
- 3+ bytes at <800kHz: forced to 800kHz
- LSB-first: software bit reversal (vs hardware)

Optional: BMP280/BME280 sensor (HW-611 breakout) wired to FIO0-FIO3 for device-specific tests.
"""
import sys
import os
import traceback

# Configuration - change this to your SPI net name
SPI_NET = os.environ.get("SPI_NET", "spi2")

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
        assert hasattr(NetType, "SPI"), "NetType.SPI not found"
        _record("import Net, NetType", True)
    except Exception as e:
        _record("import Net, NetType", False, str(e))
        ok = False

    try:
        from lager.protocols.spi import SPIBase
        _record("import SPIBase", True)
    except Exception as e:
        _record("import SPIBase", False, str(e))
        ok = False

    try:
        from lager.protocols.spi import LabJackSPI
        _record("import LabJackSPI", True)
    except Exception as e:
        _record("import LabJackSPI", False, str(e))
        ok = False

    try:
        from lager.protocols.spi import SPINet
        _record("import SPINet", True)
    except Exception as e:
        _record("import SPINet", False, str(e))
        ok = False

    try:
        from lager.protocols.spi import config, read, read_write, transfer
        _record("import dispatcher funcs", True)
    except Exception as e:
        _record("import dispatcher funcs", False, str(e))
        ok = False

    try:
        from lager.protocols.spi import SPIBackendError
        _record("import SPIBackendError", True)
    except Exception as e:
        _record("import SPIBackendError", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 2. Net.get
# ---------------------------------------------------------------------------
def test_net_get():
    """Test Net.get('spi2', NetType.SPI) returns SPINet."""
    print("\n" + "=" * 60)
    print("TEST: Net.get")
    print("=" * 60)

    try:
        from lager import Net, NetType
        from lager.protocols.spi import SPINet

        spi = Net.get(SPI_NET, NetType.SPI)
        is_spinet = isinstance(spi, SPINet)
        _record("Net.get returns SPINet", is_spinet,
                f"type={type(spi).__name__}")
        return is_spinet
    except Exception as e:
        _record("Net.get returns SPINet", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 3. get_config
# ---------------------------------------------------------------------------
def test_get_config():
    """Test spi.get_config() returns dict with expected keys."""
    print("\n" + "=" * 60)
    print("TEST: get_config")
    print("=" * 60)

    try:
        from lager import Net, NetType
        spi = Net.get(SPI_NET, NetType.SPI)

        cfg = spi.get_config()
        is_dict = isinstance(cfg, dict)
        _record("get_config returns dict", is_dict, f"type={type(cfg).__name__}")

        has_name = "name" in cfg
        _record("config has 'name' key", has_name, f"keys={list(cfg.keys())}")

        has_role = "role" in cfg
        _record("config has 'role' key", has_role)

        has_params = "params" in cfg
        if has_params:
            _record("config has 'params' key", True)
            params = cfg["params"]
            for key in ("mode", "bit_order", "frequency_hz", "word_size", "cs_active"):
                present = key in params
                _record(f"params has '{key}'", present,
                        f"value={params.get(key)}" if present else "missing")
        else:
            _record("config has 'params' key", True,
                    "not present (using defaults -- OK)")

        return is_dict and has_name and has_role
    except Exception as e:
        _record("get_config", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 4. config defaults
# ---------------------------------------------------------------------------
def test_config_defaults():
    """Test config() with all defaults (LabJack max 800kHz)."""
    print("\n" + "=" * 60)
    print("TEST: config defaults")
    print("=" * 60)

    try:
        from lager import Net, NetType
        spi = Net.get(SPI_NET, NetType.SPI)

        spi.config(mode=0, bit_order="msb", frequency_hz=800_000,
                   word_size=8, cs_active="low")
        _record("config with all defaults", True)
        return True
    except Exception as e:
        _record("config with all defaults", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 5. config all modes
# ---------------------------------------------------------------------------
def test_config_all_modes():
    """Test config(mode=0..3)."""
    print("\n" + "=" * 60)
    print("TEST: config all modes")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    for m in (0, 1, 2, 3):
        try:
            spi.config(mode=m)
            _record(f"config mode={m}", True)
        except Exception as e:
            _record(f"config mode={m}", False, str(e))
            ok = False

    # Reset to mode 0
    try:
        spi.config(mode=0)
    except Exception:
        pass

    return ok


# ---------------------------------------------------------------------------
# 6. config bit order
# ---------------------------------------------------------------------------
def test_config_bit_order():
    """Test config(bit_order='msb') and config(bit_order='lsb')."""
    print("\n" + "=" * 60)
    print("TEST: config bit_order")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    for order in ("msb", "lsb"):
        try:
            spi.config(bit_order=order)
            _record(f"config bit_order={order}", True)
        except Exception as e:
            _record(f"config bit_order={order}", False, str(e))
            ok = False

    spi.config(bit_order="msb")
    return ok


# ---------------------------------------------------------------------------
# 7. config frequencies (LabJack max ~800kHz)
# ---------------------------------------------------------------------------
def test_config_frequencies():
    """Test various frequencies: 100kHz, 500kHz, 800kHz (LabJack max)."""
    print("\n" + "=" * 60)
    print("TEST: config frequencies")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    for freq in (100_000, 500_000, 800_000):
        try:
            spi.config(frequency_hz=freq)
            _record(f"config frequency_hz={freq}", True)
        except Exception as e:
            _record(f"config frequency_hz={freq}", False, str(e))
            ok = False

    spi.config(frequency_hz=800_000)
    return ok


# ---------------------------------------------------------------------------
# 8. config word sizes
# ---------------------------------------------------------------------------
def test_config_word_sizes():
    """Test config(word_size=8), 16, 32."""
    print("\n" + "=" * 60)
    print("TEST: config word_sizes")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    for ws in (8, 16, 32):
        try:
            spi.config(word_size=ws)
            _record(f"config word_size={ws}", True)
        except Exception as e:
            _record(f"config word_size={ws}", False, str(e))
            ok = False

    spi.config(word_size=8)
    return ok


# ---------------------------------------------------------------------------
# 9. config cs polarity
# ---------------------------------------------------------------------------
def test_config_cs_polarity():
    """Test config(cs_active='low') and 'high'."""
    print("\n" + "=" * 60)
    print("TEST: config cs_polarity")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    for pol in ("low", "high"):
        try:
            spi.config(cs_active=pol)
            _record(f"config cs_active={pol}", True)
        except Exception as e:
            _record(f"config cs_active={pol}", False, str(e))
            ok = False

    spi.config(cs_active="low")
    return ok


# ---------------------------------------------------------------------------
# 10. config invalid params
# ---------------------------------------------------------------------------
def test_config_invalid_params():
    """Test invalid config parameters raise SPIBackendError."""
    print("\n" + "=" * 60)
    print("TEST: config invalid params")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.spi import SPIBackendError
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # Invalid mode
    try:
        spi.config(mode=5)
        _record("invalid mode=5 raises error", False, "no error raised")
        ok = False
    except SPIBackendError:
        _record("invalid mode=5 raises SPIBackendError", True)
    except Exception as e:
        _record("invalid mode=5 raises SPIBackendError", False,
                f"wrong exception: {type(e).__name__}: {e}")
        ok = False

    # Invalid bit_order
    try:
        spi.config(bit_order="xyz")
        _record("invalid bit_order='xyz' raises error", False, "no error raised")
        ok = False
    except SPIBackendError:
        _record("invalid bit_order='xyz' raises SPIBackendError", True)
    except Exception as e:
        _record("invalid bit_order='xyz' raises SPIBackendError", False,
                f"wrong exception: {type(e).__name__}: {e}")
        ok = False

    # Invalid word_size
    try:
        spi.config(word_size=12)
        _record("invalid word_size=12 raises error", False, "no error raised")
        ok = False
    except SPIBackendError:
        _record("invalid word_size=12 raises SPIBackendError", True)
    except Exception as e:
        _record("invalid word_size=12 raises SPIBackendError", False,
                f"wrong exception: {type(e).__name__}: {e}")
        ok = False

    # Invalid cs_active
    try:
        spi.config(cs_active="x")
        _record("invalid cs_active='x' raises error", False, "no error raised")
        ok = False
    except SPIBackendError:
        _record("invalid cs_active='x' raises SPIBackendError", True)
    except Exception as e:
        _record("invalid cs_active='x' raises SPIBackendError", False,
                f"wrong exception: {type(e).__name__}: {e}")
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 11. read basic (LabJack max 56 bytes)
# ---------------------------------------------------------------------------
def test_read_basic():
    """Test read(n_words=1), read(n_words=4), read(n_words=50)."""
    print("\n" + "=" * 60)
    print("TEST: read basic")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=800_000, word_size=8, bit_order="msb",
               cs_active="low")
    ok = True

    for n in (1, 4, 50):
        try:
            result = spi.read(n_words=n)
            is_list = isinstance(result, list)
            correct_len = len(result) == n
            _record(f"read n_words={n}", is_list and correct_len,
                    f"len={len(result)}, type={type(result).__name__}")
            if not (is_list and correct_len):
                ok = False
        except Exception as e:
            _record(f"read n_words={n}", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 12. read fill values
# ---------------------------------------------------------------------------
def test_read_fill_values():
    """Test read with fill=0xFF, 0x00, 0xAA."""
    print("\n" + "=" * 60)
    print("TEST: read fill values")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    for fill in (0xFF, 0x00, 0xAA):
        try:
            result = spi.read(n_words=4, fill=fill)
            is_list = isinstance(result, list)
            correct_len = len(result) == 4
            _record(f"read fill=0x{fill:02X}", is_list and correct_len,
                    f"len={len(result)}, data={result}")
            if not (is_list and correct_len):
                ok = False
        except Exception as e:
            _record(f"read fill=0x{fill:02X}", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 13. read output formats
# ---------------------------------------------------------------------------
def test_read_output_formats():
    """Test read output_format='list', 'hex', 'bytes', 'json'."""
    print("\n" + "=" * 60)
    print("TEST: read output formats")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # list -> list
    try:
        result = spi.read(n_words=4, output_format="list")
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
        result = spi.read(n_words=4, output_format="hex")
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
        result = spi.read(n_words=4, output_format="bytes")
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
        result = spi.read(n_words=4, output_format="json")
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
# 14. read keep_cs
# ---------------------------------------------------------------------------
def test_read_keep_cs():
    """Test read with keep_cs=True and keep_cs=False."""
    print("\n" + "=" * 60)
    print("TEST: read keep_cs")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    for kc in (True, False):
        try:
            result = spi.read(n_words=4, keep_cs=kc)
            passed = isinstance(result, list) and len(result) == 4
            _record(f"read keep_cs={kc}", passed, f"len={len(result)}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"read keep_cs={kc}", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 15. read_write basic
# ---------------------------------------------------------------------------
def test_read_write_basic():
    """Test read_write([0x9F, 0x00, 0x00, 0x00]) returns list of same length."""
    print("\n" + "=" * 60)
    print("TEST: read_write basic")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)

    try:
        data = [0x9F, 0x00, 0x00, 0x00]
        result = spi.read_write(data=data)
        is_list = isinstance(result, list)
        same_len = len(result) == len(data)
        _record("read_write 4 bytes", is_list and same_len,
                f"sent={len(data)}, recv={len(result)}, data={result}")
        return is_list and same_len
    except Exception as e:
        _record("read_write 4 bytes", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 16. read_write single byte
# ---------------------------------------------------------------------------
def test_read_write_single_byte():
    """Test read_write([0x9F]) -- 1-byte full-duplex."""
    print("\n" + "=" * 60)
    print("TEST: read_write single byte")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)

    try:
        result = spi.read_write(data=[0x9F])
        passed = isinstance(result, list) and len(result) == 1
        _record("read_write 1 byte", passed,
                f"len={len(result)}, data={result}")
        return passed
    except Exception as e:
        _record("read_write 1 byte", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 17. read_write empty
# ---------------------------------------------------------------------------
def test_read_write_empty():
    """Test read_write([]) returns empty list."""
    print("\n" + "=" * 60)
    print("TEST: read_write empty")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)

    try:
        result = spi.read_write(data=[])
        passed = isinstance(result, list) and len(result) == 0
        _record("read_write empty list", passed,
                f"len={len(result)}, data={result}")
        return passed
    except Exception as e:
        _record("read_write empty list", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 18. read_write output formats
# ---------------------------------------------------------------------------
def test_read_write_output_formats():
    """Test all 4 output formats with read_write."""
    print("\n" + "=" * 60)
    print("TEST: read_write output formats")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True
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
# 19. transfer padding
# ---------------------------------------------------------------------------
def test_transfer_padding():
    """Test transfer(n_words=4, data=[0x9F]) -- 1 byte padded to 4."""
    print("\n" + "=" * 60)
    print("TEST: transfer padding")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)

    try:
        result = spi.transfer(n_words=4, data=[0x9F])
        passed = isinstance(result, list) and len(result) == 4
        _record("transfer pad 1->4", passed,
                f"len={len(result)}, data={result}")
        return passed
    except Exception as e:
        _record("transfer pad 1->4", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 20. transfer truncation
# ---------------------------------------------------------------------------
def test_transfer_truncation():
    """Test transfer(n_words=2, data=[1,2,3,4,5]) -- truncated to 2."""
    print("\n" + "=" * 60)
    print("TEST: transfer truncation")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)

    try:
        result = spi.transfer(n_words=2, data=[1, 2, 3, 4, 5])
        passed = isinstance(result, list) and len(result) == 2
        _record("transfer truncate 5->2", passed,
                f"len={len(result)}, data={result}")
        return passed
    except Exception as e:
        _record("transfer truncate 5->2", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 21. transfer exact
# ---------------------------------------------------------------------------
def test_transfer_exact():
    """Test transfer(n_words=4, data=[1,2,3,4]) -- no padding/truncation."""
    print("\n" + "=" * 60)
    print("TEST: transfer exact")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)

    try:
        result = spi.transfer(n_words=4, data=[1, 2, 3, 4])
        passed = isinstance(result, list) and len(result) == 4
        _record("transfer exact 4=4", passed,
                f"len={len(result)}, data={result}")
        return passed
    except Exception as e:
        _record("transfer exact 4=4", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 22. transfer no data
# ---------------------------------------------------------------------------
def test_transfer_no_data():
    """Test transfer(n_words=4) -- no data arg, all fill."""
    print("\n" + "=" * 60)
    print("TEST: transfer no data")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)

    try:
        result = spi.transfer(n_words=4)
        passed = isinstance(result, list) and len(result) == 4
        _record("transfer no data (all fill)", passed,
                f"len={len(result)}, data={result}")
        return passed
    except Exception as e:
        _record("transfer no data (all fill)", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 23. transfer custom fill
# ---------------------------------------------------------------------------
def test_transfer_custom_fill():
    """Test transfer(n_words=4, data=[0x9F], fill=0x00)."""
    print("\n" + "=" * 60)
    print("TEST: transfer custom fill")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)

    try:
        result = spi.transfer(n_words=4, data=[0x9F], fill=0x00)
        passed = isinstance(result, list) and len(result) == 4
        _record("transfer custom fill=0x00", passed,
                f"len={len(result)}, data={result}")
        return passed
    except Exception as e:
        _record("transfer custom fill=0x00", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 24. write basic
# ---------------------------------------------------------------------------
def test_write_basic():
    """Test write([0x06]) returns None."""
    print("\n" + "=" * 60)
    print("TEST: write basic")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)

    try:
        result = spi.write(data=[0x06])
        passed = result is None
        _record("write 1 byte returns None", passed,
                f"returned={result!r}")
        return passed
    except Exception as e:
        _record("write 1 byte returns None", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 25. write multi-byte
# ---------------------------------------------------------------------------
def test_write_multi_byte():
    """Test write([0x02, 0x00, 0x00, 0x00, 0xDE, 0xAD])."""
    print("\n" + "=" * 60)
    print("TEST: write multi-byte")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)

    try:
        result = spi.write(data=[0x02, 0x00, 0x00, 0x00, 0xDE, 0xAD])
        passed = result is None
        _record("write 6 bytes returns None", passed,
                f"returned={result!r}")
        return passed
    except Exception as e:
        _record("write 6 bytes returns None", False, str(e))
        return False


# ---------------------------------------------------------------------------
# 26. word_size 16
# ---------------------------------------------------------------------------
def test_word_size_16():
    """Config 16-bit, read/write 16-bit words, verify values in 0-65535 range."""
    print("\n" + "=" * 60)
    print("TEST: word_size 16")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    try:
        spi.config(word_size=16)
        _record("config word_size=16", True)
    except Exception as e:
        _record("config word_size=16", False, str(e))
        return False

    try:
        result = spi.read(n_words=2)
        all_in_range = all(0 <= w <= 0xFFFF for w in result)
        passed = isinstance(result, list) and len(result) == 2 and all_in_range
        _record("read 2x16-bit words", passed,
                f"data={[hex(w) for w in result]}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read 2x16-bit words", False, str(e))
        ok = False

    try:
        result = spi.read_write(data=[0x1234, 0x5678])
        all_in_range = all(0 <= w <= 0xFFFF for w in result)
        passed = isinstance(result, list) and len(result) == 2 and all_in_range
        _record("read_write 2x16-bit words", passed,
                f"data={[hex(w) for w in result]}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read_write 2x16-bit words", False, str(e))
        ok = False

    # Reset to 8-bit
    spi.config(word_size=8)
    return ok


# ---------------------------------------------------------------------------
# 27. word_size 32
# ---------------------------------------------------------------------------
def test_word_size_32():
    """Config 32-bit, read/write 32-bit words, verify values in 0-4294967295 range."""
    print("\n" + "=" * 60)
    print("TEST: word_size 32")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    try:
        spi.config(word_size=32)
        _record("config word_size=32", True)
    except Exception as e:
        _record("config word_size=32", False, str(e))
        return False

    try:
        result = spi.read(n_words=1)
        all_in_range = all(0 <= w <= 0xFFFFFFFF for w in result)
        passed = isinstance(result, list) and len(result) == 1 and all_in_range
        _record("read 1x32-bit word", passed,
                f"data={[hex(w) for w in result]}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read 1x32-bit word", False, str(e))
        ok = False

    try:
        result = spi.read_write(data=[0x12345678])
        all_in_range = all(0 <= w <= 0xFFFFFFFF for w in result)
        passed = isinstance(result, list) and len(result) == 1 and all_in_range
        _record("read_write 1x32-bit word", passed,
                f"data={[hex(w) for w in result]}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read_write 1x32-bit word", False, str(e))
        ok = False

    # Reset to 8-bit
    spi.config(word_size=8)
    return ok


# ---------------------------------------------------------------------------
# 28. mode persistence
# ---------------------------------------------------------------------------
def test_mode_persistence():
    """Config mode 0, read, config mode 3, read -- both succeed."""
    print("\n" + "=" * 60)
    print("TEST: mode persistence")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    try:
        spi.config(mode=0)
        result = spi.read(n_words=4)
        passed = isinstance(result, list) and len(result) == 4
        _record("mode 0 then read", passed, f"data={result}")
        if not passed:
            ok = False
    except Exception as e:
        _record("mode 0 then read", False, str(e))
        ok = False

    try:
        spi.config(mode=3)
        result = spi.read(n_words=4)
        passed = isinstance(result, list) and len(result) == 4
        _record("mode 3 then read", passed, f"data={result}")
        if not passed:
            ok = False
    except Exception as e:
        _record("mode 3 then read", False, str(e))
        ok = False

    spi.config(mode=0)
    return ok


# ---------------------------------------------------------------------------
# 29. frequency change (LabJack: 500kHz and 800kHz)
# ---------------------------------------------------------------------------
def test_frequency_change():
    """Config 500kHz, read, config 800kHz, read -- both succeed."""
    print("\n" + "=" * 60)
    print("TEST: frequency change")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    try:
        spi.config(frequency_hz=500_000)
        result = spi.read(n_words=4)
        passed = isinstance(result, list) and len(result) == 4
        _record("500kHz then read", passed, f"data={result}")
        if not passed:
            ok = False
    except Exception as e:
        _record("500kHz then read", False, str(e))
        ok = False

    try:
        spi.config(frequency_hz=800_000)
        result = spi.read(n_words=4)
        passed = isinstance(result, list) and len(result) == 4
        _record("800kHz then read", passed, f"data={result}")
        if not passed:
            ok = False
    except Exception as e:
        _record("800kHz then read", False, str(e))
        ok = False

    spi.config(frequency_hz=800_000)
    return ok


# ---------------------------------------------------------------------------
# 30. dispatcher config
# ---------------------------------------------------------------------------
def test_dispatcher_config():
    """Direct dispatcher config() call."""
    print("\n" + "=" * 60)
    print("TEST: dispatcher config")
    print("=" * 60)

    try:
        from lager.protocols.spi.dispatcher import config

        config(SPI_NET, mode=0, bit_order="msb", frequency_hz=800_000,
               word_size=8, cs_active="low")
        _record("dispatcher config defaults", True)
    except Exception as e:
        _record("dispatcher config defaults", False, str(e))
        return False

    try:
        config(SPI_NET, mode=3)
        _record("dispatcher config mode=3", True)
    except Exception as e:
        _record("dispatcher config mode=3", False, str(e))
        return False

    config(SPI_NET, mode=0)
    return True


# ---------------------------------------------------------------------------
# 31. dispatcher read
# ---------------------------------------------------------------------------
def test_dispatcher_read():
    """Direct dispatcher read() call with output_format."""
    print("\n" + "=" * 60)
    print("TEST: dispatcher read")
    print("=" * 60)

    try:
        from lager.protocols.spi.dispatcher import read

        print("  (dispatcher read prints to stdout)")
        read(SPI_NET, n_words=4, fill=0xFF, output_format="hex")
        _record("dispatcher read hex", True)
    except Exception as e:
        _record("dispatcher read hex", False, str(e))
        return False

    try:
        read(SPI_NET, n_words=4, fill=0x00, output_format="bytes")
        _record("dispatcher read bytes", True)
    except Exception as e:
        _record("dispatcher read bytes", False, str(e))
        return False

    try:
        read(SPI_NET, n_words=4, output_format="json")
        _record("dispatcher read json", True)
    except Exception as e:
        _record("dispatcher read json", False, str(e))
        return False

    return True


# ---------------------------------------------------------------------------
# 32. dispatcher read_write
# ---------------------------------------------------------------------------
def test_dispatcher_read_write():
    """Direct dispatcher read_write() call."""
    print("\n" + "=" * 60)
    print("TEST: dispatcher read_write")
    print("=" * 60)

    try:
        from lager.protocols.spi.dispatcher import read_write

        print("  (dispatcher read_write prints to stdout)")
        read_write(SPI_NET, data=[0x9F, 0x00, 0x00, 0x00], output_format="hex")
        _record("dispatcher read_write", True)
    except Exception as e:
        _record("dispatcher read_write", False, str(e))
        return False

    try:
        read_write(SPI_NET, data=[0x9F], keep_cs=False, output_format="hex")
        _record("dispatcher read_write keep_cs=False", True)
    except Exception as e:
        _record("dispatcher read_write keep_cs=False", False, str(e))
        return False

    return True


# ---------------------------------------------------------------------------
# 33. dispatcher transfer
# ---------------------------------------------------------------------------
def test_dispatcher_transfer():
    """Direct dispatcher transfer() with padding and truncation."""
    print("\n" + "=" * 60)
    print("TEST: dispatcher transfer")
    print("=" * 60)

    ok = True

    try:
        from lager.protocols.spi.dispatcher import transfer

        # Padding: 1 byte data -> 4 word transfer
        print("  (dispatcher transfer prints to stdout)")
        transfer(SPI_NET, n_words=4, data=[0x9F], fill=0xFF, output_format="hex")
        _record("dispatcher transfer pad 1->4", True)
    except Exception as e:
        _record("dispatcher transfer pad 1->4", False, str(e))
        ok = False

    try:
        # Truncation: 10 bytes data -> 4 word transfer
        transfer(SPI_NET, n_words=4, data=list(range(10)), output_format="hex")
        _record("dispatcher transfer truncate 10->4", True)
    except Exception as e:
        _record("dispatcher transfer truncate 10->4", False, str(e))
        ok = False

    try:
        # No data (all fill)
        transfer(SPI_NET, n_words=4, output_format="hex")
        _record("dispatcher transfer no data", True)
    except Exception as e:
        _record("dispatcher transfer no data", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 34. dispatcher overrides
# ---------------------------------------------------------------------------
def test_dispatcher_overrides():
    """Dispatcher calls with overrides dict."""
    print("\n" + "=" * 60)
    print("TEST: dispatcher overrides")
    print("=" * 60)

    ok = True

    try:
        from lager.protocols.spi.dispatcher import read

        # Override mode and frequency
        overrides = {"mode": 1, "frequency_hz": 500_000}
        print("  (dispatcher read with overrides prints to stdout)")
        read(SPI_NET, n_words=4, output_format="hex", overrides=overrides)
        _record("dispatcher override mode=1 freq=500k", True)
    except Exception as e:
        _record("dispatcher override mode=1 freq=500k", False, str(e))
        ok = False

    try:
        # Override word size
        overrides = {"word_size": 16}
        read(SPI_NET, n_words=2, output_format="hex", overrides=overrides)
        _record("dispatcher override word_size=16", True)
    except Exception as e:
        _record("dispatcher override word_size=16", False, str(e))
        ok = False

    try:
        # Override bit order
        overrides = {"bit_order": "lsb"}
        read(SPI_NET, n_words=4, output_format="hex", overrides=overrides)
        _record("dispatcher override bit_order=lsb", True)
    except Exception as e:
        _record("dispatcher override bit_order=lsb", False, str(e))
        ok = False

    try:
        # Override cs_active
        overrides = {"cs_active": "high"}
        read(SPI_NET, n_words=4, output_format="hex", overrides=overrides)
        _record("dispatcher override cs_active=high", True)
    except Exception as e:
        _record("dispatcher override cs_active=high", False, str(e))
        ok = False

    # Reset to defaults
    from lager.protocols.spi.dispatcher import config
    config(SPI_NET, mode=0, bit_order="msb", frequency_hz=800_000,
           word_size=8, cs_active="low")

    return ok


# ---------------------------------------------------------------------------
# 35. max transaction boundary (LabJack-specific: 56 bytes)
# ---------------------------------------------------------------------------
def test_max_transaction_boundary():
    """Read exactly 56 bytes (should pass), attempt 57 bytes (should raise SPIBackendError)."""
    print("\n" + "=" * 60)
    print("TEST: max transaction boundary (LabJack 56 bytes)")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.spi import SPIBackendError
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=800_000, word_size=8)
    ok = True

    # Exactly 56 bytes -- should succeed
    try:
        result = spi.read(n_words=56)
        passed = isinstance(result, list) and len(result) == 56
        _record("read 56 bytes (max)", passed, f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read 56 bytes (max)", False, str(e))
        ok = False

    # 57 bytes -- should raise SPIBackendError
    try:
        spi.read(n_words=57)
        _record("read 57 bytes raises error", False, "no error raised")
        ok = False
    except SPIBackendError:
        _record("read 57 bytes raises SPIBackendError", True)
    except Exception as e:
        _record("read 57 bytes raises SPIBackendError", False,
                f"wrong exception: {type(e).__name__}: {e}")
        ok = False

    # Also test read_write at boundary
    try:
        data = [i & 0xFF for i in range(56)]
        result = spi.read_write(data=data)
        passed = isinstance(result, list) and len(result) == 56
        _record("read_write 56 bytes (max)", passed, f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read_write 56 bytes (max)", False, str(e))
        ok = False

    try:
        data = [i & 0xFF for i in range(57)]
        spi.read_write(data=data)
        _record("read_write 57 bytes raises error", False, "no error raised")
        ok = False
    except SPIBackendError:
        _record("read_write 57 bytes raises SPIBackendError", True)
    except Exception as e:
        _record("read_write 57 bytes raises SPIBackendError", False,
                f"wrong exception: {type(e).__name__}: {e}")
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 36. BMP280 chip ID (optional, skip if not wired)
# ---------------------------------------------------------------------------
def test_bmp280_chip_id():
    """If BMP280/BME280 is wired: read register 0xD0, expect 0x58 or 0x60."""
    print("\n" + "=" * 60)
    print("TEST: BMP280/BME280 chip ID (optional)")
    print("=" * 60)

    from lager import Net, NetType
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=800_000, word_size=8, cs_active="low")

    try:
        # BMP280/BME280 read: set bit 7 high for read -> 0xD0 | 0x80 = 0xD0
        # Register 0xD0 already has bit 7 set, so 0xD0 is the read command
        result = spi.read_write(data=[0xD0, 0x00])
        chip_id = result[1]
        known_ids = {0x58: "BMP280", 0x60: "BME280"}

        if chip_id in known_ids:
            _record("BMP280/BME280 chip ID", True,
                    f"chip_id=0x{chip_id:02X} ({known_ids[chip_id]})")
            return True
        elif chip_id == 0x00 or chip_id == 0xFF:
            _record("BMP280/BME280 chip ID", True,
                    f"chip_id=0x{chip_id:02X} (no sensor detected, SKIP)")
            return True  # Not a failure if no sensor is connected
        else:
            _record("BMP280/BME280 chip ID", True,
                    f"chip_id=0x{chip_id:02X} (unknown device, but SPI works)")
            return True  # SPI transaction worked, just unknown device
    except Exception as e:
        _record("BMP280/BME280 chip ID", False, str(e))
        return False


# ===================================================================
# Main
# ===================================================================
def main():
    """Run all tests."""
    print("LabJack T7 SPI Comprehensive Test Suite")
    print(f"Testing net: {SPI_NET}")
    print(f"Set SPI_NET environment variable to change the net name")
    print("=" * 60)

    tests = [
        ("Imports",                  test_imports),
        ("Net.get",                  test_net_get),
        ("get_config",               test_get_config),
        ("Config Defaults",          test_config_defaults),
        ("Config All Modes",         test_config_all_modes),
        ("Config Bit Order",         test_config_bit_order),
        ("Config Frequencies",       test_config_frequencies),
        ("Config Word Sizes",        test_config_word_sizes),
        ("Config CS Polarity",       test_config_cs_polarity),
        ("Config Invalid Params",    test_config_invalid_params),
        ("Read Basic",               test_read_basic),
        ("Read Fill Values",         test_read_fill_values),
        ("Read Output Formats",      test_read_output_formats),
        ("Read Keep CS",             test_read_keep_cs),
        ("Read/Write Basic",         test_read_write_basic),
        ("Read/Write Single Byte",   test_read_write_single_byte),
        ("Read/Write Empty",         test_read_write_empty),
        ("Read/Write Output Fmts",   test_read_write_output_formats),
        ("Transfer Padding",         test_transfer_padding),
        ("Transfer Truncation",      test_transfer_truncation),
        ("Transfer Exact",           test_transfer_exact),
        ("Transfer No Data",         test_transfer_no_data),
        ("Transfer Custom Fill",     test_transfer_custom_fill),
        ("Write Basic",              test_write_basic),
        ("Write Multi-Byte",         test_write_multi_byte),
        ("Word Size 16",             test_word_size_16),
        ("Word Size 32",             test_word_size_32),
        ("Mode Persistence",         test_mode_persistence),
        ("Frequency Change",         test_frequency_change),
        ("Dispatcher Config",        test_dispatcher_config),
        ("Dispatcher Read",          test_dispatcher_read),
        ("Dispatcher Read/Write",    test_dispatcher_read_write),
        ("Dispatcher Transfer",      test_dispatcher_transfer),
        ("Dispatcher Overrides",     test_dispatcher_overrides),
        ("Max Transaction Boundary", test_max_transaction_boundary),
        ("BMP280 Chip ID",           test_bmp280_chip_id),
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
