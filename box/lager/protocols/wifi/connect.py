#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
WiFi connect implementation for box execution
This file should be copied to the lager_box_python container
"""
import subprocess
import json
import sys
import tempfile
import os

_IS_DARWIN = sys.platform == "darwin"


def connect_to_wifi(ssid, password, interface='wlan0'):
    """Connect to WiFi network using wpa_supplicant"""
    if _IS_DARWIN:
        return {
            'success': False,
            'error': 'not_supported_on_macos',
            'message': 'WiFi station control is not supported on the macOS box (v1).',
        }
    try:
        # Create wpa_supplicant configuration
        if password:
            # Secured network
            wpa_config = f"""
network={{
    ssid="{ssid}"
    psk="{password}"
    key_mgmt=WPA-PSK
}}
"""
        else:
            # Open network
            wpa_config = f"""
network={{
    ssid="{ssid}"
    key_mgmt=NONE
}}
"""

        # Write temporary config file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
            config_path = f.name
            f.write(wpa_config)

        try:
            # Try using nmcli first (more modern)
            if password:
                cmd = ['nmcli', 'dev', 'wifi', 'connect', ssid, 'password', password]
            else:
                cmd = ['nmcli', 'dev', 'wifi', 'connect', ssid]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode == 0:
                return {
                    "success": True,
                    "message": f"Connected to {ssid}",
                    "method": "nmcli",
                    "interface": interface
                }

            # If nmcli fails, try wpa_supplicant
            print("nmcli failed, trying wpa_supplicant...")

            # Kill existing wpa_supplicant processes
            subprocess.run(['pkill', 'wpa_supplicant'], capture_output=True)

            # Start wpa_supplicant
            wpa_cmd = [
                'wpa_supplicant', '-B', '-i', interface,
                '-c', config_path, '-D', 'wext'
            ]

            result = subprocess.run(wpa_cmd, capture_output=True, text=True, timeout=15)

            if result.returncode != 0:
                return {
                    "success": False,
                    "error": f"wpa_supplicant failed: {result.stderr}",
                    "method": "wpa_supplicant"
                }

            # Wait a bit for association
            import time
            time.sleep(5)

            # Get DHCP lease
            dhcp_result = subprocess.run(['dhclient', interface], capture_output=True, text=True, timeout=15)

            return {
                "success": True,
                "message": f"Connected to {ssid} via wpa_supplicant",
                "method": "wpa_supplicant",
                "interface": interface,
                "dhcp_status": "requested" if dhcp_result.returncode == 0 else "failed"
            }

        finally:
            # Clean up config file
            try:
                os.unlink(config_path)
            except OSError:
                pass

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Connection timeout"
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Connection failed: {str(e)}"
        }

def main():
    """Main function"""
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Missing WiFi configuration"}))
        sys.exit(1)

    try:
        wifi_config = json.loads(sys.argv[1])
        ssid = wifi_config.get('ssid')
        password = wifi_config.get('password', '')
        interface = wifi_config.get('interface', 'wlan0')

        if not ssid:
            print(json.dumps({"error": "SSID is required"}))
            sys.exit(1)

        print(f"Connecting to WiFi network: {ssid}")
        if password:
            print("Using password authentication")
        else:
            print("Connecting to open network")

        result = connect_to_wifi(ssid, password, interface)

        if result.get('success'):
            print(f"[OK] {result['message']}")
            print(json.dumps(result))
        else:
            print(f"[FAIL] Connection failed: {result.get('error', 'Unknown error')}")
            print(json.dumps(result))
            sys.exit(1)

    except Exception as e:
        error_result = {"error": f"WiFi connection failed: {str(e)}"}
        print(json.dumps(error_result))
        sys.exit(1)

if __name__ == "__main__":
    main()