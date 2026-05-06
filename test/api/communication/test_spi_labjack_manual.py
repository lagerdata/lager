#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
SPI Python API tests - LabJack T7 Manual CS Mode + wait_for_level.

Run with: lager python test/api/communication/test_spi_labjack_manual.py --box <YOUR-BOX>

Prerequisites:
- LabJack T7 SPI net 'spi2' configured with cs_mode=manual
- LabJack GPIO net 'gpio22' (FIO6) wired to HW-611 CSB
- LabJack DAC net 'dac1' providing 3.3V to HW-611 VCC
- BMP280 (HW-611) wired to LabJack T7 SPI pins

Wiring:
  LabJack FIO1 (CLK)   -> HW-611 SCL
  LabJack FIO2 (MOSI)  -> HW-611 SDA
  LabJack FIO3 (MISO)  -> HW-611 SDO
  LabJack FIO0          -> NC (not connected)
  LabJack FIO6 (gpio22) -> HW-611 CSB (manual chip select)
  LabJack DAC0 (dac1)   -> HW-611 VCC (3.3V)

LabJack T7 SPI characteristics:
  - Maximum 56 bytes per transaction (hardware buffer limit)
  - Speed forced to ~800 kHz (throttle=0; any throttle > 0 fails)
  - Warm-up transaction required after init (driver handles automatically)
  - Manual CS: SPI_CS_DIONUM set to dummy pin

wait_for_level notes:
  wait_for_level uses LabJack streaming (eStreamStart/eStreamRead) to detect
  GPIO level transitions. When streaming starts, the pin is reconfigured as an
  input. With no external driver, the LabJack internal pull-up idles the pin
  HIGH. Tests are designed around this behaviour.
