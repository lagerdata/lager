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
An open BLE session (``ble_session.py``) holds that lock for its lifetime;
requests here then get 409 ``adapter_busy`` (see ``acquire_adapter``).
"""
import asyncio
import logging
import re
import threading

from flask import Flask, jsonify, request

from lager.protocols.ble.target import ble_target

logger = logging.getLogger(__name__)

_VALID_ACTIONS = ("scan", "info", "connect", "disconnect", "adapter")

# XX:XX:XX:XX:XX:XX (colons or dashes)
_BLE_ADDRESS_RE = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$')

# One BT adapter on the box: serialize every BLE *and* BluFi operation.
# blufi.py imports this lock so the two handlers can't fight over the radio.
bt_adapter_lock = threading.Lock()

# An open BLE session (ble_session.py) holds bt_adapter_lock for its whole
# lifetime and registers itself here, so one-shot BLE/BluFi requests can fail
# fast with 409 naming the session instead of queueing behind it for minutes.
_adapter_holder = None  # (owner, describe) or None
_adapter_holder_guard = threading.Lock()


def set_adapter_holder(owner, describe):
    """Record `owner` (holding bt_adapter_lock) as the adapter's holder.
    `describe()` returns the dict reported in adapter_busy errors."""
    global _adapter_holder
    with _adapter_holder_guard:
        _adapter_holder = (owner, describe)


def clear_adapter_holder(owner):
    """Forget `owner` as the adapter's holder (no-op if it is not)."""
    global _adapter_holder
    with _adapter_holder_guard:
        if _adapter_holder is not None and _adapter_holder[0] is owner:
            _adapter_holder = None


def adapter_holder_info():
    """describe() of the session holding the adapter, or None."""
    with _adapter_holder_guard:
        holder = _adapter_holder
    return holder[1]() if holder is not None else None


def adapter_busy_message(info):
    """The adapter_busy text for a session described by `info`."""
    who = " (holder: %s)" % info['holder'] if info.get('holder') else ""
    return (
        "The box's Bluetooth adapter is in use by an open BLE session with %s%s, "
        "idle %.0fs. Close that session, or end it with `lager ble sessions --release`."
        % (info.get('address'), who, info.get('idle_s', 0.0))
    )


def acquire_adapter():
    """Take bt_adapter_lock for a one-shot BLE/BluFi request.

    Waits while another one-shot request holds the lock (they are short), but
    returns the holder's describe() dict as soon as an open session is found
    to hold it. Returns None once the lock is held; the caller releases it.
    """
    while True:
        info = adapter_holder_info()
        if info is not None:
            return info
        if bt_adapter_lock.acquire(timeout=0.25):
            return None


def adapter_busy_response(info):
    """The 409 JSON body for a request refused because a session is open."""
    return jsonify({'success': False, 'code': 'adapter_busy',
                    'error': adapter_busy_message(info), 'session': info}), 409


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


def random_type(address, address_type):
    """Kind of a random address, from its two most significant bits.

    Core spec Vol 6 Part B 1.3: 0b11 static, 0b01 resolvable private,
    0b00 non-resolvable private. None for a public (or unknown) address.
    """
    if address_type != "random":
        return None
    try:
        top = int(address[:2], 16) >> 6
    except ValueError:
        return None
    return {0b11: "static", 0b01: "resolvable", 0b00: "non-resolvable"}.get(top)


def _device_entry(address, device, adv):
    """One scan result. `address_type` comes from BlueZ's Device1 record."""
    details = getattr(device, "details", None)
    props = details.get("props", {}) if isinstance(details, dict) else {}
    address_type = props.get("AddressType")
    return {
        "name": device.name or address,
        "address": address,
        "address_type": address_type,
        "random_type": random_type(address, address_type),
        "rssi": adv.rssi if adv is not None else -100,
        "uuids": list(adv.service_uuids or []) if adv is not None else [],
    }


