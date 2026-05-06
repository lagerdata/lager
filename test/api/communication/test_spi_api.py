#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Test script for lager SPI Python API.

Run with: lager python test_spi_api.py --box <boxname>

This tests the SPI dispatcher functions and object-based API available on the box.

Prerequisites:
- An SPI net configured in /etc/lager/saved_nets.json on the box
- Example net configuration:
  {
    "name": "spi1",
    "role": "spi",
    "instrument": "labjack_t7",
    "params": {
      "cs_pin": 4,
      "clk_pin": 5,
      "mosi_pin": 6,
      "miso_pin": 7,
      "mode": 0,
      "bit_order": "msb",
      "frequency_hz": 1000000,
      "word_size": 8,
      "cs_active": "low"
    }
  }
"""
import sys
import json
import os

# Configuration - change this to your SPI net name
SPI_NET = os.environ.get("SPI_NET", "spi1")


def test_dispatcher_config():
    """Test SPI configuration via dispatcher functions."""
    print("=" * 60)
    print("TEST: SPI Dispatcher - Configuration")
    print("=" * 60)

    try:
        from lager.protocols.spi.dispatcher import config

        # Configure with default settings
        config(SPI_NET, mode=0, bit_order="msb", frequency_hz=1_000_000,
               word_size=8, cs_active="low")
        print("PASS: Default config (mode=0, freq=1MHz, msb, 8-bit)")
    except Exception as e:
        print(f"FAIL: Default config - {e}")
        return False

    try:
        # Configure mode 3
        config(SPI_NET, mode=3)
        print("PASS: Mode 3 config")
    except Exception as e:
        print(f"FAIL: Mode 3 config - {e}")
        return False

    # Reset to mode 0 for other tests
    config(SPI_NET, mode=0)
    return True


def test_dispatcher_read():
    """Test SPI read operation via dispatcher."""
    print("\n" + "=" * 60)
    print("TEST: SPI Dispatcher - Read")
    print("=" * 60)

    try:
        from lager.protocols.spi.dispatcher import read

        # Read 4 words with default fill (0xFF)
        # Note: dispatcher prints output, doesn't return it
        print("Reading 4 words with fill=0xFF...")
        read(SPI_NET, n_words=4, fill=0xFF, output_format="hex")
        print("PASS: Read 4 words (hex format)")
    except Exception as e:
        print(f"FAIL: Read 4 words - {e}")
        return False

    try:
        # Read with custom fill byte
        print("Reading 4 words with fill=0x00...")
        read(SPI_NET, n_words=4, fill=0x00, output_format="bytes")
        print("PASS: Read 4 words with fill=0x00 (bytes format)")
    except Exception as e:
        print(f"FAIL: Read with custom fill - {e}")
        return False

    try:
        # Read with JSON output
        print("Reading 4 words with JSON output...")
        read(SPI_NET, n_words=4, output_format="json")
        print("PASS: Read 4 words (json format)")
    except Exception as e:
        print(f"FAIL: Read json output - {e}")
        return False

    return True


def test_dispatcher_read_write():
    """Test SPI read_write operation via dispatcher (full duplex)."""
    print("\n" + "=" * 60)
    print("TEST: SPI Dispatcher - Read/Write (Full Duplex)")
    print("=" * 60)

    try:
        from lager.protocols.spi.dispatcher import read_write

        # Send command and read response
        data = [0x9F, 0x00, 0x00, 0x00]  # JEDEC Read ID command
        print(f"Read/Write with data={data}...")
        read_write(SPI_NET, data=data, output_format="hex")
        print("PASS: Read/Write full duplex")
    except Exception as e:
        print(f"FAIL: Read/Write - {e}")
        return False

    try:
        # Test with keep_cs flag
        data = [0x03, 0x00, 0x00, 0x00]  # Read command with address
        print(f"Read/Write with keep_cs=False...")
        read_write(SPI_NET, data=data, keep_cs=False, output_format="hex")
        print("PASS: Read/Write with keep_cs=False")
    except Exception as e:
        print(f"FAIL: Read/Write with keep_cs - {e}")
        return False

    return True


def test_dispatcher_transfer():
    """Test SPI transfer with padding/truncation via dispatcher."""
    print("\n" + "=" * 60)
    print("TEST: SPI Dispatcher - Transfer (padding/truncation)")
    print("=" * 60)

    try:
        from lager.protocols.spi.dispatcher import transfer

        # Transfer with padding (1 byte data, 4 word transfer)
        print("Transfer 1 byte + 3 fill bytes...")
        transfer(SPI_NET, n_words=4, data=[0x9F], fill=0xFF, output_format="hex")
        print("PASS: Transfer 1 byte + 3 fill")
    except Exception as e:
        print(f"FAIL: Transfer with padding - {e}")
        return False

    try:
        # Transfer with truncation (10 bytes data, 4 word transfer)
        data = list(range(10))  # [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
        print(f"Transfer with truncation (10 bytes -> 4 words)...")
        transfer(SPI_NET, n_words=4, data=data, output_format="hex")
        print("PASS: Transfer truncated to 4 words")
    except Exception as e:
        print(f"FAIL: Transfer with truncation - {e}")
        return False

    return True


def test_object_api():
    """Test object-based SPI API via Net.get()."""
    print("\n" + "=" * 60)
    print("TEST: Object-Based API (Net.get)")
    print("=" * 60)

    try:
        from lager import Net, NetType

        # Get SPI net using object API
        spi = Net.get(SPI_NET, NetType.SPI)
        print(f"PASS: Got SPI net: {spi}")
    except Exception as e:
        print(f"FAIL: Net.get('{SPI_NET}', NetType.SPI) - {e}")
        return False

    try:
        # Test config method
        spi.config(mode=0, bit_order="msb", frequency_hz=1_000_000,
                   word_size=8, cs_active="low")
        print("PASS: spi.config() method")
    except Exception as e:
        print(f"FAIL: spi.config() - {e}")
        return False

    try:
        # Test read method (returns list)
        result = spi.read(n_words=4, fill=0xFF)
        print(f"PASS: spi.read() returned {len(result)} words: {result}")
    except Exception as e:
        print(f"FAIL: spi.read() - {e}")
        return False

    try:
        # Test read_write method (returns list)
        data = [0x9F, 0x00, 0x00, 0x00]
        result = spi.read_write(data=data)
        print(f"PASS: spi.read_write() returned {len(result)} words: {result}")
    except Exception as e:
        print(f"FAIL: spi.read_write() - {e}")
        return False

    try:
        # Test transfer method with padding
        result = spi.transfer(n_words=4, data=[0x9F], fill=0xFF)
        print(f"PASS: spi.transfer() with padding: {result}")
    except Exception as e:
        print(f"FAIL: spi.transfer() with padding - {e}")
        return False

    try:
        # Test write method (no return value)
        spi.write(data=[0x06])  # Write Enable command
        print("PASS: spi.write() method")
    except Exception as e:
        print(f"FAIL: spi.write() - {e}")
        return False

    return True


def test_object_api_output_formats():
    """Test output format options in object API."""
    print("\n" + "=" * 60)
    print("TEST: Object API - Output Formats")
    print("=" * 60)

    try:
        from lager import Net, NetType
        spi = Net.get(SPI_NET, NetType.SPI)
    except Exception as e:
        print(f"FAIL: Could not get SPI net - {e}")
        return False

    try:
        # Test list format (default)
        result = spi.read(n_words=4, output_format="list")
        assert isinstance(result, list), f"Expected list, got {type(result)}"
        print(f"PASS: list format: {result}")
    except Exception as e:
        print(f"FAIL: list format - {e}")
        return False

    try:
        # Test hex format
        result = spi.read(n_words=4, output_format="hex")
        assert isinstance(result, str), f"Expected str, got {type(result)}"
        print(f"PASS: hex format: {result}")
    except Exception as e:
        print(f"FAIL: hex format - {e}")
        return False

    try:
        # Test bytes format
        result = spi.read(n_words=4, output_format="bytes")
        assert isinstance(result, str), f"Expected str, got {type(result)}"
        print(f"PASS: bytes format: {result}")
    except Exception as e:
        print(f"FAIL: bytes format - {e}")
        return False

    try:
        # Test json format
        result = spi.read(n_words=4, output_format="json")
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert "data" in result, "Expected 'data' key in JSON output"
        print(f"PASS: json format: {result}")
    except Exception as e:
        print(f"FAIL: json format - {e}")
        return False

    return True


def test_frequency_override():
    """Test configuration overrides in dispatcher operations."""
    print("\n" + "=" * 60)
    print("TEST: Configuration Overrides")
    print("=" * 60)

    try:
        from lager.protocols.spi.dispatcher import read

        # Override frequency
        overrides = {"frequency_hz": 500_000}
        print("Reading at 500kHz (override)...")
        read(SPI_NET, n_words=4, output_format="hex", overrides=overrides)
        print("PASS: Read at 500kHz")
    except Exception as e:
        print(f"FAIL: Frequency override - {e}")
        return False

    try:
        # Override mode
        overrides = {"mode": 1}
        print("Reading in mode 1 (override)...")
        read(SPI_NET, n_words=4, output_format="hex", overrides=overrides)
        print("PASS: Read in mode 1")
    except Exception as e:
        print(f"FAIL: Mode override - {e}")
        return False

    try:
        # Override word size (16-bit)
        overrides = {"word_size": 16}
        print("Reading 2x 16-bit words (override)...")
        read(SPI_NET, n_words=2, output_format="hex", overrides=overrides)
        print("PASS: Read 2x 16-bit words")
    except Exception as e:
        print(f"FAIL: Word size override - {e}")
        return False

    return True


def test_bit_order():
    """Test MSB vs LSB bit ordering."""
    print("\n" + "=" * 60)
    print("TEST: Bit Order (MSB vs LSB)")
    print("=" * 60)

    try:
        from lager.protocols.spi.dispatcher import read_write

        # MSB first (default)
        overrides = {"bit_order": "msb"}
        print("Read/Write with MSB first (0xF0)...")
        read_write(SPI_NET, data=[0xF0], output_format="hex", overrides=overrides)
        print("PASS: MSB first")
    except Exception as e:
        print(f"FAIL: MSB bit order - {e}")
        return False

    try:
        # LSB first (software bit reversal)
        overrides = {"bit_order": "lsb"}
        print("Read/Write with LSB first (0xF0 -> bits reversed)...")
        read_write(SPI_NET, data=[0xF0], output_format="hex", overrides=overrides)
        print("PASS: LSB first (software reversal)")
    except Exception as e:
        print(f"FAIL: LSB bit order - {e}")
        return False

    return True


def test_imports():
    """Test that all SPI imports work correctly."""
    print("\n" + "=" * 60)
    print("TEST: Import Paths")
    print("=" * 60)

    try:
        from lager import Net, NetType
        assert hasattr(NetType, 'SPI'), "NetType.SPI not found"
        print("PASS: from lager import Net, NetType (NetType.SPI exists)")
    except Exception as e:
        print(f"FAIL: lager imports - {e}")
        return False

    try:
        from lager.protocols import spi
        print("PASS: from lager.protocols import spi")
    except Exception as e:
        print(f"FAIL: protocols.spi import - {e}")
        return False

    try:
        from lager.protocols.spi import SPINet, config, read, read_write, transfer
        print("PASS: from lager.protocols.spi import SPINet, config, ...")
    except Exception as e:
        print(f"FAIL: protocols.spi specific imports - {e}")
        return False

    try:
        from lager.protocols.spi import SPIBase, LabJackSPI, AardvarkSPI, SPIBackendError
        print("PASS: from lager.protocols.spi import SPIBase, LabJackSPI, AardvarkSPI, SPIBackendError")
    except Exception as e:
        print(f"FAIL: protocols.spi class imports - {e}")
        return False

    return True


def main():
    """Run all tests."""
    print("SPI API Test Suite")
    print(f"Testing net: {SPI_NET}")
    print(f"Set SPI_NET environment variable to change the net name")
    print("=" * 60)

    tests = [
        ("Imports", test_imports),
        ("Dispatcher Config", test_dispatcher_config),
        ("Dispatcher Read", test_dispatcher_read),
        ("Dispatcher Read/Write", test_dispatcher_read_write),
        ("Dispatcher Transfer", test_dispatcher_transfer),
        ("Object API", test_object_api),
        ("Object API Formats", test_object_api_output_formats),
        ("Frequency Override", test_frequency_override),
        ("Bit Order", test_bit_order),
    ]

    results = []
    for name, test_fn in tests:
        try:
            passed = test_fn()
            results.append((name, passed))
        except Exception as e:
            print(f"\nUNEXPECTED ERROR in {name}: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, p in results if p)
    total = len(results)

    for name, p in results:
        status = "PASS" if p else "FAIL"
        print(f"  {name}: {status}")

    print(f"\nTotal: {passed}/{total} tests passed")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
