#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive solar simulator tests targeting EA PSB series via lager Python API.

Run with: lager python test/api/power/test_solar_comprehensive.py --box <SOLAR_BOX>

Prerequisites:
- A solar net in /etc/lager/saved_nets.json with instrument "EA_PSB_*"
- Adjust SOLAR_NET env var or default below to match your box.

EA devices are single-threaded; 2-3s settling is needed between stop/start.
The suite always calls stop_solar_mode() in a finally block.
"""
import sys, os, time, traceback

SOLAR_NET = os.environ.get("SOLAR_NET", "solar1")
_results = []

def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    s = "PASS" if passed else "FAIL"
    print(f"  {s}: {name}" + (f" -- {detail}" if detail else ""))

def _banner(title):
    print(f"\n{'=' * 60}\nTEST: {title}\n{'=' * 60}")

try:
    from lager.power.solar import SolarBackendError
except ImportError:
    from lager.exceptions import SolarBackendError

from lager.power.solar.dispatcher import (
    set_to_solar_mode, stop_solar_mode, irradiance,
    mpp_current, mpp_voltage, resistance, temperature, voc,
)

# 1. Import Validation
def test_imports():
    _banner("Import Validation")
    ok = True
    try:
        assert callable(set_to_solar_mode) and callable(stop_solar_mode)
        _record("dispatcher function imports", True)
    except Exception as e:
        _record("dispatcher function imports", False, str(e)); ok = False
    try:
        assert issubclass(SolarBackendError, Exception)
        _record("SolarBackendError importable", True)
    except Exception as e:
        _record("SolarBackendError importable", False, str(e)); ok = False
    return ok

# 2. Start Simulation
def test_start_simulation():
    _banner("Start Simulation")
    try: set_to_solar_mode(SOLAR_NET); _record("set_to_solar_mode()", True); return True
    except Exception as e: _record("set_to_solar_mode()", False, str(e)); return False

# 3. Read Irradiance
def test_read_irradiance():
    _banner("Read Irradiance")
    try: irradiance(SOLAR_NET); _record("irradiance() read", True); return True
    except Exception as e: _record("irradiance() read", False, str(e)); return False

# 4. Set Irradiance
def test_set_irradiance():
    _banner("Set Irradiance")
    try: irradiance(SOLAR_NET, value=1000); _record("irradiance(value=1000)", True); return True
    except Exception as e: _record("irradiance(value=1000)", False, str(e)); return False

# 5. Read Measurements
def test_read_measurements():
    _banner("Read Measurements")
    ok = True
    for name, fn in [("mpp_current", mpp_current), ("mpp_voltage", mpp_voltage),
                      ("temperature", temperature), ("voc", voc)]:
        try: fn(SOLAR_NET); _record(f"{name}() read", True)
        except Exception as e: _record(f"{name}() read", False, str(e)); ok = False
    return ok

# 6. Resistance Get/Set
def test_resistance_get_set():
    _banner("Resistance Get/Set")
    ok = True
    try: resistance(SOLAR_NET, value=10.0); _record("resistance(value=10.0) set", True)
    except Exception as e: _record("resistance(value=10.0) set", False, str(e)); ok = False
    try: resistance(SOLAR_NET); _record("resistance() read", True)
    except Exception as e: _record("resistance() read", False, str(e)); ok = False
    return ok

# 7. Irradiance Range
def test_irradiance_range():
    _banner("Irradiance Range")
    ok = True
    try: irradiance(SOLAR_NET, value=0); _record("irradiance(value=0) min", True)
    except Exception as e: _record("irradiance(value=0) min", False, str(e)); ok = False
    try: irradiance(SOLAR_NET, value=1500); _record("irradiance(value=1500) max", True)
    except Exception as e: _record("irradiance(value=1500) max", False, str(e)); ok = False
    return ok

# 8. Validation: Negative Irradiance
def test_negative_irradiance():
    _banner("Validation - Negative Irradiance")
    try:
        irradiance(SOLAR_NET, value=-100)
        _record("irradiance(-100) rejected", False, "no exception"); return False
    except SolarBackendError as e:
        _record("irradiance(-100) rejected", True, str(e)); return True
    except Exception as e:
        _record("irradiance(-100) rejected", False, f"{type(e).__name__}: {e}"); return False

# 9. Validation: Irradiance Too High
def test_irradiance_too_high():
    _banner("Validation - Irradiance Too High")
    try:
        irradiance(SOLAR_NET, value=2000)
        _record("irradiance(2000) rejected", False, "no exception"); return False
    except SolarBackendError as e:
        _record("irradiance(2000) rejected", True, str(e)); return True
    except Exception as e:
        _record("irradiance(2000) rejected", False, f"{type(e).__name__}: {e}"); return False

# 10. Validation: Zero Resistance
def test_zero_resistance():
    _banner("Validation - Zero Resistance")
    try:
        resistance(SOLAR_NET, value=0)
        _record("resistance(0) rejected", False, "no exception"); return False
    except SolarBackendError as e:
        _record("resistance(0) rejected", True, str(e)); return True
    except Exception as e:
        _record("resistance(0) rejected", False, f"{type(e).__name__}: {e}"); return False

# 11. Validation: Negative Resistance
def test_negative_resistance():
    _banner("Validation - Negative Resistance")
    try:
        resistance(SOLAR_NET, value=-5)
        _record("resistance(-5) rejected", False, "no exception"); return False
    except SolarBackendError as e:
        _record("resistance(-5) rejected", True, str(e)); return True
    except Exception as e:
        _record("resistance(-5) rejected", False, f"{type(e).__name__}: {e}"); return False

# 12. Stop Simulation
def test_stop_simulation():
    _banner("Stop Simulation")
    try: stop_solar_mode(SOLAR_NET); _record("stop_solar_mode()", True); return True
    except Exception as e: _record("stop_solar_mode()", False, str(e)); return False

# 13. Set-Stop Cycle
def test_set_stop_cycle():
    _banner("Set-Stop Cycle")
    ok = True
    try:
        set_to_solar_mode(SOLAR_NET); _record("cycle: start (1st)", True)
        time.sleep(3)
        irradiance(SOLAR_NET); _record("cycle: read after settle", True)
        stop_solar_mode(SOLAR_NET); _record("cycle: stop", True)
        time.sleep(2)
        set_to_solar_mode(SOLAR_NET); _record("cycle: start (2nd)", True)
        stop_solar_mode(SOLAR_NET); _record("cycle: stop (final)", True)
    except Exception as e:
        _record("set-stop cycle", False, str(e)); ok = False
        try: stop_solar_mode(SOLAR_NET)
        except Exception: pass
    return ok

# 14. Double Stop
def test_double_stop():
    _banner("Double Stop")
    ok = True
    try: stop_solar_mode(SOLAR_NET); _record("double stop: first", True)
    except Exception as e: _record("double stop: first", False, str(e)); ok = False
    try: stop_solar_mode(SOLAR_NET); _record("double stop: second (idempotent)", True)
    except Exception as e: _record("double stop: second", False, str(e)); ok = False
    return ok


def main():
    print("Solar Simulator Comprehensive Test Suite")
    print(f"Testing net: {SOLAR_NET}  (set SOLAR_NET env var to change)")
    print("=" * 60)

    tests = [
        ("Import Validation",            test_imports),
        ("Start Simulation",             test_start_simulation),
        ("Read Irradiance",              test_read_irradiance),
        ("Set Irradiance",               test_set_irradiance),
        ("Read Measurements",            test_read_measurements),
        ("Resistance Get/Set",           test_resistance_get_set),
        ("Irradiance Range",             test_irradiance_range),
        ("Validation: Negative Irrad.",  test_negative_irradiance),
        ("Validation: Irrad. Too High",  test_irradiance_too_high),
        ("Validation: Zero Resistance",  test_zero_resistance),
        ("Validation: Neg. Resistance",  test_negative_resistance),
        ("Stop Simulation",              test_stop_simulation),
        ("Set-Stop Cycle",               test_set_stop_cycle),
        ("Double Stop",                  test_double_stop),
    ]

    test_results = []
    try:
        for name, fn in tests:
            try: test_results.append((name, fn()))
            except Exception as e:
                print(f"\nUNEXPECTED ERROR in {name}: {e}")
                traceback.print_exc(); test_results.append((name, False))
    finally:
        try: stop_solar_mode(SOLAR_NET)
        except Exception: pass

    print(f"\n{'=' * 60}\nTEST SUMMARY\n{'=' * 60}")
    passed_count = sum(1 for _, p in test_results if p)
    for name, p in test_results:
        print(f"  [{'PASS' if p else 'FAIL'}] {name}")
    print(f"\nTotal: {passed_count}/{len(test_results)} test groups passed")

    sub_passed = sum(1 for _, p, _ in _results if p)
    sub_failed = len(_results) - sub_passed
    print(f"Sub-tests: {sub_passed}/{len(_results)} passed", end="")
    if sub_failed:
        print(f" ({sub_failed} failed)\n\nFailed sub-tests:")
        for n, p, d in _results:
            if not p: print(f"  FAIL: {n} -- {d}")
    else:
        print()
    return 0 if passed_count == len(test_results) else 1

if __name__ == "__main__":
    sys.exit(main())
