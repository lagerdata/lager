# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Generic net command HTTP handler for the Lager Box HTTP server.

A single endpoint — POST /net/command — that drives any configured net via a
role-keyed action dispatch table, using the high-level Net API. Running inside
the long-lived box_http_server process (instead of a freshly-spawned
`lager python` subprocess) skips the per-call interpreter startup + `lager`
import + device-open, so these commands get the same warm path as the
supply/battery/usb /command endpoints.

This is the consolidation point that lets the rest of the instruments (GPIO,
ADC, DAC, e-load, thermocouple, watt-meter, and later i2c/spi/scope/...) stop
round-tripping through /python. Tier 1 is implemented here; new instruments are
added as entries in ROLE_ACTIONS.

Request body:
    { "netname": "gpi1", "action": "input", "params": { ... } }
    # "role" is optional; the box resolves it from saved_nets.json and is
    # authoritative. If a role is supplied it is verified, not trusted.

Response:
    { "success": true,  "action": "input", "message": "HIGH (1)", "value": 1 }
    { "success": false, "error": "Net 'gpi1' not found" }
"""
import logging

from flask import Flask, request, jsonify

from lager.nets.net import Net, NetType
from lager.nets.device import ConnectionFailed, DeviceError

logger = logging.getLogger(__name__)


class NetNotFound(Exception):
    """Raised when a netname (optionally + role) is not in saved_nets.json."""


class UnknownAction(Exception):
    """Raised when an action is not valid for the net's role."""


# ---------------------------------------------------------------------------
# Result helpers — every action returns {"message": str, "value": <optional>}.
# ---------------------------------------------------------------------------

def _ok(message, value=None):
    out = {"message": message}
    if value is not None:
        out["value"] = value
    return out


def _ok_read(value, unit):
    return _ok("%s %s" % (value, unit), value)


def _net(netname, role):
    """Acquire the Net object for the resolved role via the high-level API.

    WARM-PATH NOTE (confirmed): for these Tier-1 roles, Net.get(...) returns a
    concrete *in-process* driver (LabJackGPIO/USB202*/FT232HGPIO, LabJackADC/DAC,
    PhidgetThermocouple, YoctoWatt/Joulescope/PPK2…), NOT a hardware_service
    Device proxy — only supply/battery route through resolve_net_proxy + Device.

    Implications of running these in box_http_server (vs `lager python`):
      • We DO eliminate the dominant cost — the per-call subprocess spawn +
        `lager` import — so this is already a big win over the exec path.
      • We do NOT get hardware_service's session caching / per-device locks, so:
          - each call constructs a fresh driver and re-opens the device;
          - two concurrent calls (Flask is threaded) to the same device can race;
          - it reintroduces the multi-owner problem hardware_service avoids
            (e.g. a concurrent `lager gpi …` subprocess opening the same LabJack).

    Decisions for this branch (pick per role):
      (a) keep in-process drivers — simplest, fine for low-concurrency Workbench;
          optionally cache the driver per (netname, role) to skip re-open, with
          invalidate-on-error;
      (b) route through hardware_service (resolve_net_proxy + Device, like
          supply.py) for true caching + serialization — needs confirming
          hardware_service can dispatch these device modules/methods.
    """
    return Net.get(netname, type=NetType.from_role(role))


# ---------------------------------------------------------------------------
# Per-role action handlers — handler(netname, role, action, params) -> result.
# Method calls mirror the existing cli/impl scripts so behavior is identical.
# ---------------------------------------------------------------------------

def _gpio(netname, role, action, params):
    net = _net(netname, role)
    if action == "input":
        v = int(net.input())
        return _ok("%s (%d)" % ("HIGH" if v else "LOW", v), v)
    if action == "output_high":
        net.output(1)
        return _ok("Output set HIGH")
    if action == "output_low":
        net.output(0)
        return _ok("Output set LOW")
    raise UnknownAction(action)


def _adc(netname, role, action, params):
    if action != "read":
        raise UnknownAction(action)
    return _ok_read(float(_net(netname, role).input()), "V")


