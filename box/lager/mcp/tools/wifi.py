# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for box WiFi STA management.

These tools manage the box's own WiFi connection (wlan0), not DUT WiFi.
Since there is no instrument net, the tools call system utilities
(iwconfig, nmcli) directly via subprocess.
"""

import json
import re
import subprocess

from ..server import mcp


def _interface_status(interface: str = "wlan0") -> dict:
    """Read WiFi status for a single interface via iwconfig."""
    try:
        result = subprocess.run(
            ["iwconfig", interface], capture_output=True, text=True, timeout=5
        )
        output = result.stdout
        essid_match = re.search(r'ESSID:"([^"]*)"', output)
        essid = essid_match.group(1) if essid_match else None

        if not essid or "Not-Associated" in output:
            return {"interface": interface, "ssid": None, "state": "disconnected"}

        signal_match = re.search(r"Signal level=([^\s]+)", output)
        info: dict = {
            "interface": interface,
            "ssid": essid,
            "state": "connected",
        }
        if signal_match:
            info["signal"] = signal_match.group(1)
        return info
    except Exception as exc:
        return {"interface": interface, "state": "error", "error": str(exc)}


@mcp.tool()
def wifi_status(interface: str = "wlan0") -> str:
    """Show WiFi connection status on the box.

    Returns the current WiFi connection state, SSID, and signal strength.

    Args:
        interface: WiFi interface name (default: 'wlan0')
    """
    info = _interface_status(interface)
    return json.dumps({"status": "ok", **info})


@mcp.tool()
def wifi_scan(interface: str = "wlan0") -> str:
    """Scan for available WiFi access points.

    Args:
        interface: WiFi interface name (default: 'wlan0')
    """
    try:
        result = subprocess.run(
            ["iwlist", interface, "scan"], capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["nmcli", "dev", "wifi"], capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                return json.dumps({"status": "error", "error": "Could not scan for networks"})

        networks = []
        current: dict = {}
        for line in result.stdout.split("\n"):
            line = line.strip()
            if "Cell" in line and "Address:" in line:
                if current:
                    networks.append(current)
                current = {"address": line.split("Address: ")[1]}
            elif "ESSID:" in line:
                m = re.search(r'ESSID:"([^"]*)"', line)
                if m:
                    current["ssid"] = m.group(1)
            elif "Signal level=" in line:
                m = re.search(r"Signal level=([^\s]+)", line)
                if m:
                    current["signal"] = m.group(1)
            elif "Encryption key:" in line:
                current["security"] = "open" if "off" in line else "secured"
        if current:
            networks.append(current)

        return json.dumps({"status": "ok", "interface": interface, "access_points": networks})
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)})


@mcp.tool()
def wifi_connect(ssid: str, password: str = "", interface: str = "wlan0") -> str:
    """Connect the box to a WiFi network.

    Args:
        ssid: WiFi network name to connect to
        password: WiFi password (omit or empty for open networks)
        interface: WiFi interface name (default: 'wlan0')
    """
    try:
        cmd = ["nmcli", "dev", "wifi", "connect", ssid]
        if password:
            cmd.extend(["password", password])
        if interface != "wlan0":
            cmd.extend(["ifname", interface])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return json.dumps({
                "status": "ok", "ssid": ssid, "connected": True, "interface": interface,
            })
        error = result.stderr.strip() or result.stdout.strip() or "Connection failed"
        return json.dumps({"status": "error", "ssid": ssid, "connected": False, "error": error})
    except subprocess.TimeoutExpired:
        return json.dumps({"status": "error", "ssid": ssid, "error": "Connection timeout"})
    except Exception as exc:
        return json.dumps({"status": "error", "ssid": ssid, "error": str(exc)})


@mcp.tool()
def wifi_delete(ssid: str) -> str:
    """Delete a saved WiFi connection from the box.

    Args:
        ssid: WiFi network name to delete
    """
    try:
        result = subprocess.run(
            ["nmcli", "connection", "delete", ssid],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return json.dumps({"status": "ok", "ssid": ssid, "deleted": True})
        error = result.stderr.strip() or result.stdout.strip()
        return json.dumps({"status": "error", "ssid": ssid, "deleted": False, "error": error})
    except Exception as exc:
        return json.dumps({"status": "error", "ssid": ssid, "error": str(exc)})
