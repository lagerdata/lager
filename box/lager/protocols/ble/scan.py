#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
BLE scan implementation for box execution using existing lager BLE module
This file should be copied to the lager_box_python container
"""
import json
import sys
import asyncio
import os
import traceback

try:
    from .client import Central
except ImportError as e:
    print(json.dumps({"error": f"Could not import lager BLE module: {e}"}))
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

def main():
    """Main BLE scan function"""
    try:
        # Parse arguments
        scan_args = {}
        if len(sys.argv) >= 2:
            try:
                scan_args = json.loads(sys.argv[1])
            except (json.JSONDecodeError, ValueError):
                pass

        timeout = scan_args.get('timeout', 5.0)
        name_contains = scan_args.get('name_contains')
        name_exact = scan_args.get('name_exact')
        verbose = scan_args.get('verbose', False)

        print(f"Scanning for BLE devices for {timeout} seconds...")

        # Create BLE central and scan
        central = Central()
        devices = central.scan(scan_time=timeout)

        print(f"Found {len(devices)} device(s)")

        if not devices:
            print("No BLE devices found!")
            return

        # Apply filters
        if name_exact:
            devices = [d for d in devices if (d.name and d.name == name_exact)]

        if name_contains:
            devices = [d for d in devices if (d.name and name_contains.lower() in d.name.lower())]

        if not devices and (name_exact or name_contains):
            print("No devices found matching filter criteria!")
            return

        # Display results
        print("\n" + format_device_table(devices, verbose))

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
        print(json.dumps({"error": f"BLE scan failed: {str(e)}"}))
        sys.exit(1)

if __name__ == "__main__":
    main()