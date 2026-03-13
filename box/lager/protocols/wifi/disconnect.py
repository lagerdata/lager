#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
WiFi disconnect implementation for box execution
This file should be copied to the lager_box_python container
"""
import subprocess
import json
import sys


def disconnect_wifi(interface='wlan0'):
    """Disconnect from the current WiFi network using nmcli."""
    try:
        result = subprocess.run(
            ['nmcli', 'dev', 'disconnect', interface],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return {
                'success': True,
                'message': result.stdout.strip() or f'Disconnected {interface}',
            }
        else:
            return {
                'success': False,
                'error': result.stderr.strip() or 'Disconnect failed',
            }
    except subprocess.TimeoutExpired:
        return {
            'success': False,
            'error': 'Disconnect timeout',
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'Disconnect failed: {str(e)}',
        }


def main():
    """Main function"""
    interface = 'wlan0'
    if len(sys.argv) >= 2:
        interface = sys.argv[1]

    print(f"Disconnecting WiFi on {interface}...")
    result = disconnect_wifi(interface)

    if result.get('success'):
        print(f"[OK] {result['message']}")
    else:
        print(f"[FAIL] {result.get('error', 'Unknown error')}")

    print(json.dumps(result))


if __name__ == "__main__":
    main()
