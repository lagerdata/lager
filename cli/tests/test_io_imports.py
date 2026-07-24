#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Verify the I/O module import surface.

Covers the supported import paths for the ADC/DAC/GPIO drivers:
- lager.io.adc / lager.io.dac / lager.io.gpio  (submodule paths)
- lager.io                                     (module-level re-exports)

Historical note: `lager.adc`, `lager.dac` and `lager.gpio` were once
backward-compatible aliases for the above. They no longer exist -- the drivers
live under `lager.io.*` only. This file used to assert the aliases still
imported, which quietly stopped being true; it never ran in CI, so nothing
caught it. See test_reexports_are_not_duplicate_objects for the invariant that
actually matters now.

Runnable two ways: as a pytest module, or standalone via `python
test_io_imports.py` for a printed report.
"""

import sys
import os

# Add box directory to path so we can import lager modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'box'))


def test_adc_imports():
    """Test import path: from lager.io.adc import ..."""
    print("Testing ADC imports (lager.io.adc)...")

    # Test class import
    from lager.io.adc import LabJackADC
    assert LabJackADC is not None, "LabJackADC should be importable"
    print(f"  [OK]from lager.io.adc import LabJackADC -> {LabJackADC}")

    from lager.io.adc import USB202ADC
    assert USB202ADC is not None, "USB202ADC should be importable"
    print(f"  [OK]from lager.io.adc import USB202ADC -> {USB202ADC}")

    from lager.io.adc import ADCBase
    assert ADCBase is not None, "ADCBase should be importable"
    print(f"  [OK]from lager.io.adc import ADCBase -> {ADCBase}")

    # Test function import
    from lager.io.adc import read
    assert callable(read), "read should be a callable function"
    print(f"  [OK]from lager.io.adc import read -> {read}")

    from lager.io.adc import voltage
    assert callable(voltage), "voltage should be a callable function"
    print(f"  [OK]from lager.io.adc import voltage -> {voltage}")

    print("  All ADC imports passed!\n")


def test_dac_imports():
    """Test import path: from lager.io.dac import ..."""
    print("Testing DAC imports (lager.io.dac)...")

    # Test class import
    from lager.io.dac import LabJackDAC
    assert LabJackDAC is not None, "LabJackDAC should be importable"
    print(f"  [OK]from lager.io.dac import LabJackDAC -> {LabJackDAC}")

    from lager.io.dac import USB202DAC
    assert USB202DAC is not None, "USB202DAC should be importable"
    print(f"  [OK]from lager.io.dac import USB202DAC -> {USB202DAC}")

    from lager.io.dac import DACBase
    assert DACBase is not None, "DACBase should be importable"
    print(f"  [OK]from lager.io.dac import DACBase -> {DACBase}")

    # Test function imports
    from lager.io.dac import read
    assert callable(read), "read should be a callable function"
    print(f"  [OK]from lager.io.dac import read -> {read}")

    from lager.io.dac import write
    assert callable(write), "write should be a callable function"
    print(f"  [OK]from lager.io.dac import write -> {write}")

    print("  All DAC imports passed!\n")


def test_gpio_imports():
    """Test import path: from lager.io.gpio import ..."""
    print("Testing GPIO imports (lager.io.gpio)...")

    # Test class import
    from lager.io.gpio import LabJackGPIO
    assert LabJackGPIO is not None, "LabJackGPIO should be importable"
    print(f"  [OK]from lager.io.gpio import LabJackGPIO -> {LabJackGPIO}")

    from lager.io.gpio import USB202GPIO
    assert USB202GPIO is not None, "USB202GPIO should be importable"
    print(f"  [OK]from lager.io.gpio import USB202GPIO -> {USB202GPIO}")

    from lager.io.gpio import GPIOBase
    assert GPIOBase is not None, "GPIOBase should be importable"
    print(f"  [OK]from lager.io.gpio import GPIOBase -> {GPIOBase}")

    # Test function imports
    from lager.io.gpio import read
    assert callable(read), "read should be a callable function"
    print(f"  [OK]from lager.io.gpio import read -> {read}")

    from lager.io.gpio import write
    assert callable(write), "write should be a callable function"
    print(f"  [OK]from lager.io.gpio import write -> {write}")

    # Test gpi/gpo aliases
    from lager.io.gpio import gpi
    assert callable(gpi), "gpi should be a callable function"
    print(f"  [OK]from lager.io.gpio import gpi -> {gpi}")

    from lager.io.gpio import gpo
    assert callable(gpo), "gpo should be a callable function"
    print(f"  [OK]from lager.io.gpio import gpo -> {gpo}")

    print("  All GPIO imports passed!\n")


def test_io_module_imports():
    """Test importing from lager.io module level."""
    print("Testing lager.io module-level imports...")

    # Test submodule access
    from lager.io import adc, dac, gpio
    assert adc is not None, "adc submodule should be accessible"
    print(f"  [OK]from lager.io import adc -> {adc}")
    assert dac is not None, "dac submodule should be accessible"
    print(f"  [OK]from lager.io import dac -> {dac}")
    assert gpio is not None, "gpio submodule should be accessible"
    print(f"  [OK]from lager.io import gpio -> {gpio}")

    # Test direct class access via __getattr__
    from lager.io import LabJackADC, LabJackDAC, LabJackGPIO
    assert LabJackADC is not None, "LabJackADC should be accessible"
    print(f"  [OK]from lager.io import LabJackADC -> {LabJackADC}")
    assert LabJackDAC is not None, "LabJackDAC should be accessible"
    print(f"  [OK]from lager.io import LabJackDAC -> {LabJackDAC}")
    assert LabJackGPIO is not None, "LabJackGPIO should be accessible"
    print(f"  [OK]from lager.io import LabJackGPIO -> {LabJackGPIO}")

    # Test prefixed convenience functions
    from lager.io import adc_read, dac_read, dac_write, gpio_read, gpio_write
    assert callable(adc_read), "adc_read should be callable"
    print(f"  [OK]from lager.io import adc_read -> {adc_read}")
    assert callable(dac_read), "dac_read should be callable"
    print(f"  [OK]from lager.io import dac_read -> {dac_read}")
    assert callable(dac_write), "dac_write should be callable"
    print(f"  [OK]from lager.io import dac_write -> {dac_write}")
    assert callable(gpio_read), "gpio_read should be callable"
    print(f"  [OK]from lager.io import gpio_read -> {gpio_read}")
    assert callable(gpio_write), "gpio_write should be callable"
    print(f"  [OK]from lager.io import gpio_write -> {gpio_write}")

    print("  All lager.io module-level imports passed!\n")


def test_reexports_are_not_duplicate_objects():
    """`lager.io.X` and `lager.io.<sub>.X` must be the SAME object.

    lager/io/__init__.py re-exports the driver classes through a module-level
    __getattr__. If that ever loads a second copy of a submodule instead of
    delegating, the two paths yield distinct classes with the same name --
    and `isinstance` checks against them silently start returning False.
    """
    print("Testing re-export identity (lager.io.X is lager.io.<sub>.X)...")

    import lager.io as io_pkg

    from lager.io.adc import LabJackADC as SubLabJackADC
    assert io_pkg.LabJackADC is SubLabJackADC, "LabJackADC should be the same class"
    print("  [OK]lager.io.LabJackADC is lager.io.adc.LabJackADC")

    from lager.io.dac import LabJackDAC as SubLabJackDAC
    assert io_pkg.LabJackDAC is SubLabJackDAC, "LabJackDAC should be the same class"
    print("  [OK]lager.io.LabJackDAC is lager.io.dac.LabJackDAC")

    from lager.io.gpio import LabJackGPIO as SubLabJackGPIO
    assert io_pkg.LabJackGPIO is SubLabJackGPIO, "LabJackGPIO should be the same class"
    print("  [OK]lager.io.LabJackGPIO is lager.io.gpio.LabJackGPIO")

    # The submodule objects reached both ways must also coincide.
    from lager.io import adc as reexported_adc
    import lager.io.adc as direct_adc
    assert reexported_adc is direct_adc, "adc submodule should be the same module"
    print("  [OK]lager.io.adc is the same module object both ways")

    print("  All re-export identity checks passed!\n")


def test_removed_aliases_stay_removed():
    """The pre-consolidation aliases must NOT come back silently.

    `lager.adc` / `lager.dac` / `lager.gpio` were removed when the drivers
    moved under `lager.io`. If one reappears -- e.g. a stray module added at
    the package root -- that is a name collision worth failing on, not a
    convenience.
    """
    import importlib

    for removed in ("lager.adc", "lager.dac", "lager.gpio"):
        try:
            importlib.import_module(removed)
        except ModuleNotFoundError:
            continue
        raise AssertionError(
            f"{removed} is importable again. The I/O drivers live under "
            f"lager.io.* only; re-adding a root-level alias reintroduces the "
            f"duplicate-class hazard test_reexports_are_not_duplicate_objects "
            f"guards against."
        )


def main():
    """Run all import tests."""
    print("=" * 60)
    print("I/O Module Import Verification")
    print("=" * 60)
    print()

    all_passed = True
    tests = [
        ("ADC imports", test_adc_imports),
        ("DAC imports", test_dac_imports),
        ("GPIO imports", test_gpio_imports),
        ("lager.io module imports", test_io_module_imports),
        ("Re-export identity", test_reexports_are_not_duplicate_objects),
        ("Removed aliases stay removed", test_removed_aliases_stay_removed),
    ]

    results = []
    for name, test_func in tests:
        try:
            test_func()
        except Exception as e:
            print(f"  [FAIL] FAILED: {e}\n")
            results.append((name, f"FAIL: {e}"))
            all_passed = False
        else:
            results.append((name, "PASS"))

    # Print summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for name, status in results:
        symbol = "[OK]" if status == "PASS" else "[FAIL]"
        print(f"  {symbol} {name}: {status}")

    print()
    if all_passed:
        print("All I/O module import tests PASSED!")
        return 0
    else:
        print("Some tests FAILED!")
        return 1


if __name__ == "__main__":
    sys.exit(main())
