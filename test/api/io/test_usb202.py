#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# test_usb202.py
# Run with: lager python test/api/io/test_usb202.py --box <BOX>
#
# Tests all functions of a MCC USB-202 DAQ device:
#   ADC  -- 8 single-ended channels (CH0-CH7), ±10V range
#   DAC  -- 2 analog outputs (DAC0-DAC1), 0-5V range (no readback supported)
#   GPIO -- 8 digital I/O pins (DIO0-DIO7), TTL-level
#
# Cross-instrument accuracy tests are optional and skipped when the
# corresponding env vars are not set:
#   SUPPLY_NET + SUPPLY_ADC_NET   -- power supply drives known voltage into USB-202 ADC
#   LABJACK_ADC_NET               -- LabJack AIN2 verifies USB-202 DAC output
#   GPIO_LOOPBACK_OUT + GPIO_LOOPBACK_IN  -- USB-202 DIO pin wired to another DIO pin

import os
import sys
import time
import traceback

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _csv(var, default):
    val = os.environ.get(var, "").strip()
    return [s.strip() for s in val.split(",") if s.strip()] if val else default


ADC_NETS = _csv("ADC_NETS", ["adc15","adc16","adc17","adc18","adc19","adc20","adc21","adc22"])
DAC_NETS = _csv("DAC_NETS", ["dac3","dac4"])
GPIO_NETS = _csv("GPIO_NETS", ["gpio24","gpio25","gpio26","gpio27","gpio28","gpio29","gpio30","gpio31"])

SUPPLY_NET          = os.environ.get("SUPPLY_NET", "").strip()
SUPPLY_VOLTAGE      = float(os.environ.get("SUPPLY_VOLTAGE", "3.3"))
SUPPLY_CURRENT      = float(os.environ.get("SUPPLY_CURRENT", "0.1"))
SUPPLY_ADC_NET      = os.environ.get("SUPPLY_ADC_NET", "").strip()

LABJACK_ADC_NET         = os.environ.get("LABJACK_ADC_NET", "").strip()

GPIO_LOOPBACK_OUT   = os.environ.get("GPIO_LOOPBACK_OUT", "").strip()
GPIO_LOOPBACK_IN    = os.environ.get("GPIO_LOOPBACK_IN", "").strip()

ADC_RANGE         = (-10.0, 10.0)
DAC_TEST_VOLTAGES = [0.0, 1.0, 2.5, 5.0]
ADC_TOLERANCE     = 0.1   # 100 mV

_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def _skip(name, reason=""):
    msg = f"  SKIP: {name}"
    if reason:
        msg += f" -- {reason}"
    print(msg)


# ---------------------------------------------------------------------------
# ADC basic: read all channels, verify numeric + in range
# ---------------------------------------------------------------------------
def test_adc_basic():
    print("\n" + "=" * 60)
    print("TEST: ADC Basic (all channels)")
    print("=" * 60)

    ok = True
    lo, hi = ADC_RANGE

    for net_name in ADC_NETS:
        try:
            from lager import Net, NetType
            adc = Net.get(net_name, type=NetType.ADC)
            voltage = adc.input()

            is_numeric = isinstance(voltage, (int, float))
            _record(f"{net_name} returns numeric", is_numeric, f"value={voltage}")
            if not is_numeric:
                ok = False
                continue

            in_range = lo <= voltage <= hi
            _record(f"{net_name} in range [{lo}, {hi}] V", in_range, f"{voltage:.4f} V")
            if not in_range:
                ok = False

        except Exception as e:
            _record(f"{net_name} read", False, str(e))
            ok = False

    return ok


