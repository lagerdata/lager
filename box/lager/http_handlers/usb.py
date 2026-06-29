# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
USB hub HTTP handler for the Lager Box HTTP+WebSocket Server.

Mirrors the supply/battery fast-path: the CLI POSTs to /usb/command on
port 9000 instead of uploading a Python script to :5000/python. The
underlying USB hub drivers (Acroname BrainStem, YKUSH) already cache
their hardware handles inside this long-lived Flask process, so we get
the speedup just by not paying the subprocess+import cost per call.

Unlike supply/battery, USB drivers do not use pyvisa, so this handler
does not delegate to hardware_service.py.
"""
import logging
import threading

from flask import Flask, jsonify, request

from lager import (
    DeviceNotFoundError,
    LibraryMissingError,
    PortStateError,
    USBBackendError,
)
from lager.automation import usb_hub
from lager.util import self_restart as _self_restart

logger = logging.getLogger(__name__)

_VALID_ACTIONS = ("enable", "disable", "toggle", "state")

# Per-service cooldown stamp for the shared self-restart (box_http_server runs
# under start-services.sh's `while true` supervisor, like hardware_service).
_BHS_SELF_RESTART_STAMP = "/tmp/lager-box-http-server-self-restart"


def _usb_net_address(netname):
    """Best-effort VISA-style address for a USB net, for the sysfs-gated
    self-restart check. None if it can't be resolved."""
    try:
        from lager.automation.usb_hub.dispatcher import _load_net_definitions
        return (_load_net_definitions().get(netname) or {}).get("address")
    except Exception:
        return None


def _self_restart_if_wedged(netname, action, exc):
    """If a USB op failed because the device is unreachable but the device is
    still on the bus (sysfs), this process's USB/HID context is wedged after a
    re-enumeration — exit so the supervisor respawns box_http_server with a
    clean context. No-op when the device is genuinely absent or in cooldown."""
    address = _usb_net_address(netname)
    if address:
        _self_restart.maybe_self_restart(
            address, f"usb {action} {netname}", service="box_http_server",
            stamp_path=_BHS_SELF_RESTART_STAMP)

# Serialize hub calls within this process. The underlying Acroname/YKUSH
# drivers share cached hardware handles across requests; back-to-back
# concurrent enable/disable on the same hub would race over those handles.
_usb_lock = threading.Lock()


def register_usb_routes(app: Flask) -> None:
    """Register USB HTTP routes with the Flask app."""

    @app.route('/usb/command', methods=['POST'])
    def usb_command_http():
        """
        Execute a USB hub command against a configured USB net.

        Request body:
        {
            "netname": "usb1",
            "action": "enable" | "disable" | "toggle"
        }

        Returns:
        {
            "success": true,
            "action": "toggle",
            "state": "enabled" | "disabled",   # resulting port state
            "message": "USB port 'usb1' toggled → disabled"
        }
        """
        try:
            data = request.get_json() or {}
            netname = data.get('netname')
            action = data.get('action')

            if not netname or action not in _VALID_ACTIONS:
                return jsonify({
                    'success': False,
                    'error': 'netname and action (enable|disable|toggle|state) are required',
                }), 400

            try:
                with _usb_lock:
                    result = getattr(usb_hub, action)(netname)
            except LibraryMissingError as e:
                logger.warning("[HTTP] /usb/command library missing: %s", e)
                return jsonify({'success': False, 'error': f'library-missing: {e}'}), 500
            except DeviceNotFoundError as e:
                logger.warning("[HTTP] /usb/command device not found: %s", e)
                # "device not found" + still in sysfs = wedged USB context.
                _self_restart_if_wedged(netname, action, e)
                return jsonify({'success': False, 'error': f'device-not-found: {e}'}), 404
            except PortStateError as e:
                logger.warning("[HTTP] /usb/command port-state error: %s", e)
                return jsonify({'success': False, 'error': f'port-state: {e}'}), 409
            except USBBackendError as e:
                logger.exception("[HTTP] /usb/command backend error")
                return jsonify({'success': False, 'error': f'backend: {e}'}), 502
            except KeyError as e:
                # Net not found in saved_nets.json (raised by dispatcher).
                logger.warning("[HTTP] /usb/command unknown net: %s", e)
                return jsonify({'success': False, 'error': f"USB net not found: {e}"}), 404
            except (RuntimeError, FileNotFoundError) as e:
                logger.exception("[HTTP] /usb/command dispatcher error")
                return jsonify({'success': False, 'error': str(e)}), 502

            # toggle and state both return the live port state from the
            # dispatcher; enable/disable are unambiguous from the action itself.
            if action == "toggle":
                state = "enabled" if result else "disabled"
                message = f"USB port '{netname}' toggled → {state}"
            elif action == "state":
                state = "enabled" if result else "disabled"
                message = f"USB port '{netname}' is {state}"
            else:
                state = "enabled" if action == "enable" else "disabled"
                message = f"USB port '{netname}' {action}d"

            return jsonify({
                'success': True,
                'action': action,
                'state': state,
                'message': message,
            })
        except Exception as e:
            logger.exception("[HTTP] /usb/command unexpected error")
            # YKUSH/pykush raises a plain "device not found" (not lager's
            # DeviceNotFoundError), so an unreachable wedge lands here. Only
            # self-restart when the error means the device is unreachable (not
            # a device that responded with an error).
            if _self_restart.looks_like_device_unreachable(e):
                _self_restart_if_wedged(
                    locals().get('netname'), locals().get('action'), e)
            return jsonify({'success': False, 'error': str(e)}), 500
