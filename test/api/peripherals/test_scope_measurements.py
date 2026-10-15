# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_scope_measurements.py
# Run with: lager python test_scope_measurements.py --box MY-BOX
# NOTE: Requires a signal connected to the scope net's channel
# Runs on scope3 (Rigol MSO5204 CH1) unless --env SCOPE_NET=<net> names another
# scope net, of either make.

from lager import Net, NetType
import os
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
    net_name = os.environ.get("SCOPE_NET", "scope3")
    print(f"NOTE: This test requires a signal connected to {net_name}\n")
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

        try:
            values = scope.measurement.all()
            missing = [k for k in ("vmax", "vmin", "vpp", "vavg", "vrms") if values.get(k) is None]
            _record("all", not missing,
                    f"{len(values)} values" if not missing else f"missing {missing}")
            single = scope.measurement.voltage_peak_to_peak()
            agree = abs(values["vpp"] - single) <= max(0.1, 0.1 * abs(single))
            _record("all_agrees_with_single_read", agree,
                    f"all vpp={values['vpp']:.3f} V, voltage_peak_to_peak={single:.3f} V")
        except Exception as e:
            _record("all", False, str(e))

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
