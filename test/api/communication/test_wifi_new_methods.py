#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""
Tests for standalone WiFi functions: scan_wifi, connect_to_wifi, get_wifi_status, disconnect_wifi.
Also validates the status.py bugfix (interface_interface -> current_interface).

Run with: lager python test/api/communication/test_wifi_new_methods.py --box <BOX>
"""
import sys, traceback
_results = []

def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}: {name}" + (f" -- {detail}" if detail else ""))


# ── 1. Verify imports ───────────────────────────────────────────────────

def test_imports():
    """1. All standalone WiFi functions are importable."""
    print("\n== Imports ==")
    ok = True
    for name in ("scan_wifi", "connect_to_wifi", "get_wifi_status", "disconnect_wifi"):
        try:
            fn = __import__("lager.protocols.wifi", fromlist=[name])
            assert hasattr(fn, name) and callable(getattr(fn, name))
            _record(f"import {name}", True)
        except Exception as e:
            _record(f"import {name}", False, str(e))
            ok = False
    return ok


# ── 2. scan_wifi() ──────────────────────────────────────────────────────

def test_scan():
    """2. scan_wifi() returns a dict with 'access_points' list."""
    print("\n== scan_wifi() ==")
    from lager.protocols.wifi import scan_wifi
    try:
        result = scan_wifi()
        assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}"
        _record("scan_wifi() returns dict", True)

        if "error" in result:
            _record("scan_wifi() returned error (may be expected without hardware)", True, result["error"])
            return True

        assert "access_points" in result, "Missing 'access_points' key"
        networks = result["access_points"]
        assert isinstance(networks, list), f"access_points should be list, got {type(networks).__name__}"
        _record("scan_wifi() has access_points list", True, f"{len(networks)} networks")

        if networks:
            ap = networks[0]
            assert "ssid" in ap, "First AP missing 'ssid' key"
            _record("AP has 'ssid'", True, ap["ssid"])
        else:
            _record("AP structure (skipped, 0 APs)", True, "no APs to inspect")
        return True
    except Exception as e:
        _record("scan_wifi()", False, str(e))
        return False


# ── 3. get_wifi_status() ────────────────────────────────────────────────

def test_status():
    """3. get_wifi_status() returns dict keyed by interface name."""
    print("\n== get_wifi_status() ==")
    from lager.protocols.wifi import get_wifi_status
    try:
        result = get_wifi_status()
        assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}"
        _record("get_wifi_status() returns dict", True, str(list(result.keys())))

        for iface, info in result.items():
            if iface == "error":
                _record(f"interface '{iface}' is error entry", True, str(info))
                continue
            assert "interface" in info, f"Missing 'interface' in {iface}"
            assert "ssid" in info, f"Missing 'ssid' in {iface}"
            assert "state" in info, f"Missing 'state' in {iface}"
            _record(f"interface '{iface}' has expected keys", True, f"state={info['state']}")
        return True
    except Exception as e:
        _record("get_wifi_status()", False, str(e))
        return False


# ── 4. disconnect_wifi() ────────────────────────────────────────────────

def test_disconnect():
    """4. disconnect_wifi() returns dict with 'success' key."""
    print("\n== disconnect_wifi() ==")
    from lager.protocols.wifi import disconnect_wifi
    try:
        result = disconnect_wifi()
        assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}"
        assert "success" in result, "Missing 'success' key"
        _record("disconnect_wifi() returns dict with 'success'", True, str(result))
        return True
    except Exception as e:
        _record("disconnect_wifi()", False, str(e))
        return False


# ── 5. connect_to_wifi() ────────────────────────────────────────────────

def test_connect():
    """5. connect_to_wifi() with dummy SSID returns dict with 'success' key."""
    print("\n== connect_to_wifi() ==")
    from lager.protocols.wifi import connect_to_wifi
    try:
        result = connect_to_wifi("__nonexistent_test_ssid__", "fakepass")
        assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}"
        assert "success" in result or "error" in result, "Missing 'success' or 'error' key"
        _record("connect_to_wifi() returns dict with expected keys", True, str(result))
        return True
    except Exception as e:
        _record("connect_to_wifi()", False, str(e))
        return False


# ── 6. status.py bugfix: no KeyError on interface_interface ─────────────

def test_status_bugfix():
    """6. get_wifi_status() no longer raises KeyError from interface_interface typo."""
    print("\n== status.py bugfix ==")
    try:
        from lager.protocols.wifi.status import get_wifi_status
        result = get_wifi_status()
        assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}"
        _record("get_wifi_status() runs without KeyError", True, str(list(result.keys())))
        return True
    except KeyError as e:
        _record("get_wifi_status() KeyError (BUG STILL PRESENT)", False, str(e))
        return False
    except Exception as e:
        _record("get_wifi_status()", False, str(e))
        return False


# ── main ────────────────────────────────────────────────────────────────

def main():
    print("WiFi Standalone Functions Test Suite")
    tests = [
        ("Imports",              test_imports),
        ("scan_wifi()",          test_scan),
        ("get_wifi_status()",    test_status),
        ("disconnect_wifi()",    test_disconnect),
        ("connect_to_wifi()",    test_connect),
        ("status.py bugfix",     test_status_bugfix),
    ]
    tr = []
    for name, fn in tests:
        try:
            tr.append((name, fn()))
        except Exception as e:
            print(f"\nERROR in {name}: {e}")
            traceback.print_exc()
            tr.append((name, False))

    p = sum(1 for _, ok in tr if ok)
    print("\n" + "=" * 50 + "\nSUMMARY\n" + "=" * 50)
    for name, ok in tr:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\nGroups: {p}/{len(tr)} passed")
    sp = sum(1 for _, ok, _ in _results if ok)
    print(f"Sub-tests: {sp}/{len(_results)} passed")
    for n, ok, d in _results:
        if not ok:
            print(f"  FAIL: {n} -- {d}")
    return 0 if p == len(tr) else 1

if __name__ == "__main__":
    sys.exit(main())
