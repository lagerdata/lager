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
import importlib
import logging

from flask import Flask, request, jsonify

from lager.nets.net import Net
from lager.nets.device import ConnectionFailed, DeviceError, Device
from lager.dispatchers import helpers
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


# ---------------------------------------------------------------------------
# hardware_service routing for the IO / bus / measurement roles.
#
# Every Tier-1 role now drives its device through hardware_service
# (POST :8080/invoke) via a Device proxy, the same single-owner path supply,
# battery and eload use. hardware_service owns/caches the driver per physical
# device and serializes access under a per-device lock, so concurrent :9000
# requests (e.g. parallel cargo tests through the Rust crate) can never
# interleave I/O on the same instrument.
#
# The catch for non-VISA devices: hardware_service's per-address lock keys on
# net_info["address"], but a LabJack T7 has no VISA address and is shared
# across GPIO/ADC/DAC/SPI/I2C (one LJM handle). So each net supplies an
# explicit `device_id` (see _physical_device_id) — a stable physical-device
# identity that is SHARED by every net/role on the same hardware. The cache
# still keeps a distinct driver instance per net; only the lock is shared.
#
# Each role maps to a uniquely-named create_device factory module (adc_hs,
# gpio_hs, …) so hardware_service's import-path search resolves it to the right
# role (the raw driver module names — labjack_t7, usb202 — are ambiguous across
# io.adc / io.dac / io.gpio).
# ---------------------------------------------------------------------------

_HS_FACTORY = {
    "adc": "adc_hs",
    "dac": "dac_hs",
    "gpio": "gpio_hs",
    "thermocouple": "thermocouple_hs",
    "watt-meter": "watt_hs",
    "energy-analyzer": "energy_hs",
    "spi": "spi_hs",
    "i2c": "i2c_hs",
    "arm": "arm_hs",
}


def _address_from_rec(rec):
    """Resolve a device address from a saved-net record.

    Prefers a per-net mappings[].device_override, else the top-level address —
    the same precedence resolve_address() uses. Returns None if unset.
    """
    for mapping in rec.get("mappings") or []:
        if mapping.get("device_override"):
            return mapping["device_override"]
    return rec.get("address")


def _find_rec(netname, role, error_class):
    """Return the saved-net record for (netname, role) or raise error_class.

    Reads from the same source _resolve_role uses (Net.get_local_nets), so a
    single mock covers both the role resolution and the proxy build in tests.
    """
    for entry in Net.get_local_nets():
        if entry.get("name") == netname and entry.get("role") == role:
            return entry
    raise error_class("Net '%s' (role %s) not found." % (netname, role))


def _hw_proxy(netname, role, module_for_instrument, error_class):
    """Resolve a VISA net to a hardware_service Device proxy (POST :8080/invoke).

    Mirrors resolve_net_proxy's supply/battery flow (resolve record -> VISA
    address -> instrument->module). Used for eload; the lock is hardware_service's
    per-VISA-address lock, the single-owner path supply/battery use.
    """
    rec = _find_rec(netname, role, error_class)
    address = _address_from_rec(rec)
    if not address:
        raise error_class("Net '%s' has no device address." % netname)

    instrument = rec.get("instrument") or ""
    device_name = module_for_instrument(instrument)
    if device_name is None:
        raise error_class(
            "Unsupported %s instrument '%s' for net '%s'." % (role, instrument, netname))
    net_info = {"name": netname, "address": address, "instrument": instrument}
    return Device(device_name, net_info)


