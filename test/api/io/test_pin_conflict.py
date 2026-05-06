# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Test: Runtime pin conflict warnings for LabJack drivers.

Verifies that the PinRegistry singleton emits warnings to stderr when
multiple LabJack subsystems (SPI, I2C, GPIO) claim the same physical pin
within a single process.

Run on your box:
    lager python test/api/io/test_pin_conflict.py --box <YOUR-BOX>

Expected output (stderr warnings interleaved with stdout):
    Test 1: SPI spi2 (FIO0-FIO3) + GPIO gpio16 (FIO0) -- expect WARNING
    WARNING: Pin FIO0 is already claimed by SPI (CS). Now being used by GPIO (GPIO).
      Using the same physical pin for different functions in one script may cause unexpected behavior.
    PASS: Warning appeared for FIO0 conflict

    Test 2: I2C i2c2 (FIO4-FIO5) + GPIO gpio20 (FIO4) -- expect WARNING
    WARNING: Pin FIO4 is already claimed by I2C (SDA). Now being used by GPIO (GPIO).
      Using the same physical pin for different functions in one script may cause unexpected behavior.
    PASS: Warning appeared for FIO4 conflict

    Test 3: SPI spi2 cs_mode=manual + GPIO gpio5 (EIO0) -- expect NO warning
    PASS: No spurious warning for non-overlapping pins
"""
import sys
import io

from lager import Net, NetType

_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def main():
    # Capture stderr so we can inspect warnings programmatically
    original_stderr = sys.stderr

    # ------------------------------------------------------------------
    # Test 1: SPI on FIO0-FIO3, then GPIO on FIO0  -->  expect warning
    #
    # Net.get for SPI returns an SPINet wrapper; the underlying LabJackSPI
    # driver (where pin registration happens) is created lazily when we
    # call config().  Net.get for GPIO returns LabJackGPIO directly, so
    # pin registration is immediate.
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("TEST: SPI + GPIO pin conflict (FIO0)")
    print("=" * 60)

    original_stderr.write(
        "Test 1: SPI spi2 (FIO0-FIO3) + GPIO gpio16 (FIO0) -- expect WARNING\n"
    )
    original_stderr.flush()

    captured = io.StringIO()
    sys.stderr = captured  # start capturing

    spi2 = Net.get("spi2", NetType.SPI)
    spi2.config()   # forces driver creation -> registers FIO0-FIO3 as SPI

    gpio16 = Net.get("gpio16", NetType.GPIO)  # registers FIO0 as GPIO -> WARNING

    sys.stderr = original_stderr  # stop capturing
    warnings_1 = captured.getvalue()
    sys.stderr.write(warnings_1)  # replay so user sees them
    sys.stderr.flush()

    has_conflict = "FIO0" in warnings_1 and "SPI" in warnings_1 and "GPIO" in warnings_1
    _record("SPI+GPIO FIO0 conflict warning", has_conflict,
            f"warnings: {warnings_1.strip()!r}" if not has_conflict else "warning detected")

    # ------------------------------------------------------------------
    # Test 2: I2C on FIO4-FIO5, then GPIO on FIO4  -->  expect warning
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("TEST: I2C + GPIO pin conflict (FIO4)")
    print("=" * 60)

    original_stderr.write(
        "Test 2: I2C i2c2 (FIO4-FIO5) + GPIO gpio20 (FIO4) -- expect WARNING\n"
    )
    original_stderr.flush()

    captured = io.StringIO()
    sys.stderr = captured

    i2c2 = Net.get("i2c2", NetType.I2C)
    i2c2.config(frequency_hz=100_000)  # forces driver creation -> registers FIO4-FIO5 as I2C

    gpio20 = Net.get("gpio20", NetType.GPIO)  # registers FIO4 as GPIO -> WARNING

    sys.stderr = original_stderr
    warnings_2 = captured.getvalue()
    sys.stderr.write(warnings_2)
    sys.stderr.flush()

    has_conflict = "FIO4" in warnings_2 and "I2C" in warnings_2 and "GPIO" in warnings_2
    _record("I2C+GPIO FIO4 conflict warning", has_conflict,
            f"warnings: {warnings_2.strip()!r}" if not has_conflict else "warning detected")

    # ------------------------------------------------------------------
    # Test 3: SPI on FIO0-FIO3 (manual CS) + GPIO on EIO0  -->  no warning
    #
    # spi2 with cs_mode=manual does NOT claim FIO0 as CS, so FIO0-FIO3
    # only uses FIO1-FIO3 (already claimed by SPI from test 1 -- same
    # subsystem, no conflict).  gpio5 uses EIO0 which is a fresh pin.
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("TEST: Non-overlapping pins (no conflict)")
    print("=" * 60)

    original_stderr.write(
        "Test 3: SPI spi2 cs_mode=manual + GPIO gpio5 (EIO0) -- expect NO warning\n"
    )
    original_stderr.flush()

    captured = io.StringIO()
    sys.stderr = captured

    spi2_manual = Net.get("spi2", NetType.SPI)
    spi2_manual.config(cs_mode="manual")  # forces driver creation -> registers FIO1-FIO3 only (no CS pin)

    gpio5 = Net.get("gpio5", NetType.GPIO)  # registers EIO0 as GPIO (no overlap)

    sys.stderr = original_stderr
    warnings_3 = captured.getvalue()
    sys.stderr.write(warnings_3)
    sys.stderr.flush()

    no_spurious = "WARNING" not in warnings_3
    _record("no spurious warning for non-overlapping pins", no_spurious,
            f"unexpected: {warnings_3.strip()!r}" if not no_spurious else "clean")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    failed = [r for r in _results if not r[1]]
    print(f"\n{'='*60}")
    print(f"Results: {len(_results)-len(failed)}/{len(_results)} passed")
    if failed:
        for name, _, detail in failed:
            print(f"  FAILED: {name} -- {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
