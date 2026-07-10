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
ADC, DAC, e-load, thermocouple, watt-meter, spi, i2c, energy-analyzer, and
later scope/...) stop round-tripping through /python. Tier 1 + the
bus/measurement roles are implemented here; new instruments are added as
entries in ROLE_ACTIONS.

Request body:
    { "netname": "gpi1", "action": "input", "params": { ... } }
    # "role" is optional; the box resolves it from saved_nets.json and is
    # authoritative. If a role is supplied it is verified, not trusted.

Response:
    { "success": true,  "action": "input", "message": "HIGH (1)", "value": 1 }
    { "success": false, "error": "Net 'gpi1' not found" }
"""
import logging
import threading

from flask import Flask, request, jsonify

from lager.nets.net import Net, NetType
from lager.nets.device import ConnectionFailed, DeviceError
from lager.exceptions import LagerBackendError

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
# Per-netname serialization for in-process bus/measurement drivers.
#
# The spi/i2c dispatchers cache their drivers behind a module lock, and the
# energy-analyzer dispatcher caches too — but that lock only guards driver
# *construction*, not the device I/O. Flask runs threaded, so two Workbench
# requests (or a Workbench request racing a `lager spi/i2c/energy` subprocess)
# on the same net would interleave transactions on one physical device.
#
# Decision (per the _net() docstring's open question): for spi/i2c/energy-
# analyzer we serialize the whole resolve+transact per netname. These roles are
# inherently transactional (a transfer/scan/integration must complete before
# the next starts), low-frequency from the dashboard, and cheap to queue. The
# simpler stateless reads (gpio/adc/dac/thermocouple/watt-meter) are left
# unserialized — their re-open-per-call already matches the /python path.
# ---------------------------------------------------------------------------

_net_locks = {}
_net_locks_guard = threading.Lock()


def _get_net_lock(netname):
    """Return a process-wide lock unique to `netname` (created on first use)."""
    with _net_locks_guard:
        lock = _net_locks.get(netname)
        if lock is None:
            lock = threading.Lock()
            _net_locks[netname] = lock
        return lock


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


def _spi(netname, role, action, params):
    # Bus transaction via the SPI dispatcher's cached, lock-guarded driver
    # (matches cli/impl/protocols/spi.py and the control plane's TIER1_NET_SCRIPT). The
    # whole resolve+transact is serialized per netname so concurrent requests
    # can't interleave words on the same device.
    if action not in ("transfer", "read"):
        raise UnknownAction(action)
    from lager.protocols.spi.dispatcher import _resolve_net_and_driver
    overrides = params.get("overrides") or None
    fill = int(params.get("fill", 0xFF))
    with _get_net_lock(netname):
        drv = _resolve_net_and_driver(netname, overrides)
        if action == "read":
            result = drv.read(int(params["n_words"]), fill=fill)
        else:
            # transfer: pad/truncate data to n_words with fill (default 0xFF)
            data = [int(b) for b in (params.get("data") or [])]
            n = int(params.get("n_words") or len(data))
            if len(data) < n:
                data = data + [fill] * (n - len(data))
            elif len(data) > n:
                data = data[:n]
            result = drv.read_write(data)
    words = [int(w) for w in result]
    return _ok(" ".join("%02X" % w for w in words) or "(no data)", words)


def _i2c(netname, role, action, params):
    # I2C transaction via the I2C dispatcher's cached, lock-guarded driver
    # (matches cli/impl/protocols/i2c.py and the control plane's TIER1_NET_SCRIPT).
    # Serialized per netname (see _spi).
    if action not in ("scan", "read", "write", "transfer"):
        raise UnknownAction(action)
    from lager.protocols.i2c.dispatcher import _resolve_net_and_driver
    overrides = params.get("overrides") or None
    with _get_net_lock(netname):
        drv = _resolve_net_and_driver(netname, overrides)
        if action == "scan":
            found = [int(a) for a in drv.scan()]
            if found:
                msg = "Found %d device(s): %s" % (
                    len(found), ", ".join("0x%02x" % a for a in found))
            else:
                msg = "No devices found"
            return _ok(msg, found)
        if action == "read":
            result = [int(b) for b in drv.read(int(params["address"]), int(params["num_bytes"]))]
            return _ok(" ".join("%02X" % b for b in result) or "(no data)", result)
        if action == "write":
            data = [int(b) for b in (params.get("data") or [])]
            drv.write(int(params["address"]), data)
            return _ok("Wrote %d byte(s) to 0x%02x" % (len(data), int(params["address"])))
        # transfer: write then read in one transaction (repeated start)
        data = [int(b) for b in (params.get("data") or [])]
        result = [int(b) for b in drv.write_read(int(params["address"]), data, int(params["num_bytes"]))]
        return _ok(" ".join("%02X" % b for b in result) or "(no data)", result)


def _energy_analyzer(netname, role, action, params):
    # Joulescope JS220 / Nordic PPK2 via the energy_analyzer dispatcher, which
    # returns dicts directly. read_energy integrates, read_stats averages.
    # Serialized per netname (see _spi) — a measurement must finish before the
    # next starts on the same device.
    if action not in ("read_stats", "read_energy"):
        raise UnknownAction(action)
    from lager.measurement.energy_analyzer.dispatcher import read_energy, read_stats
    default = 10.0 if action == "read_energy" else 1.0
    duration = float(params.get("duration") or default)
    # Clamp to the box's safe measurement window. The control plane clamps to 30s too,
    # sized so the held connection stays under Nginx's 60s proxy_read_timeout.
    duration = max(0.1, min(duration, 30.0))
    with _get_net_lock(netname):
        if action == "read_energy":
            r = read_energy(netname, duration)
            msg = "%.4f J (%.4f C) over %.1f s" % (
                float(r.get("energy_j") or 0), float(r.get("charge_c") or 0),
                float(r.get("duration_s") or duration))
        else:
            r = read_stats(netname, duration)
            c = r.get("current") or {}
            v = r.get("voltage") or {}
            p = r.get("power") or {}
            msg = "I %.6f A, V %.3f V, P %.6f W (mean over %.1f s)" % (
                float(c.get("mean") or 0), float(v.get("mean") or 0),
                float(p.get("mean") or 0), duration)
    return _ok(msg, r)


# role string -> handler. Keep aligned with mcp/data/api_reference.py and the
# control-plane allowlist (NET_PYTHON_ACTIONS / NET_COMMAND_ROUTES).
ROLE_ACTIONS = {
    "gpio": _gpio,
    "adc": _adc,
    "dac": _dac,
    "thermocouple": _thermocouple,
    "watt-meter": _watt_meter,
    "eload": _eload,
    "spi": _spi,
    "i2c": _i2c,
    "energy-analyzer": _energy_analyzer,
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
            except (ConnectionFailed, DeviceError, LagerBackendError) as e:
                logger.exception("[HTTP] /net/command hardware error on %s", netname)
                return jsonify({'success': False, 'error': 'Hardware error: %s' % e}), 502

            logger.info("[HTTP] /net/command %s on %s (%s)", action, netname, role)
            return jsonify({'success': True, 'action': action, **result})

        except Exception as e:
            logger.exception("[HTTP] /net/command unexpected error")
            return jsonify({'success': False, 'error': str(e)}), 500