async def _scan_async(timeout):
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return [_device_entry(address, device, adv)
            for address, (device, adv) in found.items()]


async def _adapter_async():
    """The box's Bluetooth adapters as BlueZ reports them."""
    from bleak.backends.bluezdbus.manager import get_global_bluez_manager

    manager = await get_global_bluez_manager()
    adapters = []
    for path in sorted(manager._adapters):
        props = manager._properties.get(path, {}).get("org.bluez.Adapter1", {})
        adapters.append({
            "name": path.rsplit("/", 1)[-1],
            "address": props.get("Address"),
            "powered": bool(props.get("Powered")),
        })
    return adapters


def adapter(params):
    """Whether this box can do BLE: a BlueZ adapter that is powered.

    Read-only, and answered even while a session holds the adapter, so a
    test can check it first and skip on a box without a radio instead of
    failing on its first BLE call. Never raises for a missing radio.
    """
    try:
        adapters = run_bleak(_adapter_async(), 10.0)
    except Exception as e:  # noqa: BLE001 — "no BLE here" is the answer, not an error
        return {
            "message": "BLE is not available on this box",
            "value": {"available": False, "adapters": [],
                      "reason": bluez_unavailable_hint(e) or "BlueZ did not answer: %s" % e},
        }
    powered = [a for a in adapters if a["powered"]]
    if powered:
        reason = None
    elif adapters:
        reason = ("The Bluetooth adapter is powered off. On the box run: "
                  "bluetoothctl power on")
    else:
        reason = "The box has no Bluetooth adapter"
    return {
        "message": "BLE is %savailable on this box" % ("" if powered else "not "),
        "value": {"available": bool(powered), "adapters": adapters, "reason": reason},
    }


async def _device_info_async(address, timeout):
    """Connect and enumerate services/characteristics (used by info+connect)."""
    from bleak import BleakClient

    async with BleakClient(await ble_target(address), timeout=timeout) as client:
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
        async with BleakClient(await ble_target(address), timeout=5.0):
            pass
        return {"address": address, "disconnected": True}
    except Exception as e:
        # Unreachable means already disconnected — that's the desired state.
        return {"address": address, "disconnected": True,
                "note": bluez_unavailable_hint(e) or "Device not reachable: %s" % e}


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
    "adapter": adapter,
}


def register_ble_routes(app: Flask) -> None:
    """Register the /ble/command route on the Flask app."""

    @app.route('/ble/command', methods=['POST'])
    def ble_command_http():
        """
        Execute a BLE command using the box's Bluetooth adapter.

        Request body:
            { "action": "scan" | "info" | "connect" | "disconnect" | "adapter",
              "params": { ... } }
        """
        try:
            data = request.get_json() or {}
            action = data.get('action')
            params = data.get('params') or {}

            if action not in _VALID_ACTIONS:
                return jsonify({
                    'success': False,
                    'error': 'action (scan|info|connect|disconnect|adapter) is required',
                }), 400

            if action == 'adapter':
                # A read-only probe: no adapter lock, never refused by a session.
                return jsonify({'success': True, 'action': action, **adapter(params)})

            busy = acquire_adapter()
            if busy is not None:
                return adapter_busy_response(busy)
            try:
                result = _ACTIONS[action](params)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            except Exception as e:
                # bleak errors (adapter off, device unreachable, connect
                # timeout) are hardware errors, not server bugs.
                logger.exception("[HTTP] /ble/command %s failed", action)
                return jsonify({'success': False,
                                'error': bluez_unavailable_hint(e) or 'BLE error: %s' % e}), 502
            finally:
                bt_adapter_lock.release()

            logger.info("[HTTP] /ble/command %s ok", action)
            return jsonify({'success': True, 'action': action, **result})

        except Exception as e:
            logger.exception("[HTTP] /ble/command unexpected error")
            return jsonify({'success': False, 'error': str(e)}), 500
