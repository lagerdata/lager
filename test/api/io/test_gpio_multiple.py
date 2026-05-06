# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_gpio_multiple.py
# Run with: lager python test_gpio_multiple.py --box MY-BOX

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
    print("=== GPIO Multiple Pin Test ===\n")

    gpio_nets = ['gpio16', 'gpio17', 'gpio18', 'gpio19']

    try:
        # Get all GPIO net objects
        gpios = {}
        for net_name in gpio_nets:
            gpios[net_name] = Net.get(net_name, type=NetType.GPIO)

        # Set all HIGH
        print("Setting all HIGH...")
        for name, gpio in gpios.items():
            gpio.output(1)
        time.sleep(0.1)

        for name, gpio in gpios.items():
            val = gpio.input()
            _record(f"{name} HIGH -> read == 1", isinstance(val, int) and val == 1,
                    f"got {val!r}")

        # Set all LOW
        print("\nSetting all LOW...")
        for name, gpio in gpios.items():
            gpio.output(0)
        time.sleep(0.1)

        for name, gpio in gpios.items():
            val = gpio.input()
            _record(f"{name} LOW -> read == 0", isinstance(val, int) and val == 0,
                    f"got {val!r}")

    except Exception as e:
        _record("multiple gpio test", False, str(e))
    finally:
        # Safety: leave all pins LOW
        for net_name in gpio_nets:
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
