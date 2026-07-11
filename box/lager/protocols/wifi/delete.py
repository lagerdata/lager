#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
WiFi saved-connection deletion for box execution.

Shared by the :9000 /wifi/command handler; logic matches the old CLI impl
script (nmcli connection delete).
"""
import subprocess


def delete_wifi_connection(connection_name):
    """Delete a saved WiFi connection by name (usually the SSID).

    Returns {"connection": name, "deleted": bool, "error": optional str}.
    """
    try:
        result = subprocess.run(
            ['nmcli', 'connection', 'delete', connection_name],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return {"connection": connection_name, "deleted": False,
                "error": "Operation timeout"}
    except Exception as e:
        return {"connection": connection_name, "deleted": False,
                "error": str(e)}

    if result.returncode == 0:
        return {"connection": connection_name, "deleted": True}

    error_msg = result.stderr.strip() or result.stdout.strip() or "Delete failed"
    if "not found" in error_msg.lower() or "no such" in error_msg.lower():
        error_msg = "Connection not found"
    return {"connection": connection_name, "deleted": False, "error": error_msg}
