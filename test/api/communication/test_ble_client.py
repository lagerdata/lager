# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_ble_client.py
# Run with: lager python test_ble_client.py --box <YOUR-BOX>
# Tests BLE Client API with a real connectable BLE device

import sys
import os

DEVICE_ADDRESS = os.environ.get("BLE_DEVICE_ADDR", "AA:BB:CC:DD:EE:FF")
READABLE_CHAR = os.environ.get("BLE_READABLE_CHAR", "9b2dff02-928b-430e-9434-12c06001485c")
WRITABLE_CHAR = os.environ.get("BLE_WRITABLE_CHAR", "9b2dff01-928b-430e-9434-12c06001485c")

_results = []

def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)

def main():
    from lager.ble import Central, Client
    from bleak import BleakClient
    import asyncio

    print("=== BLE Client API Test (with Real Device) ===\n")
    print(f"Target device: {DEVICE_ADDRESS}\n")

    # Test 1: Central Constructor
    try:
        central = Central()
        _record("central_constructor", True)
    except Exception as e:
        _record("central_constructor", False, str(e))
        return 1

    # Test 2: Scan (filtered by address)
    try:
        devices = central.scan(scan_time=5.0, address=DEVICE_ADDRESS)
        _record("scan_returns_list", isinstance(devices, list),
                f"type={type(devices).__name__}")
        if not devices:
            _record("scan_found_device", False, "device not found -- may be out of range")
            return 1
        _record("scan_found_device", True,
                f"{devices[0].name or 'unknown'} ({DEVICE_ADDRESS})")
    except Exception as e:
        _record("scan_filtered", False, str(e))
        return 1

    # Test 3-7: Client connect, services, read, write, disconnect
    client = None
    try:
        loop = asyncio.get_event_loop()
        client = Client(BleakClient(DEVICE_ADDRESS), loop=loop)

        # Test 3: Connect
        connected = client.connect()
        _record("client_connect", bool(connected),
                f"connected={connected}")
        if not connected:
            return 1

        # Test 4: Get services
        try:
            services = client.get_services()
            service_list = list(services)
            _record("get_services", len(service_list) > 0,
                    f"found {len(service_list)} service(s)")
        except Exception as e:
            _record("get_services", False, str(e))

        # Test 5: Read characteristic
        try:
            value = client.read_gatt_char(READABLE_CHAR)
            is_bytes = isinstance(value, (bytes, bytearray))
            _record("read_characteristic_type", is_bytes,
                    f"type={type(value).__name__}")
            _record("read_characteristic_len", len(value) > 0,
                    f"{len(value)} bytes, hex={value.hex()}")
        except Exception as e:
            _record("read_characteristic", False, str(e))

        # Test 6: Write characteristic
        try:
            test_data = b'\x00'
            client.write_gatt_char(WRITABLE_CHAR, test_data)
            _record("write_characteristic", True, f"wrote {test_data.hex()}")
        except Exception as e:
            _record("write_characteristic", False, str(e))

        # Test 7: Disconnect
        try:
            client.disconnect()
            _record("disconnect", True)
            client = None
        except Exception as e:
            _record("disconnect", False, str(e))

    except Exception as e:
        _record("client_session", False, str(e))
    finally:
        # Safety: ensure disconnect
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass

    # Summary
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nRESULTS: {passed}/{total} passed, {failed} failed")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