def _physical_device_id(role, instrument, rec):
    """Stable identity for the physical device backing this net.

    hardware_service uses it as the shared lock key so that every net/role on
    one physical device serializes — critically the LabJack T7, whose single
    LJM handle is shared across GPIO/ADC/DAC/SPI/I2C, and a Joulescope/PPK2
    shared by a watt-meter net and an energy-analyzer net. Keyed on the device
    family + its serial/address (or a constant when a family has one shared
    handle), NOT on the net name or pin.
    """
    inst = (instrument or "").lower()
    addr = _address_from_rec(rec) or ""
    serial = ""
    if "::" in addr:
        parts = addr.split("::")
        if len(parts) > 3 and parts[3]:
            serial = parts[3]
    if any(k in inst for k in ("usb-202", "usb202", "mcc")):
        return "usb202:" + (rec.get("unique_id") or addr or "ANY")
    if "ft232h" in inst or "ftdi" in inst:
        return "ft232h:" + (serial or addr or "ANY")
    if "aardvark" in inst or "totalphase" in inst:
        port = str((rec.get("params") or {}).get("port", 0))
        return "aardvark:" + (serial or addr or port)
    if "joulescope" in inst or "js220" in inst:
        return "joulescope:" + (addr or "ANY")
    if "ppk" in inst or "nordic" in inst:
        return "ppk2:" + (addr or "ANY")
    if role == "thermocouple" or "phidget" in inst:
        return "phidget:" + (addr or "ANY")
    if role == "arm" or "dexarm" in inst or "rotrics" in inst:
        # One serial port per arm; the lock key is the arm's serial number
        # (saved under rec["serial"] or location.serial_number) or address.
        location = rec.get("location")
        loc_serial = location.get("serial_number") if isinstance(location, dict) else None
        return "dexarm:" + (rec.get("serial") or loc_serial or addr or "ANY")
    if role == "watt-meter" or "yocto" in inst:
        return "yocto:" + (addr or "ANY")
    # Default: LabJack T7 — one shared LJM handle across all roles/pins.
    return "labjack:" + (addr or "ANY")


def _proxy(netname, role, timeout=None):
    """Build a hardware_service Device proxy for a non-VISA (device_id) role."""
    rec = _find_rec(netname, role, DeviceError)
    instrument = rec.get("instrument") or ""
    net_info = {
        "name": netname,
        "instrument": instrument,
        "device_id": _physical_device_id(role, instrument, rec),
    }
    return Device(_HS_FACTORY[role], net_info, timeout=timeout)


# ---------------------------------------------------------------------------
# Per-role action handlers — handler(netname, role, action, params) -> result.
# Method calls mirror the existing cli/impl scripts so behavior is identical.
# ---------------------------------------------------------------------------

def _gpio(netname, role, action, params):
    # Blocking edge/level wait runs inside hardware_service under the LabJack's
    # shared device_id lock; the gpio_hs adapter normalizes the level and routes
    # LabJack-specific scan kwargs (matches the gpio dispatcher). The internal
    # box->hardware_service proxy timeout is sized past the caller-supplied wait
    # timeout so the device method (not the transport) decides when to give up;
    # with no wait timeout the proxy waits indefinitely, matching the old
    # unbounded `lager gpi --wait-for` behavior.
    if action == "wait_for_level":
        level = params.get("level")
        if level is None:
            raise KeyError("level")
        kwargs = {}
        if params.get("timeout") is not None:
            kwargs["timeout"] = float(params["timeout"])
        if params.get("scan_rate") is not None:
            kwargs["scan_rate"] = int(params["scan_rate"])
        if params.get("scans_per_read") is not None:
            kwargs["scans_per_read"] = int(params["scans_per_read"])
        if params.get("poll_interval") is not None:
            kwargs["poll_interval"] = float(params["poll_interval"])
        wait_timeout = kwargs.get("timeout")
        # 24h stands in for "no timeout": Device(timeout=None) means "use the
        # 10s default", so an explicit huge budget is how we defer entirely to
        # the device-side wait (or the caller's own timeout + margin).
        proxy_timeout = float(wait_timeout) + 10.0 if wait_timeout else 86400.0
        dev = _proxy(netname, role, timeout=proxy_timeout)
        elapsed = float(dev.wait_for_level(level, **kwargs))
        return _ok("Reached level %s in %.4fs" % (level, elapsed), elapsed)

    dev = _proxy(netname, role)
    if action == "input":
        v = int(dev.input())
        return _ok("%s (%d)" % ("HIGH" if v else "LOW", v), v)
    if action == "output_high":
        dev.output(1)
        return _ok("Output set HIGH")
    if action == "output_low":
        dev.output(0)
        return _ok("Output set LOW")
    # Generic output with an explicit level (supports gpo's toggle/high/low).
    # The adapter resolves toggle (read+write) in one /invoke -> atomic.
    if action == "output":
        level = params.get("level")
        if level is None:
            raise KeyError("level")
        v = int(dev.output(level))
        if str(level).strip().lower() == "toggle":
            return _ok("Toggled to %s" % ("HIGH" if v else "LOW"), v)
        return _ok("Output set %s" % ("HIGH" if v else "LOW"), v)
    raise UnknownAction(action)


