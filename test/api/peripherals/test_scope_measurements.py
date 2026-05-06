# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_scope_measurements.py
# Run with: lager python test_scope_measurements.py --box MY-BOX
# NOTE: Requires a signal connected to scope3 (Rigol MSO5204 CH1)
# Uses Rigol MSO5204 (scope3-6). Picoscope (scope1-2) not supported via Python API.

from lager import Net, NetType
import sys
import time

_results = []
def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)

def main():
    print("=== Oscilloscope Measurements Test ===\n")
    print("NOTE: This test requires a signal connected to scope3\n")

    net_name = 'scope3'  # Rigol MSO5204 channel 1
    scope = None

    try:
        scope = Net.get(net_name, type=NetType.Analog)
        _record("get_net", True, f"retrieved {net_name}")

        scope.enable()
        _record("enable", True)

        # Configure
        scope.trace_settings.set_volts_per_div(1.0)
        scope.trace_settings.set_time_per_div(0.001)
        scope.trigger_settings.set_mode_normal()
        scope.trigger_settings.edge.set_source(scope)
        scope.trigger_settings.edge.set_slope_rising()
        scope.trigger_settings.edge.set_level(1.65)
        _record("configure", True, "volts=1.0, time=0.001, trigger=rising@1.65V")

        # Capture
        scope.start_capture()
        _record("start_capture", True)
        time.sleep(0.5)

        # Measurements
        print("\n--- Signal Measurements ---")

        try:
            vmax = scope.measurement.voltage_max()
            ok = isinstance(vmax, (int, float))
            _record("voltage_max", ok, f"{vmax:.3f} V" if ok else f"unexpected type {type(vmax).__name__}")
        except Exception as e:
            _record("voltage_max", False, f"SKIP -- {e}")

        try:
            vpp = scope.measurement.voltage_peak_to_peak()
            ok = isinstance(vpp, (int, float))
            _record("voltage_peak_to_peak", ok, f"{vpp:.3f} V" if ok else f"unexpected type {type(vpp).__name__}")
        except Exception as e:
            _record("voltage_peak_to_peak", False, f"SKIP -- {e}")

        try:
            freq = scope.measurement.frequency()
            ok = isinstance(freq, (int, float))
            _record("frequency", ok, f"{freq:.2f} Hz" if ok else f"unexpected type {type(freq).__name__}")
        except Exception as e:
            _record("frequency", False, f"SKIP -- {e}")

    except Exception as e:
        _record("setup", False, str(e))

    finally:
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
    passed = sum(1 for _, p, _ in _results if p)
    failed = sum(1 for _, p, _ in _results if not p)
    print(f"\n=== Summary: {passed} passed, {failed} failed out of {len(_results)} tests ===")
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
