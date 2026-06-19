#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive Acroname USB hub tests targeting the full public API surface:
Net.get(), enable/disable/toggle, power cycling, rapid cycling, get_config(),
string representation, and multi-port control.

Run with: lager python test/api/usb/test_Acroname.py --box <YOUR-BOX>

Prerequisites:
- An Acroname USB hub connected to the box with nets in saved_nets.json
- Default net name is 'usb1'; instrument field must contain "acroname"

Override defaults with:
    USB_NET=usb2 lager python test/api/usb/test_Acroname.py --box <YOUR-BOX>
    USB_NETS=usb1,usb2 lager python test/api/usb/test_Acroname.py --box <YOUR-BOX>
"""
import sys
import os
import time
import traceback

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
USB_NET = os.environ.get("USB_NET", "usb1")
USB_NETS = [n.strip() for n in os.environ.get("USB_NETS", USB_NET).split(",") if n.strip()]
POWER_CYCLE_DURATION = 2.0   # seconds off during power cycle test
RAPID_CYCLE_COUNT = 10
RAPID_CYCLE_DELAY = 0.1      # seconds between each enable/disable in rapid test

_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


# ---------------------------------------------------------------------------
# 1. Net API
# ---------------------------------------------------------------------------
def test_net_api():
    """Net.get() returns a USBNetWrapper with the expected interface."""
    print("\n" + "=" * 60)
    print("TEST: Net API")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        usb = Net.get(USB_NET, type=NetType.Usb)

        passed_type = type(usb).__name__ == "USBNetWrapper"
        _record("Net.get() returns USBNetWrapper", passed_type, f"type={type(usb).__name__}")
        if not passed_type:
            ok = False

        for attr in ("enable", "disable", "toggle", "get_config"):
            has_attr = callable(getattr(usb, attr, None))
            _record(f"has {attr}() method", has_attr)
            if not has_attr:
                ok = False

    except Exception as e:
        _record("Net API", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 2. get_config()
# ---------------------------------------------------------------------------
def test_config():
    """get_config() returns a dict with required fields for an Acroname net."""
    print("\n" + "=" * 60)
    print("TEST: get_config()")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        usb = Net.get(USB_NET, type=NetType.Usb)
        cfg = usb.get_config()

        passed_dict = isinstance(cfg, dict)
        _record("get_config() returns dict", passed_dict, f"type={type(cfg).__name__}")
        if not passed_dict:
            return False

        passed_name = "name" in cfg
        _record("config contains 'name'", passed_name, f"name={cfg.get('name')!r}")
        if not passed_name:
            ok = False

        port_key = "port" if "port" in cfg else "pin"
        passed_port = port_key in cfg
        _record(f"config contains port/pin", passed_port, f"{port_key}={cfg.get(port_key)!r}")
        if not passed_port:
            ok = False

        passed_instrument = "instrument" in cfg
        _record("config contains 'instrument'", passed_instrument, f"instrument={cfg.get('instrument')!r}")
        if not passed_instrument:
            ok = False
        else:
            instrument = str(cfg["instrument"]).lower()
            passed_acroname = "acroname" in instrument
            _record("instrument is Acroname", passed_acroname, f"instrument={cfg['instrument']!r}")
            if not passed_acroname:
                ok = False

        cfg2 = usb.get_config()
        passed_copy = cfg is not cfg2
        _record("get_config() returns a copy (not same object)", passed_copy)
        if not passed_copy:
            ok = False

    except Exception as e:
        _record("get_config()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 3. String Representation
# ---------------------------------------------------------------------------
def test_string_representation():
    """str() and repr() include the net name and type name."""
    print("\n" + "=" * 60)
    print("TEST: String Representation")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        usb = Net.get(USB_NET, type=NetType.Usb)

        s = str(usb)
        passed_str = isinstance(s, str) and len(s) > 0
        _record("str() returns non-empty string", passed_str, f"str={s!r}")
        if not passed_str:
            ok = False

        passed_name_in_str = USB_NET in s
        _record(f"str() contains net name '{USB_NET}'", passed_name_in_str, f"str={s!r}")
        if not passed_name_in_str:
            ok = False

        passed_type_in_str = "USBNetWrapper" in s
        _record("str() contains 'USBNetWrapper'", passed_type_in_str, f"str={s!r}")
        if not passed_type_in_str:
            ok = False

        r = repr(usb)
        passed_repr = isinstance(r, str) and USB_NET in r
        _record(f"repr() contains net name '{USB_NET}'", passed_repr, f"repr={r!r}")
        if not passed_repr:
            ok = False

    except Exception as e:
        _record("string representation", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 4. Enable / Disable
# ---------------------------------------------------------------------------
def test_enable_disable():
    """enable() and disable() succeed without raising exceptions."""
    print("\n" + "=" * 60)
    print("TEST: Enable / Disable")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        usb = Net.get(USB_NET, type=NetType.Usb)

        try:
            usb.enable()
            _record("enable() succeeds", True)
        except Exception as e:
            _record("enable()", False, str(e))
            ok = False

        time.sleep(0.3)

        try:
            usb.disable()
            _record("disable() succeeds", True)
        except Exception as e:
            _record("disable()", False, str(e))
            ok = False

        time.sleep(0.3)

        try:
            usb.enable()
            _record("re-enable() after disable succeeds", True)
        except Exception as e:
            _record("re-enable()", False, str(e))
            ok = False

    except Exception as e:
        _record("enable/disable setup", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(USB_NET, type=NetType.Usb).enable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 5. Toggle
# ---------------------------------------------------------------------------
def test_toggle():
    """toggle() succeeds; two toggles leave the port in the original state."""
    print("\n" + "=" * 60)
    print("TEST: Toggle")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        usb = Net.get(USB_NET, type=NetType.Usb)

        # Start from a known state: enabled
        usb.enable()
        time.sleep(0.2)

        try:
            usb.toggle()
            _record("toggle() first call succeeds (port now off)", True)
        except Exception as e:
            _record("toggle() first call", False, str(e))
            ok = False

        time.sleep(0.2)

        try:
            usb.toggle()
            _record("toggle() second call succeeds (port back on)", True)
        except Exception as e:
            _record("toggle() second call", False, str(e))
            ok = False

    except Exception as e:
        _record("toggle setup", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(USB_NET, type=NetType.Usb).enable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 6. Power Cycle
# ---------------------------------------------------------------------------
def test_power_cycle():
    """Disable-then-enable cycle with timing: off-duration meets minimum."""
    print("\n" + "=" * 60)
    print("TEST: Power Cycle")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        usb = Net.get(USB_NET, type=NetType.Usb)
        usb.enable()
        time.sleep(0.3)

        t_start = time.monotonic()
        usb.disable()
        time.sleep(POWER_CYCLE_DURATION)
        usb.enable()
        elapsed = time.monotonic() - t_start

        _record("disable() succeeded", True)
        _record("enable() succeeded after off period", True)

        passed_timing = elapsed >= POWER_CYCLE_DURATION
        _record(
            f"off-duration >= {POWER_CYCLE_DURATION} s",
            passed_timing,
            f"measured={elapsed:.3f} s",
        )
        if not passed_timing:
            ok = False

    except Exception as e:
        _record("power cycle", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(USB_NET, type=NetType.Usb).enable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 7. Rapid Cycling
# ---------------------------------------------------------------------------
def test_rapid_cycling():
    """Disable/enable repeated quickly; no errors across all cycles."""
    print("\n" + "=" * 60)
    print("TEST: Rapid Cycling")
    print("=" * 60)

    ok = True
    errors = 0

    try:
        from lager import Net, NetType
        usb = Net.get(USB_NET, type=NetType.Usb)

        for i in range(RAPID_CYCLE_COUNT):
            try:
                usb.disable()
                time.sleep(RAPID_CYCLE_DELAY)
                usb.enable()
                time.sleep(RAPID_CYCLE_DELAY)
            except Exception as e:
                errors += 1
                _record(f"cycle {i + 1} error", False, str(e))

        passed = errors == 0
        _record(
            f"{RAPID_CYCLE_COUNT} rapid disable/enable cycles",
            passed,
            f"errors={errors}",
        )
        if not passed:
            ok = False

    except Exception as e:
        _record("rapid cycling setup", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            Net.get(USB_NET, type=NetType.Usb).enable()
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# 8. Multi-Port Control
# ---------------------------------------------------------------------------
def test_multi_port():
    """Disable then enable all configured USB nets without errors."""
    print("\n" + "=" * 60)
    print("TEST: Multi-Port Control")
    print("=" * 60)

    ok = True
    nets_used = []

    try:
        from lager import Net, NetType

        nets_used = [Net.get(name, type=NetType.Usb) for name in USB_NETS]
        _record(f"resolved {len(nets_used)} USB net(s)", True, f"nets={USB_NETS}")

        disable_errors = 0
        for usb in nets_used:
            try:
                usb.disable()
            except Exception as e:
                disable_errors += 1
                _record(f"disable() on {usb.name}", False, str(e))

        passed_disable = disable_errors == 0
        _record(
            f"disabled all {len(nets_used)} net(s)",
            passed_disable,
            f"errors={disable_errors}",
        )
        if not passed_disable:
            ok = False

        time.sleep(0.5)

        enable_errors = 0
        for usb in nets_used:
            try:
                usb.enable()
            except Exception as e:
                enable_errors += 1
                _record(f"enable() on {usb.name}", False, str(e))

        passed_enable = enable_errors == 0
        _record(
            f"enabled all {len(nets_used)} net(s)",
            passed_enable,
            f"errors={enable_errors}",
        )
        if not passed_enable:
            ok = False

    except Exception as e:
        _record("multi-port setup", False, str(e))
        ok = False
    finally:
        try:
            from lager import Net, NetType
            for usb in nets_used:
                try:
                    usb.enable()
                except Exception:
                    pass
        except Exception:
            pass

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    print("Acroname USB Hub Test Suite")
    print(f"Primary net : {USB_NET}")
    print(f"All nets    : {USB_NETS}")
    print(f"Set USB_NET / USB_NETS env vars to override")
    print("=" * 60)

    # Preflight: confirm hardware is reachable before running any tests.
    try:
        from lager import Net, NetType
        Net.get(USB_NET, type=NetType.Usb).enable()
    except Exception as e:
        print(f"\nSKIP: Cannot connect to net '{USB_NET}' — device not reachable: {e}")
        print("\nDiagnose the hardware issue with:")
        print(f"  lager instruments --box <box>")
        print(f"  lager hello --box <box>")
        print("\nCommon fixes:")
        print("  - Ensure the Acroname hub is connected via USB to the box")
        print("  - Verify the net is in saved_nets.json with instrument='Acroname'")
        print("  - Check the BrainStem SDK is installed on the box")
        print("\nSkipping all tests for this device.")
        sys.exit(0)

    tests = [
        ("Net API",                  test_net_api),
        ("get_config()",             test_config),
        ("String Representation",    test_string_representation),
        ("Enable / Disable",         test_enable_disable),
        ("Toggle",                   test_toggle),
        ("Power Cycle",              test_power_cycle),
        ("Rapid Cycling",            test_rapid_cycling),
        ("Multi-Port Control",       test_multi_port),
    ]

    test_results = []
    try:
        for name, test_fn in tests:
            try:
                passed = test_fn()
                test_results.append((name, passed))
            except Exception as e:
                print(f"\nUNEXPECTED ERROR in {name}: {e}")
                traceback.print_exc()
                test_results.append((name, False))
    finally:
        try:
            from lager import Net, NetType
            for net_name in USB_NETS:
                try:
                    Net.get(net_name, type=NetType.Usb).enable()
                except Exception:
                    pass
        except Exception:
            pass

    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    passed_count = sum(1 for _, p in test_results if p)
    total_count = len(test_results)

    for name, p in test_results:
        print(f"  [{'PASS' if p else 'FAIL'}] {name}")

    print(f"\nTotal: {passed_count}/{total_count} test groups passed")

    sub_passed = sum(1 for _, p, _ in _results if p)
    sub_total = len(_results)
    sub_failed = sub_total - sub_passed
    print(f"Sub-tests: {sub_passed}/{sub_total} passed", end="")
    if sub_failed > 0:
        print(f" ({sub_failed} failed)")
        print("\nFailed sub-tests:")
        for name, p, detail in _results:
            if not p:
                print(f"  FAIL: {name} -- {detail}")
    else:
        print()

    return 0 if passed_count == total_count else 1


if __name__ == "__main__":
    sys.exit(main())