def _adc(netname, role, action, params):
    if action != "read":
        raise UnknownAction(action)
    return _ok_read(float(_proxy(netname, role).input()), "V")


def _dac(netname, role, action, params):
    dev = _proxy(netname, role)
    if action == "set":
        v = float(params["value"])
        dev.output(v)
        return _ok("Set to %.6f V" % v, v)
    if action == "read":
        return _ok_read(float(dev.input()), "V")
    raise UnknownAction(action)


def _thermocouple(netname, role, action, params):
    if action != "read":
        raise UnknownAction(action)
    return _ok_read(float(_proxy(netname, role).read()), "°C")


def _watt_meter(netname, role, action, params):
    # "read"/"power" -> watts; "current"/"voltage" -> A/V (Joulescope/PPK2);
    # "all" -> {current, voltage, power}. The watt_hs adapter performs the timed
    # measurement in one /invoke under the device_id lock (shared with an
    # energy-analyzer net on the same Joulescope/PPK2); a power-only meter's
    # UnsupportedInstrumentError surfaces as a 502.
    if action not in ("read", "power", "current", "voltage", "all"):
        raise UnknownAction(action)
    duration = float(params.get("duration") or 0.1)
    # Widen the proxy timeout to cover the integration window (+margin).
    dev = _proxy(netname, role, timeout=duration + 15.0)
    r = dev.measure(action, duration)
    if action == "all":
        current = float(r["current"])
        voltage = float(r["voltage"])
        power = float(r["power"])
        msg = "I %.6f A, V %.3f V, P %.6f W (%gs)" % (
            current, voltage, power, duration)
        return _ok(msg, {
            "current": current, "voltage": voltage, "power": power,
            "duration_s": duration,
        })
    unit = {"read": "W", "power": "W", "current": "A", "voltage": "V"}[action]
    return _ok_read(float(r["value"]), unit)


def _eload(netname, role, action, params):
    # E-load is a VISA instrument: route through hardware_service (POST
    # :8080/invoke) so device access is serialized by hardware_service's
    # per-VISA-address lock and the pyvisa session is cached/owned there —
    # the same single-owner path supply/battery use. The composite driver
    # methods (apply_setpoint/read_setpoint/get_state_dict) make each action a
    # single /invoke, so a mode+setpoint transaction can't interleave with a
    # concurrent request on the same instrument.
    from lager.exceptions import ELoadBackendError
    dev = _hw_proxy(netname, "eload", helpers._eload_module_for_instrument,
                    ELoadBackendError)

    if action == "state":
        state = dev.get_state_dict()
        msg = "Mode %s, %s, V %.3f, I %.3f, P %.3f" % (
            state["mode"], "Enabled" if state["input_enabled"] else "Disabled",
            state["measured_voltage"], state["measured_current"],
            state["measured_power"])
        return _ok(msg, state)

    if action not in ("cc", "cv", "cr", "cp"):
        raise UnknownAction(action)
    value = params.get("value")
    if value is not None:
        res = dev.apply_setpoint(action, float(value))
    else:
        res = dev.read_setpoint(action)
    return _ok(str(res), res)