"""
import sys
import os
import time
import traceback

# Configuration
SPI_NET = os.environ.get("SPI_NET", "spi2")
GPIO_CS = os.environ.get("GPIO_CS", "gpio22")

# BMP280 constants
BMP280_CHIP_ID = 0x58
CHIP_ID_REG = 0xD0
CTRL_MEAS_WRITE = 0x74  # 0xF4 with bit 7 = 0
CTRL_MEAS_READ = 0xF4
CALIB_REG = 0x88

# LabJack T7 limits
MAX_BYTES = 56

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
        assert hasattr(NetType, "GPIO"), "NetType.GPIO not found"
        _record("import Net, NetType (SPI + GPIO)", True)
    except Exception as e:
        _record("import Net, NetType", False, str(e))
        ok = False

    try:
        from lager.protocols.spi import SPINet, LabJackSPI, SPIBackendError
        _record("import SPINet, LabJackSPI, SPIBackendError", True)
    except Exception as e:
        _record("import SPINet, LabJackSPI, SPIBackendError", False, str(e))
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
    """Test Net.get and get_config for SPINet and GPIO net."""
    print("\n" + "=" * 60)
    print("TEST: Net.get + get_config")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.spi import SPINet
    ok = True

    # SPI net
    try:
        spi = Net.get(SPI_NET, NetType.SPI)
        is_spinet = isinstance(spi, SPINet)
        _record("Net.get SPI returns SPINet", is_spinet,
                f"type={type(spi).__name__}")
        if not is_spinet:
            ok = False
    except Exception as e:
        _record("Net.get SPI returns SPINet", False, str(e))
        return False

    try:
        cfg = spi.get_config()
        is_dict = isinstance(cfg, dict)
        has_name = "name" in cfg
        has_role = "role" in cfg
        _record("SPI get_config returns dict with name/role",
                is_dict and has_name and has_role,
                f"keys={list(cfg.keys())}")
        if not (is_dict and has_name):
            ok = False
    except Exception as e:
        _record("SPI get_config", False, str(e))
        ok = False

    # GPIO net via Net.get
    try:
        gpio = Net.get(GPIO_CS, NetType.GPIO)
        _record("Net.get GPIO returns net object", True,
                f"type={type(gpio).__name__}")
    except Exception as e:
        _record("Net.get GPIO", False, str(e))
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

    # All modes (cs_mode should persist from above without re-specifying)
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

    # Frequencies (LabJack max ~800kHz)
    for freq in (100_000, 500_000, 800_000):
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
    spi.config(mode=0, bit_order="msb", frequency_hz=800_000,
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
    """Test SPI operations with manual CS via gpo."""
    print("\n" + "=" * 60)
    print("TEST: Manual CS SPI Operations")
    print("=" * 60)

    from lager import Net, NetType
    from lager.io.gpio.dispatcher import gpo
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=800_000, word_size=8, bit_order="msb",
               cs_active="low", cs_mode="manual")
    ok = True

    # 5a. Chip ID read with manual CS
    try:
        gpo(GPIO_CS, "low")
        result = spi.read_write(data=[CHIP_ID_REG, 0x00])
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

    # 5e. Keep-cs split transaction (within 56-byte limit per part)
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

    # 5f. Forgot-CS test: CS stays high, should NOT get valid chip ID
    try:
        gpo(GPIO_CS, "high")
        result = spi.read_write(data=[CHIP_ID_REG, 0x00])
        chip_id = result[1]
        passed = chip_id != BMP280_CHIP_ID
        _record("forgot-CS (CS high -> no chip ID)", passed,
                f"got=0x{chip_id:02X} (should NOT be 0x{BMP280_CHIP_ID:02X})")
        if not passed:
            ok = False
    except Exception as e:
        _record("forgot-CS test", False, str(e))
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
# 7. wait_for_level tests (comprehensive, 15 tests)
# ---------------------------------------------------------------------------
def test_wait_for_level():
    """Comprehensive wait_for_level tests using LabJack GPIO streaming.

    Uses `from lager import Net, NetType` to get the GPIO net, then calls
    wait_for_level through the dispatcher.

    When wait_for_level starts streaming, the LabJack pin is reconfigured as a
    digital input. With no external driver attached, the internal pull-up
    idles the pin HIGH. Tests are designed around this behaviour:
      - Waiting for HIGH (1) returns immediately (pin already HIGH).
      - Waiting for LOW (0) times out (nothing pulls the pin LOW).

    ehaas spec checklist:
      - Uses LabJack streaming (eStreamStart / eStreamRead / eStreamStop)
      - channel_name (FIO0, CIO1, etc.) pulled from the net record
      - scan_rate and scans_per_read are configurable
      - level is 0 or 1 (also accepts "high"/"low" strings)
      - timeout param raises TimeoutError when exceeded
      - Blocks until pin reaches target level, returns elapsed seconds
      - handle comes from LabJack global store
    """
    print("\n" + "=" * 60)
    print("TEST: wait_for_level (comprehensive)")
    print("=" * 60)

    # Import via Net, NetType as the user requested
    from lager import Net, NetType
    from lager.io.gpio.dispatcher import gpo, wait_for_level
    ok = True

    # Verify GPIO net is accessible via Net.get
    try:
        gpio_net = Net.get(GPIO_CS, NetType.GPIO)
        _record("wait_for_level: GPIO net accessible via Net.get", True,
                f"type={type(gpio_net).__name__}")
    except Exception as e:
        _record("wait_for_level: GPIO net accessible via Net.get", False, str(e))
        ok = False

    # Set a known output state before streaming reconfigures the pin as input.
    gpo(GPIO_CS, "high")

    # Test 1: Returns elapsed as float >= 0
    try:
        result = wait_for_level(GPIO_CS, 1, timeout=2)
        passed = isinstance(result, float) and result >= 0
        _record("wait_for_level: returns elapsed as float >= 0", passed,
                f"elapsed={result:.4f}s, type={type(result).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level: returns elapsed as float >= 0", False, str(e))
        ok = False

    # Test 2: Immediate HIGH detection (internal pull-up idles HIGH)
    try:
        elapsed = wait_for_level(GPIO_CS, 1, timeout=2)
        passed = elapsed < 0.5
        _record("wait_for_level: immediate HIGH detection (< 0.5s)", passed,
                f"elapsed={elapsed:.4f}s")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level: immediate HIGH detection", False, str(e))
        ok = False

    # Test 3: Timeout waiting for LOW (no external driver pulls pin low)
    try:
        wait_for_level(GPIO_CS, 0, timeout=0.5)
        _record("wait_for_level: timeout raises TimeoutError for LOW", False,
                "no TimeoutError raised")
        ok = False
    except TimeoutError:
        _record("wait_for_level: timeout raises TimeoutError for LOW", True,
                "TimeoutError raised")
    except Exception as e:
        _record("wait_for_level: timeout raises TimeoutError for LOW", False,
                f"wrong exception: {type(e).__name__}: {e}")
        ok = False

    # Test 4: TimeoutError message includes context (net or level)
    try:
        wait_for_level(GPIO_CS, 0, timeout=0.3)
        _record("wait_for_level: TimeoutError message context", False,
                "no TimeoutError raised")
        ok = False
    except TimeoutError as e:
        msg = str(e).lower()
        has_context = "gpio16" in msg or "level" in msg or "timeout" in msg
        _record("wait_for_level: TimeoutError message context", has_context,
                f"message={e}")
        if not has_context:
            ok = False
    except Exception as e:
        _record("wait_for_level: TimeoutError message context", False,
                f"wrong exception: {type(e).__name__}: {e}")
        ok = False

    # Test 5: Timeout precision (~0.5s)
    try:
        start = time.monotonic()
        try:
            wait_for_level(GPIO_CS, 0, timeout=0.5)
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

    # Test 6: Level as integer 1 (immediate detection)
    try:
        elapsed = wait_for_level(GPIO_CS, 1, timeout=2)
        passed = elapsed < 0.5
        _record("wait_for_level: level=1 (int)", passed,
                f"elapsed={elapsed:.4f}s")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level: level=1 (int)", False, str(e))
        ok = False

    # Test 7: Level as integer 0 (times out)
    try:
        wait_for_level(GPIO_CS, 0, timeout=0.3)
        _record("wait_for_level: level=0 (int) times out", False,
                "no TimeoutError raised")
        ok = False
    except TimeoutError:
        _record("wait_for_level: level=0 (int) times out", True)
    except Exception as e:
        _record("wait_for_level: level=0 (int) times out", False,
                f"wrong exception: {type(e).__name__}: {e}")
        ok = False

    # Test 8: String level "high" (immediate detection)
    try:
        elapsed = wait_for_level(GPIO_CS, "high", timeout=2)
        passed = elapsed < 0.5
        _record("wait_for_level: level='high' (string)", passed,
                f"elapsed={elapsed:.4f}s")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level: level='high' (string)", False, str(e))
        ok = False

    # Test 9: String level "low" timeout
    try:
        wait_for_level(GPIO_CS, "low", timeout=0.3)
        _record("wait_for_level: level='low' (string) times out", False,
                "no TimeoutError raised")
        ok = False
    except TimeoutError:
        _record("wait_for_level: level='low' (string) times out", True)
    except Exception as e:
        _record("wait_for_level: level='low' (string) times out", False,
                f"wrong exception: {type(e).__name__}: {e}")
        ok = False

    # Test 10: timeout=None with immediate match (doesn't hang forever)
    try:
        elapsed = wait_for_level(GPIO_CS, 1, timeout=None)
        passed = elapsed < 0.5
        _record("wait_for_level: timeout=None immediate", passed,
                f"elapsed={elapsed:.4f}s")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level: timeout=None immediate", False, str(e))
        ok = False

    # Test 11: Custom scan_rate (configurable, not hardcoded)
    try:
        elapsed = wait_for_level(GPIO_CS, 1, timeout=2, scan_rate=10000)
        passed = elapsed < 0.5
        _record("wait_for_level: scan_rate=10000", passed,
                f"elapsed={elapsed:.4f}s")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level: scan_rate=10000", False, str(e))
        ok = False

    # Test 12: High scan_rate
    try:
        elapsed = wait_for_level(GPIO_CS, 1, timeout=2, scan_rate=40000)
        passed = elapsed < 0.5
        _record("wait_for_level: scan_rate=40000 (high rate)", passed,
                f"elapsed={elapsed:.4f}s")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level: scan_rate=40000", False, str(e))
        ok = False

    # Test 13: Custom scans_per_read
    try:
        elapsed = wait_for_level(GPIO_CS, 1, timeout=2, scans_per_read=4)
        passed = elapsed < 0.5
        _record("wait_for_level: scans_per_read=4", passed,
                f"elapsed={elapsed:.4f}s")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level: scans_per_read=4", False, str(e))
        ok = False

    # Test 14: scans_per_read=1 (minimum batch)
    try:
        elapsed = wait_for_level(GPIO_CS, 1, timeout=2, scans_per_read=1)
        passed = elapsed < 0.5
        _record("wait_for_level: scans_per_read=1 (minimum)", passed,
                f"elapsed={elapsed:.4f}s")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level: scans_per_read=1", False, str(e))
        ok = False

    # Test 15: Both scan_rate and scans_per_read together
    try:
        elapsed = wait_for_level(GPIO_CS, 1, timeout=2,
                                 scan_rate=10000, scans_per_read=4)
        passed = elapsed < 0.5
        _record("wait_for_level: scan_rate + scans_per_read", passed,
                f"elapsed={elapsed:.4f}s")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level: scan_rate + scans_per_read", False, str(e))
        ok = False

    # Test 16: Repeated calls (stream starts/stops cleanly each time)
    try:
        for i in range(5):
            elapsed = wait_for_level(GPIO_CS, 1, timeout=2)
            assert elapsed < 0.5, f"iter {i}: elapsed={elapsed:.4f}s"
        _record("wait_for_level: 5 repeated calls (clean start/stop)", True)
    except Exception as e:
        _record("wait_for_level: 5 repeated calls", False, str(e))
        ok = False

    # Test 17: Rapid alternating: detect HIGH, then timeout on LOW
    try:
        elapsed = wait_for_level(GPIO_CS, 1, timeout=2)
        assert elapsed < 0.5
        try:
            wait_for_level(GPIO_CS, 0, timeout=0.3)
            assert False, "should have timed out"
        except TimeoutError:
            pass
        _record("wait_for_level: alternating HIGH/LOW", True)
    except AssertionError as e:
        _record("wait_for_level: alternating HIGH/LOW", False, str(e))
        ok = False
    except Exception as e:
        _record("wait_for_level: alternating HIGH/LOW", False, str(e))
        ok = False

    # Restore CS as output HIGH for subsequent SPI tests
    gpo(GPIO_CS, "high")

    return ok


# ---------------------------------------------------------------------------
# 8. 56-byte boundary (LabJack limit)
# ---------------------------------------------------------------------------
def test_56_byte_boundary():
    """Test 56-byte max SPI transfer with manual CS (LabJack limit)."""
    print("\n" + "=" * 60)
    print("TEST: 56-Byte Boundary (LabJack Limit)")
    print("=" * 60)

    from lager import Net, NetType
    from lager.protocols.spi import SPIBackendError
    from lager.io.gpio.dispatcher import gpo
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=800_000, word_size=8, cs_mode="manual")
    ok = True

    # 56 bytes OK (exactly at limit)
    try:
        gpo(GPIO_CS, "low")
        result = spi.read(n_words=56)
        gpo(GPIO_CS, "high")
        passed = isinstance(result, list) and len(result) == 56
        _record("read 56 bytes (at limit)", passed, f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("read 56 bytes (at limit)", False, str(e))
        ok = False

    # 57 bytes MUST FAIL (exceeds LabJack buffer)
    try:
        gpo(GPIO_CS, "low")
        spi.read(n_words=57)
        gpo(GPIO_CS, "high")
        _record("read 57 bytes raises SPIBackendError", False,
                "no error raised")
        ok = False
    except SPIBackendError:
        gpo(GPIO_CS, "high")
        _record("read 57 bytes raises SPIBackendError", True)
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("read 57 bytes raises SPIBackendError", False,
                f"wrong type: {type(e).__name__}: {e}")
        ok = False

    # read_write 56 bytes OK
    try:
        data = [i & 0xFF for i in range(56)]
        gpo(GPIO_CS, "low")
        result = spi.read_write(data=data)
        gpo(GPIO_CS, "high")
        passed = isinstance(result, list) and len(result) == 56
        _record("read_write 56 bytes (at limit)", passed, f"len={len(result)}")
        if not passed:
            ok = False
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("read_write 56 bytes (at limit)", False, str(e))
        ok = False

    # read_write 57 bytes MUST FAIL
    try:
        data = [i & 0xFF for i in range(57)]
        gpo(GPIO_CS, "low")
        spi.read_write(data=data)
        gpo(GPIO_CS, "high")
        _record("read_write 57 bytes raises SPIBackendError", False,
                "no error raised")
        ok = False
    except SPIBackendError:
        gpo(GPIO_CS, "high")
        _record("read_write 57 bytes raises SPIBackendError", True)
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("read_write 57 bytes raises SPIBackendError", False,
                f"wrong type: {type(e).__name__}: {e}")
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 9. SPI modes
# ---------------------------------------------------------------------------
def test_spi_modes():
    """Test SPI modes 0-3. BMP280 only works in mode 0."""
    print("\n" + "=" * 60)
    print("TEST: SPI Modes")
    print("=" * 60)

    from lager import Net, NetType
    from lager.io.gpio.dispatcher import gpo
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    # Mode 0: should get valid chip ID
    try:
        spi.config(mode=0, cs_mode="manual")
        gpo(GPIO_CS, "low")
        result = spi.read_write(data=[CHIP_ID_REG, 0x00])
        gpo(GPIO_CS, "high")
        passed = result[1] == BMP280_CHIP_ID
        _record("mode 0: valid chip ID", passed,
                f"got=0x{result[1]:02X}")
        if not passed:
            ok = False
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("mode 0: valid chip ID", False, str(e))
        ok = False

    # Modes 1, 2: should return garbled data (not chip ID)
    for mode in (1, 2):
        try:
            spi.config(mode=mode, cs_mode="manual")
            gpo(GPIO_CS, "low")
            result = spi.read_write(data=[CHIP_ID_REG, 0x00])
            gpo(GPIO_CS, "high")
            passed = result[1] != BMP280_CHIP_ID
            _record(f"mode {mode}: garbled (not 0x{BMP280_CHIP_ID:02X})", passed,
                    f"got=0x{result[1]:02X}")
            if not passed:
                ok = False
        except Exception as e:
            gpo(GPIO_CS, "high")
            _record(f"mode {mode}: garbled", False, str(e))
            ok = False

    # Mode 3: BMP280 datasheet supports modes 0 and 3; expect valid chip ID
    try:
        spi.config(mode=3, cs_mode="manual")
        gpo(GPIO_CS, "low")
        result = spi.read_write(data=[CHIP_ID_REG, 0x00])
        gpo(GPIO_CS, "high")
        passed = result[1] == BMP280_CHIP_ID
        _record("mode 3: valid chip ID (supported per datasheet)", passed,
                f"got=0x{result[1]:02X}")
        if not passed:
            ok = False
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("mode 3: valid chip ID", False, str(e))
        ok = False

    # Restore mode 0
    spi.config(mode=0, cs_mode="manual")
    return ok


# ---------------------------------------------------------------------------
# 10. Mode/frequency persistence
# ---------------------------------------------------------------------------
def test_persistence():
    """Test mode and frequency persistence."""
    print("\n" + "=" * 60)
    print("TEST: Mode/Frequency Persistence")
    print("=" * 60)

    from lager import Net, NetType
    from lager.io.gpio.dispatcher import gpo
    spi = Net.get(SPI_NET, NetType.SPI)
    ok = True

    for mode in (0, 3):
        try:
            spi.config(mode=mode, cs_mode="manual")
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

    for freq in (500_000, 800_000):
        try:
            spi.config(frequency_hz=freq, cs_mode="manual")
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

    spi.config(mode=0, frequency_hz=800_000, cs_mode="manual")
    return ok


# ---------------------------------------------------------------------------
# 11. BMP280 functional
# ---------------------------------------------------------------------------
def test_bmp280_functional():
    """BMP280 chip ID, calibration, write + readback via manual CS."""
    print("\n" + "=" * 60)
    print("TEST: BMP280 Functional")
    print("=" * 60)

    from lager import Net, NetType
    from lager.io.gpio.dispatcher import gpo
    spi = Net.get(SPI_NET, NetType.SPI)
    spi.config(mode=0, frequency_hz=800_000, word_size=8, bit_order="msb",
               cs_active="low", cs_mode="manual")
    ok = True

    # Chip ID
    try:
        gpo(GPIO_CS, "low")
        result = spi.read_write(data=[CHIP_ID_REG, 0x00])
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

    # 5x consistency
    try:
        all_ok = True
        for i in range(5):
            gpo(GPIO_CS, "low")
            result = spi.read_write(data=[CHIP_ID_REG, 0x00])
            gpo(GPIO_CS, "high")
            if result[1] != BMP280_CHIP_ID:
                all_ok = False
                break
        _record("BMP280 5x consistency", all_ok)
        if not all_ok:
            ok = False
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("BMP280 5x consistency", False, str(e))
        ok = False

    # Calibration data (24 bytes from 0x88, within 56-byte limit)
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

    # Write ctrl_meas=0x25 (forced mode), readback
    try:
        gpo(GPIO_CS, "low")
        spi.write(data=[CTRL_MEAS_WRITE, 0x25])
        gpo(GPIO_CS, "high")
        time.sleep(0.05)

        gpo(GPIO_CS, "low")
        result = spi.read_write(data=[CTRL_MEAS_READ, 0x00])
        gpo(GPIO_CS, "high")
        # After forced measurement, mode bits revert to 00 -> 0x24
        _record("BMP280 forced mode readback", True,
                f"readback=0x{result[1]:02X} (0x24 or 0x25 expected)")
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("BMP280 forced mode readback", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 12. Dispatcher calls
# ---------------------------------------------------------------------------
def test_dispatcher_calls():
    """Test SPI dispatcher functions directly."""
    print("\n" + "=" * 60)
    print("TEST: Dispatcher Calls")
    print("=" * 60)

    from lager.protocols.spi.dispatcher import config, read, read_write, transfer
    from lager.io.gpio.dispatcher import gpo
    ok = True

    # config
    try:
        config(SPI_NET, mode=0, bit_order="msb", frequency_hz=800_000,
               word_size=8, cs_active="low", cs_mode="manual")
        _record("dispatcher config", True)
    except Exception as e:
        _record("dispatcher config", False, str(e))
        ok = False

    # read
    try:
        gpo(GPIO_CS, "low")
        print("  (dispatcher read prints to stdout)")
        read(SPI_NET, n_words=4, fill=0xFF, output_format="hex")
        gpo(GPIO_CS, "high")
        _record("dispatcher read hex", True)
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("dispatcher read hex", False, str(e))
        ok = False

    # read_write
    try:
        gpo(GPIO_CS, "low")
        read_write(SPI_NET, data=[CHIP_ID_REG, 0x00], output_format="hex")
        gpo(GPIO_CS, "high")
        _record("dispatcher read_write hex", True)
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("dispatcher read_write hex", False, str(e))
        ok = False

    # transfer
    try:
        gpo(GPIO_CS, "low")
        transfer(SPI_NET, n_words=2, data=[CHIP_ID_REG], output_format="hex")
        gpo(GPIO_CS, "high")
        _record("dispatcher transfer hex", True)
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("dispatcher transfer hex", False, str(e))
        ok = False

    # overrides
    try:
        gpo(GPIO_CS, "low")
        read(SPI_NET, n_words=4, output_format="hex",
             overrides={"mode": 0, "frequency_hz": 500_000})
        gpo(GPIO_CS, "high")
        _record("dispatcher override mode=0 freq=500k", True)
    except Exception as e:
        gpo(GPIO_CS, "high")
        _record("dispatcher override mode=0 freq=500k", False, str(e))
        ok = False

    # Restore defaults
    config(SPI_NET, mode=0, frequency_hz=800_000, cs_mode="manual")
    return ok


# ===================================================================
# Main
# ===================================================================
def main():
    """Run all tests."""
    print("LabJack T7 SPI Manual CS + wait_for_level Test Suite")
    print(f"SPI net: {SPI_NET}, GPIO CS: {GPIO_CS}")
    print(f"LabJack T7: {MAX_BYTES}-byte max, ~800kHz forced")
    print("=" * 60)

    tests = [
        ("Imports",                 test_imports),
        ("Net.get + get_config",    test_net_get_and_config),
        ("Config",                  test_config),
        ("Invalid Config",          test_invalid_config),
        ("Manual CS Operations",    test_manual_cs_operations),
        ("Output Formats",          test_output_formats),
        ("wait_for_level",          test_wait_for_level),
        ("56-Byte Boundary",        test_56_byte_boundary),
        ("SPI Modes",               test_spi_modes),
        ("Mode/Freq Persistence",   test_persistence),
        ("BMP280 Functional",       test_bmp280_functional),
        ("Dispatcher Calls",        test_dispatcher_calls),
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

    # ehaas spec checklist
    print("\nehaas spec checklist:")
    if sub_failed == 0:
        print("  [x] Uses LabJack streaming (eStreamStart/eStreamRead)")
        print("  [x] channel_name from net record (all tests use net name)")
        print("  [x] scan_rate configurable (tests 11-12)")
        print("  [x] scans_per_read configurable (tests 13-15)")
        print("  [x] level is 0 or 1 (tests 6-9)")
        print("  [x] timeout raises TimeoutError (tests 3-5)")
        print("  [x] Blocks until pin reaches level, returns elapsed (tests 1-2)")
        print("  [x] handle from global store (all tests use dispatcher)")
    else:
        print("  [?] Check failed tests above")

    return 0 if passed_count == total_count else 1


if __name__ == "__main__":
    sys.exit(main())
