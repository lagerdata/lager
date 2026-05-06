# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_io_comprehensive.py
# Run with: lager python test_io_comprehensive.py --box MY-BOX

import sys
import time
from lager import Net, NetType

_results = []

DAC_TARGET = 1.5
DAC_TOLERANCE = 0.05  # 50mV


def _record(name, passed, detail=""):
    """Record a sub-test result."""
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def test_adc():
    """Read ADC channels and verify each returns a float in valid range."""
    print("\n--- ADC Test ---")
    adc_nets = ['adc1', 'adc2', 'adc3', 'adc4']
    for net_name in adc_nets:
        try:
            adc = Net.get(net_name, type=NetType.ADC)
            voltage = adc.input()
            is_float = isinstance(voltage, (int, float))
            in_range = -0.5 <= voltage <= 11.0 if is_float else False
            passed = is_float and in_range
            _record(f"{net_name} read is float in [-0.5, 11V]", passed,
                    f"got {voltage!r} (type={type(voltage).__name__})")
        except Exception as e:
            _record(f"{net_name} read", False, str(e))


def test_dac():
    """Set DAC to 1.5V, read back, verify within tolerance."""
    print("\n--- DAC Test ---")
    dac_nets = ['dac1', 'dac2']
    for net_name in dac_nets:
        try:
            dac = Net.get(net_name, type=NetType.DAC)
            dac.output(DAC_TARGET)
            time.sleep(0.1)
            readback = dac.get_voltage()
            is_float = isinstance(readback, (int, float))
            within_tol = abs(readback - DAC_TARGET) <= DAC_TOLERANCE if is_float else False
            passed = is_float and within_tol
            _record(f"{net_name} set {DAC_TARGET}V readback within {DAC_TOLERANCE*1000:.0f}mV",
                    passed,
                    f"readback={readback!r}, delta={abs(readback - DAC_TARGET):.4f}" if is_float else f"readback={readback!r}")
        except Exception as e:
            _record(f"{net_name} set/read", False, str(e))


def test_gpio():
    """Read GPIO channels and verify each returns an int (0 or 1)."""
    print("\n--- GPIO Test ---")
    gpio_nets = ['gpio1', 'gpio2', 'gpio3', 'gpio4']
    for net_name in gpio_nets:
        try:
            gpio = Net.get(net_name, type=NetType.GPIO)
            state = gpio.input()
            passed = isinstance(state, int) and state in (0, 1)
            _record(f"{net_name} read returns int (0 or 1)", passed,
                    f"got {state!r} (type={type(state).__name__})")
        except Exception as e:
            _record(f"{net_name} read", False, str(e))


def main():
    print("=== Comprehensive I/O Test ===\n")

    try:
        test_adc()
        test_dac()
        test_gpio()
    except Exception as e:
        _record("comprehensive io test", False, str(e))
    finally:
        # Safety: reset DACs to 0V
        for net_name in ['dac1', 'dac2']:
            try:
                dac = Net.get(net_name, type=NetType.DAC)
                dac.output(0.0)
            except Exception:
                pass

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, p, _ in _results if p)
    total = len(_results)
    failed = total - passed
    for name, p, detail in _results:
        status = "PASS" if p else "FAIL"
        print(f"  [{status}] {name}")
    print(f"\nTotal: {passed}/{total} passed", end="")
    if failed > 0:
        print(f" ({failed} failed)")
        print("\nFailed:")
        for name, p, detail in _results:
            if not p:
                print(f"  FAIL: {name} -- {detail}")
    else:
        print()
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
