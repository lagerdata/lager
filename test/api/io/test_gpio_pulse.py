# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_gpio_pulse.py
# Run with: lager python test_gpio_pulse.py --box MY-BOX

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
    print("=== GPIO Pulse Generation Test ===\n")

    net_name = 'gpio16'
    num_pulses = 5
    pulse_width = 0.1

    try:
        gpio = Net.get(net_name, type=NetType.GPIO)

        # Start LOW
        gpio.output(0)
        time.sleep(0.1)

        print(f"Generating {num_pulses} pulses...\n")

        for i in range(num_pulses):
            gpio.output(1)
            time.sleep(pulse_width)
            gpio.output(0)
            time.sleep(pulse_width)

            # After each pulse, pin should be LOW
            val = gpio.input()
            _record(f"pulse {i+1} -> pin returns to LOW (read == 0)",
                    isinstance(val, int) and val == 0,
                    f"got {val!r}")

    except Exception as e:
        _record("gpio pulse test", False, str(e))
    finally:
        # Safety: leave pin LOW
        try:
            gpio = Net.get(net_name, type=NetType.GPIO)
            gpio.output(0)
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
