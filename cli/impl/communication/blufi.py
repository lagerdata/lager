#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
BluFi implementation for box execution - ESP32 WiFi provisioning over BLE.
This file is uploaded to the box and executed via lager python.
"""
import json
import sys
import time
import traceback

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

try:
    from lager.blufi import BlufiClient, OP_MODE_STA, STA_CONN_SUCCESS
except ImportError as e:
    print(f"{RED}" + json.dumps({"error": f"Could not import BluFi modules: {e}"}) + f"{RESET}")
    sys.exit(1)

# BluFi BLE service UUID for scan filtering
BLUFI_SERVICE_UUID = "0000ffff-0000-1000-8000-00805f9b34fb"

# Human-readable names for STA connection status
STA_CONN_NAMES = {
    0x00: "Connected",
    0x01: "Failed",
    0x02: "Connecting",
    0x03: "No IP",
}

# Human-readable names for op mode
OP_MODE_NAMES = {
    0x00: "NULL",
    0x01: "STA",
    0x02: "SoftAP",
    0x03: "STA+SoftAP",
}


def _connect_and_secure(args):
    """Connect to a BluFi device by name and negotiate security.

    Returns the connected BlufiClient instance.
    Caller is responsible for calling client._cleanup() in a finally block.
    """
    device_name = args.get('device_name')
    timeout = args.get('timeout', 20.0)

    if not device_name:
        raise ValueError("Missing device_name")

    client = BlufiClient()

    print(f"{GREEN}Connecting to BluFi device: {device_name}{RESET}")
    if not client.connectByName(device_name, timeout=timeout):
        client._cleanup()
        raise RuntimeError(f"Failed to connect to '{device_name}' within {timeout}s")

    print(f"{GREEN}[OK] Connected to {device_name}{RESET}")

    print(f"{GREEN}Negotiating security...{RESET}")
    client.negotiateSecurity()
    print(f"{GREEN}[OK] Security negotiated{RESET}")

    return client


def blufi_scan(args):
    """Scan for BluFi-capable BLE devices."""
    try:
        timeout = args.get('timeout', 10.0)
        name_contains = args.get('name_contains')

        print(f"{GREEN}Scanning for BluFi devices for {timeout} seconds...{RESET}")

        from bleak import BleakScanner

        import asyncio

        async def _scan():
            devices_and_data = []
            scanner = BleakScanner()
            devices = await scanner.discover(timeout=timeout)
            for d in devices:
                # Filter by BluFi service UUID or name substring
                adv = d.metadata.get('uuids', []) if hasattr(d, 'metadata') and d.metadata else []
                has_blufi_uuid = BLUFI_SERVICE_UUID in adv

                name_match = True
                if name_contains:
                    name_match = d.name and name_contains.lower() in d.name.lower()

                if has_blufi_uuid or (name_contains and name_match):
                    devices_and_data.append(d)

            return devices_and_data

        devices = asyncio.run(_scan())

        print(f"{GREEN}Found {len(devices)} BluFi device(s){RESET}")

        if not devices:
            print(f"{RED}No BluFi devices found!{RESET}")
            return

        # Display table
        print(f"\n{GREEN}{'Name':<30} {'Address':<17} {'RSSI'}{RESET}")
        print(f"{GREEN}{'-' * 55}{RESET}")
        for d in sorted(devices, key=lambda x: (x.name is None, x.name or x.address)):
            name = d.name or d.address
            rssi = getattr(d, 'rssi', -100)
            print(f"{GREEN}{name:<30} {d.address:<17} {rssi}{RESET}")

        # JSON output
        device_data = []
        for d in devices:
            device_data.append({
                'name': d.name or d.address,
                'address': d.address,
                'rssi': getattr(d, 'rssi', -100),
            })

        print(f"\nJSON Output:")
        print(json.dumps(device_data, indent=2))

    except Exception as e:
        traceback.print_exc()
        print(f"{RED}" + json.dumps({"error": f"BluFi scan failed: {str(e)}"}) + f"{RESET}")
        sys.exit(1)


def blufi_connect(args):
    """Connect to a BluFi device and retrieve version + status."""
    client = None
    try:
        client = _connect_and_secure(args)

        # Get version
        client.requestVersion()
        time.sleep(0.5)
        version = client.getVersion()

        # Get status
        client.requestDeviceStatus()
        time.sleep(0.5)
        state = client.getWifiState()

        op_mode_name = OP_MODE_NAMES.get(state['opMode'], f"Unknown({state['opMode']})")
        sta_conn_name = STA_CONN_NAMES.get(state['staConn'], f"Unknown({state['staConn']})")

        print(f"\n{GREEN}Device Info:{RESET}")
        print(f"{GREEN}  Version:    {version or 'N/A'}{RESET}")
        print(f"{GREEN}  Op Mode:    {op_mode_name}{RESET}")
        print(f"{GREEN}  STA Conn:   {sta_conn_name}{RESET}")
        print(f"{GREEN}  SoftAP:     {state['softAPConn']}{RESET}")

        result = {
            'device_name': args.get('device_name'),
            'version': version,
            'opMode': state['opMode'],
            'opModeName': op_mode_name,
            'staConn': state['staConn'],
            'staConnName': sta_conn_name,
            'softAPConn': state['softAPConn'],
        }

        print(f"\nJSON Output:")
        print(json.dumps(result, indent=2))

    except Exception as e:
        traceback.print_exc()
        print(f"{RED}" + json.dumps({"error": f"BluFi connect failed: {str(e)}"}) + f"{RESET}")
        sys.exit(1)
    finally:
        if client:
            client._cleanup()


def blufi_provision(args):
    """Provision WiFi credentials to a BluFi device."""
    client = None
    try:
        ssid = args.get('ssid')
        password = args.get('password')

        if not ssid or not password:
            raise ValueError("Missing ssid or password")

        client = _connect_and_secure(args)

        print(f"{GREEN}Setting device to STA mode...{RESET}")
        client.postDeviceMode(OP_MODE_STA)
        time.sleep(0.5)

        print(f"{GREEN}Sending WiFi credentials for '{ssid}'...{RESET}")
        client.postStaWifiInfo({"ssid": ssid, "pass": password})

        # Wait for the device to attempt connection
        print(f"{GREEN}Waiting for device to connect to WiFi...{RESET}")
        time.sleep(5)

        # Check status
        client.requestDeviceStatus()
        time.sleep(1)
        state = client.getWifiState()

        sta_conn = state['staConn']
        sta_conn_name = STA_CONN_NAMES.get(sta_conn, f"Unknown({sta_conn})")

        if sta_conn == STA_CONN_SUCCESS:
            print(f"\n{GREEN}[OK] Device connected to '{ssid}' successfully!{RESET}")
        else:
            print(f"\n{RED}[WARN] Device connection status: {sta_conn_name}{RESET}")

        result = {
            'device_name': args.get('device_name'),
            'ssid': ssid,
            'staConn': sta_conn,
            'staConnName': sta_conn_name,
            'success': sta_conn == STA_CONN_SUCCESS,
        }

        print(f"\nJSON Output:")
        print(json.dumps(result, indent=2))

        if sta_conn != STA_CONN_SUCCESS:
            sys.exit(1)

    except Exception as e:
        traceback.print_exc()
        print(f"{RED}" + json.dumps({"error": f"BluFi provision failed: {str(e)}"}) + f"{RESET}")
        sys.exit(1)
    finally:
        if client:
            client._cleanup()


def blufi_wifi_scan(args):
    """Scan for WiFi networks via a BluFi device."""
    client = None
    try:
        scan_timeout = args.get('scan_timeout', 15.0)

        client = _connect_and_secure(args)

        print(f"{GREEN}Requesting WiFi scan (timeout={scan_timeout}s)...{RESET}")
        client.requestDeviceScan(timeout=scan_timeout)
        networks = client.getSSIDList()

        print(f"{GREEN}Found {len(networks)} network(s){RESET}")

        if not networks:
            print(f"{RED}No WiFi networks found!{RESET}")
            result = {'device_name': args.get('device_name'), 'networks': []}
            print(f"\nJSON Output:")
            print(json.dumps(result, indent=2))
            return

        # Display table
        print(f"\n{GREEN}{'SSID':<32} {'RSSI'}{RESET}")
        print(f"{GREEN}{'-' * 40}{RESET}")
        for net in sorted(networks, key=lambda n: n.get('rssi', -100), reverse=True):
            ssid = net.get('ssid', '???')
            rssi = net.get('rssi', -100)
            print(f"{GREEN}{ssid:<32} {rssi}{RESET}")

        result = {
            'device_name': args.get('device_name'),
            'networks': networks,
        }

        print(f"\nJSON Output:")
        print(json.dumps(result, indent=2))

    except Exception as e:
        traceback.print_exc()
        print(f"{RED}" + json.dumps({"error": f"BluFi WiFi scan failed: {str(e)}"}) + f"{RESET}")
        sys.exit(1)
    finally:
        if client:
            client._cleanup()


def blufi_status(args):
    """Get WiFi connection status from a BluFi device."""
    client = None
    try:
        client = _connect_and_secure(args)

        client.requestDeviceStatus()
        time.sleep(0.5)
        state = client.getWifiState()

        op_mode_name = OP_MODE_NAMES.get(state['opMode'], f"Unknown({state['opMode']})")
        sta_conn_name = STA_CONN_NAMES.get(state['staConn'], f"Unknown({state['staConn']})")

        print(f"\n{GREEN}WiFi Status:{RESET}")
        print(f"{GREEN}  Op Mode:    {op_mode_name}{RESET}")
        print(f"{GREEN}  STA Conn:   {sta_conn_name}{RESET}")
        print(f"{GREEN}  SoftAP:     {state['softAPConn']}{RESET}")

        result = {
            'device_name': args.get('device_name'),
            'opMode': state['opMode'],
            'opModeName': op_mode_name,
            'staConn': state['staConn'],
            'staConnName': sta_conn_name,
            'softAPConn': state['softAPConn'],
        }

        print(f"\nJSON Output:")
        print(json.dumps(result, indent=2))

    except Exception as e:
        traceback.print_exc()
        print(f"{RED}" + json.dumps({"error": f"BluFi status failed: {str(e)}"}) + f"{RESET}")
        sys.exit(1)
    finally:
        if client:
            client._cleanup()


def blufi_version(args):
    """Get firmware version from a BluFi device."""
    client = None
    try:
        client = _connect_and_secure(args)

        client.requestVersion()
        time.sleep(0.5)
        version = client.getVersion()

        print(f"\n{GREEN}Firmware Version: {version or 'N/A'}{RESET}")

        result = {
            'device_name': args.get('device_name'),
            'version': version,
        }

        print(f"\nJSON Output:")
        print(json.dumps(result, indent=2))

    except Exception as e:
        traceback.print_exc()
        print(f"{RED}" + json.dumps({"error": f"BluFi version failed: {str(e)}"}) + f"{RESET}")
        sys.exit(1)
    finally:
        if client:
            client._cleanup()


def main():
    """Main BluFi function - dispatches based on action argument."""
    try:
        if len(sys.argv) < 2:
            print(f"{RED}Error: Missing command arguments{RESET}")
            sys.exit(1)

        args = json.loads(sys.argv[1])
        action = args.get('action', 'scan')

        if action == 'scan':
            blufi_scan(args)
        elif action == 'connect':
            blufi_connect(args)
        elif action == 'provision':
            blufi_provision(args)
        elif action == 'wifi_scan':
            blufi_wifi_scan(args)
        elif action == 'status':
            blufi_status(args)
        elif action == 'version':
            blufi_version(args)
        else:
            print(f"{RED}Error: Unknown action '{action}'. "
                  f"Use 'scan', 'connect', 'provision', 'wifi_scan', 'status', or 'version'{RESET}")
            sys.exit(1)

    except json.JSONDecodeError as e:
        print(f"{RED}Error: Invalid JSON arguments: {e}{RESET}")
        sys.exit(1)
    except Exception as e:
        traceback.print_exc()
        print(f"{RED}Error: {str(e)}{RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()
