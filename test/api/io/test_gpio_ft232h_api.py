#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
FT232H GPIO Python API edge-case tests using only `from lager import Net, NetType`.

Run with: lager python test/api/io/test_gpio_ft232h_api.py --box <YOUR-BOX>

Hardware:
- FT232H C232HM-DDHSL-0 cable connected to <YOUR-BOX>
- gpio3 = AD6 (white wire)
- gpio4 = AD7 (blue wire) -- used for multi-pin isolation tests
- No external load required (tests exercise the driver, not a motor)

These tests exercise every code path reachable through the public
Net.get() / .output() / .input() interface without importing any
internal lager modules.
"""
import sys
import os
import time
import traceback

# ----- Configuration -----
NET_NAME = os.environ.get("GPIO_NET", "gpio3")
NET_NAME_2 = os.environ.get("GPIO_NET_2", "gpio4")

# ----- Test framework -----
_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def _skip(name, reason=""):
    _results.append((name, True, f"SKIP: {reason}"))
    msg = f"  SKIP: {name}"
    if reason:
        msg += f" -- {reason}"
    print(msg)


# ===================================================================
# 1. Net.get Factory
# ===================================================================
def test_net_get():
    """Verify Net.get returns a working GPIO object."""
    print("\n" + "=" * 60)
    print("TEST GROUP 1: Net.get Factory")
    print("=" * 60)

    from lager import Net, NetType
    ok = True

    # 1a. Basic get succeeds
    try:
        gpio = Net.get(NET_NAME, type=NetType.GPIO)
        passed = gpio is not None
        _record("Net.get returns object", passed, f"type={type(gpio).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("Net.get returns object", False, str(e))
        ok = False

    # 1b. Repeated get returns same type
    try:
        g1 = Net.get(NET_NAME, type=NetType.GPIO)
        g2 = Net.get(NET_NAME, type=NetType.GPIO)
        passed = type(g1) is type(g2)
        _record("repeated get same type", passed,
                f"{type(g1).__name__} vs {type(g2).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("repeated get same type", False, str(e))
        ok = False

    # 1c. Has .output callable
    try:
        gpio = Net.get(NET_NAME, type=NetType.GPIO)
        passed = callable(getattr(gpio, "output", None))
        _record("has .output()", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("has .output()", False, str(e))
        ok = False

    # 1d. Has .input callable
    try:
        gpio = Net.get(NET_NAME, type=NetType.GPIO)
        passed = callable(getattr(gpio, "input", None))
        _record("has .input()", passed)
        if not passed:
            ok = False
    except Exception as e:
        _record("has .input()", False, str(e))
        ok = False

    # 1e. Has .name property
    try:
        gpio = Net.get(NET_NAME, type=NetType.GPIO)
        passed = gpio.name == NET_NAME
        _record(".name == net name", passed, f"name={gpio.name!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record(".name == net name", False, str(e))
        ok = False

    # 1f. Has .pin property (not None)
    try:
        gpio = Net.get(NET_NAME, type=NetType.GPIO)
        passed = gpio.pin is not None
        _record(".pin is not None", passed, f"pin={gpio.pin!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record(".pin is not None", False, str(e))
        ok = False

    # 1g. Invalid net name raises exception
    try:
        Net.get("NONEXISTENT_NET_XYZ_999", type=NetType.GPIO)
        _record("invalid net raises exception", False, "no exception raised")
        ok = False
    except Exception:
        _record("invalid net raises exception", True)

    return ok


# ===================================================================
# 2. Output Integer Levels
# ===================================================================
def test_output_int_levels():
    """Test output() with integer and boolean values."""
    print("\n" + "=" * 60)
    print("TEST GROUP 2: Output Integer Levels")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(NET_NAME, type=NetType.GPIO)
    ok = True

    cases = [
        (1, "output(1)"),
        (0, "output(0)"),
        (True, "output(True)"),
        (False, "output(False)"),
        (-1, "output(-1) [nonzero -> HIGH]"),
        (42, "output(42) [nonzero -> HIGH]"),
        (255, "output(255) [nonzero -> HIGH]"),
    ]

    for level, desc in cases:
        try:
            gpio.output(level)
            _record(desc, True)
        except Exception as e:
            _record(desc, False, str(e))
            ok = False

    # Cleanup
    gpio.output(0)
    return ok


# ===================================================================
# 3. Output String Levels
# ===================================================================
def test_output_string_levels():
    """Test output() with all supported string values and case variants."""
    print("\n" + "=" * 60)
    print("TEST GROUP 3: Output String Levels")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(NET_NAME, type=NetType.GPIO)
    ok = True

    # Standard strings
    standard = ["high", "low", "on", "off", "1", "0", "true", "false"]
    for level in standard:
        try:
            gpio.output(level)
            _record(f"output('{level}')", True)
        except Exception as e:
            _record(f"output('{level}')", False, str(e))
            ok = False

    # Case variations
    case_variants = ["HIGH", "Low", "ON", "oFf", "True", "FALSE"]
    for level in case_variants:
        try:
            gpio.output(level)
            _record(f"output('{level}') [case variant]", True)
        except Exception as e:
            _record(f"output('{level}') [case variant]", False, str(e))
            ok = False

    # Whitespace padding
    padded = [" high ", " 0 ", "  low  "]
    for level in padded:
        try:
            gpio.output(level)
            _record(f"output({level!r}) [whitespace]", True)
        except Exception as e:
            _record(f"output({level!r}) [whitespace]", False, str(e))
            ok = False

    # Cleanup
    gpio.output(0)
    return ok


# ===================================================================
# 4. Output Edge-Case Types
# ===================================================================
def test_output_edge_types():
    """Test output() with unusual or invalid types."""
    print("\n" + "=" * 60)
    print("TEST GROUP 4: Output Edge-Case Types")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(NET_NAME, type=NetType.GPIO)
    ok = True

    # 4a. Float 3.14 -> int(3.14) = 3 -> truthy -> HIGH
    try:
        gpio.output(3.14)
        _record("output(3.14) [float -> HIGH]", True)
    except Exception as e:
        _record("output(3.14)", False, str(e))
        ok = False

    # 4b. Float 0.0 -> int(0.0) = 0 -> LOW
    try:
        gpio.output(0.0)
        _record("output(0.0) [float -> LOW]", True)
    except Exception as e:
        _record("output(0.0)", False, str(e))
        ok = False

    # 4c. None -> should raise (int(None) is TypeError)
    try:
        gpio.output(None)
        _record("output(None) raises error", False, "no error raised")
        ok = False
    except (TypeError, ValueError):
        _record("output(None) raises TypeError/ValueError", True)
    except Exception as e:
        # Any error is acceptable
        _record("output(None) raises error", True,
                f"{type(e).__name__}: {e}")

    # 4d. Unrecognized string -> maps to LOW (not in HIGH set)
    try:
        gpio.output("banana")
        _record("output('banana') -> LOW (no error)", True)
    except Exception as e:
        _record("output('banana')", False, str(e))
        ok = False

    # 4e. Empty string -> maps to LOW
    try:
        gpio.output("")
        _record("output('') -> LOW (no error)", True)
    except Exception as e:
        _record("output('')", False, str(e))
        ok = False

    # 4f. Negative float -> int(-2.5) -> -2 -> truthy -> HIGH
    try:
        gpio.output(-2.5)
        _record("output(-2.5) [neg float -> HIGH]", True)
    except Exception as e:
        _record("output(-2.5)", False, str(e))
        ok = False

    # Cleanup
    gpio.output(0)
    return ok


# ===================================================================
# 5. Output Return Value
# ===================================================================
def test_output_return():
    """Verify output() returns None."""
    print("\n" + "=" * 60)
    print("TEST GROUP 5: Output Return Value")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(NET_NAME, type=NetType.GPIO)
    ok = True

    # 5a. output(1) returns None
    try:
        result = gpio.output(1)
        passed = result is None
        _record("output(1) returns None", passed, f"returned {result!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("output(1) returns None", False, str(e))
        ok = False

    # 5b. output(0) returns None
    try:
        result = gpio.output(0)
        passed = result is None
        _record("output(0) returns None", passed, f"returned {result!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("output(0) returns None", False, str(e))
        ok = False

    # 5c. output("high") returns None
    try:
        result = gpio.output("high")
        passed = result is None
        _record("output('high') returns None", passed, f"returned {result!r}")
        if not passed:
            ok = False
    except Exception as e:
        _record("output('high') returns None", False, str(e))
        ok = False

    # Cleanup
    gpio.output(0)
    return ok


# ===================================================================
# 6. Input Read Behavior
# ===================================================================
def test_input_read():
    """Test input() return type and value range."""
    print("\n" + "=" * 60)
    print("TEST GROUP 6: Input Read Behavior")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(NET_NAME, type=NetType.GPIO)
    ok = True

    # 6a. Returns 0 or 1
    try:
        value = gpio.input()
        passed = value in (0, 1)
        _record("input() returns 0 or 1", passed, f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("input() returns 0 or 1", False, str(e))
        ok = False

    # 6b. Returns int type (not bool)
    try:
        value = gpio.input()
        passed = isinstance(value, int)
        _record("input() returns int", passed, f"type={type(value).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("input() returns int", False, str(e))
        ok = False

    # 6c. Strict type is int (not bool subclass)
    try:
        value = gpio.input()
        passed = type(value) is int
        _record("type(input()) is int, not bool", passed,
                f"type={type(value).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("type is int not bool", False, str(e))
        ok = False

    # 6d. Takes no arguments
    try:
        value = gpio.input()
        _record("input() takes no args", True)
    except TypeError as e:
        _record("input() takes no args", False, str(e))
        ok = False

    # 6e. 5 consecutive reads all succeed
    try:
        vals = [gpio.input() for _ in range(5)]
        passed = all(v in (0, 1) for v in vals)
        _record("5 consecutive reads", passed, f"values={vals}")
        if not passed:
            ok = False
    except Exception as e:
        _record("5 consecutive reads", False, str(e))
        ok = False

    return ok


# ===================================================================
# 7. Output-then-Input Readback (Cache Verification)
# ===================================================================
def test_output_input_readback():
    """Set output, then read back via input() -- verifies cache behavior."""
    print("\n" + "=" * 60)
    print("TEST GROUP 7: Output-then-Input Readback")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(NET_NAME, type=NetType.GPIO)
    ok = True

    # 7a. Set HIGH, read back 1
    try:
        gpio.output(1)
        value = gpio.input()
        passed = value == 1
        _record("output(1) -> input() == 1", passed, f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("output(1) -> input()", False, str(e))
        ok = False

    # 7b. Set LOW, read back 0
    try:
        gpio.output(0)
        value = gpio.input()
        passed = value == 0
        _record("output(0) -> input() == 0", passed, f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("output(0) -> input()", False, str(e))
        ok = False

    # 7c. String "high" -> read back 1
    try:
        gpio.output("high")
        value = gpio.input()
        passed = value == 1
        _record("output('high') -> input() == 1", passed, f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("output('high') -> input()", False, str(e))
        ok = False

    # 7d. String "low" -> read back 0
    try:
        gpio.output("low")
        value = gpio.input()
        passed = value == 0
        _record("output('low') -> input() == 0", passed, f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("output('low') -> input()", False, str(e))
        ok = False

    # 7e. Alternating readback sequence
    try:
        errors = 0
        for expected in [1, 0, 1, 0, 1]:
            gpio.output(expected)
            actual = gpio.input()
            if actual != expected:
                errors += 1
        passed = errors == 0
        _record("alternating readback (1,0,1,0,1)", passed,
                f"errors={errors}")
        if not passed:
            ok = False
    except Exception as e:
        _record("alternating readback", False, str(e))
        ok = False

    # Cleanup
    gpio.output(0)
    return ok


# ===================================================================
# 8. State Persistence Across Net.get Calls
# ===================================================================
def test_state_persistence():
    """Verify output state persists when re-fetching the net."""
    print("\n" + "=" * 60)
    print("TEST GROUP 8: State Persistence Across Net.get Calls")
    print("=" * 60)

    from lager import Net, NetType
    ok = True

    # 8a. Set HIGH on first instance, read from second instance
    try:
        gpio1 = Net.get(NET_NAME, type=NetType.GPIO)
        gpio1.output(1)

        gpio2 = Net.get(NET_NAME, type=NetType.GPIO)
        value = gpio2.input()
        passed = value == 1
        _record("set HIGH, re-get, read == 1", passed, f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("persistence HIGH", False, str(e))
        ok = False

    # 8b. Set LOW on first instance, read from second instance
    try:
        gpio1 = Net.get(NET_NAME, type=NetType.GPIO)
        gpio1.output(0)

        gpio2 = Net.get(NET_NAME, type=NetType.GPIO)
        value = gpio2.input()
        passed = value == 0
        _record("set LOW, re-get, read == 0", passed, f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("persistence LOW", False, str(e))
        ok = False

    # Cleanup
    gpio1.output(0)
    return ok


# ===================================================================
# 9. Idempotent Output
# ===================================================================
def test_idempotent_output():
    """Setting the same level multiple times should not error."""
    print("\n" + "=" * 60)
    print("TEST GROUP 9: Idempotent Output")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(NET_NAME, type=NetType.GPIO)
    ok = True

    # 9a. Set HIGH 5 times
    try:
        for _ in range(5):
            gpio.output(1)
        value = gpio.input()
        passed = value == 1
        _record("5x output(1), still HIGH", passed, f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("5x output(1)", False, str(e))
        ok = False

    # 9b. Set LOW 5 times
    try:
        for _ in range(5):
            gpio.output(0)
        value = gpio.input()
        passed = value == 0
        _record("5x output(0), still LOW", passed, f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("5x output(0)", False, str(e))
        ok = False

    # 9c. Mixed idempotent: "high" then 1 then "on" then True
    try:
        gpio.output("high")
        gpio.output(1)
        gpio.output("on")
        gpio.output(True)
        value = gpio.input()
        passed = value == 1
        _record("'high'/1/'on'/True all -> HIGH", passed, f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("mixed HIGH idempotent", False, str(e))
        ok = False

    # 9d. Mixed idempotent LOW: "low" then 0 then "off" then False
    try:
        gpio.output("low")
        gpio.output(0)
        gpio.output("off")
        gpio.output(False)
        value = gpio.input()
        passed = value == 0
        _record("'low'/0/'off'/False all -> LOW", passed, f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record("mixed LOW idempotent", False, str(e))
        ok = False

    # Cleanup
    gpio.output(0)
    return ok


# ===================================================================
# 10. Rapid Toggle Stress
# ===================================================================
def test_rapid_toggle():
    """Rapidly toggle output to stress USB communication."""
    print("\n" + "=" * 60)
    print("TEST GROUP 10: Rapid Toggle Stress")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(NET_NAME, type=NetType.GPIO)
    ok = True

    # 10a. 10 cycles
    try:
        for _ in range(10):
            gpio.output(1)
            gpio.output(0)
        _record("10 rapid cycles", True)
    except Exception as e:
        _record("10 rapid cycles", False, str(e))
        ok = False

    # 10b. 50 cycles
    try:
        for _ in range(50):
            gpio.output(1)
            gpio.output(0)
        _record("50 rapid cycles", True)
    except Exception as e:
        _record("50 rapid cycles", False, str(e))
        ok = False

    # 10c. 100 cycles (stress)
    try:
        t0 = time.monotonic()
        for _ in range(100):
            gpio.output(1)
            gpio.output(0)
        elapsed = time.monotonic() - t0
        _record("100 rapid cycles", True, f"elapsed={elapsed:.3f}s")
    except Exception as e:
        _record("100 rapid cycles", False, str(e))
        ok = False

    # 10d. Verify pin is still responsive after stress
    try:
        gpio.output(1)
        v1 = gpio.input()
        gpio.output(0)
        v0 = gpio.input()
        passed = v1 == 1 and v0 == 0
        _record("responsive after stress", passed,
                f"after_high={v1}, after_low={v0}")
        if not passed:
            ok = False
    except Exception as e:
        _record("responsive after stress", False, str(e))
        ok = False

    # Cleanup
    gpio.output(0)
    return ok


# ===================================================================
# 11. Interleaved Output/Input
# ===================================================================
def test_interleaved():
    """Interleave output and input calls in various patterns."""
    print("\n" + "=" * 60)
    print("TEST GROUP 11: Interleaved Output/Input")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(NET_NAME, type=NetType.GPIO)
    ok = True

    # 11a. output-input-output-input
    try:
        gpio.output(1)
        v1 = gpio.input()
        gpio.output(0)
        v0 = gpio.input()
        passed = v1 == 1 and v0 == 0
        _record("output-input-output-input", passed,
                f"v1={v1}, v0={v0}")
        if not passed:
            ok = False
    except Exception as e:
        _record("output-input-output-input", False, str(e))
        ok = False

    # 11b. Multiple reads between outputs
    try:
        gpio.output(1)
        vals_high = [gpio.input() for _ in range(3)]
        gpio.output(0)
        vals_low = [gpio.input() for _ in range(3)]
        passed = all(v == 1 for v in vals_high) and all(v == 0 for v in vals_low)
        _record("3 reads between outputs", passed,
                f"high={vals_high}, low={vals_low}")
        if not passed:
            ok = False
    except Exception as e:
        _record("3 reads between outputs", False, str(e))
        ok = False

    # 11c. Read-output-read (verify read before first output)
    try:
        gpio.output(0)  # known state
        v_before = gpio.input()
        gpio.output(1)
        v_after = gpio.input()
        passed = v_before == 0 and v_after == 1
        _record("read-output-read transition", passed,
                f"before={v_before}, after={v_after}")
        if not passed:
            ok = False
    except Exception as e:
        _record("read-output-read", False, str(e))
        ok = False

    # Cleanup
    gpio.output(0)
    return ok


# ===================================================================
# 12. Multi-Pin Isolation
# ===================================================================
def test_multi_pin_isolation():
    """Operations on one pin should not affect another."""
    print("\n" + "=" * 60)
    print("TEST GROUP 12: Multi-Pin Isolation")
    print("=" * 60)

    from lager import Net, NetType
    ok = True

    try:
        gpio3 = Net.get(NET_NAME, type=NetType.GPIO)
        gpio4 = Net.get(NET_NAME_2, type=NetType.GPIO)
    except Exception as e:
        _skip("multi-pin isolation",
              f"could not get both nets: {e}")
        return True

    # 12a. Set gpio3 HIGH, gpio4 LOW, verify both
    try:
        gpio3.output(1)
        gpio4.output(0)
        v3 = gpio3.input()
        v4 = gpio4.input()
        passed = v3 == 1 and v4 == 0
        _record(f"{NET_NAME}=HIGH, {NET_NAME_2}=LOW", passed,
                f"{NET_NAME}={v3}, {NET_NAME_2}={v4}")
        if not passed:
            ok = False
    except Exception as e:
        _record("set independent levels", False, str(e))
        ok = False

    # 12b. Set gpio3 LOW, gpio4 HIGH, verify both
    try:
        gpio3.output(0)
        gpio4.output(1)
        v3 = gpio3.input()
        v4 = gpio4.input()
        passed = v3 == 0 and v4 == 1
        _record(f"{NET_NAME}=LOW, {NET_NAME_2}=HIGH", passed,
                f"{NET_NAME}={v3}, {NET_NAME_2}={v4}")
        if not passed:
            ok = False
    except Exception as e:
        _record("swap independent levels", False, str(e))
        ok = False

    # 12c. Toggle gpio3, verify gpio4 unchanged
    try:
        gpio4.output(1)
        gpio3.output(0)
        gpio3.output(1)
        gpio3.output(0)
        v4 = gpio4.input()
        passed = v4 == 1
        _record(f"toggle {NET_NAME}, {NET_NAME_2} unchanged", passed,
                f"{NET_NAME_2}={v4}")
        if not passed:
            ok = False
    except Exception as e:
        _record("toggle isolation", False, str(e))
        ok = False

    # 12d. Both HIGH simultaneously
    try:
        gpio3.output(1)
        gpio4.output(1)
        v3 = gpio3.input()
        v4 = gpio4.input()
        passed = v3 == 1 and v4 == 1
        _record("both HIGH", passed,
                f"{NET_NAME}={v3}, {NET_NAME_2}={v4}")
        if not passed:
            ok = False
    except Exception as e:
        _record("both HIGH", False, str(e))
        ok = False

    # 12e. Both LOW simultaneously
    try:
        gpio3.output(0)
        gpio4.output(0)
        v3 = gpio3.input()
        v4 = gpio4.input()
        passed = v3 == 0 and v4 == 0
        _record("both LOW", passed,
                f"{NET_NAME}={v3}, {NET_NAME_2}={v4}")
        if not passed:
            ok = False
    except Exception as e:
        _record("both LOW", False, str(e))
        ok = False

    # Cleanup
    gpio3.output(0)
    gpio4.output(0)
    return ok


# ===================================================================
# 13. wait_for_level
# ===================================================================
def test_wait_for_level():
    """Test the wait_for_level polling method via Net API."""
    print("\n" + "=" * 60)
    print("TEST GROUP 13: wait_for_level")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(NET_NAME, type=NetType.GPIO)
    ok = True

    # 13a. Set HIGH, wait_for_level(1) returns immediately
    try:
        gpio.output(1)
        t0 = time.monotonic()
        elapsed = gpio.wait_for_level(1, timeout=2.0)
        wall = time.monotonic() - t0
        passed = elapsed < 0.5 and wall < 0.5
        _record("wait_for_level(1) immediate", passed,
                f"elapsed={elapsed:.4f}s, wall={wall:.4f}s")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level(1) immediate", False, str(e))
        ok = False

    # 13b. Set LOW, wait_for_level(0) returns immediately
    try:
        gpio.output(0)
        t0 = time.monotonic()
        elapsed = gpio.wait_for_level(0, timeout=2.0)
        wall = time.monotonic() - t0
        passed = elapsed < 0.5 and wall < 0.5
        _record("wait_for_level(0) immediate", passed,
                f"elapsed={elapsed:.4f}s, wall={wall:.4f}s")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level(0) immediate", False, str(e))
        ok = False

    # 13c. Set HIGH, wait_for_level(0) with short timeout -> TimeoutError
    try:
        gpio.output(1)
        try:
            gpio.wait_for_level(0, timeout=0.5)
            _record("wait_for_level timeout raises TimeoutError", False,
                    "no exception raised")
            ok = False
        except TimeoutError:
            _record("wait_for_level timeout raises TimeoutError", True)
    except Exception as e:
        _record("wait_for_level timeout", False, str(e))
        ok = False

    # 13d. Return value is a float
    try:
        gpio.output(1)
        elapsed = gpio.wait_for_level(1, timeout=2.0)
        passed = isinstance(elapsed, float)
        _record("wait_for_level returns float", passed,
                f"type={type(elapsed).__name__}")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level returns float", False, str(e))
        ok = False

    # 13e. Return value is non-negative
    try:
        gpio.output(0)
        elapsed = gpio.wait_for_level(0, timeout=2.0)
        passed = elapsed >= 0.0
        _record("wait_for_level returns >= 0", passed,
                f"elapsed={elapsed:.4f}s")
        if not passed:
            ok = False
    except Exception as e:
        _record("wait_for_level >= 0", False, str(e))
        ok = False

    # Cleanup
    gpio.output(0)
    return ok


# ===================================================================
# 14. Timing / Throughput
# ===================================================================
def test_timing():
    """Measure output and input timing to catch regressions."""
    print("\n" + "=" * 60)
    print("TEST GROUP 14: Timing / Throughput")
    print("=" * 60)

    from lager import Net, NetType
    gpio = Net.get(NET_NAME, type=NetType.GPIO)
    ok = True

    # 14a. Single output latency
    try:
        t0 = time.monotonic()
        gpio.output(1)
        latency = time.monotonic() - t0
        # Expect < 500ms for a single USB GPIO write
        passed = latency < 0.5
        _record("single output latency", passed,
                f"{latency*1000:.1f}ms")
        if not passed:
            ok = False
    except Exception as e:
        _record("single output latency", False, str(e))
        ok = False

    # 14b. Single input latency
    try:
        t0 = time.monotonic()
        gpio.input()
        latency = time.monotonic() - t0
        passed = latency < 0.5
        _record("single input latency", passed,
                f"{latency*1000:.1f}ms")
        if not passed:
            ok = False
    except Exception as e:
        _record("single input latency", False, str(e))
        ok = False

    # 14c. 20 output calls throughput
    try:
        t0 = time.monotonic()
        for i in range(20):
            gpio.output(i % 2)
        elapsed = time.monotonic() - t0
        rate = 20 / elapsed if elapsed > 0 else float('inf')
        passed = elapsed < 10.0  # 20 calls in < 10s
        _record("20 outputs throughput", passed,
                f"{elapsed:.3f}s ({rate:.1f} ops/s)")
        if not passed:
            ok = False
    except Exception as e:
        _record("20 outputs throughput", False, str(e))
        ok = False

    # 14d. 20 input calls throughput
    try:
        t0 = time.monotonic()
        for _ in range(20):
            gpio.input()
        elapsed = time.monotonic() - t0
        rate = 20 / elapsed if elapsed > 0 else float('inf')
        passed = elapsed < 10.0
        _record("20 inputs throughput", passed,
                f"{elapsed:.3f}s ({rate:.1f} ops/s)")
        if not passed:
            ok = False
    except Exception as e:
        _record("20 inputs throughput", False, str(e))
        ok = False

    # Cleanup
    gpio.output(0)
    return ok


# ===================================================================
# 15. Physical Verification (Timed Holds)
# ===================================================================
def test_physical_hold():
    """Hold output HIGH for visible/audible confirmation of physical actuation.

    Connect a QYF-740 vibrating motor module (or LED) to the test pin
    to physically verify the GPIO is driving the load.
    """
    print("\n" + "=" * 60)
    print("TEST GROUP 15: Physical Verification (Timed Holds)")
    print("=" * 60)
    print("  -- Watch/listen for physical actuation on the connected load --")

    from lager import Net, NetType
    gpio = Net.get(NET_NAME, type=NetType.GPIO)
    ok = True

    # 15a. Sustained HIGH for 5 seconds
    try:
        print(f"  >>> {NET_NAME} HIGH for 5s -- motor/LED should be ON")
        sys.stdout.flush()
        gpio.output(1)
        v = gpio.input()
        time.sleep(5)
        v_after = gpio.input()
        gpio.output(0)
        passed = v == 1 and v_after == 1
        _record("5s sustained HIGH", passed,
                f"start={v}, after_5s={v_after}")
        if not passed:
            ok = False
    except Exception as e:
        _record("5s sustained HIGH", False, str(e))
        gpio.output(0)
        ok = False

    time.sleep(1)  # pause between tests

    # 15b. Sustained LOW for 5 seconds (confirm motor/LED is OFF)
    try:
        print(f"  >>> {NET_NAME} LOW for 5s -- motor/LED should be OFF")
        sys.stdout.flush()
        gpio.output(0)
        v = gpio.input()
        time.sleep(5)
        v_after = gpio.input()
        passed = v == 0 and v_after == 0
        _record("5s sustained LOW", passed,
                f"start={v}, after_5s={v_after}")
        if not passed:
            ok = False
    except Exception as e:
        _record("5s sustained LOW", False, str(e))
        ok = False

    time.sleep(1)

    # 15c. On-off-on-off pattern (2s on, 2s off, 2s on, 2s off)
    try:
        print(f"  >>> {NET_NAME} pattern: 2s ON, 2s OFF, 2s ON, 2s OFF")
        sys.stdout.flush()
        pattern_ok = True
        for i, (level, label) in enumerate([
            (1, "ON"), (0, "OFF"), (1, "ON"), (0, "OFF")
        ]):
            gpio.output(level)
            v = gpio.input()
            if v != level:
                pattern_ok = False
            time.sleep(2)
        gpio.output(0)
        _record("on-off-on-off pattern (2s each)", pattern_ok)
        if not pattern_ok:
            ok = False
    except Exception as e:
        _record("on-off-on-off pattern", False, str(e))
        gpio.output(0)
        ok = False

    time.sleep(1)

    # 15d. Rapid blink pattern (0.5s on, 0.5s off x5 = 5s total)
    try:
        print(f"  >>> {NET_NAME} rapid blink: 0.5s ON/OFF x5")
        sys.stdout.flush()
        blink_ok = True
        for _ in range(5):
            gpio.output(1)
            if gpio.input() != 1:
                blink_ok = False
            time.sleep(0.5)
            gpio.output(0)
            if gpio.input() != 0:
                blink_ok = False
            time.sleep(0.5)
        _record("rapid blink 0.5s x5", blink_ok)
        if not blink_ok:
            ok = False
    except Exception as e:
        _record("rapid blink 0.5s x5", False, str(e))
        gpio.output(0)
        ok = False

    # Cleanup
    gpio.output(0)
    return ok


# ===================================================================
# 16. Cleanup
# ===================================================================
def test_cleanup():
    """Leave all pins LOW."""
    print("\n" + "=" * 60)
    print("TEST GROUP 16: Cleanup")
    print("=" * 60)

    from lager import Net, NetType
    ok = True

    # gpio3 LOW
    try:
        gpio = Net.get(NET_NAME, type=NetType.GPIO)
        gpio.output(0)
        value = gpio.input()
        passed = value == 0
        _record(f"{NET_NAME} -> LOW", passed, f"value={value}")
        if not passed:
            ok = False
    except Exception as e:
        _record(f"{NET_NAME} -> LOW", False, str(e))
        ok = False

    # gpio4 LOW (best effort)
    try:
        gpio4 = Net.get(NET_NAME_2, type=NetType.GPIO)
        gpio4.output(0)
        _record(f"{NET_NAME_2} -> LOW", True)
    except Exception:
        _skip(f"{NET_NAME_2} -> LOW", "net not available")

    return ok


# ===================================================================
# Main
# ===================================================================
def main():
    print("FT232H GPIO API Edge-Case Test Suite")
    print(f"Primary net:   {NET_NAME}")
    print(f"Secondary net: {NET_NAME_2}")
    print(f"Set GPIO_NET / GPIO_NET_2 env vars to change")
    print("=" * 60)

    tests = [
        ("1.  Net.get Factory",               test_net_get),
        ("2.  Output Integer Levels",          test_output_int_levels),
        ("3.  Output String Levels",           test_output_string_levels),
        ("4.  Output Edge-Case Types",         test_output_edge_types),
        ("5.  Output Return Value",            test_output_return),
        ("6.  Input Read Behavior",            test_input_read),
        ("7.  Output-then-Input Readback",     test_output_input_readback),
        ("8.  State Persistence (re-get)",     test_state_persistence),
        ("9.  Idempotent Output",              test_idempotent_output),
        ("10. Rapid Toggle Stress",            test_rapid_toggle),
        ("11. Interleaved Output/Input",       test_interleaved),
        ("12. Multi-Pin Isolation",            test_multi_pin_isolation),
        ("13. wait_for_level",                 test_wait_for_level),
        ("14. Timing / Throughput",            test_timing),
        ("15. Physical Verification (Holds)",  test_physical_hold),
        ("16. Cleanup",                        test_cleanup),
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