# ---------------------------------------------------------------------------
# ADC supply accuracy: power supply drives known voltage into USB-202 ADC
# ---------------------------------------------------------------------------
def test_adc_supply_accuracy():
    print("\n" + "=" * 60)
    print("TEST: ADC Supply Accuracy")
    print("=" * 60)

    if not SUPPLY_NET:
        _skip("supply accuracy", "SUPPLY_NET not set")
        return True
    if not SUPPLY_ADC_NET:
        _skip("supply accuracy", "SUPPLY_ADC_NET not set")
        return True

    ok = True
    supply = None
    try:
        from lager import Net, NetType
        supply = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
        supply.set_voltage(SUPPLY_VOLTAGE)
        supply.set_current(SUPPLY_CURRENT)
        supply.enable()
        time.sleep(0.3)

        adc = Net.get(SUPPLY_ADC_NET, type=NetType.ADC)
        reading = adc.input()

        diff = abs(reading - SUPPLY_VOLTAGE)
        passed = diff <= ADC_TOLERANCE
        _record(
            f"{SUPPLY_ADC_NET} reads supply {SUPPLY_VOLTAGE:.3f} V",
            passed,
            f"got {reading:.4f} V, diff={diff * 1000:.1f} mV (tol={ADC_TOLERANCE * 1000:.0f} mV)"
        )
        if not passed:
            ok = False

    except Exception as e:
        _record("supply accuracy test", False, str(e))
        ok = False
    finally:
        try:
            if supply is None:
                from lager import Net, NetType
                supply = Net.get(SUPPLY_NET, type=NetType.PowerSupply)
            supply.disable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# DAC basic: output voltages, verify no exception (USB-202 has no readback)
# ---------------------------------------------------------------------------
def test_dac_basic():
    print("\n" + "=" * 60)
    print("TEST: DAC Basic (output only, no readback)")
    print("=" * 60)

    ok = True
    for net_name in DAC_NETS:
        dac = None
        try:
            from lager import Net, NetType
            dac = Net.get(net_name, type=NetType.DAC)
            for v in DAC_TEST_VOLTAGES:
                try:
                    dac.output(v)
                    time.sleep(0.05)
                    _record(f"{net_name} output({v:.1f} V)", True)
                except Exception as e:
                    _record(f"{net_name} output({v:.1f} V)", False, str(e))
                    ok = False
        except Exception as e:
            _record(f"{net_name} Net.get", False, str(e))
            ok = False
        finally:
            try:
                if dac is None:
                    from lager import Net, NetType
                    dac = Net.get(net_name, type=NetType.DAC)
                dac.output(0.0)
            except Exception:
                pass

    return ok


# ---------------------------------------------------------------------------
# DAC LabJack verify: USB-202 DAC output measured by LabJack ADC
# ---------------------------------------------------------------------------
def test_dac_labjack_verify():
    print("\n" + "=" * 60)
    print("TEST: DAC Verification via LabJack ADC (USB-202 DAC → LabJack ADC)")
    print("=" * 60)

    if not LABJACK_ADC_NET:
        _skip("USB-202 DAC → LabJack ADC", "LABJACK_ADC_NET not set")
        return True
    if not DAC_NETS:
        _skip("USB-202 DAC → LabJack ADC", "DAC_NETS is empty")
        return True

    test_voltages = [0.5, 1.0, 2.5, 5.0]
    ok = True
    dac = None

    try:
        from lager import Net, NetType
        dac = Net.get(DAC_NETS[0], type=NetType.DAC)
        lj_adc = Net.get(LABJACK_ADC_NET, type=NetType.ADC)

        for v in test_voltages:
            try:
                dac.output(v)
                time.sleep(0.1)
                reading = lj_adc.input()
                diff = abs(reading - v)
                passed = diff <= ADC_TOLERANCE
                _record(
                    f"{DAC_NETS[0]} output({v:.1f} V) → LabJack ADC",
                    passed,
                    f"got {reading:.4f} V, diff={diff * 1000:.1f} mV"
                )
                if not passed:
                    ok = False
            except Exception as e:
                _record(f"{DAC_NETS[0]} output({v:.1f} V) → LabJack ADC", False, str(e))
                ok = False

    except Exception as e:
        _record("USB-202 DAC → LabJack ADC setup", False, str(e))
        ok = False
    finally:
        try:
            if dac is None:
                from lager import Net, NetType
                dac = Net.get(DAC_NETS[0], type=NetType.DAC)
            dac.output(0.0)
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# GPIO basic: output HIGH/LOW, verify cache-based readback
# ---------------------------------------------------------------------------
def test_gpio_basic():
    print("\n" + "=" * 60)
    print("TEST: GPIO Basic (cache readback)")
    print("=" * 60)

    ok = True
    # Exclude loopback pins — the loopback test uses uldaq directly and needs
    # their cache clean (in_pin must never have been set as output).
    nets_to_test = [n for n in GPIO_NETS if n not in (GPIO_LOOPBACK_OUT, GPIO_LOOPBACK_IN)]
    for net_name in nets_to_test:
        gpio = None
        try:
            from lager import Net, NetType
            gpio = Net.get(net_name, type=NetType.GPIO)

            gpio.output(1)
            time.sleep(0.05)
            val = gpio.input()
            passed = isinstance(val, int) and val == 1
            _record(f"{net_name} output(1) → input() == 1", passed, f"got {val!r}")
            if not passed:
                ok = False

            gpio.output(0)
            time.sleep(0.05)
            val = gpio.input()
            passed = isinstance(val, int) and val == 0
            _record(f"{net_name} output(0) → input() == 0", passed, f"got {val!r}")
            if not passed:
                ok = False

        except Exception as e:
            _record(f"{net_name} gpio test", False, str(e))
            ok = False
        finally:
            try:
                if gpio is None:
                    from lager import Net, NetType
                    gpio = Net.get(net_name, type=NetType.GPIO)
                gpio.output(0)
            except Exception:
                pass

    return ok


