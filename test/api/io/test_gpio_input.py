# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_gpio_input.py
# Run with: lager python test_gpio_input.py --box MY-BOX

import sys
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
    print("=== GPIO Input Test ===\n")

    gpio_nets = ['gpio1', 'gpio2', 'gpio3', 'gpio4']

    print("Reading GPIO input states...\n")

    for net_name in gpio_nets:
        try:
            gpio = Net.get(net_name, type=NetType.GPIO)
            state = gpio.input()
            passed = isinstance(state, int) and state in (0, 1)
            _record(f"{net_name} read returns int (0 or 1)", passed,
                    f"got {state!r} (type={type(state).__name__})")
        except Exception as e:
            _record(f"{net_name} read", False, str(e))

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
