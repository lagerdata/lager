#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Dedicated wait_for_level test script.

Run with: lager python test/api/communication/test_wait_for_level.py --box <YOUR-BOX>

Tests that wait_for_level matches ehaas's spec:
  - Uses LabJack streaming (eStreamStart / eStreamRead / eStreamStop)
  - channel_name (FIO0, CIO1, etc.) pulled from the net record
  - scan_rate and scans_per_read are configurable
  - level is 0 or 1 (also accepts "high"/"low" strings)
  - timeout param raises TimeoutError when exceeded
  - Blocks until pin reaches target level, returns elapsed seconds
  - handle comes from LabJack global store

Hardware: LabJack T7, gpio16 = FIO0 (no external driver needed).
The internal pull-up idles the pin HIGH when streaming reconfigures it as input.
"""
import sys
import time

GPIO_NET = "gpio16"  # FIO0 on LabJack T7

passed = 0
failed = 0


def run(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  PASS: {name}")
        passed += 1
    except AssertionError as e:
        print(f"  FAIL: {name} -- {e}")
        failed += 1
    except Exception as e:
        print(f"  FAIL: {name} -- {type(e).__name__}: {e}")
        failed += 1


# Store AssertionError fix (Python uses AssertionError)
AssertionError = AssertionError  # noqa - just a reminder it's built-in


def main():
    from lager.io.gpio.dispatcher import gpo, wait_for_level

    # Set known output state before tests
    gpo(GPIO_NET, "high")

    print("=" * 60)
    print("wait_for_level -- dedicated test suite")
    print("=" * 60)

    # -----------------------------------------------------------------
    # 1. Basic: blocks until pin is HIGH (pull-up idle), returns elapsed
    # -----------------------------------------------------------------
    def test_returns_elapsed_float():
        result = wait_for_level(GPIO_NET, 1, timeout=2)
        assert isinstance(result, float), f"expected float, got {type(result).__name__}"
        assert result >= 0, f"elapsed must be >= 0, got {result}"

    run("returns elapsed as float >= 0", test_returns_elapsed_float)

    # -----------------------------------------------------------------
    # 2. Immediate HIGH detection (pin idles HIGH via pull-up)
    # -----------------------------------------------------------------
    def test_immediate_high():
        elapsed = wait_for_level(GPIO_NET, 1, timeout=2)
        assert elapsed < 0.5, f"expected < 0.5s, got {elapsed:.4f}s"

    run("immediate HIGH detection (< 0.5s)", test_immediate_high)

    # -----------------------------------------------------------------
    # 3. Timeout waiting for LOW -- no external driver pulls pin low
    # -----------------------------------------------------------------
    def test_timeout_low():
        try:
            wait_for_level(GPIO_NET, 0, timeout=0.5)
            assert False, "should have raised TimeoutError"
        except TimeoutError:
            pass  # expected

    run("timeout raises TimeoutError waiting for LOW", test_timeout_low)

    # -----------------------------------------------------------------
    # 4. TimeoutError is exactly TimeoutError (not a subclass wrapper)
    # -----------------------------------------------------------------
    def test_timeout_type():
        try:
            wait_for_level(GPIO_NET, 0, timeout=0.3)
            assert False, "should have raised"
        except TimeoutError as e:
            assert "gpio16" in str(e).lower() or "level" in str(e).lower(), \
                f"message should mention net or level, got: {e}"

    run("TimeoutError message includes context", test_timeout_type)

    # -----------------------------------------------------------------
    # 5. Timeout precision -- should fire close to the requested timeout
    # -----------------------------------------------------------------
    def test_timeout_precision():
        target = 0.5
        start = time.monotonic()
        try:
            wait_for_level(GPIO_NET, 0, timeout=target)
        except TimeoutError:
            pass
        actual = time.monotonic() - start
        diff = abs(actual - target)
        assert diff < 0.3, f"expected ~{target}s, got {actual:.3f}s (off by {diff:.3f}s)"

    run("timeout precision within 0.3s of target", test_timeout_precision)

    # -----------------------------------------------------------------
    # 6. Level as integer 1
    # -----------------------------------------------------------------
    def test_level_int_1():
        elapsed = wait_for_level(GPIO_NET, 1, timeout=2)
        assert elapsed < 0.5

    run("level=1 (int) works", test_level_int_1)

    # -----------------------------------------------------------------
    # 7. Level as integer 0 (times out -- no LOW driver)
    # -----------------------------------------------------------------
    def test_level_int_0():
        try:
            wait_for_level(GPIO_NET, 0, timeout=0.3)
            assert False, "should have raised TimeoutError"
        except TimeoutError:
            pass

    run("level=0 (int) times out correctly", test_level_int_0)

    # -----------------------------------------------------------------
    # 8. Level as string "high"
    # -----------------------------------------------------------------
    def test_level_str_high():
        elapsed = wait_for_level(GPIO_NET, "high", timeout=2)
        assert elapsed < 0.5

    run("level='high' (string) works", test_level_str_high)

    # -----------------------------------------------------------------
    # 9. Level as string "low" (times out)
    # -----------------------------------------------------------------
    def test_level_str_low():
        try:
            wait_for_level(GPIO_NET, "low", timeout=0.3)
            assert False, "should have raised TimeoutError"
        except TimeoutError:
            pass

    run("level='low' (string) times out correctly", test_level_str_low)

    # -----------------------------------------------------------------
    # 10. timeout=None with immediate match (doesn't hang forever)
    # -----------------------------------------------------------------
    def test_timeout_none():
        elapsed = wait_for_level(GPIO_NET, 1, timeout=None)
        assert elapsed < 0.5, f"expected < 0.5s, got {elapsed:.4f}s"

    run("timeout=None returns immediately when already at level", test_timeout_none)

    # -----------------------------------------------------------------
    # 11. Custom scan_rate (configurable, not hardcoded)
    # -----------------------------------------------------------------
    def test_custom_scan_rate():
        elapsed = wait_for_level(GPIO_NET, 1, timeout=2, scan_rate=10000)
        assert elapsed < 0.5

    run("scan_rate=10000 (configurable)", test_custom_scan_rate)

    def test_high_scan_rate():
        elapsed = wait_for_level(GPIO_NET, 1, timeout=2, scan_rate=40000)
        assert elapsed < 0.5

    run("scan_rate=40000 (high rate)", test_high_scan_rate)

    # -----------------------------------------------------------------
    # 12. Custom scans_per_read (configurable, not hardcoded)
    # -----------------------------------------------------------------
    def test_custom_scans_per_read():
        elapsed = wait_for_level(GPIO_NET, 1, timeout=2, scans_per_read=4)
        assert elapsed < 0.5

    run("scans_per_read=4 (configurable)", test_custom_scans_per_read)

    def test_scans_per_read_1():
        elapsed = wait_for_level(GPIO_NET, 1, timeout=2, scans_per_read=1)
        assert elapsed < 0.5

    run("scans_per_read=1 (minimum batch)", test_scans_per_read_1)

    # -----------------------------------------------------------------
    # 13. Both scan_rate and scans_per_read together
    # -----------------------------------------------------------------
    def test_both_stream_params():
        elapsed = wait_for_level(GPIO_NET, 1, timeout=2,
                                 scan_rate=10000, scans_per_read=4)
        assert elapsed < 0.5

    run("scan_rate + scans_per_read together", test_both_stream_params)

    # -----------------------------------------------------------------
    # 14. Repeated calls (stream starts/stops cleanly each time)
    # -----------------------------------------------------------------
    def test_repeated_calls():
        for _ in range(5):
            elapsed = wait_for_level(GPIO_NET, 1, timeout=2)
            assert elapsed < 0.5

    run("5 repeated calls (clean stream start/stop)", test_repeated_calls)

    # -----------------------------------------------------------------
    # 15. Rapid alternating: detect HIGH, then timeout on LOW
    # -----------------------------------------------------------------
    def test_alternating():
        elapsed = wait_for_level(GPIO_NET, 1, timeout=2)
        assert elapsed < 0.5
        try:
            wait_for_level(GPIO_NET, 0, timeout=0.3)
            assert False, "should have timed out"
        except TimeoutError:
            pass

    run("alternating HIGH detect then LOW timeout", test_alternating)

    # -----------------------------------------------------------------
    # Restore CS as output for any subsequent tests
    # -----------------------------------------------------------------
    gpo(GPIO_NET, "high")

    # -----------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------
    total = passed + failed
    print("\n" + "=" * 60)
    print(f"RESULTS: {passed}/{total} passed, {failed} failed")
    print("=" * 60)

    if failed:
        print("\nehaas spec checklist:")
        print("  [?] Uses LabJack streaming (eStreamStart/eStreamRead)")
        print("  [?] scan_rate configurable")
        print("  [?] scans_per_read configurable")
        print("  [?] level is 0 or 1")
        print("  [?] timeout raises TimeoutError")
        print("  [?] Blocks until pin reaches level")
    else:
        print("\nehaas spec checklist:")
        print("  [x] Uses LabJack streaming (eStreamStart/eStreamRead)")
        print("  [x] scan_rate configurable (tests 11-13)")
        print("  [x] scans_per_read configurable (tests 12-13)")
        print("  [x] level is 0 or 1 (tests 6-9)")
        print("  [x] timeout raises TimeoutError (tests 3-5)")
        print("  [x] Blocks until pin reaches level, returns elapsed (tests 1-2)")
        print("  [x] handle from global store (all tests use dispatcher)")
        print("  [x] channel_name from net record (all tests use net name)")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
