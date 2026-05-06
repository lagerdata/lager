#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive BluFi API tests covering client construction, BLE connection,
security negotiation, WiFi provisioning, scan, custom data, and error handling.

Run with: lager python test/api/communication/test_blufi_comprehensive.py --box <BOX>

Environment variables:
  BLUFI_DEVICE_NAME - BLE advertised name (default: BLUFI_DEVICE)
  BLUFI_WIFI_SSID   - WiFi SSID to provision (default: YourSSID)
  BLUFI_WIFI_PASS   - WiFi password (default: YourPassword)
"""
import sys, os, time, traceback

DEVICE_NAME = os.environ.get("BLUFI_DEVICE_NAME", "BLUFI_DEVICE")
WIFI_SSID = os.environ.get("BLUFI_WIFI_SSID", "YourSSID")
WIFI_PASS = os.environ.get("BLUFI_WIFI_PASS", "YourPassword")

_results = []
def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    print(f"  {status}: {name}" + (f" -- {detail}" if detail else ""))

def _skip(name, reason=""):
    _results.append((name, True, f"SKIP: {reason}" if reason else "SKIP"))
    print(f"  SKIP: {name}" + (f" -- {reason}" if reason else ""))

def _heading(title):
    print(f"\n{'=' * 60}\nTEST: {title}\n{'=' * 60}")


# Phase 1: Imports
def test_imports():
    _heading("Imports")
    ok = True
    try:
        from lager.blufi import BlufiClient
        _record("import BlufiClient from lager.blufi", True)
    except Exception as e:
        _record("import BlufiClient from lager.blufi", False, str(e)); ok = False
    try:
        from lager.blufi.constants import (
            CTRL, DATA, OP_MODE_STA, OP_MODE_SOFTAP,
            STA_CONN_SUCCESS, STA_CONN_FAIL, STA_CONN_CONNECTING,
        )
        _record("import constants", True)
    except Exception as e:
        _record("import constants", False, str(e)); ok = False
    try:
        from lager.blufi.exceptions import BluetoothError, ConnectionError, SecurityError
        _record("import exceptions", True)
    except Exception as e:
        _record("import exceptions", False, str(e)); ok = False
    try:
        from lager.blufi.security import BlufiAES, BlufiCRC, BlufiCrypto
        _record("import security classes", True)
    except Exception as e:
        _record("import security classes", False, str(e)); ok = False
    return ok


# Phase 2: Client Construction
def test_client_construction():
    _heading("Client Construction")
    try:
        from lager.blufi import BlufiClient
        client = BlufiClient()
        ok = client is not None
        _record("BlufiClient() constructor", ok)
        thread_alive = client._bleak_thread.is_alive()
        _record("bleak thread running", thread_alive)
        return ok and thread_alive, client
    except Exception as e:
        _record("BlufiClient() constructor", False, str(e))
        return False, None


# Phase 3: BLE Scan + Connect
def test_ble_connect(client, device_name, timeout=20):
    _heading("BLE Connect by Name")
    try:
        result = client.connectByName(device_name, timeout=timeout)
        if result is False:
            _record(f"connectByName('{device_name}')", False, "device not found or timeout")
            return False
        ok = client.connected
        _record(f"connectByName('{device_name}')", ok,
                "connected" if ok else "not connected")
        return ok
    except Exception as e:
        _record(f"connectByName('{device_name}')", False, str(e))
        return False


# Phase 4: Security Negotiation
def test_negotiate_security(client):
    _heading("Security Negotiation")
    try:
        client.negotiateSecurity()
        enc = client.mEncrypted
        cs = client.mChecksum
        has_key = client.mAESKey is not None
        _record("negotiateSecurity()", enc and cs and has_key,
                f"encrypted={enc}, checksum={cs}, hasKey={has_key}")
        return enc and cs and has_key
    except Exception as e:
        _record("negotiateSecurity()", False, str(e))
        return False


# Phase 5: Request Version
def test_request_version(client):
    _heading("Request Version")
    try:
        client.requestVersion()
        time.sleep(1)
        ver = client.getVersion()
        ok = ver is not None
        _record("requestVersion()", ok, f"version={ver}")
        return ok
    except Exception as e:
        _record("requestVersion()", False, str(e))
        return False


# Phase 6: Request Device Status
def test_request_device_status(client):
    _heading("Request Device Status")
    try:
        client.requestDeviceStatus()
        time.sleep(1)
        ws = client.getWifiState()
        ok = isinstance(ws, dict) and "opMode" in ws
        _record("requestDeviceStatus()", ok, f"wifiState={ws}")
        return ok
    except Exception as e:
        _record("requestDeviceStatus()", False, str(e))
        return False


# Phase 7: Set Device Mode
def test_post_device_mode(client):
    _heading("Set Device Mode")
    from lager.blufi.constants import OP_MODE_STA
    try:
        client.postDeviceMode(OP_MODE_STA)
        _record("postDeviceMode(OP_MODE_STA)", True)
        return True
    except Exception as e:
        _record("postDeviceMode(OP_MODE_STA)", False, str(e))
        return False


# Phase 8: Request WiFi Scan
def test_request_wifi_scan(client):
    _heading("Request WiFi Scan")
    try:
        client.requestDeviceScan(timeout=15)
        ssids = client.getSSIDList()
        ok = isinstance(ssids, list) and len(ssids) > 0
        _record("requestDeviceScan()", ok, f"found {len(ssids)} SSIDs")
        if ok:
            for entry in ssids[:5]:
                print(f"    {entry['ssid']} [{entry['rssi']} dBm]")
            if len(ssids) > 5:
                print(f"    ... and {len(ssids) - 5} more")
        return ok
    except Exception as e:
        _record("requestDeviceScan()", False, str(e))
        return False


# Phase 9: Post WiFi Credentials
def test_post_wifi_credentials(client, ssid, password):
    _heading("Post WiFi Credentials")
    try:
        client.postStaWifiInfo({"ssid": ssid, "pass": password})
        _record(f"postStaWifiInfo(ssid='{ssid}')", True)
        return True
    except Exception as e:
        _record(f"postStaWifiInfo(ssid='{ssid}')", False, str(e))
        return False


# Phase 10: Request Status After Connect
def test_status_after_connect(client):
    _heading("Status After WiFi Connect")
    try:
        time.sleep(5)  # Give the ESP32 time to connect
        client.requestDeviceStatus()
        time.sleep(1)
        ws = client.getWifiState()
        from lager.blufi.constants import STA_CONN_SUCCESS
        conn = ws.get("staConn", -1)
        ok = conn == STA_CONN_SUCCESS
        detail = f"staConn={conn}"
        if not ok:
            detail += " (may not have connected -- check SSID/password)"
        _record("staConn after postStaWifiInfo", ok, detail)
        return ok
    except Exception as e:
        _record("staConn after postStaWifiInfo", False, str(e))
        return False


# Phase 11: Custom Data
def test_custom_data(client):
    _heading("Custom Data")
    try:
        client.postCustomData(b"test")
        _record("postCustomData(b'test')", True)
        return True
    except Exception as e:
        _record("postCustomData(b'test')", False, str(e))
        return False


# Phase 12: Error -- Connect Nonexistent
def test_connect_nonexistent():
    _heading("Error -- Connect Nonexistent Device")
    try:
        from lager.blufi import BlufiClient
        err_client = BlufiClient()
        try:
            result = err_client.connectByName("NONEXISTENT_DEVICE_12345", timeout=5)
            if result is False:
                _record("connectByName('NONEXISTENT...') returns False", True)
                return True
            ok = not err_client.connected
            _record("connectByName('NONEXISTENT...') not connected", ok)
            return ok
        except Exception as e:
            _record("connectByName('NONEXISTENT...') raises exception", True, str(e))
            return True
        finally:
            try:
                err_client._cleanup()
            except Exception:
                pass
    except Exception as e:
        _record("Error test setup", False, str(e))
        return False


# Phase 13: Cleanup
def test_cleanup(client):
    _heading("Cleanup")
    try:
        client._cleanup()
        ok = not client.connected
        _record("client._cleanup()", ok, "disconnected" if ok else "still connected")
        return ok
    except Exception as e:
        _record("client._cleanup()", False, str(e))
        return False


# ===========================================================================
def _safe(name, fn):
    try:
        ret = fn()
        passed = ret if isinstance(ret, bool) else ret[0]
        return (name, passed), ret
    except Exception as e:
        print(f"\nUNEXPECTED ERROR in {name}: {e}"); traceback.print_exc()
        return (name, False), None


def main():
    print("=== BluFi Comprehensive Test ===")
    print(f"  Device name: {DEVICE_NAME}")
    print(f"  WiFi SSID  : {WIFI_SSID}")
    print(f"  WiFi Pass  : {'*' * len(WIFI_PASS)}")
    TR = []

    # Phase 1: Imports
    r, _ = _safe("Imports", test_imports); TR.append(r)

    # Phase 2: Client Construction
    r, ret = _safe("Client Construction", test_client_construction); TR.append(r)
    client = ret[1] if ret and isinstance(ret, tuple) else None
    if client is None:
        for n in ["BLE Connect", "Security Negotiation", "Request Version",
                   "Request Device Status", "Set Device Mode", "Request WiFi Scan",
                   "Post WiFi Credentials", "Status After Connect", "Custom Data",
                   "Error: Connect Nonexistent", "Cleanup"]:
            _skip(n, "client construction failed"); TR.append((n, True))
        return _summary(TR)

    # Phase 3: BLE Connect
    r, ret = _safe("BLE Connect", lambda: test_ble_connect(client, DEVICE_NAME))
    TR.append(r)
    connected = ret if isinstance(ret, bool) else False

    if not connected:
        # Skip phases 4-11, still run 12-13
        for n in ["Security Negotiation", "Request Version", "Request Device Status",
                   "Set Device Mode", "Request WiFi Scan", "Post WiFi Credentials",
                   "Status After Connect", "Custom Data"]:
            _skip(n, "device not found"); TR.append((n, True))
    else:
        # Phase 4: Security Negotiation
        r, ret = _safe("Security Negotiation", lambda: test_negotiate_security(client))
        TR.append(r)
        sec_ok = ret if isinstance(ret, bool) else False

        if not sec_ok:
            for n in ["Request Version", "Request Device Status", "Set Device Mode",
                       "Request WiFi Scan", "Post WiFi Credentials",
                       "Status After Connect", "Custom Data"]:
                _skip(n, "security negotiation failed"); TR.append((n, True))
        else:
            # Phase 5-11
            for name, fn in [
                ("Request Version",        lambda: test_request_version(client)),
                ("Request Device Status",  lambda: test_request_device_status(client)),
                ("Set Device Mode",        lambda: test_post_device_mode(client)),
                ("Request WiFi Scan",      lambda: test_request_wifi_scan(client)),
                ("Post WiFi Credentials",  lambda: test_post_wifi_credentials(client, WIFI_SSID, WIFI_PASS)),
                ("Status After Connect",   lambda: test_status_after_connect(client)),
                ("Custom Data",            lambda: test_custom_data(client)),
            ]:
                r, _ = _safe(name, fn); TR.append(r)

    # Phase 12: Error -- Connect Nonexistent
    r, _ = _safe("Error: Connect Nonexistent", test_connect_nonexistent); TR.append(r)

    # Phase 13: Cleanup
    r, _ = _safe("Cleanup", lambda: test_cleanup(client)); TR.append(r)

    return _summary(TR)


def _summary(TR):
    print(f"\n{'=' * 60}\nTEST SUMMARY\n{'=' * 60}")
    pc = sum(1 for _, p in TR if p)
    for name, p in TR:
        print(f"  [{'PASS' if p else 'FAIL'}] {name}")
    print(f"\nTotal: {pc}/{len(TR)} test groups passed")
    sp = sum(1 for _, p, _ in _results if p)
    sf = len(_results) - sp
    print(f"Sub-tests: {sp}/{len(_results)} passed", end="")
    if sf:
        print(f" ({sf} failed)\n\nFailed sub-tests:")
        for n, p, d in _results:
            if not p: print(f"  FAIL: {n} -- {d}")
    else:
        print()
    return 0 if pc == len(TR) else 1


if __name__ == "__main__":
    sys.exit(main())
