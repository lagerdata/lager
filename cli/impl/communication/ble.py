#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
BLE implementation for box execution - combines scan and connect functionality
This file should be copied to the python container
"""
import json
import sys
import asyncio
import os
import traceback
import warnings

# Suppress FutureWarning from bleak about deprecated BLEDevice attributes
# (BLEDevice.rssi, BLEDevice.metadata are deprecated in favor of AdvertisementData)
warnings.filterwarnings("ignore", category=FutureWarning)

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

try:
    from lager.protocols.ble import Central, Client
    from bleak import BleakClient
except ImportError as e:
    print(f"{RED}" + json.dumps({"error": f"Could not import BLE modules: {e}"}) + f"{RESET}")
    sys.exit(1)


def format_device_table(devices, verbose=False):
    """Format devices in a table similar to the CLI output"""
    if not devices:
        return "No devices found!"

    # Sort devices (addresses first, then named devices)
    sorted_devices = sorted(devices, key=lambda d: (d.name is None, d.name or d.address))

    # Create table
    lines = []
    if verbose:
        lines.append(f"{'Name':<20} {'Address':<17} {'RSSI':<6} {'UUIDs'}")
        lines.append("-" * 80)
    else:
        lines.append(f"{'Name':<20} {'Address':<17} {'RSSI'}")
        lines.append("-" * 50)

    for device in sorted_devices:
        device_name = device.name or device.address
        rssi = getattr(device, 'rssi', -100)

        if verbose:
            # Get UUIDs from device metadata if available
            uuids = []
            if hasattr(device, 'metadata') and device.metadata:
                uuids = device.metadata.get('uuids', [])
            uuids_str = ', '.join([str(uuid)[:8] + '...' for uuid in uuids[:3]])
            if len(uuids) > 3:
                uuids_str += f" (+{len(uuids)-3} more)"

            lines.append(f"{device_name:<20} {device.address:<17} {rssi:<6} {uuids_str}")
        else:
            lines.append(f"{device_name:<20} {device.address:<17} {rssi}")

    return '\n'.join(lines)


def ble_scan(scan_args):
    """BLE scan function"""
    try:
        timeout = scan_args.get('timeout', 5.0)
        name_contains = scan_args.get('name_contains')
        name_exact = scan_args.get('name_exact')
        verbose = scan_args.get('verbose', False)

        print(f"{GREEN}Scanning for BLE devices for {timeout} seconds...{RESET}")

        # Create BLE central and scan
        central = Central()
        devices = central.scan(scan_time=timeout)

        print(f"{GREEN}Found {len(devices)} device(s){RESET}")

        if not devices:
            print(f"{RED}No BLE devices found!{RESET}")
            return

        # Apply filters
        if name_exact:
            devices = [d for d in devices if (d.name and d.name == name_exact)]

        if name_contains:
            devices = [d for d in devices if (d.name and name_contains.lower() in d.name.lower())]

        if not devices and (name_exact or name_contains):
            print(f"{RED}No devices found matching filter criteria!{RESET}")
            return

        # Display results
        print(f"\n{GREEN}" + format_device_table(devices, verbose) + f"{RESET}")

        # Also output structured data for programmatic use
        device_data = []
        for device in devices:
            device_info = {
                'name': device.name or device.address,
                'address': device.address,
                'rssi': getattr(device, 'rssi', -100)
            }

            if verbose and hasattr(device, 'metadata'):
                device_info['uuids'] = device.metadata.get('uuids', [])

            device_data.append(device_info)

        print(f"\nJSON Output:")
        print(json.dumps(device_data, indent=2))

    except Exception as e:
        traceback.print_exc()
        print(f"{RED}" + json.dumps({"error": f"BLE scan failed: {str(e)}"}) + f"{RESET}")
        sys.exit(1)


async def connect_to_device(address, timeout=10):
    """Connect to BLE device and get basic info"""
    try:
        print(f"{GREEN}Connecting to BLE device: {address}{RESET}")

        async with BleakClient(address) as client:
            if await client.is_connected():
                print(f"{GREEN}[OK] Connected to {address}{RESET}")

                # Get device info
                device_info = {
                    "address": address,
                    "connected": True,
                    "services": []
                }

                # Get services
                try:
                    services = await client.get_services()
                    for service in services:
                        service_info = {
                            "uuid": str(service.uuid),
                            "description": service.description,
                            "characteristics": []
                        }

                        for char in service.characteristics:
                            char_info = {
                                "uuid": str(char.uuid),
                                "description": char.description,
                                "properties": char.properties
                            }
                            service_info["characteristics"].append(char_info)

                        device_info["services"].append(service_info)

                    print(f"{GREEN}Found {len(services)} services{RESET}")

                except Exception as e:
                    print(f"Warning: Could not enumerate services: {e}")

                return device_info

            else:
                return {
                    "address": address,
                    "connected": False,
                    "error": "Failed to establish connection"
                }

    except Exception as e:
        return {
            "address": address,
            "connected": False,
            "error": f"Connection failed: {str(e)}"
        }


def ble_connect(connect_args):
    """BLE connect function"""
    address = connect_args.get('address')

    if not address:
        print(f"{RED}" + json.dumps({"error": "Missing BLE device address"}) + f"{RESET}")
        sys.exit(1)

    try:
        # Validate address format
        if len(address) != 17 or address.count(':') != 5:
            print(f"{RED}" + json.dumps({"error": "Invalid BLE address format. Use XX:XX:XX:XX:XX:XX"}) + f"{RESET}")
            sys.exit(1)

        # Run connection
        result = asyncio.run(connect_to_device(address))

        if result.get('connected'):
            print(f"\n{GREEN}Connection successful!{RESET}")
            print(f"{GREEN}Device: {result['address']}{RESET}")
            print(f"{GREEN}Services: {len(result.get('services', []))}{RESET}")

            # Show first few services
            services = result.get('services', [])
            if services:
                print(f"\n{GREEN}Services found:{RESET}")
                for i, service in enumerate(services[:3]):  # Show first 3
                    print(f"{GREEN}  {i+1}. {service['uuid'][:8]}... ({len(service['characteristics'])} characteristics){RESET}")
                if len(services) > 3:
                    print(f"{GREEN}  ... and {len(services)-3} more services{RESET}")

        else:
            print(f"{RED}[FAIL] Connection failed: {result.get('error', 'Unknown error')}{RESET}")

        print(f"\nJSON Output:")
        print(json.dumps(result, indent=2))

        if not result.get('connected'):
            sys.exit(1)

    except Exception as e:
        traceback.print_exc()
        error_result = {"error": f"BLE connection failed: {str(e)}"}
        print(f"{RED}" + json.dumps(error_result) + f"{RESET}")
        sys.exit(1)


def ble_info(info_args):
    """BLE info function - get device information (services and characteristics)"""
    address = info_args.get('address')

    if not address:
        print(f"{RED}" + json.dumps({"error": "Missing BLE device address"}) + f"{RESET}")
        sys.exit(1)

    try:
        # Validate address format
        if len(address) != 17 or address.count(':') != 5:
            print(f"{RED}" + json.dumps({"error": "Invalid BLE address format. Use XX:XX:XX:XX:XX:XX"}) + f"{RESET}")
            sys.exit(1)

        print(f"{GREEN}Getting info for BLE device: {address}{RESET}")

        # Run connection to get info
        result = asyncio.run(connect_to_device(address))

        if result.get('connected'):
            print(f"\n{GREEN}Device Information:{RESET}")
            print(f"{GREEN}Address: {result['address']}{RESET}")

            services = result.get('services', [])
            print(f"{GREEN}Services: {len(services)}{RESET}")

            if services:
                print(f"\n{GREEN}Services:{RESET}")
                for i, service in enumerate(services):
                    desc = service.get('description') or 'Unknown Service'
                    print(f"{GREEN}  {i+1}. {service['uuid']}{RESET}")
                    print(f"{GREEN}     Description: {desc}{RESET}")
                    print(f"{GREEN}     Characteristics: {len(service['characteristics'])}{RESET}")
                    for char in service['characteristics'][:3]:
                        char_desc = char.get('description') or 'Unknown'
                        props = ', '.join(char.get('properties', []))
                        print(f"{GREEN}       - {char['uuid'][:8]}... [{props}]{RESET}")
                    if len(service['characteristics']) > 3:
                        print(f"{GREEN}       ... and {len(service['characteristics'])-3} more{RESET}")

        else:
            print(f"{RED}[FAIL] Failed to get device info: {result.get('error', 'Unknown error')}{RESET}")

        print(f"\nJSON Output:")
        print(json.dumps(result, indent=2))

        if not result.get('connected'):
            sys.exit(1)

    except Exception as e:
        traceback.print_exc()
        error_result = {"error": f"BLE info failed: {str(e)}"}
        print(f"{RED}" + json.dumps(error_result) + f"{RESET}")
        sys.exit(1)


def ble_disconnect(disconnect_args):
    """BLE disconnect function - disconnect from a device"""
    address = disconnect_args.get('address')

    if not address:
        print(f"{RED}" + json.dumps({"error": "Missing BLE device address"}) + f"{RESET}")
        sys.exit(1)

    try:
        # Validate address format
        if len(address) != 17 or address.count(':') != 5:
            print(f"{RED}" + json.dumps({"error": "Invalid BLE address format. Use XX:XX:XX:XX:XX:XX"}) + f"{RESET}")
            sys.exit(1)

        # BLE connections via bleak are transient - they automatically disconnect
        # when the async context exits. This command is mainly for explicit user intent
        # and to clean up any lingering connection state.
        print(f"{GREEN}Disconnecting from BLE device: {address}{RESET}")

        # Attempt to connect and immediately disconnect to ensure clean state
        async def disconnect_device(addr):
            try:
                async with BleakClient(addr, timeout=5.0) as client:
                    if await client.is_connected():
                        await client.disconnect()
                        return {"address": addr, "disconnected": True}
                    else:
                        return {"address": addr, "disconnected": True, "note": "Device was not connected"}
            except Exception as e:
                # If we can't connect, the device is already disconnected
                return {"address": addr, "disconnected": True, "note": f"Device not reachable: {str(e)}"}

        result = asyncio.run(disconnect_device(address))

        if result.get('disconnected'):
            print(f"{GREEN}[OK] Disconnected from {address}{RESET}")
            if result.get('note'):
                print(f"{GREEN}  Note: {result['note']}{RESET}")
        else:
            print(f"{RED}[FAIL] Disconnect failed{RESET}")

        print(f"\nJSON Output:")
        print(json.dumps(result, indent=2))

    except Exception as e:
        traceback.print_exc()
        error_result = {"error": f"BLE disconnect failed: {str(e)}"}
        print(f"{RED}" + json.dumps(error_result) + f"{RESET}")
        sys.exit(1)


def main():
    """Main BLE function - dispatches to scan, connect, info, or disconnect based on arguments"""
    try:
        # Parse arguments
        if len(sys.argv) < 2:
            print(f"{RED}Error: Missing command arguments{RESET}")
            sys.exit(1)

        args = json.loads(sys.argv[1])
        action = args.get('action', 'scan')

        if action == 'scan':
            ble_scan(args)
        elif action == 'connect':
            ble_connect(args)
        elif action == 'info':
            ble_info(args)
        elif action == 'disconnect':
            ble_disconnect(args)
        else:
            print(f"{RED}Error: Unknown action '{action}'. Use 'scan', 'connect', 'info', or 'disconnect'{RESET}")
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
