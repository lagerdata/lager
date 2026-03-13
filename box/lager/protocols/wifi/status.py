#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
WiFi status implementation for box execution
This file should be copied to the lager_box_python container
"""
import subprocess
import json
import re

def get_wifi_status():
    """Get WiFi status using system commands"""
    try:
        # Try to get wireless interfaces
        result = subprocess.run(['iwconfig'], capture_output=True, text=True)

        interfaces = {}
        current_interface = None

        for line in result.stdout.split('\n'):
            line = line.strip()
            if not line:
                continue

            # Look for interface lines
            if 'IEEE 802.11' in line:
                # Extract interface name (first word)
                interface_name = line.split()[0]
                current_interface = interface_name
                interfaces[interface_name] = {
                    'interface': interface_name,
                    'ssid': 'Not Connected',
                    'state': 'Disconnected'
                }

                # Parse ESSID from same line
                essid_match = re.search(r'ESSID:"([^"]*)"', line)
                if essid_match:
                    essid = essid_match.group(1)
                    if essid and essid != "":
                        interfaces[interface_name]['ssid'] = essid
                        interfaces[current_interface]['state'] = 'Connected'

            elif current_interface and line.startswith('Access Point:'):
                # Check connection status
                if 'Not-Associated' in line:
                    interfaces[current_interface]['state'] = 'Disconnected'
                    interfaces[current_interface]['ssid'] = 'Not Connected'
                else:
                    interfaces[current_interface]['state'] = 'Connected'

        # If no interfaces found, try a different approach
        if not interfaces:
            # Try nmcli if available
            try:
                result = subprocess.run(['nmcli', 'dev', 'wifi'], capture_output=True, text=True)
                if result.returncode == 0:
                    interfaces['wlan0'] = {
                        'interface': 'wlan0',
                        'ssid': 'Available (nmcli detected)',
                        'state': 'Available'
                    }
            except Exception:
                # Fallback
                interfaces['wlan0'] = {
                    'interface': 'wlan0',
                    'ssid': 'Unknown',
                    'state': 'Interface detection failed'
                }

        return interfaces

    except Exception as e:
        return {
            'error': {
                'interface': 'error',
                'ssid': f'Error: {str(e)}',
                'state': 'Failed'
            }
        }

def main():
    """Main function to output WiFi status"""
    try:
        wifi_status = get_wifi_status()

        print("WiFi Interface Status:")
        print("=" * 50)

        for interface_name, info in wifi_status.items():
            print(f"Interface: {info['interface']}")
            print(f"    SSID:  {info['ssid']}")
            print(f"    State: {info['state']}")
            print()

        # Also output JSON for programmatic use
        print("\n" + "="*50)
        print("JSON Output:")
        print(json.dumps(wifi_status, indent=2))

    except Exception as e:
        print(f"Error getting WiFi status: {e}")
        print(json.dumps({"error": str(e)}))

if __name__ == "__main__":
    main()