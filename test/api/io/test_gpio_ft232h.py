#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive GPIO edge case tests targeting the FT232H adapter.

Run with: lager python test/api/io/test_gpio_ft232h.py --box <boxname>

Prerequisites:
- GPIO nets configured via 'lager nets add-all' (creates gpio1=AD4, gpio2=AD5, etc.)
- Or manually: 'lager nets add gpio1 gpio FTDI_FT232H <address> --pin 4'

Hardware:
- QYF-740 vibrating motor module wired as:
    VCC  -> Rigol DP821 CH1 positive (supply1 net, 3.3V)
    GND  -> Rigol DP821 CH1 negative
    IN   -> FT232H AD4 grey wire (gpio1 net)
- FT232H GND (black wire) tied to Rigol CH1 negative (common ground)
- Optional: AD5 for input testing (gpio2 net)
- Optional: AD4->AD5 jumper wire for loopback testing
- Power: lager supply supply1 voltage 3.3 --yes && lager supply supply1 enable --box <box>

FT232H GPIO capabilities:
- 16 GPIO pins (AD0-AD7, AC0-AC7) -- AD0-AD2 reserved for I2C
- File-based state caching for cross-process toggle support
- USB disconnect recovery with exponential backoff
- Cannot use GPIO and I2C simultaneously (each claims FTDI interface)
"""
import sys
import os
import json
import time
import traceback

# Configuration - change these or set env vars
GPIO_OUT_NET = os.environ.get("GPIO_OUT_NET", "gpio1")
GPIO_IN_NET = os.environ.get("GPIO_IN_NET", "gpio2")

# Cache file path must match driver constant
FT232H_CACHE_FILE = "/tmp/ft232h_gpio_cache.json"

# Loopback detection flag (set in Group 13)
LOOPBACK_AVAILABLE = None  # None = not yet tested, True/False after detection

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


def _skip(name, reason=""):
    """Record a skipped sub-test."""
    _results.append((name, True, f"SKIP: {reason}"))
    msg = f"  SKIP: {name}"
    if reason:
        msg += f" -- {reason}"
    print(msg)


# ---------------------------------------------------------------------------
# 1. Imports
# ---------------------------------------------------------------------------
def test_imports():
    """Verify all GPIO module imports work."""
    print("\n" + "=" * 60)
    print("TEST GROUP 1: Imports")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        assert hasattr(NetType, "GPIO"), "NetType.GPIO not found"
        _record("import Net, NetType", True)
    except Exception as e:
        _record("import Net, NetType", False, str(e))
        ok = False

    try:
        from lager.io.gpio import GPIOBase
        _record("import GPIOBase", True)
    except Exception as e:
        _record("import GPIOBase", False, str(e))
        ok = False

    try:
        from lager.io.gpio import FT232HGPIO
        _record("import FT232HGPIO", True)
    except Exception as e:
        _record("import FT232HGPIO", False, str(e))
        ok = False

    try:
        from lager.exceptions import GPIOBackendError
        _record("import GPIOBackendError", True)
    except Exception as e:
        _record("import GPIOBackendError", False, str(e))
        ok = False

    try:
        from lager.io.gpio.dispatcher import gpi, gpo
        _record("import dispatcher gpi, gpo", True)
    except Exception as e:
        _record("import dispatcher gpi, gpo", False, str(e))
        ok = False

    try:
        from lager.io.gpio import gpi as mod_gpi, gpo as mod_gpo
        _record("import module-level gpi, gpo", True)
    except Exception as e:
        _record("import module-level gpi, gpo", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 2. Net.get Factory
# ---------------------------------------------------------------------------
def test_net_get_factory():
    """Test Net.get returns FT232HGPIO for both output and input nets."""
    print("\n" + "=" * 60)
    print("TEST GROUP 2: Net.get Factory")
    print("=" * 60)

    from lager import Net, NetType
    from lager.io.gpio import FT232HGPIO
    ok = True

    # 2a. Output net -> FT232HGPIO
    try:
        gpio = Net.get(GPIO_OUT_NET, type=NetType.GPIO)
        passed = isinstance(gpio, FT232HGPIO)
        _record("Net.get output -> FT232HGPIO", passed,
                f"type={type(gpio).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("Net.get output -> FT232HGPIO", False, str(e))
        ok = False

    # 2b. Input net -> FT232HGPIO (skip if GPIO_IN_NET not configured)
    try:
        gpio = Net.get(GPIO_IN_NET, type=NetType.GPIO)
        passed = isinstance(gpio, FT232HGPIO)
        _record("Net.get input -> FT232HGPIO", passed,
                f"type={type(gpio).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _skip("Net.get input -> FT232HGPIO",
              f"input net '{GPIO_IN_NET}' not configured: {e}")

    # 2c. Separate calls return same type
    try:
        g1 = Net.get(GPIO_OUT_NET, type=NetType.GPIO)
        g2 = Net.get(GPIO_OUT_NET, type=NetType.GPIO)
        passed = type(g1) is type(g2)
        _record("separate calls same type", passed,
                f"type1={type(g1).__name__}, type2={type(g2).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("separate calls same type", False, str(e))
        ok = False

    # 2d. Invalid net raises exception
    try:
        Net.get("NONEXIST_GPIO_NET_999", type=NetType.GPIO)
        _record("invalid net raises exception", False, "no exception raised")
        ok = False
    except Exception:
        _record("invalid net raises exception", True)

    return ok


# ---------------------------------------------------------------------------
# 3. GPIOBase Inheritance & Properties
# ---------------------------------------------------------------------------
def test_gpio_base():
    """Test GPIOBase inheritance and basic properties."""
    print("\n" + "=" * 60)
    print("TEST GROUP 3: GPIOBase Inheritance & Properties")
    print("=" * 60)

    from lager import Net, NetType
    from lager.io.gpio import GPIOBase, FT232HGPIO
    ok = True

    # 3a. issubclass
    passed = issubclass(FT232HGPIO, GPIOBase)
    _record("issubclass(FT232HGPIO, GPIOBase)", passed)
    if not passed:
        ok = False

    # 3b-3e. Properties on a real instance
    try:
        gpio = Net.get(GPIO_OUT_NET, type=NetType.GPIO)

        # 3b. .name returns net name
        passed = gpio.name == GPIO_OUT_NET
        _record(".name returns net name", passed,
                f"name={gpio.name!r}")
        if not passed:
            ok = False

        # 3c. .pin returns pin value
        passed = gpio.pin is not None
        _record(".pin returns pin value", passed,
                f"pin={gpio.pin!r}")
        if not passed:
            ok = False

        # 3d. ._pin_num is int 0-15
        passed = isinstance(gpio._pin_num, int) and 0 <= gpio._pin_num <= 15
        _record("._pin_num is int 0-15", passed,
                f"_pin_num={gpio._pin_num!r}")
        if not passed:
            ok = False

        # 3e. output and input are callable
        passed = callable(gpio.output) and callable(gpio.input)
        _record("output/input are callable", passed)
        if not passed:
            ok = False

    except Exception as e:
        _record("property access", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 4. Output Integer Levels
# ---------------------------------------------------------------------------
def test_output_int_levels():
    """Test output() with various integer values."""
    print("\n" + "=" * 60)
    print("TEST GROUP 4: Output Integer Levels")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(GPIO_OUT_NET, type=NetType.GPIO)
    ok = True

    # 4a. output(1) -> HIGH
    try:
        gpio.output(1)
        _record("output(1)", True)
    except Exception as e:
        _record("output(1)", False, str(e))
        ok = False

    # 4b. output(0) -> LOW
    try:
        gpio.output(0)
        _record("output(0)", True)
    except Exception as e:
        _record("output(0)", False, str(e))
        ok = False

    # 4c. output(-1) -> nonzero is truthy -> HIGH
    try:
        gpio.output(-1)
        _record("output(-1) [nonzero->HIGH]", True)
    except Exception as e:
        _record("output(-1)", False, str(e))
        ok = False

    # 4d. output(42) -> nonzero is truthy -> HIGH
    try:
        gpio.output(42)
        _record("output(42) [nonzero->HIGH]", True)
    except Exception as e:
        _record("output(42)", False, str(e))
        ok = False

    # 4e. output(True) and output(False)
    try:
        gpio.output(True)
        _record("output(True)", True)
    except Exception as e:
        _record("output(True)", False, str(e))
        ok = False

    try:
        gpio.output(False)
        _record("output(False)", True)
    except Exception as e:
        _record("output(False)", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 5. Output String Levels
# ---------------------------------------------------------------------------
def test_output_string_levels():
    """Test output() with various string values."""
    print("\n" + "=" * 60)
    print("TEST GROUP 5: Output String Levels")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(GPIO_OUT_NET, type=NetType.GPIO)
    ok = True

    for level in ("high", "low", "on", "off", "1", "0", "true", "false"):
        try:
            gpio.output(level)
            _record(f"output('{level}')", True)
        except Exception as e:
            _record(f"output('{level}')", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 6. Output Edge Case Types
# ---------------------------------------------------------------------------
def test_output_edge_cases():
    """Test output() with edge case types: float, None, nonsense string."""
    print("\n" + "=" * 60)
    print("TEST GROUP 6: Output Edge Case Types")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(GPIO_OUT_NET, type=NetType.GPIO)
    ok = True

    # 6a. output(3.14) -> float, int(3.14) = 3, truthy -> HIGH
    try:
        gpio.output(3.14)
        _record("output(3.14) [float->int->truthy->HIGH]", True)
    except Exception as e:
        _record("output(3.14)", False, str(e))
        ok = False

    # 6b. output(0.0) -> int(0.0) = 0 -> LOW
    try:
        gpio.output(0.0)
        _record("output(0.0) [float->int->0->LOW]", True)
    except Exception as e:
        _record("output(0.0)", False, str(e))
        ok = False

    # 6c. output(None) -> int(None) raises TypeError
    try:
        gpio.output(None)
        _record("output(None) raises TypeError", False, "no error raised")
        ok = False
    except TypeError:
        _record("output(None) raises TypeError", True)
    except Exception as e:
        _record("output(None) raises error", True,
                f"{type(e).__name__}: {e}")

    # 6d. output("banana") -> not in HIGH set -> maps to LOW (no error)
    # _parse_level: "banana" not in ("1","on","high","true") -> returns 0
    try:
        gpio.output("banana")
        _record("output('banana') -> LOW (no error)", True)
    except Exception as e:
        _record("output('banana')", False, str(e))
        ok = False

    # 6e. output("") -> empty string -> maps to LOW
    try:
        gpio.output("")
        _record("output('') -> LOW (no error)", True)
    except Exception as e:
        _record("output('')", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 7. Input Read Behavior
# ---------------------------------------------------------------------------
def test_input_read():
    """Test input() returns correct types and values."""
    print("\n" + "=" * 60)
    print("TEST GROUP 7: Input Read Behavior")
    print("=" * 60)

    # Use OUT_NET for basic input tests -- a separate input net may not exist.
    # The output pin can always be read back.
    from lager import Net, NetType
    gpio = Net.get(GPIO_OUT_NET, type=NetType.GPIO)
    ok = True

    # 7a. Returns 0 or 1
    try:
        value = gpio.input()
        passed = value in (0, 1)
        _record("input() returns 0 or 1", passed,
                f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("input() returns 0 or 1", False, str(e))
        ok = False

    # 7b. Returns int type
    try:
        value = gpio.input()
        passed = isinstance(value, int)
        _record("input() returns int", passed,
                f"type={type(value).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("input() returns int", False, str(e))
        ok = False

    # 7c. type(value) is int (not bool)
    try:
        value = gpio.input()
        passed = type(value) is int
        _record("type(input()) is int (not bool)", passed,
                f"type={type(value).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("type is int not bool", False, str(e))
        ok = False

    # 7d. 3 consecutive reads succeed
    try:
        vals = [gpio.input() for _ in range(3)]
        passed = all(v in (0, 1) for v in vals)
        _record("3 consecutive reads succeed", passed,
                f"values={vals}")
        if not passed:
            ok = False
    except Exception as e:
        _record("3 consecutive reads", False, str(e))
        ok = False

    # 7e. Input on output pin (same net, no loopback required)
    try:
        gpio_out = Net.get(GPIO_OUT_NET, type=NetType.GPIO)
        value = gpio_out.input()
        passed = value in (0, 1)
        _record("input() on output pin", passed,
                f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("input() on output pin", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 8. Pin Parsing Valid
# ---------------------------------------------------------------------------
def test_pin_parsing_valid():
    """Verify FT232HGPIO accepts valid pin formats."""
    print("\n" + "=" * 60)
    print("TEST GROUP 8: Pin Parsing Valid")
    print("=" * 60)

    from lager.io.gpio import FT232HGPIO
    ok = True

    # Boundary integers
    test_cases = [
        (0, 0, "pin 0 (boundary)"),
        (15, 15, "pin 15 (boundary)"),
        (7, 7, "pin 7 (last AD)"),
        (8, 8, "pin 8 (first AC)"),
    ]
    for pin_input, expected, desc in test_cases:
        try:
            gpio = FT232HGPIO(f"test_{desc}", pin_input)
            passed = gpio._pin_num == expected
            _record(f"pin={pin_input} ({desc})", passed,
                    f"_pin_num={gpio._pin_num}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"pin={pin_input} ({desc})", False, str(e))
            ok = False

    # String numeric
    for pin_str in ("4", "0", "15"):
        try:
            gpio = FT232HGPIO(f"test_str_{pin_str}", pin_str)
            passed = gpio._pin_num == int(pin_str)
            _record(f"pin='{pin_str}' (string)", passed,
                    f"_pin_num={gpio._pin_num}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"pin='{pin_str}' (string)", False, str(e))
            ok = False

    # Named pins
    named_cases = [
        ("AD4", 4),
        ("ad4", 4),   # case insensitive
        ("AC0", 8),
        ("AC7", 15),
    ]
    for pin_name, expected in named_cases:
        try:
            gpio = FT232HGPIO(f"test_{pin_name}", pin_name)
            passed = gpio._pin_num == expected
            _record(f"pin='{pin_name}' -> {expected}", passed,
                    f"_pin_num={gpio._pin_num}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"pin='{pin_name}'", False, str(e))
            ok = False

    # Invalid: 16 (out of range)
    try:
        FT232HGPIO("test_16", 16)
        _record("pin=16 raises error", False, "no error raised")
        ok = False
    except Exception:
        _record("pin=16 raises error", True)

    # Invalid: -1 (negative)
    try:
        FT232HGPIO("test_neg", -1)
        _record("pin=-1 raises error", False, "no error raised")
        ok = False
    except Exception:
        _record("pin=-1 raises error", True)

    # Invalid: "XX9" (bad name)
    try:
        FT232HGPIO("test_xx9", "XX9")
        _record("pin='XX9' raises error", False, "no error raised")
        ok = False
    except Exception:
        _record("pin='XX9' raises error", True)

    return ok


# ---------------------------------------------------------------------------
# 9. Pin Parsing Invalid Types
# ---------------------------------------------------------------------------
def test_pin_parsing_invalid_types():
    """FT232HGPIO rejects invalid pin types."""
    print("\n" + "=" * 60)
    print("TEST GROUP 9: Pin Parsing Invalid Types")
    print("=" * 60)

    from lager.io.gpio import FT232HGPIO
    from lager.exceptions import GPIOBackendError
    ok = True

    invalid_pins = [
        (None, "None"),
        (3.14, "float 3.14"),
        ([4], "list [4]"),
        ("", "empty string"),
    ]

    for pin_val, desc in invalid_pins:
        try:
            FT232HGPIO(f"test_{desc}", pin_val)
            _record(f"pin={desc} raises error", False, "no error raised")
            ok = False
        except (GPIOBackendError, TypeError, ValueError):
            _record(f"pin={desc} raises error", True)
        except Exception as e:
            _record(f"pin={desc} raises error", True,
                    f"{type(e).__name__}: {e}")

    return ok


# ---------------------------------------------------------------------------
# 10. Serial Parameter
# ---------------------------------------------------------------------------
def test_serial_param():
    """Test FT232HGPIO serial parameter handling."""
    print("\n" + "=" * 60)
    print("TEST GROUP 10: Serial Parameter")
    print("=" * 60)

    from lager.io.gpio import FT232HGPIO
    ok = True

    # 10a. serial=None -> ._serial is None
    try:
        gpio = FT232HGPIO("test_no_serial", 4, serial=None)
        passed = gpio._serial is None
        _record("serial=None -> ._serial is None", passed,
                f"_serial={gpio._serial!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("serial=None", False, str(e))
        ok = False

    # 10b. serial="FT123" -> stored
    try:
        gpio = FT232HGPIO("test_serial", 4, serial="FT123")
        passed = gpio._serial == "FT123"
        _record("serial='FT123' -> stored", passed,
                f"_serial={gpio._serial!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("serial='FT123'", False, str(e))
        ok = False

    # 10c. _build_url() includes serial
    try:
        gpio = FT232HGPIO("test_url_serial", 4, serial="FT123")
        url = gpio._build_url()
        passed = "FT123" in url
        _record("_build_url() includes serial", passed,
                f"url={url!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("_build_url() serial", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 11. Exception Hierarchy
# ---------------------------------------------------------------------------
def test_exception_hierarchy():
    """Verify GPIOBackendError inherits from LagerBackendError."""
    print("\n" + "=" * 60)
    print("TEST GROUP 11: Exception Hierarchy")
    print("=" * 60)

    ok = True

    # 11a. GPIOBackendError -> LagerBackendError
    try:
        from lager.exceptions import GPIOBackendError, LagerBackendError
        passed = issubclass(GPIOBackendError, LagerBackendError)
        _record("GPIOBackendError -> LagerBackendError", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("GPIOBackendError -> LagerBackendError", False, str(e))
        ok = False

    # 11b. GPIOBackendError -> Exception
    try:
        from lager.exceptions import GPIOBackendError
        passed = issubclass(GPIOBackendError, Exception)
        _record("GPIOBackendError -> Exception", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("GPIOBackendError -> Exception", False, str(e))
        ok = False

    # 11c. Instantiation
    try:
        from lager.exceptions import GPIOBackendError
        err = GPIOBackendError("test error message")
        passed = isinstance(err, GPIOBackendError)
        _record("GPIOBackendError instantiation", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("GPIOBackendError instantiation", False, str(e))
        ok = False

    # 11d. str representation
    try:
        from lager.exceptions import GPIOBackendError
        err = GPIOBackendError("test error message")
        passed = "test error" in str(err)
        _record("GPIOBackendError str()", passed,
                f"str={str(err)!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("GPIOBackendError str()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 12. Toggle via Output
# ---------------------------------------------------------------------------
def test_toggle_via_output():
    """Test manual toggling using output(0)/output(1) sequences."""
    print("\n" + "=" * 60)
    print("TEST GROUP 12: Toggle via Output")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(GPIO_OUT_NET, type=NetType.GPIO)
    ok = True

    # 12a. Manual 0 -> 1 -> 0 sequence
    try:
        gpio.output(0)
        gpio.output(1)
        gpio.output(0)
        _record("manual 0->1->0 sequence", True)
    except Exception as e:
        _record("manual 0->1->0", False, str(e))
        ok = False

    # 12b. Rapid 10 cycles
    try:
        for _ in range(10):
            gpio.output(1)
            gpio.output(0)
        _record("rapid 10 cycles", True)
    except Exception as e:
        _record("rapid 10 cycles", False, str(e))
        ok = False

    # 12c. Rapid 50 cycles (stress)
    try:
        for _ in range(50):
            gpio.output(1)
            gpio.output(0)
        _record("rapid 50 cycles (stress)", True)
    except Exception as e:
        _record("rapid 50 cycles", False, str(e))
        ok = False

    # 12d. String toggle: "high" -> "low" -> "high"
    try:
        gpio.output("high")
        gpio.output("low")
        gpio.output("high")
        _record("string toggle high->low->high", True)
    except Exception as e:
        _record("string toggle", False, str(e))
        ok = False

    # 12e. Interleaved output/input on same pin
    try:
        gpio.output(1)
        v1 = gpio.input()
        gpio.output(0)
        v0 = gpio.input()
        passed = v1 in (0, 1) and v0 in (0, 1)
        _record("interleaved output/input", passed,
                f"after_high={v1}, after_low={v0}")
        if not passed:
            ok = False
    except Exception as e:
        _record("interleaved output/input", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 13. Loopback (AD4 output -> AD5 input)
# ---------------------------------------------------------------------------
def test_loopback():
    """Set output pin, read input pin (if wired together)."""
    global LOOPBACK_AVAILABLE

    print("\n" + "=" * 60)
    print("TEST GROUP 13: Loopback (output -> input)")
    print("=" * 60)
    print("  (Requires AD4->AD5 jumper wire)")

    from lager import Net, NetType
    gpio_out = Net.get(GPIO_OUT_NET, type=NetType.GPIO)

    # Try to get the input net -- skip all loopback tests if it doesn't exist
    try:
        gpio_in = Net.get(GPIO_IN_NET, type=NetType.GPIO)
    except Exception as e:
        LOOPBACK_AVAILABLE = False
        _skip("loopback detection",
              f"input net '{GPIO_IN_NET}' not configured: {e}")
        print(f"  Input net '{GPIO_IN_NET}' not configured -- loopback tests skipped")
        return True

    ok = True

    # 13a. Detect loopback: set high, read input
    try:
        gpio_out.output(1)
        time.sleep(0.1)
        value = gpio_in.input()
        if value == 1:
            LOOPBACK_AVAILABLE = True
            _record("loopback detection (high->read)", True,
                    "loopback detected")
        else:
            LOOPBACK_AVAILABLE = False
            _record("loopback detection", True,
                    "no loopback (tests will skip)")
            print("  No loopback detected -- remaining loopback tests will skip")
            return True
    except Exception as e:
        LOOPBACK_AVAILABLE = False
        _record("loopback detection", False, str(e))
        return False

    # 13b. High -> read 1
    try:
        gpio_out.output(1)
        time.sleep(0.1)
        value = gpio_in.input()
        passed = value == 1
        _record("loopback high -> read 1", passed,
                f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("loopback high", False, str(e))
        ok = False

    # 13c. Low -> read 0
    try:
        gpio_out.output(0)
        time.sleep(0.1)
        value = gpio_in.input()
        passed = value == 0
        _record("loopback low -> read 0", passed,
                f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("loopback low", False, str(e))
        ok = False

    # 13d. Toggle and verify
    try:
        gpio_out.output(0)
        time.sleep(0.05)
        gpio_out.output(1)
        time.sleep(0.1)
        value = gpio_in.input()
        passed = value == 1
        _record("loopback toggle -> read 1", passed,
                f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("loopback toggle", False, str(e))
        ok = False

    # 13e. 3x consistency high
    try:
        gpio_out.output(1)
        time.sleep(0.1)
        vals = [gpio_in.input() for _ in range(3)]
        passed = all(v == 1 for v in vals)
        _record("loopback high 3x consistent", passed,
                f"values={vals}")
        if not passed:
            ok = False
    except Exception as e:
        _record("loopback high 3x", False, str(e))
        ok = False

    # 13f. 3x consistency low
    try:
        gpio_out.output(0)
        time.sleep(0.1)
        vals = [gpio_in.input() for _ in range(3)]
        passed = all(v == 0 for v in vals)
        _record("loopback low 3x consistent", passed,
                f"values={vals}")
        if not passed:
            ok = False
    except Exception as e:
        _record("loopback low 3x", False, str(e))
        ok = False

    # 13g. Rapid 10x with verification
    try:
        errors = 0
        for i in range(10):
            expected = i % 2  # alternating 0,1,0,1,...
            gpio_out.output(expected)
            time.sleep(0.05)
            actual = gpio_in.input()
            if actual != expected:
                errors += 1
        passed = errors == 0
        _record("loopback rapid 10x verify", passed,
                f"errors={errors}")
        if not passed:
            ok = False
    except Exception as e:
        _record("loopback rapid 10x", False, str(e))
        ok = False

    # Cleanup
    gpio_out.output(0)
    return ok


# ---------------------------------------------------------------------------
# 14. Dispatcher Direct
# ---------------------------------------------------------------------------
def test_dispatcher_direct():
    """Import and call gpi(), gpo() from dispatcher module."""
    print("\n" + "=" * 60)
    print("TEST GROUP 14: Dispatcher Direct")
    print("=" * 60)

    from lager.io.gpio.dispatcher import gpi, gpo
    ok = True

    # 14a. gpo(OUT_NET, "low")
    try:
        gpo(GPIO_OUT_NET, "low")
        _record("dispatcher gpo('low')", True)
    except Exception as e:
        _record("dispatcher gpo('low')", False, str(e))
        ok = False

    # 14b. gpo(OUT_NET, "high")
    try:
        gpo(GPIO_OUT_NET, "high")
        _record("dispatcher gpo('high')", True)
    except Exception as e:
        _record("dispatcher gpo('high')", False, str(e))
        ok = False

    # 14c. gpi(OUT_NET)
    try:
        value = gpi(GPIO_OUT_NET)
        passed = value in (0, 1)
        _record("dispatcher gpi()", passed,
                f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("dispatcher gpi()", False, str(e))
        ok = False

    # 14d. gpo("NONEXIST", "low") raises error
    try:
        gpo("NONEXIST_GPIO_NET_999", "low")
        _record("dispatcher gpo(NONEXIST) raises error", False,
                "no error raised")
        ok = False
    except Exception:
        _record("dispatcher gpo(NONEXIST) raises error", True)

    # 14e. gpi("NONEXIST") raises error
    try:
        gpi("NONEXIST_GPIO_NET_999")
        _record("dispatcher gpi(NONEXIST) raises error", False,
                "no error raised")
        ok = False
    except Exception:
        _record("dispatcher gpi(NONEXIST) raises error", True)

    # Cleanup
    try:
        gpo(GPIO_OUT_NET, "low")
    except Exception:
        pass

    return ok


# ---------------------------------------------------------------------------
# 15. Module-level Aliases
# ---------------------------------------------------------------------------
def test_module_aliases():
    """Verify module-level gpi/gpo/read/write aliases."""
    print("\n" + "=" * 60)
    print("TEST GROUP 15: Module-level Aliases")
    print("=" * 60)

    ok = True

    try:
        from lager.io.gpio import gpi, gpo, read, write

        # 15a. gpi is read
        passed = gpi is read
        _record("gpi is read", passed)
        if not passed:
            ok = False

        # 15b. gpo is write
        passed = gpo is write
        _record("gpo is write", passed)
        if not passed:
            ok = False

        # 15c. read callable
        passed = callable(read)
        _record("read is callable", passed)
        if not passed:
            ok = False

        # 15d. write callable
        passed = callable(write)
        _record("write is callable", passed)
        if not passed:
            ok = False

    except Exception as e:
        _record("module aliases", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 16. Cache File Behavior
# ---------------------------------------------------------------------------
def test_cache_file():
    """After output, cache file exists and contains valid data."""
    print("\n" + "=" * 60)
    print("TEST GROUP 16: Cache File Behavior")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(GPIO_OUT_NET, type=NetType.GPIO)
    ok = True

    # Write a known value to populate cache
    try:
        gpio.output(1)
    except Exception as e:
        _record("setup: output(1)", False, str(e))
        return False

    # 16a. Cache file exists
    try:
        passed = os.path.exists(FT232H_CACHE_FILE)
        _record("cache file exists", passed,
                f"path={FT232H_CACHE_FILE}")
        if not passed:
            ok = False
    except Exception as e:
        _record("cache file exists", False, str(e))
        ok = False

    # 16b. Valid JSON
    try:
        with open(FT232H_CACHE_FILE, "r") as f:
            data = json.load(f)
        passed = isinstance(data, dict)
        _record("cache file valid JSON", passed,
                f"type={type(data).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("cache file valid JSON", False, str(e))
        ok = False

    # 16c. Contains entry for this pin
    try:
        with open(FT232H_CACHE_FILE, "r") as f:
            data = json.load(f)
        # Key format: "{serial_or_default}:{pin_num}"
        found = False
        for key, entry in data.items():
            if str(gpio._pin_num) in key:
                found = True
                break
        _record("cache contains entry for pin", found,
                f"keys={list(data.keys())}")
        if not found:
            ok = False
    except Exception as e:
        _record("cache contains entry", False, str(e))
        ok = False

    # Cleanup
    gpio.output(0)
    return ok


# ---------------------------------------------------------------------------
# 17. Cleanup
# ---------------------------------------------------------------------------
def test_cleanup():
    """Set output low (motor off)."""
    print("\n" + "=" * 60)
    print("TEST GROUP 17: Cleanup")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(GPIO_OUT_NET, type=NetType.GPIO)

    try:
        gpio.output(0)
        _record("cleanup: output(0) motor off", True)
        return True
    except Exception as e:
        _record("cleanup: output(0)", False, str(e))
        return False


# ===================================================================
# Main
# ===================================================================
def main():
    """Run all tests."""
    print("FT232H GPIO Comprehensive Test Suite")
    print(f"Output net: {GPIO_OUT_NET}")
    print(f"Input net:  {GPIO_IN_NET}")
    print(f"Set GPIO_OUT_NET / GPIO_IN_NET env vars to change")
    print("=" * 60)

    tests = [
        ("1. Imports",                    test_imports),
        ("2. Net.get Factory",            test_net_get_factory),
        ("3. GPIOBase Inheritance",       test_gpio_base),
        ("4. Output Integer Levels",      test_output_int_levels),
        ("5. Output String Levels",       test_output_string_levels),
        ("6. Output Edge Case Types",     test_output_edge_cases),
        ("7. Input Read Behavior",        test_input_read),
        ("8. Pin Parsing Valid",          test_pin_parsing_valid),
        ("9. Pin Parsing Invalid Types",  test_pin_parsing_invalid_types),
        ("10. Serial Parameter",          test_serial_param),
        ("11. Exception Hierarchy",       test_exception_hierarchy),
        ("12. Toggle via Output",         test_toggle_via_output),
        ("13. Loopback",                  test_loopback),
        ("14. Dispatcher Direct",         test_dispatcher_direct),
        ("15. Module-level Aliases",      test_module_aliases),
        ("16. Cache File Behavior",       test_cache_file),
        ("17. Cleanup",                   test_cleanup),
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