def _spi(netname, role, action, params):
    # SPI bus transactions run inside hardware_service (spi_hs adapter) under the
    # bus device's shared device_id lock, so a full transfer can't interleave
    # with another request on the same device. Config persistence stays box-side
    # (a saved_nets.json write); the effective config is then applied to the
    # hardware_service-owned driver. Returned `value` is the raw word list; the
    # CLI formats it (hex/bytes/json + word size).
    spi_disp = importlib.import_module("lager.protocols.spi.dispatcher")
    dev = _proxy(netname, role)

    if action == "config":
        cfg = {}
        for key in ("mode", "bit_order", "frequency_hz", "word_size",
                    "cs_active", "cs_mode"):
            if params.get(key) is not None:
                cfg[key] = params[key]
        if cfg.get("frequency_hz") is not None and int(cfg["frequency_hz"]) <= 0:
            raise ValueError("Invalid SPI frequency: %sHz" % cfg["frequency_hz"])
        # Apply to the live driver, persist explicit overrides, read back effective.
        dev.config(cfg)
        if cfg:
            spi_disp._persist_params(netname, **cfg)
        rec = spi_disp.helpers.find_saved_net(netname, spi_disp.SPIBackendError)
        effective = spi_disp._get_spi_params(rec)
        msg = ("SPI configured: mode=%s, freq=%sHz, word_size=%s, "
               "bit_order=%s, cs_active=%s, cs_mode=%s" % (
                   effective["mode"], effective["frequency_hz"],
                   effective["word_size"], effective["bit_order"],
                   effective["cs_active"], effective["cs_mode"]))
        return _ok(msg, effective)

    if action not in ("transfer", "read", "write", "read_write"):
        raise UnknownAction(action)
    overrides = params.get("overrides") or None
    fill = int(params.get("fill", 0xFF))
    keep_cs = bool(params.get("keep_cs", False))
    if action == "read":
        out = dev.read(int(params["n_words"]), fill, keep_cs, overrides)
    elif action in ("write", "read_write"):
        # Full-duplex transfer of exactly the supplied words.
        out = dev.read_write([int(b) for b in (params.get("data") or [])],
                             keep_cs, overrides)
    else:
        # transfer: pad/truncate data to n_words with fill (default 0xFF)
        out = dev.transfer([int(b) for b in (params.get("data") or [])],
                           params.get("n_words"), fill, keep_cs, overrides)
    words = [int(w) for w in out["words"]]
    res = _ok(" ".join("%02X" % w for w in words) or "(no data)", words)
    res["word_size"] = int(out["word_size"])
    return res


def _i2c(netname, role, action, params):
    # I2C transactions run inside hardware_service (i2c_hs adapter) under the bus
    # device's shared device_id lock. Config persistence stays box-side; the
    # effective config is applied to the hardware_service-owned driver.
    i2c_disp = importlib.import_module("lager.protocols.i2c.dispatcher")
    dev = _proxy(netname, role)

    if action == "config":
        freq = params.get("frequency_hz")
        pull_ups = params.get("pull_ups")
        if freq is not None and int(freq) <= 0:
            raise ValueError("Invalid I2C frequency: %sHz" % freq)
        rec = i2c_disp.helpers.find_saved_net(netname, i2c_disp.I2CBackendError)
        stored = rec.get("params", {})
        eff_freq = freq if freq is not None else stored.get("frequency_hz", 100_000)
        eff_pull_ups = pull_ups if pull_ups is not None else stored.get("pull_ups", False)
        dev.config(eff_freq, eff_pull_ups)
        persist = {}
        if freq is not None:
            persist["frequency_hz"] = freq
        if pull_ups is not None:
            persist["pull_ups"] = pull_ups
        if persist:
            i2c_disp._persist_params(netname, **persist)
        return _ok(
            "I2C configured: freq=%sHz, pull_ups=%s" % (
                eff_freq, "on" if eff_pull_ups else "off"),
            {"frequency_hz": eff_freq, "pull_ups": bool(eff_pull_ups)})

    if action not in ("scan", "read", "write", "transfer"):
        raise UnknownAction(action)
    overrides = params.get("overrides") or None
    if action == "scan":
        found = [int(a) for a in dev.scan(
            params.get("start_addr"), params.get("end_addr"), overrides)]
        if found:
            msg = "Found %d device(s): %s" % (
                len(found), ", ".join("0x%02x" % a for a in found))
        else:
            msg = "No devices found"
        return _ok(msg, found)
    if action == "read":
        result = [int(b) for b in dev.read(
            int(params["address"]), int(params["num_bytes"]), overrides)]
        return _ok(" ".join("%02X" % b for b in result) or "(no data)", result)
    if action == "write":
        data = [int(b) for b in (params.get("data") or [])]
        dev.write(int(params["address"]), data, overrides)
        return _ok("Wrote %d byte(s) to 0x%02x" % (len(data), int(params["address"])))
    # transfer: write then read in one transaction (repeated start)
    data = [int(b) for b in (params.get("data") or [])]
    result = [int(b) for b in dev.write_read(
        int(params["address"]), data, int(params["num_bytes"]), overrides)]
    return _ok(" ".join("%02X" % b for b in result) or "(no data)", result)


