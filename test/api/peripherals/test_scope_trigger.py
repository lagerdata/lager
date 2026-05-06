# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_scope_trigger.py
# Run with: lager python test_scope_trigger.py --box MY-BOX
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
    print("=== Oscilloscope Trigger Configuration Test ===\n")

    net_name = 'scope3'  # Rigol MSO5204 channel 1
    scope = None

    try:
        scope = Net.get(net_name, type=NetType.Analog)
        scope.enable()
        _record("enable", True)

        # Configure trigger
        try:
            scope.trigger_settings.set_mode_normal()
            _record("set_mode_normal", True)
        except Exception as e:
            _record("set_mode_normal", False, str(e))

        try:
            scope.trigger_settings.set_coupling_DC()
            _record("set_coupling_DC", True)
        except Exception as e:
            _record("set_coupling_DC", False, str(e))

        try:
            scope.trigger_settings.edge.set_source(scope)
            _record("edge_set_source", True)
        except Exception as e:
            _record("edge_set_source", False, str(e))

        try:
            scope.trigger_settings.edge.set_slope_rising()
            _record("edge_set_slope_rising", True)
        except Exception as e:
            _record("edge_set_slope_rising", False, str(e))

        try:
            scope.trigger_settings.edge.set_level(1.65)
            _record("edge_set_level", True, "level=1.65V")
        except Exception as e:
            _record("edge_set_level", False, str(e))

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
