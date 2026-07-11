# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
BluFi HTTP handler for the Lager Box HTTP+WebSocket Server.

POST /blufi/command replaces the old :5000 ``blufi.py`` impl-script path
(ESP32 WiFi provisioning over BLE). Like BLE, BluFi is a box-level capability
driving the box's single Bluetooth adapter, so every action serializes under
the shared ``bt_adapter_lock`` from the ble handler — a BluFi provision and a
plain BLE scan can never race on the radio.

``BlufiClient`` runs its own internal bleak event-loop thread, so its blocking
API is safe to call from Flask worker threads. Each request builds a fresh
client and tears it down (disconnect + stop its loop thread + drop its atexit
hook) in a finally block — leaking either the thread or the atexit
registration would accumulate forever in this long-lived server process.

Provisioning can take 30s+ end to end (BLE connect + security negotiation +
target joining WiFi), so no aggressive timeouts here; the CLI/Rust clients
widen their HTTP budgets to match.
"""
import atexit
import logging
import time

from flask import Flask, jsonify, request

from lager.http_handlers.ble import bt_adapter_lock, run_bleak

logger = logging.getLogger(__name__)

_VALID_ACTIONS = ("scan", "connect", "provision", "wifi_scan", "status", "version")

# BluFi BLE service UUID, used to identify BluFi-capable devices in scans.
BLUFI_SERVICE_UUID = "0000ffff-0000-1000-8000-00805f9b34fb"

STA_CONN_NAMES = {
    0x00: "Connected",
    0x01: "Failed",
    0x02: "Connecting",
    0x03: "No IP",
}

OP_MODE_NAMES = {
    0x00: "NULL",
    0x01: "STA",
    0x02: "SoftAP",
    0x03: "STA+SoftAP",
}


def _connect_and_secure(params):
    """Build a BlufiClient, connect by device name, negotiate security.

    Caller must pass the returned client to _teardown() in a finally block.
    """
    from lager.blufi import BlufiClient

    device_name = params.get("device_name")
    if not device_name:
        raise ValueError("device_name is required")
    timeout = float(params.get("timeout") or 20.0)

    client = BlufiClient()
    try:
        if not client.connectByName(device_name, timeout=timeout):
            raise RuntimeError(
                "Failed to connect to '%s' within %ss" % (device_name, timeout))
        client.negotiateSecurity()
    except Exception:
        _teardown(client)
        raise
    return client


def _teardown(client):
    """Disconnect and fully release a BlufiClient.

    BlufiClient starts a daemon event-loop thread and registers an atexit
    cleanup on construction; neither is released by _cleanup() alone, so a
    per-request client in a long-lived server would leak one thread and one
    atexit entry per command without this.
    """
    try:
        client._cleanup()
    except Exception:
        logger.debug("BlufiClient cleanup failed", exc_info=True)
    finally:
        try:
            atexit.unregister(client._cleanup)
        except Exception:
            pass
        loop = client._bleak_loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)


def _wifi_state(client):
    state = client.getWifiState()
    op_mode = state["opMode"]
    sta_conn = state["staConn"]
    return {
        "opMode": op_mode,
        "opModeName": OP_MODE_NAMES.get(op_mode, "Unknown(%s)" % op_mode),
        "staConn": sta_conn,
        "staConnName": STA_CONN_NAMES.get(sta_conn, "Unknown(%s)" % sta_conn),
        "softAPConn": state["softAPConn"],
    }


async def _scan_async(timeout):
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    devices = []
    for address, (device, adv) in found.items():
        uuids = list(adv.service_uuids or []) if adv is not None else []
        devices.append({
            "name": device.name or address,
            "address": address,
            "rssi": adv.rssi if adv is not None else -100,
            "uuids": uuids,
        })
    return devices


def _scan(params):
    timeout = float(params.get("timeout") or 10.0)
    name_contains = params.get("name_contains")

    devices = run_bleak(_scan_async(timeout), timeout + 20.0)

    matched = []
    for d in devices:
        has_blufi_uuid = BLUFI_SERVICE_UUID in d["uuids"]
        name_match = bool(
            name_contains and d["name"]
            and name_contains.lower() in d["name"].lower())
        if has_blufi_uuid or name_match:
            matched.append(d)
    matched.sort(key=lambda d: (d["name"] == d["address"], d["name"]))

    return {
        "message": "Found %d BluFi device(s)" % len(matched),
        "value": {"devices": matched},
    }


def _connect(params):
    client = _connect_and_secure(params)
    try:
        client.requestVersion()
        time.sleep(0.5)
        version = client.getVersion()

        client.requestDeviceStatus()
        time.sleep(0.5)
        state = _wifi_state(client)

        value = {"device_name": params.get("device_name"),
                 "version": version, **state}
        return {
            "message": "Connected to %s (version %s, STA: %s)"
                       % (params.get("device_name"), version or "N/A",
                          state["staConnName"]),
            "value": value,
        }
    finally:
        _teardown(client)


def _provision(params):
    from lager.blufi import OP_MODE_STA, STA_CONN_SUCCESS

    ssid = params.get("ssid")
    password = params.get("password")
    if not ssid or not password:
        raise ValueError("ssid and password are required")

    client = _connect_and_secure(params)
    try:
        client.postDeviceMode(OP_MODE_STA)
        time.sleep(0.5)
        client.postStaWifiInfo({"ssid": ssid, "pass": password})

        # Give the target time to join the network before polling status.
        time.sleep(5)
        client.requestDeviceStatus()
        time.sleep(1)
        state = _wifi_state(client)

        if state["staConn"] != STA_CONN_SUCCESS:
            # Report the provisioning outcome as a command failure so clients
            # (CLI exit code, Rust Result) see it without inspecting `value`.
            raise RuntimeError(
                "Device connection status: %s" % state["staConnName"])
        return {
            "message": "Device connected to '%s' successfully" % ssid,
            "value": {
                "device_name": params.get("device_name"),
                "ssid": ssid,
                "staConn": state["staConn"],
                "staConnName": state["staConnName"],
                "success": True,
            },
        }
    finally:
        _teardown(client)


def _wifi_scan(params):
    scan_timeout = float(params.get("scan_timeout") or 15.0)

    client = _connect_and_secure(params)
    try:
        client.requestDeviceScan(timeout=scan_timeout)
        networks = client.getSSIDList()
        networks = sorted(networks, key=lambda n: n.get("rssi", -100),
                          reverse=True)
        return {
            "message": "Found %d network(s)" % len(networks),
            "value": {"device_name": params.get("device_name"),
                      "networks": networks},
        }
    finally:
        _teardown(client)


def _status(params):
    client = _connect_and_secure(params)
    try:
        client.requestDeviceStatus()
        time.sleep(0.5)
        state = _wifi_state(client)
        return {
            "message": "Op Mode: %s, STA: %s, SoftAP: %s"
                       % (state["opModeName"], state["staConnName"],
                          state["softAPConn"]),
            "value": {"device_name": params.get("device_name"), **state},
        }
    finally:
        _teardown(client)


def _version(params):
    client = _connect_and_secure(params)
    try:
        client.requestVersion()
        time.sleep(0.5)
        version = client.getVersion()
        return {
            "message": "Firmware version: %s" % (version or "N/A"),
            "value": {"device_name": params.get("device_name"),
                      "version": version},
        }
    finally:
        _teardown(client)


_ACTIONS = {
    "scan": _scan,
    "connect": _connect,
    "provision": _provision,
    "wifi_scan": _wifi_scan,
    "status": _status,
    "version": _version,
}


def register_blufi_routes(app: Flask) -> None:
    """Register the /blufi/command route on the Flask app."""

    @app.route('/blufi/command', methods=['POST'])
    def blufi_command_http():
        """
        Execute a BluFi (ESP32 WiFi provisioning over BLE) command.

        Request body:
            { "action": "scan" | "connect" | "provision" | "wifi_scan"
                        | "status" | "version",
              "params": { "device_name": ..., ... } }
        """
        try:
            data = request.get_json() or {}
            action = data.get('action')
            params = data.get('params') or {}

            if action not in _VALID_ACTIONS:
                return jsonify({
                    'success': False,
                    'error': 'action (scan|connect|provision|wifi_scan|status'
                             '|version) is required',
                }), 400

            try:
                with bt_adapter_lock:
                    result = _ACTIONS[action](params)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            except Exception as e:
                logger.exception("[HTTP] /blufi/command %s failed", action)
                return jsonify({'success': False,
                                'error': 'BluFi error: %s' % e}), 502

            logger.info("[HTTP] /blufi/command %s ok", action)
            return jsonify({'success': True, 'action': action, **result})

        except Exception as e:
            logger.exception("[HTTP] /blufi/command unexpected error")
            return jsonify({'success': False, 'error': str(e)}), 500
