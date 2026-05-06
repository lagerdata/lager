#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive WiFi API tests for router parental control via a wifi net.
Run with: lager python test/api/communication/test_wifi_comprehensive.py --box <WIFI_BOX>

enable()/disable() controls router parental controls (MAC blocking).
Box local network (HTTP port 5000) is unaffected.
Always wifi.enable() in finally block to avoid leaving internet disabled.
"""
import sys, os, traceback
WIFI_NET = os.environ.get("WIFI_NET", "wifi1")
_results = []

def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}: {name}" + (f" -- {detail}" if detail else ""))

def test_imports():
    """1. Verify lager WiFi imports."""
    print("\n== Imports ==")
    try:
        from lager import Net, NetType
        assert hasattr(NetType, "Wifi"), "NetType.Wifi not found"
        _record("import Net, NetType (Wifi)", True)
        return True
    except Exception as e:
        _record("import Net, NetType (Wifi)", False, str(e))
        return False

def test_net_get():
    """2. Net.get returns Wifi object."""
    print("\n== Net.get ==")
    try:
        from lager import Net, NetType
        wifi = Net.get(WIFI_NET, type=NetType.Wifi)
        assert wifi is not None and type(wifi).__name__ == "Wifi"
        _record("Net.get returns Wifi", True)
        return True
    except Exception as e:
        _record("Net.get returns Wifi", False, str(e))
        return False

def test_properties():
    """3. Verify wifi.name and wifi.pin."""
    print("\n== Properties ==")
    from lager import Net, NetType
    wifi = Net.get(WIFI_NET, type=NetType.Wifi)
    ok = True
    try:
        assert wifi.name == WIFI_NET
        _record("wifi.name", True, wifi.name)
    except Exception as e:
        _record("wifi.name", False, str(e)); ok = False
    try:
        assert isinstance(wifi.pin, (int, str))
        _record("wifi.pin", True, str(wifi.pin))
    except Exception as e:
        _record("wifi.pin", False, str(e)); ok = False
    return ok

def test_string_repr():
    """4. str(wifi) contains 'lager.Wifi'."""
    print("\n== String Repr ==")
    from lager import Net, NetType
    wifi = Net.get(WIFI_NET, type=NetType.Wifi)
    try:
        s = str(wifi)
        assert "lager.Wifi" in s and wifi.name in s
        _record("str(wifi)", True, s)
        return True
    except Exception as e:
        _record("str(wifi)", False, str(e))
        return False

def test_enable():
    """5. wifi.enable() returns truthy."""
    print("\n== Enable Internet ==")
    from lager import Net, NetType
    wifi = Net.get(WIFI_NET, type=NetType.Wifi)
    try:
        assert wifi.enable(), "enable() returned falsy"
        _record("wifi.enable()", True)
        return True
    except Exception as e:
        _record("wifi.enable()", False, str(e))
        return False

def test_disable():
    """6. wifi.disable() returns truthy; re-enables in finally."""
    print("\n== Disable Internet ==")
    from lager import Net, NetType
    wifi = Net.get(WIFI_NET, type=NetType.Wifi)
    try:
        assert wifi.disable(), "disable() returned falsy"
        _record("wifi.disable()", True)
        return True
    except Exception as e:
        _record("wifi.disable()", False, str(e))
        return False
    finally:
        wifi.enable()

def test_enable_disable_cycle():
    """7. enable -> disable -> enable cycle, all succeed."""
    print("\n== Enable-Disable Cycle ==")
    from lager import Net, NetType
    wifi = Net.get(WIFI_NET, type=NetType.Wifi)
    ok = True
    try:
        for label, fn in [("enable", wifi.enable), ("disable", wifi.disable), ("re-enable", wifi.enable)]:
            assert fn(), f"{label} falsy"
            _record(f"cycle {label}", True)
    except Exception as e:
        _record(f"cycle {label}", False, str(e)); ok = False
    finally:
        try: wifi.enable()
        except Exception: pass
    return ok

def test_reenable_safety():
    """8. Double enable() is idempotent."""
    print("\n== Re-enable Safety ==")
    from lager import Net, NetType
    wifi = Net.get(WIFI_NET, type=NetType.Wifi)
    try:
        r1, r2 = wifi.enable(), wifi.enable()
        assert r1 and r2, f"r1={r1}, r2={r2}"
        _record("double enable() idempotent", True)
        return True
    except Exception as e:
        _record("double enable() idempotent", False, str(e))
        return False
    finally:
        try: wifi.enable()
        except Exception: pass

def main():
    print(f"WiFi Comprehensive Test Suite  (net: {WIFI_NET})")
    tests = [
        ("Imports",              test_imports),
        ("Net.get",              test_net_get),
        ("Properties",           test_properties),
        ("String Repr",          test_string_repr),
        ("Enable Internet",      test_enable),
        ("Disable Internet",     test_disable),
        ("Enable-Disable Cycle", test_enable_disable_cycle),
        ("Re-enable Safety",     test_reenable_safety),
    ]
    tr = []
    for name, fn in tests:
        try: tr.append((name, fn()))
        except Exception as e:
            print(f"\nERROR in {name}: {e}"); traceback.print_exc(); tr.append((name, False))
    try:
        from lager import Net, NetType
        Net.get(WIFI_NET, type=NetType.Wifi).enable()
    except Exception: pass
    p = sum(1 for _, ok in tr if ok)
    print("\n" + "=" * 50 + "\nSUMMARY\n" + "=" * 50)
    for name, ok in tr:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\nGroups: {p}/{len(tr)} passed")
    sp = sum(1 for _, ok, _ in _results if ok)
    print(f"Sub-tests: {sp}/{len(_results)} passed")
    for n, ok, d in _results:
        if not ok: print(f"  FAIL: {n} -- {d}")
    return 0 if p == len(tr) else 1

if __name__ == "__main__":
    sys.exit(main())
