#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
BLE connect implementation for box execution
This file should be copied to the lager_box_python container
"""
import json
import sys
import asyncio
import traceback

try:
    from .client import Central, Client
    from bleak import BleakClient
except ImportError as e:
    print(json.dumps({"error": f"Could not import BLE modules: {e}"}))
    sys.exit(1)

async def connect_to_device(address, timeout=10):
    """Connect to BLE device and get basic info"""
    try:
        print(f"Connecting to BLE device: {address}")

        async with BleakClient(address) as client:
            if await client.is_connected():
                print(f"[OK] Connected to {address}")

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

                    print(f"Found {len(services)} services")

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

def main():
    """Main BLE connect function"""
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Missing BLE device address"}))
        sys.exit(1)

    try:
        address = sys.argv[1]

        # Validate address format
        if len(address) != 17 or address.count(':') != 5:
            print(json.dumps({"error": "Invalid BLE address format. Use XX:XX:XX:XX:XX:XX"}))
            sys.exit(1)

        # Run connection
        result = asyncio.run(connect_to_device(address))

        if result.get('connected'):
            print(f"\nConnection successful!")
            print(f"Device: {result['address']}")
            print(f"Services: {len(result.get('services', []))}")

            # Show first few services
            services = result.get('services', [])
            if services:
                print(f"\nServices found:")
                for i, service in enumerate(services[:3]):  # Show first 3
                    print(f"  {i+1}. {service['uuid'][:8]}... ({len(service['characteristics'])} characteristics)")
                if len(services) > 3:
                    print(f"  ... and {len(services)-3} more services")

        else:
            print(f"[FAIL] Connection failed: {result.get('error', 'Unknown error')}")

        print(f"\nJSON Output:")
        print(json.dumps(result, indent=2))

        if not result.get('connected'):
            sys.exit(1)

    except Exception as e:
        traceback.print_exc()
        error_result = {"error": f"BLE connection failed: {str(e)}"}
        print(json.dumps(error_result))
        sys.exit(1)

if __name__ == "__main__":
    main()