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

Links are per request. Each GATT action (read, write, notify, pair) connects,
does its one operation and disconnects, so a device that needs a held
connection between operations cannot be driven from here.
"""
import asyncio
import logging
import re
import threading

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

_VALID_ACTIONS = ("scan", "info", "connect", "disconnect",
                  "read", "write", "notify", "pair")

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


# bleak reaches the HOST's bluetoothd over the mounted /var/run/dbus socket; the
# container runs no bluetoothd of its own. With BlueZ missing on the host (Ubuntu
# Server ships without it) every call fails with D-Bus ServiceUnknown for
# org.bluez, which says nothing about the fix. cli/core/net_helpers.py carries
# the same text for boxes whose image predates this one; a unit test pins them.
BLUEZ_UNAVAILABLE_MESSAGE = (
    "BlueZ is not running on the box host. Run `lager update` for this box, "
    "or on the box run: sudo apt install -y bluez && sudo systemctl enable --now bluetooth"
)


def bluez_unavailable_hint(exc):
    """BLUEZ_UNAVAILABLE_MESSAGE when `exc` is the host having no BlueZ, else None."""
    text = str(exc)
    if "org.bluez" in text and "ServiceUnknown" in text:
        return BLUEZ_UNAVAILABLE_MESSAGE
    return None


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
                "note": bluez_unavailable_hint(e) or "Device not reachable: %s" % e}


async def _read_async(address, char, timeout):
    from bleak import BleakClient

    async with BleakClient(address, timeout=timeout) as client:
        data = await client.read_gatt_char(char)
    return bytes(data)


async def _write_async(address, char, data, response, timeout):
    from bleak import BleakClient

    async with BleakClient(address, timeout=timeout) as client:
        await client.write_gatt_char(char, data, response=response)


async def _notify_async(address, char, max_messages, duration, timeout):
    """Subscribe, collect up to max_messages notifications or until duration
    seconds pass, then unsubscribe. Returns (messages, timed_out)."""
    from bleak import BleakClient

    messages = []
    done = asyncio.Event()

    def on_notify(_char, data):
        if len(messages) < max_messages:
            messages.append(bytes(data))
        if len(messages) >= max_messages:
            done.set()

    async with BleakClient(address, timeout=timeout) as client:
        await client.start_notify(char, on_notify)
        try:
            await asyncio.wait_for(done.wait(), timeout=duration)
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
        try:
            await client.stop_notify(char)
        except Exception:
            # The link is torn down on exit anyway; the messages are what
            # the caller asked for.
            logger.debug("stop_notify failed", exc_info=True)
    return messages, timed_out


async def _pair_async(address, timeout):
    from bleak import BleakClient

    async with BleakClient(address, timeout=timeout) as client:
        await client.pair()


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


def _require_char(params):
    """A characteristic UUID string, or an integer handle."""
    char = params.get("char")
    if isinstance(char, bool):
        char = None
    if isinstance(char, int):
        if char < 0:
            raise ValueError("char handle must be non-negative")
        return char
    if isinstance(char, str) and char.strip():
        return char.strip()
    raise ValueError("char (characteristic UUID or handle) is required")


def _number(params, key, default):
    # An explicit 0 is a value to range-check, not a request for the default.
    value = params.get(key)
    return default if value is None else float(value)


def _timeout(params, default):
    timeout = _number(params, "timeout", default)
    if not 0.1 <= timeout <= 300.0:
        raise ValueError("timeout must be between 0.1 and 300 seconds")
    return timeout


def read(params):
    address = _require_address(params)
    char = _require_char(params)
    timeout = _timeout(params, 10.0)
    data = run_bleak(_read_async(address, char, timeout), timeout + 20.0)
    return {
        "message": "Read %d byte(s) from %s" % (len(data), char),
        "value": {"address": address, "char": char, "data": data.hex()},
    }


def write(params):
    address = _require_address(params)
    char = _require_char(params)
    timeout = _timeout(params, 10.0)
    raw = params.get("data")
    if not isinstance(raw, str):
        raise ValueError("data (hex string) is required")
    try:
        data = bytes.fromhex(raw)
    except ValueError:
        raise ValueError("data must be a hex string, e.g. '0102ff'")
    # bleak picks write-with-response or without from the characteristic's
    # properties when response is None.
    response = params.get("response")
    if response is not None:
        response = bool(response)
    run_bleak(_write_async(address, char, data, response, timeout),
              timeout + 20.0)
    return {
        "message": "Wrote %d byte(s) to %s" % (len(data), char),
        "value": {"address": address, "char": char, "length": len(data)},
    }


def notify(params):
    address = _require_address(params)
    char = _require_char(params)
    timeout = _timeout(params, 10.0)
    max_messages = int(_number(params, "max_messages", 1))
    if not 1 <= max_messages <= 10000:
        raise ValueError("max_messages must be between 1 and 10000")
    duration = _number(params, "duration", 10.0)
    if not 0.1 <= duration <= 300.0:
        raise ValueError("duration must be between 0.1 and 300 seconds")
    messages, timed_out = run_bleak(
        _notify_async(address, char, max_messages, duration, timeout),
        timeout + duration + 20.0)
    return {
        "message": "Received %d notification(s) from %s" % (len(messages), char),
        "value": {
            "address": address,
            "char": char,
            "messages": [m.hex() for m in messages],
            "timed_out": timed_out,
        },
    }


def pair(params):
    address = _require_address(params)
    timeout = _timeout(params, 10.0)
    run_bleak(_pair_async(address, timeout), timeout + 20.0)
    return {
        "message": "Paired with %s" % address,
        "value": {"address": address, "paired": True},
    }


_ACTIONS = {
    "scan": scan,
    "info": info,
    "connect": info,  # connect == connect + enumerate services, like the old script
    "disconnect": disconnect,
    "read": read,
    "write": write,
    "notify": notify,
    "pair": pair,
}


def register_ble_routes(app: Flask) -> None:
    """Register the /ble/command route on the Flask app."""

    @app.route('/ble/command', methods=['POST'])
    def ble_command_http():
        """
        Execute a BLE command using the box's Bluetooth adapter.

        Request body:
            { "action": "scan" | "info" | "connect" | "disconnect"
                        | "read" | "write" | "notify" | "pair",
              "params": { ... } }
        """
        try:
            data = request.get_json() or {}
            action = data.get('action')
            params = data.get('params') or {}

            if action not in _VALID_ACTIONS:
                return jsonify({
                    'success': False,
                    'error': 'action (scan|info|connect|disconnect|read|write'
                             '|notify|pair) is required',
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
                                'error': bluez_unavailable_hint(e) or 'BLE error: %s' % e}), 502

            logger.info("[HTTP] /ble/command %s ok", action)
            return jsonify({'success': True, 'action': action, **result})

        except Exception as e:
            logger.exception("[HTTP] /ble/command unexpected error")
            return jsonify({'success': False, 'error': str(e)}), 500
