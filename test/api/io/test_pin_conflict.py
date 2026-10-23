# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Test: runtime pin-conflict warnings for LabJack drivers.

The PinRegistry warns on stderr when two LabJack subsystems (I2C, SPI, GPIO)
claim the same physical pin within one `lager python` process. This drives
it with real nets: an I2C net, a GPIO net on that I2C net's SDA pin, and a
GPIO net on a pin nothing else uses.

Run on your box:
    lager python test/api/io/test_pin_conflict.py --box <YOUR-BOX>

Nothing is driven. Getting a GPIO net only registers its pin, and the I2C
driver registers its pins when it is created, by a scan, a read-only probe
of the bus. `i2c.config()` is not used: it writes the net's settings back to
saved_nets.json. No SPI net is used, because a bench has none that is safe:
the old version of this test put one on FIO0-FIO3, where FIO0 and FIO1 switch
instrument AC power.

Environment:
  I2C_NET             the I2C net (default i2c1)
  CONFLICT_GPIO_NET   a GPIO net on I2C_NET's SDA pin (default gpio20)
  CONFLICT_PIN        that pin's name (default FIO4)
  FREE_GPIO_NET       a GPIO net on a pin nothing else here claims (default gpio18)
"""
import io
import os
import sys

from lager import Net, NetType

I2C_NET = os.environ.get("I2C_NET", "i2c1")
CONFLICT_GPIO_NET = os.environ.get("CONFLICT_GPIO_NET", "gpio20")
CONFLICT_PIN = os.environ.get("CONFLICT_PIN", "FIO4")
FREE_GPIO_NET = os.environ.get("FREE_GPIO_NET", "gpio18")
# FIO0/FIO1 switch instrument AC power on a bench.
RELAY_NETS = {"gpio16", "gpio17", "RIGOL_POWER", "KEITHLEY_POWER"}

_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def _captured(fn):
    """Run fn() and return what it wrote to stderr, replayed for the reader."""
    original = sys.stderr
    buf = io.StringIO()
    sys.stderr = buf
    try:
        fn()
    finally:
        sys.stderr = original
    text = buf.getvalue()
    if text:
        sys.stderr.write(text)
        sys.stderr.flush()
    return text


def _i2c_scan():
    i2c = Net.get(I2C_NET, NetType.I2C)
    try:
        i2c.scan()
    except Exception as e:
        # Nothing may answer on the bus; the driver, and its pin claim,
        # exist by the time the scan runs.
        print(f"  INFO: scan on {I2C_NET} raised {type(e).__name__}: {e}")


def main():
    bad = sorted({CONFLICT_GPIO_NET, FREE_GPIO_NET} & RELAY_NETS)
    if bad:
        print(f"Refusing: {', '.join(bad)} switch instrument power on a bench.")
        return 2

    print("=== LabJack pin conflict test ===\n")

    # The I2C driver claims SDA and SCL. Nothing else has claimed them yet.
    warn = _captured(_i2c_scan)
    _record(f"{I2C_NET} claims its pins without a warning", "WARNING" not in warn,
            warn.strip() or "clean")

    # A GPIO net on the I2C net's SDA pin: the registry must say so.
    warn = _captured(lambda: Net.get(CONFLICT_GPIO_NET, NetType.GPIO))
    named = CONFLICT_PIN in warn and "I2C" in warn and "GPIO" in warn
    _record(f"{CONFLICT_GPIO_NET} on {CONFLICT_PIN} warns of the I2C claim", named,
            warn.strip() or "no warning")

    # A GPIO net on a pin nothing else uses: no warning.
    warn = _captured(lambda: Net.get(FREE_GPIO_NET, NetType.GPIO))
    _record(f"{FREE_GPIO_NET} on a free pin does not warn", "WARNING" not in warn,
            warn.strip() or "clean")

    # The same subsystem claiming its own pin again is not a conflict.
    warn = _captured(lambda: Net.get(FREE_GPIO_NET, NetType.GPIO))
    _record(f"getting {FREE_GPIO_NET} again does not warn", "WARNING" not in warn,
            warn.strip() or "clean")

    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nRESULTS: {passed}/{total} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