def _energy_analyzer(netname, role, action, params):
    # Joulescope JS220 / Nordic PPK2 via the energy_hs adapter, run inside
    # hardware_service under the device's shared device_id lock (SAME id as a
    # watt-meter net on the same physical unit, so the two serialize). A
    # measurement must finish before the next starts on the same device.
    if action not in ("read_stats", "read_energy"):
        raise UnknownAction(action)
    default = 10.0 if action == "read_energy" else 1.0
    duration = float(params.get("duration") or default)
    # Clamp to the box's safe measurement window (matches the old :5000 path's
    # 120s CLI budget). Direct :9000 callers (CLI/Rust crate) are not proxied
    # through Nginx, so long-held requests are fine; the control plane's
    # dashboard path clamps itself to 30s to stay under Nginx's 60s
    # proxy_read_timeout.
    duration = max(0.1, min(duration, 120.0))
    dev = _proxy(netname, role, timeout=duration + 15.0)
    r = dev.measure(action, duration)
    if action == "read_energy":
        msg = "%.4f J (%.4f C) over %.1f s" % (
            float(r.get("energy_j") or 0), float(r.get("charge_c") or 0),
            float(r.get("duration_s") or duration))
    else:
        c = r.get("current") or {}
        v = r.get("voltage") or {}
        p = r.get("power") or {}
        msg = "I %.6f A, V %.3f V, P %.6f W (mean over %.1f s)" % (
            float(c.get("mean") or 0), float(v.get("mean") or 0),
            float(p.get("mean") or 0), duration)
    return _ok(msg, r)


def _arm(netname, role, action, params):
    # Dexarm robot arm via the arm_hs adapter: hardware_service owns the
    # serial handle (opened once, cached) and serializes every call under the
    # arm's device_id lock, so two concurrent commands can't interleave
    # G-code. Moves block on the box until the arm reaches the target, so the
    # internal proxy timeout is widened past the caller's move timeout.
    if action == "position":
        pos = _proxy(netname, role).position()
        return _ok("X: %s Y: %s Z: %s" % tuple(pos), [float(v) for v in pos])

    if action in ("move", "move_by"):
        timeout = float(params.get("timeout") or 15.0)
        dev = _proxy(netname, role, timeout=timeout + 15.0)
        if action == "move":
            for key in ("x", "y", "z"):
                if params.get(key) is None:
                    raise KeyError(key)
            pos = dev.move(float(params["x"]), float(params["y"]),
                           float(params["z"]), timeout=timeout)
        else:
            pos = dev.move_by(float(params.get("dx") or 0.0),
                              float(params.get("dy") or 0.0),
                              float(params.get("dz") or 0.0),
                              timeout=timeout)
        return _ok("X: %s Y: %s Z: %s" % tuple(pos), [float(v) for v in pos])

    dev = _proxy(netname, role, timeout=30.0)
    if action == "go_home":
        dev.go_home()
        return _ok("Arm moving to home position (X0 Y300 Z0)")
    if action == "enable_motor":
        dev.enable_motor()
        return _ok("Arm motors enabled")
    if action == "disable_motor":
        dev.disable_motor()
        return _ok("Arm motors disabled")
    if action == "read_and_save_position":
        pos = dev.read_and_save_position()
        return _ok("Saved position X: %s Y: %s Z: %s" % tuple(pos),
                   [float(v) for v in pos])
    if action == "set_acceleration":
        acceleration = params.get("acceleration")
        travel = params.get("travel_acceleration")
        if acceleration is None:
            raise KeyError("acceleration")
        if travel is None:
            raise KeyError("travel_acceleration")
        retract = int(params.get("retract_acceleration") or 60)
        dev.set_acceleration(int(acceleration), int(travel),
                             retract_acceleration=retract)
        return _ok(
            "Acceleration set (M204): print=%d travel=%d retract=%d"
            % (int(acceleration), int(travel), retract))
    raise UnknownAction(action)