# ---------------------------------------------------------------------------
# GPIO loopback: output pin drives input pin over physical wire
#
# The USB-202 resets all GPIO to input mode on daq_device.disconnect(), so
# separate Net.get().output() / .input() calls cannot span a loopback: the
# output state is gone before the input read opens a new connection.
# We use uldaq directly to hold the device open across both operations.
# ---------------------------------------------------------------------------

def _get_usb202_dio_pin(net_name):
    """Return the DIO bit number (0-7) for a USB-202 GPIO net from box config."""
    from lager.cache import NetsCache
    rec = NetsCache().find_by_name(net_name)
    if rec is None:
        raise ValueError(f"Net '{net_name}' not found in saved_nets.json")
    pin = None
    for m in (rec.get("mappings") or []):
        if m.get("net") == net_name:
            pin = m.get("pin")
            break
    if pin is None:
        pin = rec.get("pin")
    if pin is None:
        raise ValueError(f"No pin configured for net '{net_name}'")
    try:
        return int(pin)
    except (TypeError, ValueError):
        s = str(pin).upper().strip()
        if s.startswith("DIO"):
            return int(s[3:])
        raise ValueError(f"Cannot parse DIO pin '{pin}' for net '{net_name}'")


def test_gpio_loopback():
    print("\n" + "=" * 60)
    print("TEST: GPIO Loopback (electrical continuity)")
    print("=" * 60)

    if not GPIO_LOOPBACK_OUT or not GPIO_LOOPBACK_IN:
        _skip("GPIO loopback", "GPIO_LOOPBACK_OUT or GPIO_LOOPBACK_IN not set")
        return True

    try:
        out_bit = _get_usb202_dio_pin(GPIO_LOOPBACK_OUT)
        in_bit  = _get_usb202_dio_pin(GPIO_LOOPBACK_IN)
    except Exception as e:
        _record("GPIO loopback pin resolution", False, str(e))
        return False

    try:
        from uldaq import (
            get_daq_device_inventory, InterfaceType, DaqDevice,
            DigitalDirection, DigitalPortType,
        )
    except ImportError:
        _skip("GPIO loopback", "uldaq not available")
        return True

    devices = get_daq_device_inventory(InterfaceType.USB)
    desc = next(
        (d for d in devices if 'USB-202' in d.product_name or d.product_id == 299),
        None,
    )
    if desc is None:
        _record("GPIO loopback device find", False, "USB-202 not found on USB bus")
        return False

    print(f"  INFO: {GPIO_LOOPBACK_OUT}=DIO{out_bit} (output)  {GPIO_LOOPBACK_IN}=DIO{in_bit} (input)")

    ok = True
    daq = DaqDevice(desc)
    try:
        daq.connect()
        dio = daq.get_dio_device()

        # Configure in_bit as input first, then out_bit as output last so the
        # output direction is set when we write.
        dio.d_config_bit(DigitalPortType.AUXPORT, in_bit, DigitalDirection.INPUT)
        dio.d_config_bit(DigitalPortType.AUXPORT, out_bit, DigitalDirection.OUTPUT)

        for level, expected in [(1, 1), (0, 0)]:
            dio.d_bit_out(DigitalPortType.AUXPORT, out_bit, level)
            val = int(dio.d_bit_in(DigitalPortType.AUXPORT, in_bit))
            passed = val == expected
            _record(
                f"{GPIO_LOOPBACK_OUT} output({level}) → {GPIO_LOOPBACK_IN} input() == {expected}",
                passed,
                f"got {val!r}"
            )
            if not passed:
                ok = False

        # Scan all 8 input bits while out_bit is HIGH to help locate the wire
        dio.d_bit_out(DigitalPortType.AUXPORT, out_bit, 1)
        reads = {}
        for b in range(8):
            if b == out_bit:
                continue
            dio.d_config_bit(DigitalPortType.AUXPORT, b, DigitalDirection.INPUT)
            reads[b] = int(dio.d_bit_in(DigitalPortType.AUXPORT, b))
        high_bits = [b for b, v in reads.items() if v == 1]
        print(f"  INFO: DIO{out_bit}=HIGH scan → DIO bits reading 1: {high_bits or '(none — check wire)'}")

        dio.d_bit_out(DigitalPortType.AUXPORT, out_bit, 0)

    except Exception as e:
        _record("GPIO loopback hardware", False, str(e))
        ok = False
    finally:
        try:
            daq.disconnect()
            daq.release()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    print("MCC USB-202 Comprehensive Test")
    print(f"ADC nets:          {ADC_NETS}")
    print(f"DAC nets:          {DAC_NETS}")
    print(f"GPIO nets:         {GPIO_NETS}")
    print(f"Supply net:        {SUPPLY_NET or '(not configured)'}")
    if SUPPLY_NET:
        print(f"  Supply voltage:  {SUPPLY_VOLTAGE} V  →  {SUPPLY_ADC_NET or '(SUPPLY_ADC_NET not set)'}")
    print(f"LabJack ADC net:   {LABJACK_ADC_NET or '(not configured)'}")
    print(f"GPIO loopback:     {GPIO_LOOPBACK_OUT or '(not configured)'} → {GPIO_LOOPBACK_IN or '(not configured)'}")
    print("=" * 60)

    # Preflight: verify USB-202 is reachable before running any tests
    try:
        from lager import Net, NetType
        if ADC_NETS:
            Net.get(ADC_NETS[0], type=NetType.ADC).input()
        if GPIO_NETS:
            Net.get(GPIO_NETS[0], type=NetType.GPIO).input()
    except Exception as e:
        print(f"\nERROR: Cannot connect to USB-202: {e}")
        sys.exit(1)

    tests = [
        ("ADC Basic",            test_adc_basic),
        ("ADC Supply Accuracy",  test_adc_supply_accuracy),
        ("DAC Basic",            test_dac_basic),
        ("DAC LabJack Verify",   test_dac_labjack_verify),
        ("GPIO Basic",           test_gpio_basic),
        ("GPIO Loopback",        test_gpio_loopback),
    ]

    test_results = []
    for name, fn in tests:
        try:
            test_results.append((name, fn()))
        except Exception as e:
            print(f"\nUNEXPECTED ERROR in {name}: {e}")
            traceback.print_exc()
            test_results.append((name, False))

    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    for name, p in test_results:
        print(f"  [{'PASS' if p else 'FAIL'}] {name}")
    passed = sum(1 for _, p in test_results if p)
    print(f"\nTotal: {passed}/{len(test_results)} test groups passed")
    return 0 if passed == len(test_results) else 1


if __name__ == "__main__":
    sys.exit(main())
