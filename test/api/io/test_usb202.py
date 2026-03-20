#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
# test_usb202.py
# Run with: lager python test/api/io/test_usb202.py --box TEST-3
#
# Tests all nets for a MCC USB-202 DAQ device:
#   ADC  -- 8 single-ended channels (CH0-CH7), ±10V range
#   DAC  -- 2 analog outputs (DAC0-DAC1), 0-5V range (no readback supported)
#   GPIO -- 8 digital I/O pins (DIO0-DIO7), TTL-level

import sys
import time
import traceback

from lager import Net, NetType

# ---------------------------------------------------------------------------
# Configuration -- update net names to match your TEST-3 setup
# ---------------------------------------------------------------------------
ADC_NETS  = ['adc15', 'adc16', 'adc17', 'adc18', 'adc19', 'adc20', 'adc21', 'adc22']
DAC_NETS  = ['dac3', 'dac4']
GPIO_NETS = ['gpio24', 'gpio25', 'gpio26', 'gpio27', 'gpio28', 'gpio29', 'gpio30', 'gpio31']

DAC_TEST_VOLTAGES = [0.0, 1.0, 2.5, 5.0]   # Must stay within 0-5V (USB-202 limit)
ADC_RANGE = (-10.0, 10.0)                   # USB-202 single-ended ±10V

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


# ---------------------------------------------------------------------------
# ADC -- read all 8 channels
# ---------------------------------------------------------------------------
def test_adc():
    print("\n" + "=" * 60)
    print("TEST: ADC (CH0-CH7)")
    print("=" * 60)

    ok = True
    lo, hi = ADC_RANGE

    for net_name in ADC_NETS:
        try:
            adc = Net.get(net_name, type=NetType.ADC)
            voltage = adc.input()

            is_numeric = isinstance(voltage, (int, float))
            _record(f"{net_name} returns numeric", is_numeric,
                    f"type={type(voltage).__name__}, value={voltage}")
            if not is_numeric:
                ok = False
                continue

            in_range = lo <= voltage <= hi
            _record(f"{net_name} in range [{lo}, {hi}] V", in_range,
                    f"{voltage:.4f} V")
            if not in_range:
                ok = False

        except Exception as e:
            _record(f"{net_name} read", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# DAC -- set voltages on both channels (readback not supported by USB-202)
# ---------------------------------------------------------------------------
def test_dac():
    print("\n" + "=" * 60)
    print("TEST: DAC (DAC0-DAC1)")
    print("=" * 60)

    ok = True

    for net_name in DAC_NETS:
        dac = None
        try:
            dac = Net.get(net_name, type=NetType.DAC)

            for voltage in DAC_TEST_VOLTAGES:
                try:
                    dac.output(voltage)
                    time.sleep(0.05)
                    _record(f"{net_name} output({voltage:.1f}V)", True)
                except Exception as e:
                    _record(f"{net_name} output({voltage:.1f}V)", False, str(e))
                    ok = False

        except Exception as e:
            _record(f"{net_name} Net.get", False, str(e))
            ok = False
        finally:
            try:
                if dac is None:
                    dac = Net.get(net_name, type=NetType.DAC)
                dac.output(0.0)
            except Exception:
                pass

    return ok


# ---------------------------------------------------------------------------
# GPIO -- output HIGH/LOW and read back via device cache
# ---------------------------------------------------------------------------
def test_gpio():
    print("\n" + "=" * 60)
    print("TEST: GPIO (DIO0-DIO7)")
    print("=" * 60)

    ok = True

    for net_name in GPIO_NETS:
        gpio = None
        try:
            gpio = Net.get(net_name, type=NetType.GPIO)

            # Set HIGH, read back
            gpio.output(1)
            time.sleep(0.05)
            val = gpio.input()
            _record(f"{net_name} output(1) -> input() == 1",
                    isinstance(val, int) and val == 1,
                    f"got {val!r}")
            if not (isinstance(val, int) and val == 1):
                ok = False

            # Set LOW, read back
            gpio.output(0)
            time.sleep(0.05)
            val = gpio.input()
            _record(f"{net_name} output(0) -> input() == 0",
                    isinstance(val, int) and val == 0,
                    f"got {val!r}")
            if not (isinstance(val, int) and val == 0):
                ok = False

        except Exception as e:
            _record(f"{net_name} gpio test", False, str(e))
            ok = False
        finally:
            try:
                if gpio is None:
                    gpio = Net.get(net_name, type=NetType.GPIO)
                gpio.output(0)
            except Exception:
                pass

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    print("MCC USB-202 Comprehensive Test")
    print(f"ADC nets:  {ADC_NETS}")
    print(f"DAC nets:  {DAC_NETS}")
    print(f"GPIO nets: {GPIO_NETS}")
    print("=" * 60)

    tests = [
        ("ADC",  test_adc),
        ("DAC",  test_dac),
        ("GPIO", test_gpio),
    ]

    for name, test_fn in tests:
        try:
            test_fn()
        except Exception as e:
            print(f"\nUNEXPECTED ERROR in {name}: {e}")
            traceback.print_exc()
            _record(name, False, str(e))

    failed = [r for r in _results if not r[1]]
    print(f"\n{'='*60}")
    print(f"Results: {len(_results) - len(failed)}/{len(_results)} passed")
    if failed:
        print("Failed:")
        for name, _, detail in failed:
            print(f"  FAIL: {name} -- {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
