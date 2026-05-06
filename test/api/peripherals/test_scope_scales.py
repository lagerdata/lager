# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_scope_scales.py
# Run with: lager python test_scope_scales.py --box MY-BOX
# Uses Rigol MSO5204 (scope3-6). Picoscope (scope1-2) not supported via Python API.

from lager import Net, NetType
import sys

_results = []
def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)

def main():
    print("=== Oscilloscope Scale Settings Test ===\n")

    net_name = 'scope3'  # Rigol MSO5204 channel 1
    scope = None

    try:
        scope = Net.get(net_name, type=NetType.Analog)
        scope.enable()
        _record("enable", True)

        # Test vertical scale
        print("\n--- Vertical Scale (V/div) ---")
        for scale in [0.5, 1.0, 2.0]:
            try:
                scope.trace_settings.set_volts_per_div(scale)
                readback = scope.trace_settings.get_volts_per_div()
                ok = isinstance(readback, (int, float))
                _record(
                    f"volts_per_div_{scale}",
                    ok,
                    f"set={scale}, read={readback}" if ok else f"unexpected type {type(readback).__name__}",
                )
            except Exception as e:
                _record(f"volts_per_div_{scale}", False, str(e))

        # Test horizontal scale
        print("\n--- Horizontal Scale (time/div) ---")
        for scale in [0.001, 0.01, 0.1]:
            try:
                scope.trace_settings.set_time_per_div(scale)
                readback = scope.trace_settings.get_time_per_div()
                ok = isinstance(readback, (int, float))
                label = f"{scale*1000:.1f}ms"
                _record(
                    f"time_per_div_{label}",
                    ok,
                    f"set={scale}, read={readback}" if ok else f"unexpected type {type(readback).__name__}",
                )
            except Exception as e:
                label = f"{scale*1000:.1f}ms"
                _record(f"time_per_div_{label}", False, str(e))

    except Exception as e:
        _record("setup", False, str(e))

    finally:
        if scope is not None:
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
