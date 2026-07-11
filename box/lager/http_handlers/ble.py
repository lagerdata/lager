# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
BLE HTTP handler for the Lager Box HTTP+WebSocket Server.

POST /ble/command replaces the old :5000 ``ble.py`` impl-script path. BLE is a
box-level capability (the box's own Bluetooth adapter), not a saved net, so it
gets a dedicated endpoint like /usb/command rather than a /net/command role.

bleak is asyncio-only and box_http_server runs Flask-SocketIO in threading
mode, so all bleak coroutines execute on one dedicated event-loop thread
(created lazily, shared across requests). Requests submit coroutines with
``asyncio.run_coroutine_threadsafe`` and block on the result with a widened
timeout. There is a single BT adapter, so every BLE operation additionally
serializes under ``bt_adapter_lock`` — the same lock the blufi handler takes,
because BluFi drives the same adapter through its own internal bleak thread.
"""
import asyncio
import logging
import re
import threading

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

_VALID_ACTIONS = ("scan", "info", "connect", "disconnect")

# XX:XX:XX:XX:XX:XX (colons or dashes)
_BLE_ADDRESS_RE = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$')

# One BT adapter on the box: serialize every BLE *and* BluFi operation.
# blufi.py imports this lock so the two handlers can't fight over the radio.
bt_adapter_lock = threading.Lock()

# Dedicated event-loop thread for bleak coroutines (created on first use).
_bleak_loop = None
_bleak_loop_guard = threading.Lock()


def get_bleak_loop():
    """Return the shared bleak event loop, starting its thread on first use."""
    global _bleak_loop
    with _bleak_loop_guard:
        if _bleak_loop is None or _bleak_loop.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="ble-bleak-loop", daemon=True)
            thread.start()
            _bleak_loop = loop
        return _bleak_loop


def run_bleak(coro, timeout):
    """Run a coroutine on the bleak loop from Flask's worker thread."""
    future = asyncio.run_coroutine_threadsafe(coro, get_bleak_loop())
    return future.result(timeout)


async def _scan_async(timeout):
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    devices = []
    for address, (device, adv) in found.items():
        devices.append({
            "name": device.name or address,
            "address": address,
            "rssi": adv.rssi if adv is not None else -100,
            "uuids": list(adv.service_uuids or []) if adv is not None else [],
        })
    return devices


async def _device_info_async(address, timeout):
    """Connect and enumerate services/characteristics (used by info+connect)."""
    from bleak import BleakClient

    async with BleakClient(address, timeout=timeout) as client:
        services = []
        for service in client.services:
            services.append({
                "uuid": str(service.uuid),
                "description": service.description,
                "characteristics": [
                    {
                        "uuid": str(char.uuid),
                        "description": char.description,
                        "properties": list(char.properties),
                    }
                    for char in service.characteristics
                ],
            })
    return {"address": address, "connected": True, "services": services}


async def _disconnect_async(address):
    """Ensure a device is disconnected. BLE links via bleak are transient, so
    this connects briefly and lets the context exit tear the link down —
    mirroring the old impl script's explicit-user-intent semantics."""
    from bleak import BleakClient

    try:
        async with BleakClient(address, timeout=5.0):
            pass
        return {"address": address, "disconnected": True}
    except Exception as e:
        # Unreachable means already disconnected — that's the desired state.
        return {"address": address, "disconnected": True,
                "note": "Device not reachable: %s" % e}


def scan(params):
    timeout = float(params.get("timeout") or 5.0)
    if not 0.1 <= timeout <= 300.0:
        raise ValueError("timeout must be between 0.1 and 300 seconds")
    name_contains = params.get("name_contains")
    name_exact = params.get("name_exact")

    devices = run_bleak(_scan_async(timeout), timeout + 20.0)

    if name_exact:
        devices = [d for d in devices if d["name"] == name_exact]
    if name_contains:
        devices = [d for d in devices
                   if name_contains.lower() in d["name"].lower()]
    devices.sort(key=lambda d: (d["name"] == d["address"], d["name"]))

    return {
        "message": "Found %d device(s)" % len(devices),
        "value": {"devices": devices},
    }


def _require_address(params):
    address = params.get("address") or ""
    if not _BLE_ADDRESS_RE.match(address):
        raise ValueError(
            "Invalid BLE address format. Use XX:XX:XX:XX:XX:XX")
    return address


def info(params):
    address = _require_address(params)
    timeout = float(params.get("timeout") or 10.0)
    result = run_bleak(_device_info_async(address, timeout), timeout + 20.0)
    return {
        "message": "Connected to %s: %d service(s)"
                   % (address, len(result["services"])),
        "value": result,
    }


def disconnect(params):
    address = _require_address(params)
    result = run_bleak(_disconnect_async(address), 30.0)
    return {
        "message": "Disconnected from %s" % address,
        "value": result,
    }


_ACTIONS = {
    "scan": scan,
    "info": info,
    "connect": info,  # connect == connect + enumerate services, like the old script
    "disconnect": disconnect,
}


def register_ble_routes(app: Flask) -> None:
    """Register the /ble/command route on the Flask app."""

    @app.route('/ble/command', methods=['POST'])
    def ble_command_http():
        """
        Execute a BLE command using the box's Bluetooth adapter.

        Request body:
            { "action": "scan" | "info" | "connect" | "disconnect",
              "params": { ... } }
        """
        try:
            data = request.get_json() or {}
            action = data.get('action')
            params = data.get('params') or {}

            if action not in _VALID_ACTIONS:
                return jsonify({
                    'success': False,
                    'error': 'action (scan|info|connect|disconnect) is required',
                }), 400

            try:
                with bt_adapter_lock:
                    result = _ACTIONS[action](params)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            except Exception as e:
                # bleak errors (adapter off, device unreachable, connect
                # timeout) are hardware errors, not server bugs.
                logger.exception("[HTTP] /ble/command %s failed", action)
                return jsonify({'success': False,
                                'error': 'BLE error: %s' % e}), 502

            logger.info("[HTTP] /ble/command %s ok", action)
            return jsonify({'success': True, 'action': action, **result})

        except Exception as e:
            logger.exception("[HTTP] /ble/command unexpected error")
            return jsonify({'success': False, 'error': str(e)}), 500
