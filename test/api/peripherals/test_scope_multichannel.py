# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_scope_multichannel.py
# Run with: lager python test_scope_multichannel.py --box MY-BOX
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
    print("=== Oscilloscope Multi-Channel Test ===\n")

    channels = ['scope3', 'scope4', 'scope5', 'scope6']  # Rigol MSO5204 channels 1-4
    scopes = {}

    try:
        # Enable all channels
        print("Enabling channels...")
        for ch_name in channels:
            try:
                scope = Net.get(ch_name, type=NetType.Analog)
                scope.enable()
                scopes[ch_name] = scope
                _record(f"enable_{ch_name}", True)
            except Exception as e:
                _record(f"enable_{ch_name}", False, str(e))

        # Start capture on all enabled channels
        print("\nStarting capture...")
        for ch_name, scope in scopes.items():
            try:
                scope.start_capture()
                _record(f"start_{ch_name}", True)
            except Exception as e:
                _record(f"start_{ch_name}", False, str(e))

        time.sleep(0.5)

        # Stop capture on all
        print("\nStopping capture...")
        for ch_name, scope in scopes.items():
            try:
                scope.stop_capture()
                _record(f"stop_{ch_name}", True)
            except Exception as e:
                _record(f"stop_{ch_name}", False, str(e))

        # Disable all channels
        print("\nDisabling channels...")
        for ch_name, scope in scopes.items():
            try:
                scope.disable()
                _record(f"disable_{ch_name}", True)
            except Exception as e:
                _record(f"disable_{ch_name}", False, str(e))

    except Exception as e:
        _record("multichannel_setup", False, str(e))

    finally:
        for ch_name, scope in scopes.items():
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
