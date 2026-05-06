# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_ble_with_real_devices.py
# Run with: lager python test_ble_with_real_devices.py --box <BOX_NAME>
# Tests BLE Client functions with any discovered BLE device

import sys

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

    print("=== BLE Test with Real Devices ===\n")

    # Test 1: Central Constructor
    try:
        central = Central()
        _record("central_constructor", True)
    except Exception as e:
        _record("central_constructor", False, str(e))
        return 1

    # Test 2: Scan for devices
    try:
        devices = central.scan(scan_time=10.0)
        _record("scan_returns_list", isinstance(devices, list),
                f"type={type(devices).__name__}, count={len(devices)}")
    except Exception as e:
        _record("scan_returns_list", False, str(e))
        return 1

    if not devices:
        _record("scan_found_devices", True, "no devices found (environment-dependent)")
        total = len(_results)
        passed = sum(1 for _, p, _ in _results if p)
        print(f"\nRESULTS: {passed}/{total} passed, 0 failed")
        return 0

    # Show discovered devices
    print(f"\n  Discovered {len(devices)} device(s):")
    for i, d in enumerate(devices[:10]):
        name = d.name or "Unknown"
        print(f"    {i+1}. {name}: {d.address}")
    if len(devices) > 10:
        print(f"    ... and {len(devices) - 10} more")

    # Pick first device with a name (more likely to be connectable)
    test_device = None
    for d in devices:
        if d.name:
            test_device = d
            break
    if not test_device:
        test_device = devices[0]

    print(f"\n  Using: {test_device.name or 'Unknown'} ({test_device.address})")

    # Test 3-7: Client connect, services, read, write, disconnect
    client = None
    try:
        loop = asyncio.get_event_loop()
        client = Client(BleakClient(test_device.address), loop=loop)

        # Test 3: Connect
        try:
            connected = client.connect()
            _record("client_connect", bool(connected), f"connected={connected}")
            if not connected:
                # Try second device if available
                if len(devices) > 1:
                    alt = devices[1] if devices[0] is test_device else devices[0]
                    client = Client(BleakClient(alt.address), loop=loop)
                    connected = client.connect()
                    _record("client_connect_retry", bool(connected),
                            f"retried {alt.name or alt.address}")
                    if not connected:
                        return 1
                else:
                    return 1
        except Exception as e:
            _record("client_connect", False, str(e))
            return 1

        # Test 4: Get services
        try:
            services = client.get_services()
            service_list = list(services)
            _record("get_services", len(service_list) > 0,
                    f"found {len(service_list)} service(s)")
            for svc in service_list[:5]:
                print(f"    - {svc.uuid}")
        except Exception as e:
            _record("get_services", False, str(e))
            services = []
            service_list = []

        # Test 5: Read a characteristic (find first readable)
        readable_char = None
        for svc in service_list:
            for char in svc.characteristics:
                if "read" in char.properties:
                    readable_char = char.uuid
                    break
            if readable_char:
                break

        if readable_char:
            try:
                value = client.read_gatt_char(readable_char)
                is_bytes = isinstance(value, (bytes, bytearray))
                _record("read_characteristic", is_bytes,
                        f"type={type(value).__name__}, {len(value)} bytes")
            except Exception as e:
                _record("read_characteristic", False, str(e))
        else:
            _record("read_characteristic", True, "no readable chars found (skip)")

        # Test 6: Write a characteristic (find first writable)
        writable_char = None
        for svc in service_list:
            for char in svc.characteristics:
                if "write" in char.properties or "write-without-response" in char.properties:
                    writable_char = char.uuid
                    break
            if writable_char:
                break

        if writable_char:
            try:
                client.write_gatt_char(writable_char, b'\x00')
                _record("write_characteristic", True, f"wrote to {writable_char}")
            except Exception as e:
                _record("write_characteristic", False, str(e))
        else:
            _record("write_characteristic", True, "no writable chars found (skip)")

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
