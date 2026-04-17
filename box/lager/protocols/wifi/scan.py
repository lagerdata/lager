#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
WiFi scan implementation for box execution
This file should be copied to the lager_box_python container
"""
import subprocess
import json
import re
import sys

_IS_DARWIN = sys.platform == "darwin"


def scan_wifi_networks(interface='wlan0'):
    """Scan for available WiFi networks"""
    if _IS_DARWIN:
        return {
            'error': 'not_supported_on_macos',
            'message': 'WiFi station control is not supported on the macOS box (v1).',
            'networks': [],
        }
    try:
        # Try iwlist scan first
        result = subprocess.run(['iwlist', interface, 'scan'], capture_output=True, text=True)

        if result.returncode != 0:
            # Try nmcli as fallback
            result = subprocess.run(['nmcli', 'dev', 'wifi'], capture_output=True, text=True)
            if result.returncode == 0:
                return parse_nmcli_output(result.stdout)
            else:
                return {"error": "Could not scan for networks"}

        return parse_iwlist_output(result.stdout)

    except Exception as e:
        return {"error": f"WiFi scan failed: {str(e)}"}

def parse_iwlist_output(output):
    """Parse iwlist scan output"""
    networks = []
    current_network = {}

    for line in output.split('\n'):
        line = line.strip()

        if 'Cell' in line and 'Address:' in line:
            # Start of new network entry
            if current_network:
                networks.append(current_network)
            current_network = {'address': line.split('Address: ')[1]}

        elif 'ESSID:' in line:
            essid_match = re.search(r'ESSID:"([^"]*)"', line)
            if essid_match:
                current_network['ssid'] = essid_match.group(1)

        elif 'Signal level=' in line:
            signal_match = re.search(r'Signal level=([^\s]+)', line)
            if signal_match:
                signal_str = signal_match.group(1)
                try:
                    # Convert to approximate percentage
                    if 'dBm' in signal_str:
                        dbm = int(signal_str.replace('dBm', ''))
                        strength = min(100, max(0, (dbm + 100) * 2))
                    else:
                        strength = 50  # Default
                    current_network['strength'] = strength
                except (ValueError, TypeError):
                    current_network['strength'] = 50

        elif 'Encryption key:' in line:
            if 'off' in line:
                current_network['security'] = 'Open'
            else:
                current_network['security'] = 'Secured'

    # Add the last network
    if current_network:
        networks.append(current_network)

    return {"access_points": networks}

def parse_nmcli_output(output):
    """Parse nmcli dev wifi output"""
    networks = []
    lines = output.strip().split('\n')[1:]  # Skip header

    for line in lines:
        parts = line.split()
        if len(parts) >= 6:
            try:
                network = {
                    'ssid': parts[1] if parts[1] != '--' else 'Hidden',
                    'address': parts[0],
                    'strength': int(parts[5]) if parts[5].isdigit() else 50,
                    'security': 'Secured' if parts[7] != '--' else 'Open'
                }
                networks.append(network)
            except (IndexError, ValueError):
                continue

    return {"access_points": networks}

def format_networks_table(networks):
    """Format networks in a table"""
    if not networks:
        return "No networks found!"

    lines = []
    lines.append(f"{'SSID':<25} {'Security':<10} {'Strength'}")
    lines.append("-" * 50)

    # Sort by signal strength (descending)
    sorted_networks = sorted(networks, key=lambda x: x.get('strength', 0), reverse=True)

    for net in sorted_networks:
        ssid = net.get('ssid', 'Unknown')[:24]
        security = net.get('security', 'Unknown')
        strength = net.get('strength', 0)
        lines.append(f"{ssid:<25} {security:<10} {strength}%")

    return '\n'.join(lines)

def main():
    """Main function"""
    try:
        interface = 'wlan0'
        if len(sys.argv) >= 2:
            interface = sys.argv[1]

        print(f"Scanning for WiFi networks on {interface}...")

        result = scan_wifi_networks(interface)

        if 'error' in result:
            print(f"Error: {result['error']}")
            print(json.dumps(result))
            return

        networks = result.get('access_points', [])

        print(f"\nFound {len(networks)} network(s):")
        print(format_networks_table(networks))

        print(f"\nJSON Output:")
        print(json.dumps(result, indent=2))

    except Exception as e:
        error_result = {"error": f"WiFi scan failed: {str(e)}"}
        print(json.dumps(error_result))

# Alias for compatibility
scan_wifi = scan_wifi_networks

if __name__ == "__main__":
    main()