def _webcam_box_ip(params):
    """Resolve the IP viewers should use in stream URLs: an explicit
    params['box_ip'] wins, else the host the caller reached us at."""
    box_ip = params.get("box_ip")
    if box_ip:
        return str(box_ip)
    host = request.host or "localhost"
    return host.rsplit(":", 1)[0]


def _webcam(netname, role, action, params):
    # In-process: WebcamService already manages its own stream subprocesses
    # and state file, so there is no device handle to cache or lock.
    from lager.automation import webcam as webcam_svc

    box_ip = _webcam_box_ip(params)
    if action == "start":
        rec = _find_rec(netname, role, DeviceError)
        video_device = rec.get("pin")
        if not video_device:
            raise ValueError(
                "Net '%s' does not have a video device configured." % netname)
        video_device = str(video_device)
        if not video_device.startswith("/dev/"):
            video_device = "/dev/" + video_device
        try:
            result = webcam_svc.start_stream(netname, video_device, box_ip)
        except RuntimeError as e:
            raise DeviceError(str(e))
        msg = ("Stream already running at %s" if result.get("already_running")
               else "Stream started at %s") % result["url"]
        return _ok(msg, {
            "url": result["url"],
            "port": result["port"],
            "already_running": bool(result.get("already_running", False)),
        })

    if action == "stop":
        stopped = bool(webcam_svc.stop_stream(netname))
        msg = "Stream stopped" if stopped else "Stream not running"
        return _ok(msg, {"stopped": stopped})

    if action in ("url", "status"):
        info = webcam_svc.get_stream_info(netname, box_ip)
        if not info:
            return _ok("No active stream for net '%s'" % netname,
                       {"running": False})
        return _ok("Streaming at %s" % info["url"], {
            "running": True,
            "url": info["url"],
            "port": info["port"],
            "video_device": info["video_device"],
        })

    raise UnknownAction(action)


