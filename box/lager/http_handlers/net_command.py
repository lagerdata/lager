# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Uniform role-based net command handler for the Lager Box HTTP server.

`POST /net/command` is a thin, instrument-agnostic bridge that reproduces the
exact Python net call a `lager python` script would make:

    obj = Net.get(netname, NetType.from_role(role))
    result = getattr(obj, action)(**params)

The role is inferred from the saved net config, so callers never name a driver,
a SCPI command, or a channel. This is the analog of `Net.get(...).<method>(...)`
for clients (e.g. a `lager rust` binary) that cannot ``import lager`` and must
talk to the box over localhost HTTP.

Because it runs the *same* code path as `lager python`, the safety properties
come for free:
- supply/battery/eload Net instances wrap a mapper over a ``Device`` HTTP proxy,
  so their calls route through hardware_service.py:/invoke and never open a
  competing in-process pyvisa session.
- adc/dac/gpio return a driver constructed in-process (non-VISA, thread-safe).
- i2c/spi return I2CNet/SPINet wrappers that call their dispatchers in-process.
- usb returns a USBNetWrapper.

Request body:
    {
        "netname": "adc1",
        "action": "input",
        "params": {}            # optional; keys are the Python method's kwarg names
    }

Success response (``data`` is the normalized return value of the method —
a scalar, object, or array depending on the method):
    {
        "success": true,
        "netname": "adc1",
        "role": "adc",
        "action": "input",
        "data": 3.31
    }

Error response (mirrors the supply/battery handlers):
    {"success": false, "error": "..."}
"""
import logging

from flask import Flask, request, jsonify

from lager.cache import get_nets_cache

logger = logging.getLogger(__name__)


# Per-role allow-list of permitted actions. These are the verbatim public method
# names exposed by the object `Net.get(name, NetType.X)` returns for each role.
# getattr() may ONLY reach a name in this set, so the endpoint can never be
# coerced into calling an internal/dunder method or an unrelated attribute.
ALLOWED_ACTIONS = {
    "adc": frozenset({
        "input",
    }),
    "dac": frozenset({
        "output", "get_voltage",
    }),
    "gpio": frozenset({
        "input", "output", "wait_for_level",
    }),
    "power-supply": frozenset({
        "set_voltage", "set_current", "voltage", "current", "power",
        "enable", "disable", "set_mode",
        "set_ovp", "set_ocp", "get_ovp_limit", "get_ocp_limit",
        "is_ovp", "is_ocp", "clear_ovp", "clear_ocp",
    }),
    # Battery uses the KeithleyBatteryFunctionMapper's verbatim method names:
    # setters like soc(percent)/voc(voltage) and getters get_soc()/get_voc().
    "battery": frozenset({
        "soc", "get_soc", "voc", "get_voc",
        "terminal_voltage", "current", "esr",
        "set_capacity", "current_limit",
        "voltage_full", "voltage_empty", "set_terminal_voltage",
        "set_mode_battery", "list_battery_models", "setup_battery",
        "enable", "disable",
    }),
    "i2c": frozenset({
        "config", "scan", "read", "write", "write_read", "get_config",
    }),
    "spi": frozenset({
        "config", "read", "read_write", "transfer", "write", "get_config",
    }),
    "usb": frozenset({
        "enable", "disable", "toggle",
    }),
}


def _normalize(value):
    """
    Coerce a method's return value into something jsonify() can serialize.

    Net/driver methods return plain Python scalars, lists, dicts, or None, but
    a few hardware paths hand back numpy scalars or tuples. Pass JSON-friendly
    values through untouched; convert the stragglers.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    # numpy float/int, Decimal, etc. — fall back to float then str.
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def register_net_command_routes(app: Flask) -> None:
    """Register the uniform /net/command route with the Flask app."""

    @app.route('/net/command', methods=['POST'])
    def net_command_http():
        # These are light imports; the heavy lager.nets.net is imported lazily
        # below, only after request validation passes.
        from lager.nets.constants import NetType
        from lager.nets.device import ConnectionFailed, DeviceError
        from lager.exceptions import LagerBackendError

        try:
            data = request.get_json(silent=True) or {}
            netname = data.get('netname')
            action = data.get('action')
            params = data.get('params') or {}

            if not netname or not action:
                return jsonify({'success': False, 'error': 'netname and action are required'}), 400
            if not isinstance(params, dict):
                return jsonify({'success': False, 'error': 'params must be an object'}), 400

            rec = get_nets_cache().find_by_name(netname)
            if not rec:
                return jsonify({
                    'success': False,
                    'error': f"Net '{netname}' not found. Create it with 'lager nets create'.",
                }), 404

            role = rec.get('role') or ''
            try:
                nettype = NetType.from_role(role)
            except (KeyError, ValueError):
                return jsonify({
                    'success': False,
                    'error': f"Net '{netname}' has unknown role '{role}'.",
                }), 400

            allowed = ALLOWED_ACTIONS.get(role)
            if allowed is None:
                return jsonify({
                    'success': False,
                    'error': f"Role '{role}' is not yet supported by /net/command.",
                }), 400
            if action not in allowed:
                return jsonify({
                    'success': False,
                    'error': (
                        f"Action '{action}' is not valid for '{role}' net '{netname}'. "
                        f"Allowed: {', '.join(sorted(allowed))}."
                    ),
                }), 400

            # Lazy import: lager.nets.net pulls in many driver modules. Import it
            # here — after validation — so an import failure degrades only this
            # request, and trivially-invalid requests never pay the import cost.
            from lager.nets.net import Net, InvalidNetError, SetupFunctionRequiredError

            try:
                obj = Net.get(netname, nettype)
            except (InvalidNetError, SetupFunctionRequiredError) as e:
                return jsonify({'success': False, 'error': str(e)}), 404

            try:
                result = getattr(obj, action)(**params)
            except TypeError as e:
                # Wrong/missing kwargs for the method.
                return jsonify({
                    'success': False,
                    'error': f"Invalid params for action '{action}': {e}",
                }), 400
            except (ConnectionFailed, DeviceError) as e:
                logger.exception("[HTTP] /net/command %s.%s hardware_service error", netname, action)
                return jsonify({'success': False, 'error': f'Hardware service error: {e}'}), 502
            except LagerBackendError as e:
                logger.warning("[HTTP] /net/command %s.%s backend error: %s", netname, action, e)
                return jsonify({'success': False, 'error': str(e)}), 400
            except NotImplementedError as e:
                return jsonify({
                    'success': False,
                    'error': f"Action '{action}' is not supported by this instrument: {e}",
                }), 400

            logger.info("[HTTP] /net/command executed: %s.%s on %s", role, action, netname)
            return jsonify({
                'success': True,
                'netname': netname,
                'role': role,
                'action': action,
                'data': _normalize(result),
            })

        except Exception as e:
            logger.exception("[HTTP] Error executing /net/command")
            return jsonify({'success': False, 'error': str(e)}), 500
