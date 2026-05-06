#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Test script to verify I/O module imports work correctly.

Tests both old (backward-compat) and new import paths for:
- lager.adc / lager.io.adc
- lager.dac / lager.io.dac
- lager.gpio / lager.io.gpio
"""

import sys
import os

# Add box directory to path so we can import lager modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'box'))


def test_old_adc_imports():
    """Test old import path: from lager.adc import ..."""
    print("Testing old ADC imports (lager.adc)...")

    # Test class import
    from lager.adc import LabJackADC
    assert LabJackADC is not None, "LabJackADC should be importable"
    print(f"  [OK]from lager.adc import LabJackADC -> {LabJackADC}")

    from lager.adc import USB202ADC
    assert USB202ADC is not None, "USB202ADC should be importable"
    print(f"  [OK]from lager.adc import USB202ADC -> {USB202ADC}")

    from lager.adc import ADCBase
    assert ADCBase is not None, "ADCBase should be importable"
    print(f"  [OK]from lager.adc import ADCBase -> {ADCBase}")

    # Test function import
    from lager.adc import read
    assert callable(read), "read should be a callable function"
    print(f"  [OK]from lager.adc import read -> {read}")

    from lager.adc import voltage
    assert callable(voltage), "voltage should be a callable function"
    print(f"  [OK]from lager.adc import voltage -> {voltage}")

    # Test dispatcher
    from lager.adc import ADCDispatcher
    assert ADCDispatcher is not None, "ADCDispatcher should be importable"
    print(f"  [OK]from lager.adc import ADCDispatcher -> {ADCDispatcher}")

    print("  All old ADC imports passed!\n")
    return True


def test_new_adc_imports():
    """Test new import path: from lager.io.adc import ..."""
    print("Testing new ADC imports (lager.io.adc)...")

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

    print("  All new ADC imports passed!\n")
    return True


def test_old_dac_imports():
    """Test old import path: from lager.dac import ..."""
    print("Testing old DAC imports (lager.dac)...")

    # Test class import
    from lager.dac import LabJackDAC
    assert LabJackDAC is not None, "LabJackDAC should be importable"
    print(f"  [OK]from lager.dac import LabJackDAC -> {LabJackDAC}")

    from lager.dac import USB202DAC
    assert USB202DAC is not None, "USB202DAC should be importable"
    print(f"  [OK]from lager.dac import USB202DAC -> {USB202DAC}")

    from lager.dac import DACBase
    assert DACBase is not None, "DACBase should be importable"
    print(f"  [OK]from lager.dac import DACBase -> {DACBase}")

    # Test function imports
    from lager.dac import read
    assert callable(read), "read should be a callable function"
    print(f"  [OK]from lager.dac import read -> {read}")

    from lager.dac import write
    assert callable(write), "write should be a callable function"
    print(f"  [OK]from lager.dac import write -> {write}")

    print("  All old DAC imports passed!\n")
    return True


def test_new_dac_imports():
    """Test new import path: from lager.io.dac import ..."""
    print("Testing new DAC imports (lager.io.dac)...")

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

    print("  All new DAC imports passed!\n")
    return True


def test_old_gpio_imports():
    """Test old import path: from lager.gpio import ..."""
    print("Testing old GPIO imports (lager.gpio)...")

    # Test class import
    from lager.gpio import LabJackGPIO
    assert LabJackGPIO is not None, "LabJackGPIO should be importable"
    print(f"  [OK]from lager.gpio import LabJackGPIO -> {LabJackGPIO}")

    from lager.gpio import USB202GPIO
    assert USB202GPIO is not None, "USB202GPIO should be importable"
    print(f"  [OK]from lager.gpio import USB202GPIO -> {USB202GPIO}")

    from lager.gpio import GPIOBase
    assert GPIOBase is not None, "GPIOBase should be importable"
    print(f"  [OK]from lager.gpio import GPIOBase -> {GPIOBase}")

    # Test function imports
    from lager.gpio import read
    assert callable(read), "read should be a callable function"
    print(f"  [OK]from lager.gpio import read -> {read}")

    from lager.gpio import write
    assert callable(write), "write should be a callable function"
    print(f"  [OK]from lager.gpio import write -> {write}")

    # Test gpi/gpo aliases
    from lager.gpio import gpi
    assert callable(gpi), "gpi should be a callable function"
    print(f"  [OK]from lager.gpio import gpi -> {gpi}")

    from lager.gpio import gpo
    assert callable(gpo), "gpo should be a callable function"
    print(f"  [OK]from lager.gpio import gpo -> {gpo}")

    print("  All old GPIO imports passed!\n")
    return True


def test_new_gpio_imports():
    """Test new import path: from lager.io.gpio import ..."""
    print("Testing new GPIO imports (lager.io.gpio)...")

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

    print("  All new GPIO imports passed!\n")
    return True


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
    return True


def test_identity_check():
    """Verify that old and new paths reference the same objects."""
    print("Testing import identity (old path == new path)...")

    # ADC
    from lager.adc import LabJackADC as OldLabJackADC
    from lager.io.adc import LabJackADC as NewLabJackADC
    assert OldLabJackADC is NewLabJackADC, "LabJackADC should be the same class"
    print(f"  [OK]lager.adc.LabJackADC is lager.io.adc.LabJackADC")

    from lager.adc import ADCBase as OldADCBase
    from lager.io.adc import ADCBase as NewADCBase
    assert OldADCBase is NewADCBase, "ADCBase should be the same class"
    print(f"  [OK]lager.adc.ADCBase is lager.io.adc.ADCBase")

    # DAC
    from lager.dac import LabJackDAC as OldLabJackDAC
    from lager.io.dac import LabJackDAC as NewLabJackDAC
    assert OldLabJackDAC is NewLabJackDAC, "LabJackDAC should be the same class"
    print(f"  [OK]lager.dac.LabJackDAC is lager.io.dac.LabJackDAC")

    from lager.dac import DACBase as OldDACBase
    from lager.io.dac import DACBase as NewDACBase
    assert OldDACBase is NewDACBase, "DACBase should be the same class"
    print(f"  [OK]lager.dac.DACBase is lager.io.dac.DACBase")

    # GPIO
    from lager.gpio import LabJackGPIO as OldLabJackGPIO
    from lager.io.gpio import LabJackGPIO as NewLabJackGPIO
    assert OldLabJackGPIO is NewLabJackGPIO, "LabJackGPIO should be the same class"
    print(f"  [OK]lager.gpio.LabJackGPIO is lager.io.gpio.LabJackGPIO")

    from lager.gpio import GPIOBase as OldGPIOBase
    from lager.io.gpio import GPIOBase as NewGPIOBase
    assert OldGPIOBase is NewGPIOBase, "GPIOBase should be the same class"
    print(f"  [OK]lager.gpio.GPIOBase is lager.io.gpio.GPIOBase")

    print("  All identity checks passed!\n")
    return True


def main():
    """Run all import tests."""
    print("=" * 60)
    print("I/O Module Import Verification")
    print("=" * 60)
    print()

    all_passed = True
    tests = [
        ("Old ADC imports", test_old_adc_imports),
        ("New ADC imports", test_new_adc_imports),
        ("Old DAC imports", test_old_dac_imports),
        ("New DAC imports", test_new_dac_imports),
        ("Old GPIO imports", test_old_gpio_imports),
        ("New GPIO imports", test_new_gpio_imports),
        ("lager.io module imports", test_io_module_imports),
        ("Identity checks", test_identity_check),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, "PASS" if result else "FAIL"))
        except Exception as e:
            print(f"  [FAIL] FAILED: {e}\n")
            results.append((name, f"FAIL: {e}"))
            all_passed = False

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
