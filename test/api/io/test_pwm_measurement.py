# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_pwm_measurement.py
# Run with: lager python test_pwm_measurement.py --box <YOUR-BOX>
# NOTE: Requires a PWM signal connected to scope1

import sys
import time
from lager import Net, NetType

_results = []


def _record(name, passed, detail=""):
    """Record a sub-test result."""
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def main():
    print("=== PWM Signal Measurement Test ===\n")
    print("NOTE: This test requires a PWM signal connected to scope1\n")

    net_name = 'scope1'
    scope = None

    try:
        scope = Net.get(net_name, type=NetType.Analog)
        scope.enable()

        # Configure
        scope.trace_settings.set_volts_per_div(1.0)
        scope.trace_settings.set_time_per_div(0.001)
        scope.trigger_settings.set_mode_normal()
        scope.trigger_settings.edge.set_source(scope)
        scope.trigger_settings.edge.set_slope_rising()
        scope.trigger_settings.edge.set_level(1.65)

        # Capture
        scope.start_capture()
        time.sleep(0.5)

        # Frequency measurement
        try:
            freq = scope.measurement.frequency()
            passed = isinstance(freq, (int, float)) and freq > 0
            _record("frequency > 0", passed, f"got {freq!r} Hz")
        except Exception as e:
            _record("frequency > 0", False, f"could not measure -- {e}")

        # Vpp measurement
        try:
            vpp = scope.measurement.voltage_peak_to_peak()
            passed = isinstance(vpp, (int, float)) and vpp > 0
            _record("Vpp > 0", passed, f"got {vpp!r} V")
        except Exception as e:
            _record("Vpp > 0", False, f"could not measure -- {e}")

        # Duty cycle measurement
        try:
            duty = scope.measurement.duty_cycle()
            passed = isinstance(duty, (int, float)) and 0 <= duty <= 100
            _record("duty cycle in [0, 100]%", passed, f"got {duty!r}%")
        except Exception as e:
            _record("duty cycle in [0, 100]%", False, f"could not measure -- {e}")

        scope.stop_capture()

    except Exception as e:
        _record("pwm measurement test", False, str(e))
    finally:
        # Safety: disable scope
        if scope is not None:
            try:
                scope.stop_capture()
            except Exception:
                pass
            try:
                scope.disable()
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
