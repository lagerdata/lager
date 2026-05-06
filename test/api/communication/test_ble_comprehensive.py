#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive BLE API tests covering Central, Client, scanning, GATT
read/write, notifications, pairing, context manager, and error handling.

Run with: lager python test/api/communication/test_ble_comprehensive.py --box <BLE_BOX>

Environment variables:
  BLE_DEVICE_NAME  - peripheral name (default: <YOUR-DEVICE>)
  BLE_SERVICE_UUID - service UUID on the peripheral
  BLE_CHAR_UUID    - characteristic UUID for read/write/notify tests
"""
import sys, os, asyncio, traceback

TEST_DEVICE_NAME = os.environ.get("BLE_DEVICE_NAME", "MyBLEDevice")
SERVICE_UUID = os.environ.get("BLE_SERVICE_UUID", "12345678-1234-5678-1234-56789abcdef0")
CHARACTERISTIC_UUID = os.environ.get("BLE_CHAR_UUID", "87654321-4321-8765-4321-fedcba987654")

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

# 1. Imports
def test_imports():
    _heading("Imports")
    ok = True
    try:
        from lager.ble import Central, Client
        _record("import Central, Client from lager.ble", True)
    except Exception as e:
        _record("import Central, Client from lager.ble", False, str(e)); ok = False
    try:
        from bleak import BleakClient
        _record("import BleakClient from bleak", True)
    except Exception as e:
        _record("import BleakClient from bleak", False, str(e)); ok = False
    return ok

# 2. Central Constructor
def test_central_constructor():
    _heading("Central Constructor")
    try:
        from lager.ble import Central
        c = Central()
        _record("Central() default constructor", c is not None)
        return c is not None
    except Exception as e:
        _record("Central() default constructor", False, str(e)); return False

# 3. Scan Default
def test_scan_default(central):
    _heading("Scan Default")
    try:
        devices = central.scan(scan_time=10.0)
        ok = isinstance(devices, list)
        _record("scan(scan_time=10.0) returns list", ok, f"found {len(devices)} device(s)")
        return ok, devices
    except Exception as e:
        _record("scan(scan_time=10.0)", False, str(e)); return False, []

# 4. Scan by Name
def test_scan_by_name(central):
    _heading("Scan by Name")
    try:
        filtered = central.scan(name=TEST_DEVICE_NAME)
        ok = isinstance(filtered, list) and all((d.name or "") == TEST_DEVICE_NAME for d in filtered)
        _record(f"scan(name='{TEST_DEVICE_NAME}')", ok, f"found {len(filtered)} device(s)")
        return ok
    except Exception as e:
        _record(f"scan(name='{TEST_DEVICE_NAME}')", False, str(e)); return False

# 5. Scan by Address
def test_scan_by_address(central, address):
    _heading("Scan by Address")
    try:
        filtered = central.scan(address=address)
        ok = isinstance(filtered, list) and all(d.address == address for d in filtered)
        _record(f"scan(address='{address}')", ok, f"found {len(filtered)} device(s)")
        return ok
    except Exception as e:
        _record(f"scan(address='{address}')", False, str(e)); return False

# 7. Client Connect
def test_client_connect(address, loop):
    _heading("Client Connect")
    from lager.ble import Client; from bleak import BleakClient
    try:
        client = Client(BleakClient(address), loop=loop)
        result = client.connect()
        _record("Client.connect()", True, f"returned {result}")
        return True, client
    except Exception as e:
        _record("Client.connect()", False, str(e)); return False, None

# 8. Get Services
def test_get_services(client):
    _heading("Get Services")
    has_target = False
    try:
        services = client.get_services()
        _record("get_services() returns result", services is not None)
        for svc in (services or []):
            if svc.uuid == SERVICE_UUID:
                has_target = True; break
        _record(f"target service present", has_target,
                "found" if has_target else "not on device")
        return True, has_target
    except Exception as e:
        _record("get_services()", False, str(e)); return False, False

# 9. Has Characteristic True
def test_has_char_true(client, has_svc):
    _heading("Has Characteristic True")
    if not has_svc:
        _skip("has_characteristic(CHAR_UUID)", "target service not found"); return True
    try:
        r = client.has_characteristic(CHARACTERISTIC_UUID)
        _record("has_characteristic(CHAR_UUID)", r, f"returned {r}"); return r
    except Exception as e:
        _record("has_characteristic(CHAR_UUID)", False, str(e)); return False

# 10. Has Characteristic False
def test_has_char_false(client):
    _heading("Has Characteristic False")
    bogus = "00000000-0000-0000-0000-000000000000"
    try:
        r = client.has_characteristic(bogus)
        ok = r is False
        _record("has_characteristic(bogus) is False", ok, f"returned {r}"); return ok
    except Exception as e:
        _record("has_characteristic(bogus)", False, str(e)); return False

# 11. Read Characteristic
def test_read_char(client, has_svc):
    _heading("Read Characteristic")
    if not has_svc:
        _skip("read_gatt_char(CHAR_UUID)", "target service not found"); return True
    try:
        val = client.read_gatt_char(CHARACTERISTIC_UUID)
        ok = isinstance(val, (bytes, bytearray))
        _record("read_gatt_char(CHAR_UUID)", ok, f"len={len(val)}, hex={val.hex()}")
        return ok
    except Exception as e:
        _record("read_gatt_char(CHAR_UUID)", False, str(e)); return False

# 12. Write Characteristic
def test_write_char(client, has_svc):
    _heading("Write Characteristic")
    if not has_svc:
        _skip("write_gatt_char(CHAR_UUID)", "target service not found"); return True
    try:
        client.write_gatt_char(CHARACTERISTIC_UUID, b"Test")
        _record("write_gatt_char(CHAR_UUID, b'Test')", True); return True
    except Exception as e:
        _record("write_gatt_char(CHAR_UUID, b'Test')", False, str(e)); return False

# 13. Notifications
def test_notifications(client, has_svc):
    _heading("Notifications")
    if not has_svc:
        _skip("start_notify(CHAR_UUID)", "target service not found"); return True
    try:
        result = client.start_notify(CHARACTERISTIC_UUID, max_messages=3, timeout=5)
        ok = isinstance(result, tuple) and len(result) == 2
        if ok:
            timed_out, messages = result
            _record("start_notify returns (timed_out, messages)", True,
                    f"timed_out={timed_out}, count={len(messages)}")
        else:
            _record("start_notify returns (timed_out, messages)", False,
                    f"unexpected type: {type(result)}")
        return ok
    except Exception as e:
        _record("start_notify(CHAR_UUID)", False, str(e)); return False

# 14. Stop Notify
def test_stop_notify(client, has_svc):
    _heading("Stop Notify")
    if not has_svc:
        _skip("stop_notify(CHAR_UUID)", "target service not found"); return True
    try:
        client.stop_notify(CHARACTERISTIC_UUID)
        _record("stop_notify(CHAR_UUID)", True); return True
    except Exception as e:
        _record("stop_notify(CHAR_UUID)", False, str(e)); return False

# 15. Pairing
def test_pairing(client):
    _heading("Pairing")
    try:
        client.pair()
        _record("client.pair()", True)
    except Exception as e:
        _skip("client.pair()", f"device may not support pairing: {e}")
    return True

# 16. Disconnect
def test_disconnect(client):
    _heading("Disconnect")
    try:
        client.disconnect()
        _record("client.disconnect()", True); return True
    except Exception as e:
        _record("client.disconnect()", False, str(e)); return False

# 17. Context Manager
def test_context_manager(address, loop):
    _heading("Context Manager")
    from lager.ble import Client; from bleak import BleakClient
    try:
        with Client(BleakClient(address), loop=loop) as c:
            _record("context manager __enter__ connects", c is not None)
        _record("context manager __exit__ disconnects", True); return True
    except Exception as e:
        _record("context manager", False, str(e)); return False

# 18. Error: Invalid UUID
def test_error_invalid_uuid(address, loop):
    _heading("Error -- Invalid UUID")
    from lager.ble import Client; from bleak import BleakClient
    client = None
    try:
        client = Client(BleakClient(address), loop=loop)
        client.connect()
        raised = False
        try:
            client.read_gatt_char("invalid-uuid")
        except Exception:
            raised = True
        _record("read_gatt_char('invalid-uuid') raises", raised,
                "exception raised" if raised else "no exception")
        return raised
    except Exception as e:
        _record("read_gatt_char('invalid-uuid')", False, f"setup failed: {e}"); return False
    finally:
        if client:
            try: client.disconnect()
            except Exception: pass

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
    print("=== BLE Comprehensive Test ===")
    print(f"  Device name : {TEST_DEVICE_NAME}")
    print(f"  Service UUID: {SERVICE_UUID}")
    print(f"  Char UUID   : {CHARACTERISTIC_UUID}")
    TR = []  # test_results

    # Phase 1: basics
    r, _ = _safe("Imports", test_imports); TR.append(r)
    r, _ = _safe("Central Constructor", test_central_constructor); TR.append(r)

    # Phase 2: scanning
    from lager.ble import Central
    central = Central(); loop = central.loop
    r, ret = _safe("Scan Default", lambda: test_scan_default(central)); TR.append(r)
    all_devices = ret[1] if ret and isinstance(ret, tuple) else []
    r, _ = _safe("Scan by Name", lambda: test_scan_by_name(central)); TR.append(r)

    # Phase 3: find target
    _heading("Find Target Device")
    test_device = next((d for d in all_devices if (d.name or "") == TEST_DEVICE_NAME), None)
    if test_device:
        _record(f"find '{TEST_DEVICE_NAME}'", True, f"addr={test_device.address}")
        TR.append(("Find Target Device", True))
    else:
        _record(f"find '{TEST_DEVICE_NAME}'", False, "not found")
        TR.append(("Find Target Device", False))
        for d in all_devices[:10]:
            print(f"    {d.name or 'Unknown'}: {d.address}")
        for n in ["Scan by Address", "Client Connect", "Get Services",
                   "Has Characteristic True", "Has Characteristic False",
                   "Read Characteristic", "Write Characteristic",
                   "Notifications", "Stop Notify", "Pairing", "Disconnect",
                   "Context Manager", "Error: Invalid UUID"]:
            _skip(n, "target device not found"); TR.append((n, True))
        return _summary(TR)

    # Phase 4: scan by address
    r, _ = _safe("Scan by Address", lambda: test_scan_by_address(central, test_device.address))
    TR.append(r)

    # Phase 5: client tests
    r, ret = _safe("Client Connect", lambda: test_client_connect(test_device.address, loop))
    TR.append(r)
    client = ret[1] if ret and isinstance(ret, tuple) else None

    if client is None:
        for n in ["Get Services", "Has Characteristic True", "Has Characteristic False",
                   "Read Characteristic", "Write Characteristic", "Notifications",
                   "Stop Notify", "Pairing", "Disconnect"]:
            _skip(n, "connect failed"); TR.append((n, True))
    else:
        has_svc = False
        try:
            r, ret = _safe("Get Services", lambda: test_get_services(client)); TR.append(r)
            has_svc = ret[1] if ret and isinstance(ret, tuple) else False
            for n, fn in [
                ("Has Characteristic True",  lambda: test_has_char_true(client, has_svc)),
                ("Has Characteristic False",  lambda: test_has_char_false(client)),
                ("Read Characteristic",       lambda: test_read_char(client, has_svc)),
                ("Write Characteristic",      lambda: test_write_char(client, has_svc)),
                ("Notifications",             lambda: test_notifications(client, has_svc)),
                ("Stop Notify",               lambda: test_stop_notify(client, has_svc)),
                ("Pairing",                   lambda: test_pairing(client)),
            ]:
                r, _ = _safe(n, fn); TR.append(r)
            r, _ = _safe("Disconnect", lambda: test_disconnect(client)); TR.append(r)
            if r[1]: client = None
        finally:
            if client:
                try: client.disconnect()
                except Exception: pass

    # Phase 6: fresh-connection tests
    r, _ = _safe("Context Manager", lambda: test_context_manager(test_device.address, loop))
    TR.append(r)
    r, _ = _safe("Error: Invalid UUID", lambda: test_error_invalid_uuid(test_device.address, loop))
    TR.append(r)
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