def _router(netname, role, action, params):
    # MikroTik router via its REST API — a pure `requests` client, so it runs
    # in-process (no device caching or locking needed; the router serializes
    # its own configuration changes).
    from lager.nets.constants import NetType

    router = Net.get_from_saved_json(netname, NetType.Router)
    if router is None:
        raise DeviceError("Router net '%s' could not be loaded" % netname)

    def _kwargs(*exclude):
        return {k: v for k, v in params.items() if k not in exclude}

    try:
        # -- Read-only / system --
        if action == "connect":
            result = router.connect()
        elif action == "system_info":
            result = router.get_system_info()
        elif action == "interfaces":
            result = router.get_interfaces()
        elif action == "wireless_interfaces":
            result = router.get_wireless_interfaces()
        elif action == "wireless_clients":
            result = router.get_wireless_clients()
        elif action == "dhcp_leases":
            result = router.get_dhcp_leases()
        elif action == "security_profiles":
            result = router.get_security_profiles()
        elif action == "access_list":
            result = router.get_access_list()

        # -- System actions --
        elif action == "reboot":
            result = router.reboot()
        elif action == "wait_for_ready":
            result = {"ready": router.wait_for_ready(
                timeout=params.get("timeout", 120))}

        # -- Interface control --
        elif action == "set_interface_disabled":
            result = router.set_interface_disabled(
                params["interface"], params["disabled"])
        elif action == "enable_interface":
            router.enable_interface(params["interface"])
            result = {"interface": params["interface"], "disabled": False}
        elif action == "disable_interface":
            router.disable_interface(params["interface"])
            result = {"interface": params["interface"], "disabled": True}
        elif action == "wait_for_wireless_ready":
            result = {"ready": router.wait_for_wireless_ready(
                params["interface"], timeout=params.get("timeout", 30))}

        # -- Wireless configuration --
        elif action == "set_wireless_ssid":
            result = router.set_wireless_ssid(params["interface"], params["ssid"])
        elif action == "configure_wireless":
            result = router.configure_wireless(
                params["interface"], **(params.get("kwargs") or {}))

        # -- Security profiles --
        elif action == "create_security_profile":
            result = router.create_security_profile(
                name=params["name"],
                mode=params.get("mode", "dynamic-keys"),
                authentication_types=params.get("authentication_types", "wpa2-psk"),
                unicast_ciphers=params.get("unicast_ciphers", "aes-ccm"),
                wpa2_pre_shared_key=params.get("wpa2_pre_shared_key", ""),
                wpa_pre_shared_key=params.get("wpa_pre_shared_key", ""),
            )
        elif action == "create_open_security_profile":
            result = router.create_open_security_profile(params.get("name", "open"))
        elif action == "update_security_profile_password":
            result = {"updated": router.update_security_profile_password(
                params["name"], params["new_password"])}
        elif action == "delete_security_profile":
            result = {"deleted": router.delete_security_profile(params["name"])}

        # -- DHCP --
        elif action == "enable_dhcp":
            router.enable_dhcp()
            result = {"dhcp": "enabled"}
        elif action == "disable_dhcp":
            router.disable_dhcp()
            result = {"dhcp": "disabled"}
        elif action == "set_dhcp_lease_time":
            router.set_dhcp_lease_time(params.get("lease_time", "10m"))
            result = {"lease_time": params.get("lease_time", "10m")}

        # -- Bandwidth limits --
        elif action == "add_bandwidth_limit":
            result = router.add_bandwidth_limit(
                target=params["target"],
                max_limit=params["max_limit"],
                name=params.get("name"),
            )
        elif action == "remove_bandwidth_limits":
            router.remove_bandwidth_limits()
            result = {"removed": True}

        # -- Firewall --
        elif action == "add_firewall_rule":
            result = router.add_firewall_rule(
                chain=params.get("chain", "forward"),
                action=params.get("rule_action", "drop"),
                **_kwargs("chain", "rule_action"),
            )
        elif action == "remove_firewall_rules":
            router.remove_firewall_rules()
            result = {"removed": True}
        elif action == "block_internet":
            router.block_internet()
            result = {"blocked": "internet"}
        elif action == "block_dns":
            router.block_dns()
            result = {"blocked": "dns"}
        elif action == "block_port":
            router.block_port(params["port"], params.get("protocol", "tcp"))
            result = {"blocked": "%s/%s" % (params.get("protocol", "tcp"),
                                            params["port"])}

        # -- Access list --
        elif action == "add_access_list_entry":
            result = router.add_access_list_entry(
                mac_address=params["mac_address"],
                authentication=params.get("authentication", True),
                interface=params.get("interface", ""),
                signal_range=params.get("signal_range", ""),
            )
        elif action == "remove_access_list_entry":
            router.remove_access_list_entry(params["mac_address"])
            result = {"removed": params["mac_address"]}
        elif action == "clear_access_list":
            router.clear_access_list()
            result = {"cleared": True}

        # -- Test reset --
        elif action == "reset_to_defaults":
            router.reset_to_defaults(
                baseline_ssid=params.get("baseline_ssid"),
                baseline_pass=params.get("baseline_pass"),
                wireless_interfaces=params.get("wireless_interfaces"),
            )
            result = {"reset": True}

        # -- Raw API --
        elif action == "run":
            result = router.run(params.get("path", ""),
                                params=params.get("params"))

        else:
            raise UnknownAction(action)
    except (UnknownAction, KeyError, ValueError):
        raise
    except (ConnectionFailed, DeviceError, LagerBackendError):
        raise
    except Exception as e:
        # requests transport errors, MikroTik API errors -> 502 hardware error
        raise DeviceError(str(e))

    return _ok("router %s ok" % action, result)


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
    "arm": _arm,
    "webcam": _webcam,
    "router": _router,
    "mikrotik": _router,  # saved-net role alias (NetType.from_role)
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
