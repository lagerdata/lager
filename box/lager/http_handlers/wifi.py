# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
WiFi HTTP handler for the Lager Box HTTP+WebSocket Server.

POST /wifi/command replaces the old :5000 ``wifi.py`` impl-script path. WiFi
is a box-level capability (the box's own wlan interface), so it gets a
dedicated endpoint like /usb/command. The actual scan/connect/status/delete
logic lives in ``lager.protocols.wifi`` (nmcli/iwlist/wpa_supplicant
subprocess wrappers) and is shared with any other box-side caller.

Unlike the old script path, every action returns structured JSON — including
``status``, which previously only printed a table.
"""
import logging
import threading

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

_VALID_ACTIONS = ("status", "scan", "connect", "delete")

# One wlan radio: back-to-back nmcli/wpa_supplicant invocations against the
# same interface interleave badly (the connect path kills wpa_supplicant),
# so serialize all wifi actions in this process.
_wifi_lock = threading.Lock()


def _status(params):
    from lager.protocols.wifi import get_wifi_status

    interfaces = get_wifi_status()
    if "error" in interfaces and len(interfaces) == 1:
        raise RuntimeError(interfaces["error"].get("ssid", "WiFi status failed"))
    values = list(interfaces.values())
    connected = [i for i in values if i.get("state") == "Connected"]
    if connected:
        message = "Connected to %s on %s" % (
            connected[0].get("ssid"), connected[0].get("interface"))
    else:
        message = "No WiFi connection"
    return {"message": message, "value": {"interfaces": values}}


def _scan(params):
    from lager.protocols.wifi import scan_wifi

    interface = params.get("interface") or "wlan0"
    result = scan_wifi(interface)
    if "error" in result:
        raise RuntimeError(result["error"])
    networks = result.get("access_points", [])
    networks.sort(key=lambda n: n.get("strength", 0), reverse=True)
    return {
        "message": "Found %d network(s)" % len(networks),
        "value": {"access_points": networks},
    }


def _connect(params):
    from lager.protocols.wifi import connect_to_wifi

    ssid = params.get("ssid")
    if not ssid:
        raise ValueError("ssid is required")
    password = params.get("password") or ""
    interface = params.get("interface") or "wlan0"

    result = connect_to_wifi(ssid, password, interface)
    if not result.get("success"):
        raise RuntimeError(result.get("error") or "Connection failed")
    return {
        "message": result.get("message") or ("Connected to %s" % ssid),
        "value": {
            "ssid": ssid,
            "connected": True,
            "interface": interface,
            "method": result.get("method"),
        },
    }


def _delete(params):
    from lager.protocols.wifi import delete_wifi_connection

    connection_name = params.get("connection_name") or params.get("ssid")
    if not connection_name:
        raise ValueError("ssid or connection_name is required")

    result = delete_wifi_connection(connection_name)
    if not result.get("deleted"):
        raise RuntimeError(result.get("error") or "Delete failed")
    return {
        "message": "Deleted connection '%s'" % connection_name,
        "value": result,
    }


_ACTIONS = {
    "status": _status,
    "scan": _scan,
    "connect": _connect,
    "delete": _delete,
}


def register_wifi_routes(app: Flask) -> None:
    """Register the /wifi/command route on the Flask app."""

    @app.route('/wifi/command', methods=['POST'])
    def wifi_command_http():
        """
        Execute a WiFi command against the box's own wlan interface.

        Request body:
            { "action": "status" | "scan" | "connect" | "delete",
              "params": { ... } }
        """
        try:
            data = request.get_json() or {}
            action = data.get('action')
            params = data.get('params') or {}

            if action not in _VALID_ACTIONS:
                return jsonify({
                    'success': False,
                    'error': 'action (status|scan|connect|delete) is required',
                }), 400

            try:
                with _wifi_lock:
                    result = _ACTIONS[action](params)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            except Exception as e:
                # nmcli/iwlist failures (bad password, no such SSID, no
                # radio) are environment errors, not server bugs.
                logger.exception("[HTTP] /wifi/command %s failed", action)
                return jsonify({'success': False,
                                'error': 'WiFi error: %s' % e}), 502

            logger.info("[HTTP] /wifi/command %s ok", action)
            return jsonify({'success': True, 'action': action, **result})

        except Exception as e:
            logger.exception("[HTTP] /wifi/command unexpected error")
            return jsonify({'success': False, 'error': str(e)}), 500
