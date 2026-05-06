#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Test script to check VISA resource locking with Keithley power supply.
Uses Net API to set voltage, then user runs CLI command to check for locking.

Usage:
    lager python --box <YOUR-BOX> test/test_keithley_locking.py
"""

from lager import Net, NetType

print("=" * 60)
print("Keithley VISA Resource Locking Test - Using Net API")
print("=" * 60)

# Get power supply net
print("\n[1] Getting power supply net 'supply1' (Keithley 2281S)...")
try:
    psu = Net.get('supply1', type=NetType.PowerSupply)
    print(f"    [OK] Got power supply net: supply1")
except Exception as e:
    print(f"    [FAIL] Failed to get power supply: {e}")
    exit(1)

# Set voltage
print("\n[2] Setting voltage to 3.3V...")
try:
    psu.set_voltage(3.3)
    print(f"    [OK] Voltage set to 3.3V")
except Exception as e:
    print(f"    [FAIL] Failed to set voltage: {e}")
    exit(1)

# Set current limit
print("\n[3] Setting current limit to 0.5A...")
try:
    psu.set_current(0.5)
    print(f"    [OK] Current limit set to 0.5A")
except Exception as e:
    print(f"    [FAIL] Failed to set current: {e}")
    exit(1)

print("\n" + "=" * 60)
print("Test Complete!")
print("=" * 60)
print("\nNOTE: Hardware service cache holds VISA connection to Keithley.")
print("Now run: lager supply voltage supply1 --box <YOUR-BOX>")
print("Expected: 'Resource busy' error due to VISA locking")
print("=" * 60)
