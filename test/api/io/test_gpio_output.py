# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_gpio_output.py
# Run with: lager python test_gpio_output.py --box MY-BOX

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
    print("=== GPIO Output Test ===\n")

    net_name = 'gpio16'

    try:
        gpio = Net.get(net_name, type=NetType.GPIO)

        # Set HIGH -> read should be 1
        gpio.output(1)
        time.sleep(0.2)
        val_high = gpio.input()
        _record("set HIGH -> read == 1", isinstance(val_high, int) and val_high == 1,
                f"got {val_high!r} (type={type(val_high).__name__})")

        # Set LOW -> read should be 0
        gpio.output(0)
        time.sleep(0.2)
        val_low = gpio.input()
        _record("set LOW -> read == 0", isinstance(val_low, int) and val_low == 0,
                f"got {val_low!r} (type={type(val_low).__name__})")

    except Exception as e:
        _record("gpio output test", False, str(e))
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
