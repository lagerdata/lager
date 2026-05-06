# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_scope_basic.py
# Run with: lager python test_scope_basic.py --box MY-BOX
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
    print("=== Oscilloscope Basic Setup Test ===\n")

    net_name = 'scope3'  # Rigol MSO5204 channel 1
    scope = None

    try:
        scope = Net.get(net_name, type=NetType.Analog)
        _record("get_net", True, f"retrieved {net_name}")

        try:
            scope.enable()
            _record("enable", True)
        except Exception as e:
            _record("enable", False, str(e))

        try:
            scope.start_capture()
            _record("start_capture", True)
        except Exception as e:
            _record("start_capture", False, str(e))

        try:
            scope.stop_capture()
            _record("stop_capture", True)
        except Exception as e:
            _record("stop_capture", False, str(e))

        try:
            scope.disable()
            _record("disable", True)
        except Exception as e:
            _record("disable", False, str(e))

    except Exception as e:
        _record("get_net", False, str(e))

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