def _dac(netname, role, action, params):
    net = _net(netname, role)
    if action == "set":
        v = float(params["value"])
        net.output(v)
        return _ok("Set to %.6f V" % v, v)
    if action == "read":
        return _ok_read(float(net.input()), "V")
    raise UnknownAction(action)


def _thermocouple(netname, role, action, params):
    if action != "read":
        raise UnknownAction(action)
    return _ok_read(float(_net(netname, role).read()), "°C")


def _watt_meter(netname, role, action, params):
    if action != "read":
        raise UnknownAction(action)
    net = _net(netname, role)
    p = float(net.read())
    try:
        net.close()
    except Exception:
        pass
    return _ok_read(p, "W")


def _eload(netname, role, action, params):
    # E-load uses the dispatcher functions (matches cli/impl/power/eload.py),
    # not the Net object — set when params.value is present, else read.
    from lager.power.eload.dispatcher import (
        set_constant_current, get_constant_current,
        set_constant_voltage, get_constant_voltage,
        set_constant_resistance, get_constant_resistance,
        set_constant_power, get_constant_power,
    )
    setters = {"cc": set_constant_current, "cv": set_constant_voltage,
               "cr": set_constant_resistance, "cp": set_constant_power}
    getters = {"cc": get_constant_current, "cv": get_constant_voltage,
               "cr": get_constant_resistance, "cp": get_constant_power}
    if action not in setters:
        raise UnknownAction(action)
    value = params.get("value")
    res = setters[action](netname, float(value)) if value is not None else getters[action](netname)
    if isinstance(res, dict) and res.get("error"):
        raise DeviceError(res["error"])
    return _ok(str(res), res)


# role string -> handler. Keep aligned with mcp/data/api_reference.py and the
# Stout-side allowlist (control-plane NET_PYTHON_ACTIONS / NET_COMMAND_ROUTES).
ROLE_ACTIONS = {
    "gpio": _gpio,
    "adc": _adc,
    "dac": _dac,
    "thermocouple": _thermocouple,
    "watt-meter": _watt_meter,
    "eload": _eload,
}


def _resolve_role(netname, requested_role):
    """Resolve a net's configured role from saved_nets.json (authoritative).

    If requested_role is given it must match a saved (name, role) pair; without
    it, the first saved entry for `netname` wins.
    """
    saved = Net.get_local_nets()
    for entry in saved:
        if entry.get("name") != netname:
            continue
        role = entry.get("role")
        if requested_role is None or requested_role == role:
            return role
    raise NetNotFound(netname if requested_role is None else "%s (role %s)" % (netname, requested_role))


def register_net_command_routes(app: Flask) -> None:
    """Register the generic /net/command route on the Flask app."""

    @app.route('/net/command', methods=['POST'])
    def net_command_http():
        try:
            data = request.get_json() or {}
            netname = data.get('netname')
            action = data.get('action')
            params = data.get('params') or {}
            requested_role = data.get('role')

            if not netname or not action:
                return jsonify({'success': False, 'error': 'netname and action are required'}), 400

            try:
                role = _resolve_role(netname, requested_role)
            except NetNotFound as e:
                return jsonify({'success': False, 'error': "Net '%s' not found" % e}), 404

            handler = ROLE_ACTIONS.get(role)
            if handler is None:
                return jsonify({
                    'success': False,
                    'error': "Role '%s' is not supported by /net/command" % role,
                }), 501

            try:
                result = handler(netname, role, action, params)
            except UnknownAction as e:
                return jsonify({'success': False, 'error': "Unknown action '%s' for %s" % (e, role)}), 400
            except KeyError as e:
                # Missing required param (e.g. dac set without value) or net key.
                return jsonify({'success': False, 'error': 'Missing required value: %s' % e}), 400
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            except (ConnectionFailed, DeviceError) as e:
                logger.exception("[HTTP] /net/command hardware error on %s", netname)
                return jsonify({'success': False, 'error': 'Hardware error: %s' % e}), 502

            logger.info("[HTTP] /net/command %s on %s (%s)", action, netname, role)
            return jsonify({'success': True, 'action': action, **result})

        except Exception as e:
            logger.exception("[HTTP] /net/command unexpected error")
            return jsonify({'success': False, 'error': str(e)}), 500
