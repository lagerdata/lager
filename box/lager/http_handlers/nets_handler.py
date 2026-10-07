# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Nets HTTP handler for the Lager Box HTTP server.

Provides endpoints to list, update, delete, and query live state of saved nets.
"""

import contextlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from flask import Flask, Response, jsonify, request

from ..exceptions import I2CBackendError, SPIBackendError
from ..nets.net import Net

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-role brief state probes — each returns a short string summarising the
# net's live hardware state (e.g. "on/3.30V/0.12A", "HIGH (1)", "disabled").
# A probe MUST NOT raise; on any error it returns None so the caller can
# display "–" and move on.
# ---------------------------------------------------------------------------

# Whole-request deadline for /nets/state, in seconds. Not per-probe: individual
# probes cannot be interrupted (they are blocked in a driver's USB call or on an
# instrument lock), so what is bounded is how long the endpoint waits before it
# answers with nulls for whatever has not come back. Sized to stay well inside
# the CLI's own 30s HTTP timeout.
_STATE_TIMEOUT = 8

# Per-instrument budget inside that deadline, so one unresponsive instrument is
# cut short on its own instead of holding its nets until the whole request
# gives up. The clock starts when the group starts running, not when it is
# queued behind the worker cap.
#
# 3s: a healthy sweep measures 2.2-2.8s end to end, and since groups run in
# parallel that is the slowest single group -- usually a multi-channel supply,
# which reads one channel after another. So the budget grows by
# _GROUP_BUDGET_PER_NET_S for each net after the first wherever the reads are
# sequential (see _group_budget); a flat 3s would leave a healthy three-channel
# supply a few hundred milliseconds of headroom, and a false timeout costs that
# instrument a whole cooldown. The LabJack batch reads every net in one
# operation and gets the base. USB hubs keep the whole request deadline; see
# _group_budget for why.
_GROUP_BUDGET_S = 3.0
_GROUP_BUDGET_PER_NET_S = 1.0

# How long a group that timed out or found its instrument busy is answered from
# memory instead of being probed again. Matches hardware_service's
# _INVOKE_DEADLINE_S: the longest an abandoned probe can keep the device lock
# and still give it back. Probing again inside that window only queues behind
# the leftover call; past it, the leftover has either finished or the service
# has restarted itself.
_COOLDOWN_S = 30.0


_UD_INSTRUMENT_RE = re.compile(r"labjack[_\-\s]*u[36]", re.IGNORECASE)


def _ud_pin_span_error(data):
    """Reject a LabJack UD spi/i2c net whose pins the hardware cannot drive.

    Returns an error string, or None when the record is fine or is not a UD
    spi/i2c net.

    This runs at CREATE time on purpose. Without it the only guard is in the
    driver's ``_resolve_pin``, which does not run until the first transaction --
    so a net naming FIO0-FIO3 on a U3-HV saved cleanly, listed cleanly, and only
    failed later, at the point of use, a long way from the mistake. ``lager nets
    add`` already refuses these via ``_channel_rejection_hint``; this closes the
    same hole for the HTTP API and the net TUI, which reach this handler
    directly.

    The two parsers are deliberately kept apart -- a U3's SPI span is
    CS/CLK/MISO/MOSI while a T7's is CS/CLK/MOSI/MISO -- so this dispatches to
    whichever one owns the record rather than reimplementing either.
    """
    role = str(data.get("role", "")).lower()
    if role not in ("spi", "i2c"):
        return None
    if not _UD_INSTRUMENT_RE.search(str(data.get("instrument", ""))):
        return None

    try:
        if role == "spi":
            from ..protocols.spi.dispatcher import _ud_spi_pin_config
            config = _ud_spi_pin_config(data)
        else:
            from ..protocols.i2c.dispatcher import _ud_pin_config
            config = _ud_pin_config(data)
    except (SPIBackendError, I2CBackendError) as exc:
        # These two carry a specific, net-named message the dispatchers wrote
        # for a user to read, so pass it through verbatim.
        return str(exc)
    except Exception:
        # Anything else is a defect in the parser, not something the caller
        # did wrong, and an arbitrary exception's text can carry internal
        # paths or state to an HTTP client. Log it where an operator will see
        # it and hand back a fixed string.
        logger.exception("Unexpected error validating a LabJack UD %s net", role)
        return (
            f"Could not validate the {role} pin configuration for this net. "
            f"See the box log for details."
        )

    usable = "FIO4-FIO7" if role == "spi" else "FIO6-FIO7"
    for key, dio in sorted(config.items()):
        if isinstance(dio, int) and dio <= 3:
            signal = key.replace("_pin", "").upper()
            return (
                f"{signal} pin FIO{dio} cannot be used for {role.upper()}. "
                f"FIO0-FIO3 are the U3-HV's fixed high-voltage analog inputs "
                f"and no configuration makes them digital. Use {usable}, "
                f"EIO0-EIO7 or CIO0-CIO3 (EIO and CIO need the DB15). Those "
                f"pins are readable as an adc net on AIN0-AIN3, the same "
                f"physical pins."
            )
    return None


def _on_off(enabled):
    """The on/off slot of a brief: ``?`` when the instrument did not say.

    ``"on" if enabled else "off"`` rendered an unreadable output state
    (``None``) as a confident "off" -- the one wrong answer a state report must
    not give. ``?`` matches the ``?V``/``?A`` already used for unread values.
    """
    if enabled is None:
        return "?"
    return "on" if enabled else "off"


def _enabled_or_none(value):
    """A driver's output state as True/False, or None when it is not a bool."""
    return value if isinstance(value, bool) else None


def _supply_brief(channel, state):
    """One supply channel's monitor state as ``(CH<n>/<on|off>/<V>/<I>, enabled)``."""
    enabled = _enabled_or_none(state.get("enabled"))
    v = state.get("voltage")
    i = state.get("current")
    v_s = "%.2fV" % v if v is not None else "?V"
    i_s = "%.3fA" % i if i is not None else "?A"
    parts = ["CH%s" % channel, _on_off(enabled), v_s, i_s]
    return "/".join(parts), enabled


def _brief_supply(netname):
    """Power-supply: CH<n>/<on|off>/<V>/<I>."""
    from ..dispatchers.helpers import resolve_net_proxy
    from ..exceptions import SupplyBackendError
    from ..nets.device import Device
    try:
        device_name, net_info, channel = resolve_net_proxy(
            netname, "power-supply", SupplyBackendError)
        supply = Device(device_name, net_info)
        return _supply_brief(channel, supply.get_monitor_state(channel))
    except Exception as e:
        logger.debug("brief_supply %s: %s", netname, e)
        return None


def _brief_supply_batch(netnames, causes=None, codes=None, deadline=None):
    """Every channel of one supply in ONE hardware_service call.

    The group is one physical supply (``_group_key`` includes the address), so
    its channels share one cached driver and one device lock. Reading them as
    one ``get_monitor_states`` call pays one HTTP round trip, one lock
    acquisition and one liveness ``*IDN?`` instead of one per channel; the
    per-channel SCPI reads are the same.

    Falls back to ``_brief_supply`` per net when hardware_service does not
    have the method, so a driver outside ``SupplyNet`` keeps working.

    Returns dict[net name, (brief, enabled) | None].
    """
    from ..dispatchers.helpers import resolve_net_proxy
    from ..exceptions import SupplyBackendError
    from ..nets.device import Device, DeviceError, call_limits

    out = {n: None for n in netnames}
    channels = {}
    device_name = net_info = None
    for name in netnames:
        try:
            dev, info, channel = resolve_net_proxy(
                name, "power-supply", SupplyBackendError)
        except Exception as e:
            logger.debug("brief_supply_batch %s: %s", name, e)
            if causes is not None:
                causes[name] = f"{type(e).__name__}: {e}"
            continue
        channels[name] = channel
        if device_name is None:
            device_name, net_info = dev, info
    if not channels:
        return out

    limits = (call_limits(deadline) if deadline is not None
              else contextlib.nullcontext([]))
    with limits as failures:
        try:
            states = Device(device_name, net_info).get_monitor_states(
                sorted(set(channels.values()), key=str))
        except DeviceError as e:
            if "Function not found" not in str(e):
                states = None
            else:
                logger.debug("brief_supply_batch: no get_monitor_states on "
                             "%s; reading per channel", device_name)
                for name in channels:
                    out[name] = _brief_supply(name)
                return out
        except Exception as e:
            logger.debug("brief_supply_batch %s: %s", netnames, e)
            states = None

    if states is None:
        if failures:
            rec = {"instrument": (net_info or {}).get("instrument")}
            reason, code = _device_failure(rec, failures[-1])
            detail = reason.split(": ", 1)[1] if reason.startswith("unreadable: ") else reason
            for name in channels:
                if causes is not None:
                    causes[name] = detail
                if codes is not None and code:
                    codes[name] = code
        return out

    for name, channel in channels.items():
        state = states.get(str(channel)) if isinstance(states, dict) else None
        if isinstance(state, dict):
            out[name] = _supply_brief(channel, state)
    return out


def _brief_battery(netname):
    """Battery: CH<n>/<on|off>/<Vterm>/<I>/<SOC%>."""
    from ..dispatchers.helpers import resolve_net_proxy
    from ..exceptions import BatteryBackendError
    from ..nets.device import Device
    try:
        device_name, net_info, channel = resolve_net_proxy(
            netname, "battery", BatteryBackendError)
        battery = Device(device_name, net_info)
        state = battery.get_monitor_state(channel)
        enabled = _enabled_or_none(state.get("enabled"))
        v = state.get("terminal_voltage")
        i = state.get("current")
        soc = state.get("soc")
        v_s = "%.2fV" % v if v is not None else "?V"
        i_s = "%.3fA" % i if i is not None else "?A"
        soc_s = "%d%%" % soc if soc is not None else "?%"
        return "/".join(["CH%s" % channel, _on_off(enabled), v_s, i_s, soc_s]), enabled
    except Exception as e:
        logger.debug("brief_battery %s: %s", netname, e)
        return None


def _brief_usb(netname):
    """USB hub port: enabled/disabled."""
    from ..automation import usb_hub
    try:
        enabled = usb_hub.state(netname)
        return "enabled" if enabled else "disabled"
    except Exception as e:
        logger.debug("brief_usb %s: %s", netname, e)
        return None


def _brief_usb_batch(netnames, causes=None, codes=None, deadline=None):
    """USB hub ports for several nets, grouped by physical hub.

    The per-net probe costs a full hub open/read/close under that hub's lock, so
    a bench with a dozen USB nets on three hubs pays twelve enumerate/connect/
    disconnect cycles in three serialised lanes -- seconds, not milliseconds.
    ``usb_hub.states`` groups by hub and pays one cycle per hub.

    Args:
        netnames: USB net names to read.
        causes: optional dict, filled in place with ``net name -> "Type: msg"``
            for nets that came back None with a known cause. Passed through to
            ``usb_hub.states``; also filled here when the whole call fails.
        codes: optional dict, filled in place with ``net name -> classification``
            for the same nets. Left empty when the whole call fails: a failure
            to load net definitions says nothing about the state of the bus,
            and a code that names the wrong fault is worse than none.
        deadline: optional absolute ``time.monotonic()`` budget, forwarded to
            the dispatcher so the serialised per-hub loop can sub-budget it
            (issue #205).

    Returns:
        dict[str, str | None]: net name -> "enabled"/"disabled", or None.
    """
    from ..automation import usb_hub
    try:
        raw = usb_hub.states(netnames, causes=causes, codes=codes,
                             deadline=deadline)
    except Exception as e:
        logger.debug("brief_usb_batch %s: %s", netnames, e)
        # Everything below the dispatcher's own per-hub guard lands here --
        # loading the net definitions, building a controller for an unsupported
        # instrument. One cause for every net, since none of them was reached.
        if causes is not None:
            cause = f"{type(e).__name__}: {e}"
            for n in netnames:
                causes[n] = cause
        return {n: None for n in netnames}

    out = {}
    for name in netnames:
        value = raw.get(name)
        out[name] = None if value is None else ("enabled" if value else "disabled")
    return out


_LABJACK_T7_RE = re.compile(r"labjack[_\-\s]*t7", re.IGNORECASE)


def _is_labjack_t7(rec):
    """True if this net's instrument is a LabJack the batch endpoint can read.

    Deliberately narrower than "is a LabJack". ``/labjack/batch_read`` speaks
    LJM Modbus register names (``DIO_STATE``, ``AIN0``, ``DAC0``), and only the
    T-series answers those. A U3/U6 is every bit a LabJack but talks Exodriver,
    so a bare ``"labjack" in instrument`` test would route it into the T7 read
    path and report values that came from the wrong device -- or from no device
    at all. Non-T7 LabJacks fall through to the per-role probe instead.
    """
    return bool(_LABJACK_T7_RE.search(rec.get("instrument") or ""))


_LABJACK_BATCH_ROLES = {"gpio", "adc", "dac"}


def _record_pin(rec):
    """``pin``, else ``channel``, else ``""`` -- skipping only absent or blank.

    ``rec.get("pin") or rec.get("channel")`` treated an integer pin of 0 as
    missing, so FIO0/AIN0/DAC0 nets read as having no pin.
    """
    for key in ("pin", "channel"):
        value = rec.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return ""


def _brief_labjack_batch(recs, causes=None, codes=None, deadline=None):
    """Batch probe for all GPIO/ADC/DAC nets on one LabJack T7.

    Delegates to hardware_service ``POST /labjack/batch_read`` which owns
    the LabJack USB handle.  One HTTP call, register-level reads for GPIO
    (no direction mutation), batched eReadNames for AIN/DAC.

    Sends ``device_id`` -- the SAME identity ``/invoke`` locks on, from
    ``_physical_device_id`` -- so the batch read serialises against a
    concurrent ``lager gpo``/``gpi``/``adc``/``dac`` on this T7. Deriving it
    here rather than letting the endpoint guess is the point: every net in the
    group already resolves to one physical device, and a key invented at the
    other end is not guaranteed to be the same string.

    ``deadline`` (absolute ``time.monotonic()``) cuts both the HTTP timeout
    and the endpoint's wait for the device lock to the time left, so a T7 whose
    lock an abandoned call still holds answers "busy" inside the group's budget.
    Without it the lock wait is the endpoint's own 8s, which this call's 5s
    timeout used to give up on before the answer arrived. ``causes``/``codes``
    are filled, as for the other batch probes, when the endpoint says busy.

    Returns dict[netname, brief_str | None].
    """
    import requests as _req

    from .net_command import _physical_device_id

    payload = [
        {"name": r.get("name", ""), "role": r.get("role", ""),
         "pin": _record_pin(r)}
        for r in recs
    ]
    device_id = _physical_device_id(
        recs[0].get("role", ""), recs[0].get("instrument", "") or "", recs[0])
    body = {"nets": payload, "device_id": device_id}
    timeout = 5.0
    if deadline is not None:
        remaining = max(deadline - time.monotonic(), 0.05)
        timeout = min(timeout, remaining)
        body["lock_timeout_s"] = remaining
    names = [r.get("name", "") for r in recs]
    try:
        resp = _req.post("http://localhost:8080/labjack/batch_read",
                         json=body, timeout=timeout)
        if resp.ok:
            if resp.headers.get("X-Lager-Device-Busy") == "1":
                for n in names:
                    if causes is not None:
                        causes[n] = "device-busy: the LabJack is in use by another operation"
                    if codes is not None:
                        codes[n] = CODE_BUSY
            return resp.json()
    except _req.Timeout as e:
        logger.debug("labjack batch_read call timed out: %s", e)
        for n in names:
            if causes is not None:
                causes[n] = "no answer from the LabJack within %.1fs" % timeout
            if codes is not None:
                codes[n] = CODE_TIMEOUT
    except Exception as e:
        logger.debug("labjack batch_read call failed: %s", e)

    return {n: None for n in names}


# Roles that can answer for several nets in one instrument session. Anything
# absent here falls back to the per-net probe in _BRIEF_PROBES.
# Role -> batch probe. A probe here MUST accept
# ``(netnames, *, causes=None, codes=None, deadline=None)``: `_probe_group`
# passes all three unconditionally, so a probe that omits any raises TypeError
# at the call site rather than silently losing the diagnostics (or the budget).
_BATCH_PROBES = {
    "usb": _brief_usb_batch,
    "power-supply": _brief_supply_batch,
}


def _brief_gpio(netname):
    """GPIO net: HIGH (1) / LOW (0) — fallback for non-LabJack instruments.

    LabJack GPIO nets are handled by ``_brief_labjack_batch`` (routed through
    hardware_service) and never reach this function.
    """
    from .net_command import _proxy
    try:
        dev = _proxy(netname, "gpio")
        v = int(dev.input())
        return "HIGH (1)" if v else "LOW (0)"
    except Exception as e:
        logger.debug("brief_gpio %s: %s", netname, e)
        return None


def _brief_adc(netname):
    """ADC read — fallback for non-LabJack instruments."""
    from .net_command import _proxy
    try:
        v = float(_proxy(netname, "adc").input())
        return "%.4fV" % v
    except Exception as e:
        logger.debug("brief_adc %s: %s", netname, e)
        return None


def _brief_dac(netname):
    """DAC read — fallback for non-LabJack instruments."""
    from .net_command import _proxy
    try:
        v = float(_proxy(netname, "dac").input())
        return "%.4fV" % v
    except Exception as e:
        logger.debug("brief_dac %s: %s", netname, e)
        return None


def _brief_thermocouple(netname):
    from .net_command import _proxy
    try:
        v = float(_proxy(netname, "thermocouple").read())
        return "%.1f°C" % v
    except Exception as e:
        logger.debug("brief_tc %s: %s", netname, e)
        return None


def _brief_eload(netname):
    """E-load: <mode>/<on|off>/<V>/<I>."""
    from ..dispatchers import helpers
    from ..exceptions import ELoadBackendError
    from .net_command import _hw_proxy
    try:
        dev = _hw_proxy(netname, "eload",
                        helpers._eload_module_for_instrument,
                        ELoadBackendError)
        state = dev.get_state_dict()
        mode = state.get("mode", "?")
        enabled = _enabled_or_none(state.get("input_enabled"))
        v = state.get("measured_voltage")
        i = state.get("measured_current")
        v_s = "%.2fV" % v if v is not None else "?V"
        i_s = "%.3fA" % i if i is not None else "?A"
        return "/".join([mode, _on_off(enabled), v_s, i_s]), enabled
    except Exception as e:
        logger.debug("brief_eload %s: %s", netname, e)
        return None


def _brief_webcam(netname):
    from ..automation import webcam as webcam_svc
    try:
        info = webcam_svc.get_stream_info(netname, "localhost")
        if not info:
            return "stopped"
        return "streaming %s" % info.get("url", "")
    except Exception as e:
        logger.debug("brief_webcam %s: %s", netname, e)
        return None


def _fmt_reading(value, fmt, missing):
    """Format one measured value, or ``missing`` when it was not reported.

    A field the instrument did not return used to default to 0, and 0.0000 A
    is a real, plausible reading -- indistinguishable from an idle load.
    """
    if value is None:
        return missing
    try:
        return fmt % float(value)
    except (TypeError, ValueError):
        return missing


def _brief_watt(netname):
    """Watt-meter: quick 0.1s reading → I/V/P."""
    from .net_command import _proxy
    try:
        dev = _proxy(netname, "watt-meter", timeout=15.0)
        r = dev.measure("all", 0.1) or {}
        return "/".join((_fmt_reading(r.get("current"), "%.4fA", "?A"),
                         _fmt_reading(r.get("voltage"), "%.3fV", "?V"),
                         _fmt_reading(r.get("power"), "%.4fW", "?W")))
    except Exception as e:
        logger.debug("brief_watt %s: %s", netname, e)
        return None


def _brief_energy_analyzer(netname):
    """Energy-analyzer: quick 0.5s stats → I/V/P (mean)."""
    from .net_command import _proxy
    try:
        dev = _proxy(netname, "energy-analyzer", timeout=15.0)
        r = dev.measure("read_stats", 0.5)
        c = r.get("current") or {}
        v = r.get("voltage") or {}
        p = r.get("power") or {}
        return "/".join((_fmt_reading(c.get("mean"), "%.4fA", "?A"),
                         _fmt_reading(v.get("mean"), "%.3fV", "?V"),
                         _fmt_reading(p.get("mean"), "%.4fW", "?W")))
    except Exception as e:
        logger.debug("brief_ea %s: %s", netname, e)
        return None


def _brief_arm(netname):
    """Robot arm: current X/Y/Z position."""
    from .net_command import _proxy
    try:
        pos = _proxy(netname, "arm").position()
        return "X%.1f/Y%.1f/Z%.1f" % tuple(float(v) for v in pos)
    except Exception as e:
        logger.debug("brief_arm %s: %s", netname, e)
        return None


def _brief_debug(netname):
    """Debug probe: connected/<backend> or disconnected."""
    try:
        from ..debug.probes import resolve_backend, resolve_serial_from_net
        from ..debug.gdbserver import get_jlink_gdbserver_status
        from ..debug.openocd import get_openocd_status
        from ..debug.probes import BACKEND_OPENOCD

        rec = None
        for entry in Net.get_local_nets():
            if entry.get("name") == netname and entry.get("role") == "debug":
                rec = entry
                break
        if rec is None:
            return None

        backend = resolve_backend(rec)
        serial = resolve_serial_from_net(rec)

        if backend == BACKEND_OPENOCD:
            st = get_openocd_status(serial=serial)
        else:
            st = get_jlink_gdbserver_status(serial=serial)

        if st.get("running"):
            return "connected/%s" % backend
        return "disconnected"
    except Exception as e:
        logger.debug("brief_debug %s: %s", netname, e)
        return None


def _brief_solar(netname):
    """Solar simulator: read irradiance / Voc / Isc if running."""
    from ..dispatchers import helpers
    from ..exceptions import SolarBackendError
    from .net_command import _hw_proxy
    try:
        dev = _hw_proxy(netname, "solar",
                        helpers._solar_module_for_instrument,
                        SolarBackendError, timeout=30.0)
        irr = str(dev.irradiance())
        return "irr:%s" % irr
    except Exception as e:
        logger.debug("brief_solar %s: %s", netname, e)
        return None


def _brief_router(netname):
    """MikroTik router: connected/uptime."""
    from ..nets.constants import NetType
    try:
        router = Net.get_from_saved_json(netname, NetType.Router)
        if router is None:
            return None
        info = router.get_system_info()
        uptime = info.get("uptime", "?")
        return "up/%s" % uptime
    except Exception as e:
        logger.debug("brief_router %s: %s", netname, e)
        return None


def _brief_i2c(netname):
    """I2C bus: list detected device addresses."""
    from .net_command import _proxy
    try:
        addrs = _proxy(netname, "i2c").scan()
        if not addrs:
            return "no devices"
        return ",".join("0x%02X" % a for a in sorted(addrs))
    except Exception as e:
        logger.debug("brief_i2c %s: %s", netname, e)
        return None


# Map role -> probe function (netname) -> Optional[str]
_BRIEF_PROBES = {
    "power-supply": _brief_supply,
    "battery": _brief_battery,
    "usb": _brief_usb,
    "gpio": _brief_gpio,
    "adc": _brief_adc,
    "dac": _brief_dac,
    "thermocouple": _brief_thermocouple,
    "eload": _brief_eload,
    "webcam": _brief_webcam,
    "watt-meter": _brief_watt,
    "energy-analyzer": _brief_energy_analyzer,
    "arm": _brief_arm,
    "debug": _brief_debug,
    "solar": _brief_solar,
    "router": _brief_router,
    "mikrotik": _brief_router,
    "i2c": _brief_i2c,
}


# Why a net's state came back null. `state: null` used to mean three unrelated
# things -- the instrument was never reached before the request deadline, the
# probe failed, or the role has no probe at all -- and the response said which
# for none of them. They need different remedies, and telling them apart from
# the outside was impossible. See issue #196.
REASON_DEADLINE = "deadline"
REASON_NO_PROBE = "no probe for role"

# Fault classes the sweep itself can name, on ``reason_code`` (see below). The
# ``reason`` beside each is complete on its own; these only let a client group
# or colour them.
#
# CODE_ABSENT: the instrument's USB address is not enumerated, so it was not
#   probed at all -- unplugged, powered off, or a different unit on the bench.
# CODE_TIMEOUT: the instrument was probed and did not answer inside its group
#   budget (or the LabJack/Device call's own timeout cut it to that budget).
# CODE_BUSY: hardware_service could not get the device lock inside the budget.
#   Something else -- often a probe a previous sweep abandoned -- is using it.
# CODE_COOLDOWN: not probed this time, because the last probe timed out or
#   found the device busy moments ago; the reason quotes that earlier answer.
CODE_ABSENT = "instrument-absent"
CODE_TIMEOUT = "instrument-timeout"
CODE_BUSY = "device-busy"
CODE_COOLDOWN = "probe-cooldown"

# Codes that mean "the instrument is there but did not answer": a group whose
# every net ends on one of these goes into cooldown.
_COOLDOWN_CODES = frozenset({CODE_TIMEOUT, CODE_BUSY})


# A null entry may also carry ``reason_code``: a stable token naming the fault
# class, where the driver produced one.
#
# THE COMPATIBILITY RULE, because a future edit will otherwise break it without
# noticing: the box always sends a complete, self-sufficient human ``reason``.
# ``reason_code`` only ever UPGRADES presentation -- a colour, a remedy line,
# grouping -- and is never the sole carrier of meaning.
#
# That is what makes both directions of version skew safe with no negotiation.
# An older CLI reads only ``reason`` and never looks at the extra key. A newer
# CLI against an older box sees no code and falls back to printing ``reason``,
# which is exactly today's behaviour. Move the meaning into the code and every
# older CLI silently starts printing less than it used to.
def _unreadable(detail):
    """Reason string for a probe that ran and did not produce an answer."""
    detail = str(detail).strip()
    return f"unreadable: {detail}" if detail else "unreadable"


def _split_brief(role, value):
    """A probe's answer as ``(state text, enabled)``.

    Probes for roles with an on/off output return ``(text, enabled)``; the rest
    return the text alone. USB answers are the words ``enabled``/``disabled``,
    so their bool is read from the word rather than threaded through the hub
    batch. ``enabled`` is None whenever the on/off is not known.
    """
    if isinstance(value, tuple):
        text, enabled = value
        return text, _enabled_or_none(enabled)
    if role == "usb" and value in ("enabled", "disabled"):
        return value, value == "enabled"
    return value, None


def _entry(name, role, state, reason=None, code=None, enabled=None):
    """One net's answer.

    ``reason`` is attached only when *state* is None, so its presence means
    "this is a null, and here is why". A net with a state carries no reason.
    ``reason_code`` rides alongside it, and only alongside it -- the key is
    absent, not null, when there is no classification, so an older CLI's
    ``.get("reason_code")`` sees nothing rather than something falsy.
    """
    out = {"name": name, "role": role, "state": state}
    # Machine-readable on/off for roles that have one. Present only when known:
    # absent means "no on/off here, or it could not be read" -- never False.
    # The text in ``state`` stays complete on its own (see the compatibility
    # rule above), so a consumer that ignores this key loses nothing.
    if state is not None and isinstance(enabled, bool):
        out["enabled"] = enabled
    if state is None and reason:
        out["reason"] = reason
        if code:
            out["reason_code"] = code
    return out


def _device_failure(net_rec, exc):
    """``(reason, code)`` for a hardware_service call that failed under a budget.

    The per-net probes swallow every exception and return None, so without this
    a supply whose lock was held, or which never answered, came back null with
    no reason at all -- indistinguishable from any other empty read.
    """
    from ..nets.device import ConnectionFailed, DeviceError, describe_error
    import requests as _req

    if isinstance(exc, DeviceError) and str(exc).startswith("device-busy"):
        return _unreadable(str(exc)), CODE_BUSY
    if isinstance(exc, ConnectionFailed) and isinstance(
            exc.__cause__, _req.Timeout):
        instrument = net_rec.get("instrument") or "the instrument"
        return f"timed out: no answer from {instrument}", CODE_TIMEOUT
    return _unreadable(describe_error(exc)), None


def _probe_net_state(net_rec, deadline=None):
    """Return {"name": ..., "role": ..., "state": <str|None>[, "reason": ...]}.

    With a ``deadline`` (absolute ``time.monotonic()``), every hardware_service
    call the probe makes is bounded by it (``Device`` ``call_limits``), and a
    null answer that came from a failed call says which failure it was.
    Without one the probe runs exactly as it always has.
    """
    name = net_rec.get("name", "")
    role = net_rec.get("role", "")
    probe = _BRIEF_PROBES.get(role)
    if probe is None:
        return _entry(name, role, None, REASON_NO_PROBE)
    try:
        if deadline is None:
            state, enabled = _split_brief(role, probe(name))
            return _entry(name, role, state, enabled=enabled)
        from ..nets.device import call_limits
        with call_limits(deadline) as failures:
            state, enabled = _split_brief(role, probe(name))
        if state is None and failures:
            reason, code = _device_failure(net_rec, failures[-1])
            return _entry(name, role, None, reason, code)
        return _entry(name, role, state, enabled=enabled)
    except Exception as e:
        logger.debug("probe %s (%s) failed: %s", name, role, e)
        return _entry(name, role, None, _unreadable(f"{type(e).__name__}: {e}"))


def _unknown(net_rec, reason):
    """The "we could not find out" answer for one net, and why."""
    return _entry(net_rec.get("name", ""), net_rec.get("role", ""), None, reason)


def _timed_out(net_rec, budget=None):
    """A net whose instrument did not answer inside its group budget."""
    instrument = net_rec.get("instrument") or "the instrument"
    within = f" within {budget:.1f}s" if budget is not None else ""
    return _entry(net_rec.get("name", ""), net_rec.get("role", ""), None,
                  f"timed out: no answer from {instrument}{within}",
                  CODE_TIMEOUT)


def _group_key(net_rec):
    """Group nets that share one physical instrument into one work unit.

    Probing is per-instrument, not per-net, because instruments serialise: every
    net on one hub, LabJack or scope contends for the same lock and the same USB
    claim, so N nets on one device cost N sequential sessions no matter how wide
    the thread pool is.

    For LabJack devices with batchable roles (GPIO/ADC/DAC), we drop role from
    the key so all three land in a single work unit — one HTTP call to
    hardware_service's ``/labjack/batch_read``.  Other instruments keep role in
    the key because their batch probes are per-role.
    """
    role = net_rec.get("role", "")
    instrument = net_rec.get("instrument", "") or ""
    address = net_rec.get("address", "") or ""
    if _is_labjack_t7(net_rec) and role in _LABJACK_BATCH_ROLES:
        return ("_labjack_", instrument, address)
    return (role, instrument, address)


def _probe_group(recs, deadline=None, sink=None):
    """Probe every net in one instrument group. Returns a list of results.

    ``deadline`` is the group's absolute ``time.monotonic()`` budget. Batch
    probes that serialise several physical devices behind one group (path 2)
    sub-budget it -- see issue #205; the LabJack batch (path 1) and every
    hardware_service call on the per-net path (path 3) are bounded by it, so a
    probe behind a held device lock answers "busy" inside the budget instead
    of queueing for hardware_service's full lock wait. Without a deadline every
    path runs unbounded, as it always has.

    ``sink``, if given, is a list each result is appended to as soon as it
    exists. The per-net path produces one net at a time, and a caller that
    stops waiting on this group part-way can still use the nets it finished.

    Three dispatch paths, checked in order:

    1. **LabJack batch** — the group was keyed with ``"_labjack_"`` (see
       ``_group_key``), so it may contain GPIO + ADC + DAC nets on one T7.
       ``_brief_labjack_batch`` makes one HTTP call to hardware_service's
       ``/labjack/batch_read`` which reads registers without direction
       mutation.
    2. **Per-role batch** (``_BATCH_PROBES``) — e.g. USB hub ports.
    3. **Per-net fallback** — one ``_probe_net_state`` call per net.
    """
    out = _probe_group_entries(recs, deadline, sink)
    if sink is not None and len(sink) < len(out):
        # The batch paths answer all at once; publish them for a caller that
        # reads the sink rather than the return value.
        sink.extend(out[len(sink):])
    return out


def _probe_group_entries(recs, deadline, sink):
    """``_probe_group``'s body; ``sink`` is only appended to on path 3."""
    if not recs:
        return []

    # Path 1: cross-role LabJack batch
    if recs[0].get("role", "") in _LABJACK_BATCH_ROLES and _is_labjack_t7(recs[0]):
        causes: dict = {}
        codes: dict = {}
        try:
            states = _brief_labjack_batch(recs, causes=causes, codes=codes,
                                          deadline=deadline)
        except Exception as e:
            logger.debug("labjack batch probe failed: %s", e)
            reason = _unreadable(f"{type(e).__name__}: {e}")
            return [_unknown(rec, reason) for rec in recs]
        return [
            _entry(
                rec.get("name", ""),
                rec.get("role", ""),
                states.get(rec.get("name", "")),
                _unreadable(causes.get(rec.get("name", ""))
                            or "no value from instrument"),
                codes.get(rec.get("name", "")),
            )
            for rec in recs
        ]

    # Path 2: per-role batch (e.g. USB)
    role = recs[0].get("role", "")
    batch = _BATCH_PROBES.get(role)
    if batch is None:
        # Path 3: per-net fallback
        out = []
        for rec in recs:
            if deadline is not None and time.monotonic() >= deadline:
                # Budget spent on the nets before this one. Probing on would
                # only queue more work on an instrument already cut off.
                entry = _timed_out(rec)
            else:
                entry = _probe_net_state(rec, deadline)
            out.append(entry)
            if sink is not None:
                sink.append(entry)
        return out

    names = [rec.get("name", "") for rec in recs]
    # Filled by the probe for nets whose instrument named a reason (e.g. a hub
    # that would not open). A net the batch merely omitted has no entry and
    # keeps the generic wording, so "we know why" stays distinguishable from
    # "no value came back".
    causes: dict = {}
    # Machine-readable counterpart to `causes`, filled only where the driver
    # classified the fault. Batch probes that do not take it are unaffected.
    codes: dict = {}
    try:
        states = batch(names, causes=causes, codes=codes, deadline=deadline)
    except Exception as e:
        logger.debug("batch probe for role %s failed: %s", role, e)
        reason = _unreadable(f"{type(e).__name__}: {e}")
        return [_unknown(rec, reason) for rec in recs]

    out = []
    for rec in recs:
        name = rec.get("name", "")
        state, enabled = _split_brief(role, states.get(name))
        out.append(_entry(
            name,
            role,
            state,
            _unreadable(causes.get(name) or "no value from instrument"),
            codes.get(name),
            enabled=enabled,
        ))
    return out


# ---------------------------------------------------------------------------
# The /nets/state sweep: presence check, per-group budget, cooldown, and the
# generator both the array response and the ndjson stream are built from.
# ---------------------------------------------------------------------------

# Roles whose probe does not ask the instrument itself anything, or that
# already classify an absent device more precisely than a presence check can.
# A debug net's state is whether a GDB server is running; a webcam's is
# whether a stream is; a router is on the network, not the USB bus; and the
# USB-hub dispatcher answers hub-absent / hub-serial-mismatch on its own.
_PRESENCE_EXEMPT_ROLES = frozenset({"usb", "debug", "webcam", "router", "mikrotik"})

# USB hubs are probed in this process, under the hub dispatcher's own per-hub
# budget and fail-fast hub lock. So they are left out of the cooldown -- there
# is no leftover hardware_service call to back off from -- and they keep the
# whole request deadline rather than a group budget (see _group_budget).
_USB_HUB_ROLES = frozenset({"usb"})
_COOLDOWN_EXEMPT_ROLES = _USB_HUB_ROLES


def _usb_presence():
    """One sysfs read of the USB bus, or None when it says nothing usable.

    An empty listing is treated as unknown rather than as "nothing is
    plugged in": a container without a USB view reads that way, and taking it
    at its word would report every instrument on the bench absent.
    """
    from ..util.self_restart import enumerated_usb_ids
    return enumerated_usb_ids() or None


def _rec_absent(rec, usb_ids):
    """True only when this net's instrument is positively not connected."""
    from ..util.self_restart import usb_address_enumerated
    from .net_command import _address_from_rec

    address = str(_address_from_rec(rec) or "")
    if address.startswith("/dev/tty"):
        return not os.path.exists(address)
    return usb_address_enumerated(address, usb_ids) is False


def _absent(net_rec):
    from .net_command import _address_from_rec

    instrument = net_rec.get("instrument") or "instrument"
    address = _address_from_rec(net_rec) or ""
    return _entry(net_rec.get("name", ""), net_rec.get("role", ""), None,
                  f"not connected: {instrument} ({address}) is not on the "
                  f"USB bus", CODE_ABSENT)


def _group_budget(recs):
    """Seconds this group may run before it is cut short on its own.

    Grows per net wherever the instrument reads its nets one after another --
    the per-net path, and the supply batch, which is one call but still reads
    each channel in turn. The LabJack batch reads every net in one operation
    and gets the base.

    A USB hub gets the whole request deadline, as it did before groups had
    budgets of their own. A healthy Acroname 8-port read measured 2.1-2.5s on
    a bench, too close to the base for a cut-off there to mean anything but a
    false timeout on every port. Nor does a hub need one: the hub dispatcher
    already budgets each hub (issue #205), its lock fails fast instead of
    queueing, and an absent hub is classified ``hub-absent`` on its own.
    """
    if not recs:
        return _GROUP_BUDGET_S
    first = recs[0]
    if first.get("role", "") in _USB_HUB_ROLES:
        return float(_STATE_TIMEOUT)
    if first.get("role", "") in _LABJACK_BATCH_ROLES and _is_labjack_t7(first):
        return _GROUP_BUDGET_S
    return _GROUP_BUDGET_S + _GROUP_BUDGET_PER_NET_S * (len(recs) - 1)


# Cooldown memory: _group_key -> {"at", "reason", "keys"}. "at" is in
# _cooldown_clock() time; "keys" are the identities hardware_service records a
# success under (the net's address and, for roles whose proxy sends one, its
# device_id), so a success through ANY caller ends the cooldown.
_cooldown = {}
_cooldown_lock = threading.Lock()


def _cooldown_clock():
    """Indirection so tests can move the cooldown clock without moving time."""
    return time.monotonic()


def _identity_keys(recs):
    """The hardware_service lock identities this group's probes use."""
    from .net_command import _HS_FACTORY, _address_from_rec, _physical_device_id

    keys = set()
    for rec in recs:
        address = _address_from_rec(rec)
        if address:
            keys.add(str(address))
        role = rec.get("role", "")
        if role in _HS_FACTORY or (_is_labjack_t7(rec)
                                   and role in _LABJACK_BATCH_ROLES):
            try:
                keys.add(_physical_device_id(
                    role, rec.get("instrument", "") or "", rec))
            except Exception:
                logger.debug("nets_state: no device_id for %s",
                             rec.get("name"), exc_info=True)
    return keys


def _fetch_last_ok():
    """hardware_service's ``{identity: seconds since last success}``, or {}."""
    import requests as _req

    from ..nets.constants import HARDWARE_PORT
    try:
        resp = _req.get(f"http://localhost:{HARDWARE_PORT}/devices/last_ok",
                        timeout=0.5)
        if resp.ok:
            data = resp.json()
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.debug("nets_state: /devices/last_ok unavailable: %s", e)
    return {}


def _cooldown_snapshot():
    """Live cooldown entries, after dropping any that have ended.

    An entry ends when its window passes, or when hardware_service reports a
    success under one of the group's identities more recently than the
    failure that started it. hardware_service is asked only when there is
    something to clear, so a healthy bench pays nothing for this.
    """
    now = _cooldown_clock()
    with _cooldown_lock:
        for key in [k for k, v in _cooldown.items()
                    if now - v["at"] >= _COOLDOWN_S]:
            del _cooldown[key]
        if not _cooldown:
            return {}
    last_ok = _fetch_last_ok()
    with _cooldown_lock:
        for key, item in list(_cooldown.items()):
            since_failure = now - item["at"]
            for ident in item["keys"]:
                age = last_ok.get(ident)
                if isinstance(age, (int, float)) and age < since_failure:
                    logger.info("nets_state: %s answered %s since its probe "
                                "failed; ending cooldown", ident,
                                "%.1fs ago" % age)
                    del _cooldown[key]
                    break
        return dict(_cooldown)


def _cooldown_set(group_key, recs, reason):
    with _cooldown_lock:
        _cooldown[group_key] = {"at": _cooldown_clock(), "reason": reason,
                                "keys": _identity_keys(recs)}


def _cooldown_clear(group_key):
    with _cooldown_lock:
        _cooldown.pop(group_key, None)


def _cooled(net_rec, item, now):
    ago = max(now - item["at"], 0.0)
    left = max(_COOLDOWN_S - ago, 0.0)
    return _entry(net_rec.get("name", ""), net_rec.get("role", ""), None,
                  f"not probed: {item['reason']} ({ago:.0f}s ago); next probe "
                  f"in {left:.0f}s", CODE_COOLDOWN)


def _settle_cooldown(group_key, recs, entries):
    """Start or end a group's cooldown from the answer it just gave."""
    if recs and recs[0].get("role", "") in _COOLDOWN_EXEMPT_ROLES:
        return
    if any(e.get("state") is not None for e in entries):
        _cooldown_clear(group_key)
        return
    failed = [e for e in entries if e.get("reason_code") in _COOLDOWN_CODES]
    probed = [e for e in entries if e.get("reason") != REASON_NO_PROBE]
    if probed and len(failed) == len(probed):
        _cooldown_set(group_key, recs, failed[0]["reason"])


class _Group:
    """One instrument's work unit, and what the sweep knows about its run."""

    def __init__(self, key, recs):
        self.key = key
        self.recs = recs
        self.budget = _group_budget(recs)
        self.sink = []
        self.started = None  # time.monotonic() when a worker picked it up


def _run_group(group, request_deadline):
    group.started = time.monotonic()
    deadline = min(request_deadline, group.started + group.budget)
    return _probe_group(group.recs, deadline, group.sink)


def _cut_short(group, reason_for_rest):
    """The group's finished nets, plus ``reason_for_rest(rec)`` for the others."""
    done = {e["name"]: e for e in list(group.sink)}
    answered = [done[r.get("name", "")] for r in group.recs
                if r.get("name", "") in done]
    rest = [reason_for_rest(r) for r in group.recs
            if r.get("name", "") not in done]
    return answered, rest


def _sweep(nets):
    """Probe every saved net; yield the answers as they come.

    Yields ``("states", [entry, ...])`` for each batch of answers -- first one
    for every net that needs no instrument I/O (no probe for its role, its
    instrument not on the bus, or in cooldown), then one per instrument group
    as it completes or is cut short at its own budget -- and finally exactly
    one ``("done", [entry, ...], elapsed_ms)`` carrying the nets the request
    deadline cut off, each with ``reason: "deadline"``.

    Closing the generator early (a streaming client that went away) runs the
    same cleanup as reaching the end: queued groups are cancelled, running
    ones are left to finish and be discarded, and nothing new is submitted.
    """
    started = time.monotonic()
    request_deadline = started + _STATE_TIMEOUT

    grouped = {}
    for rec in nets:
        grouped.setdefault(_group_key(rec), []).append(rec)

    immediate = []
    groups = []
    usb_ids = _usb_presence()
    cooling = _cooldown_snapshot()
    cool_now = _cooldown_clock()
    for key, recs in grouped.items():
        probed = [r for r in recs if r.get("role", "") in _BRIEF_PROBES
                  or r.get("role", "") in _BATCH_PROBES
                  or (_is_labjack_t7(r) and r.get("role", "") in _LABJACK_BATCH_ROLES)]
        if not probed:
            immediate.extend(_entry(r.get("name", ""), r.get("role", ""), None,
                                    REASON_NO_PROBE) for r in recs)
            continue
        role = recs[0].get("role", "")
        if (usb_ids is not None and role not in _PRESENCE_EXEMPT_ROLES
                and all(_rec_absent(r, usb_ids) for r in recs)):
            immediate.extend(_absent(r) for r in recs)
            continue
        if key in cooling:
            immediate.extend(_cooled(r, cooling[key], cool_now) for r in recs)
            continue
        groups.append(_Group(key, recs))

    pool = ThreadPoolExecutor(max_workers=min(len(groups), 8)) if groups else None
    cut_off = []
    try:
        # Submitted before the first line goes out, so the instruments are
        # already being probed while it is written.
        futures = {pool.submit(_run_group, g, request_deadline): g
                   for g in groups}
        pending = set(futures)
        if immediate:
            yield ("states", immediate)
        while pending:
            now = time.monotonic()
            if now >= request_deadline:
                break
            # Wake for the earliest of: the request deadline, the end of a
            # running group's budget, or -- while groups are still queued
            # behind the worker cap, whose start time is not known yet -- a
            # short poll to notice one has started.
            wake = request_deadline
            queued = False
            for fut in pending:
                g = futures[fut]
                if g.started is None:
                    queued = True
                else:
                    wake = min(wake, g.started + g.budget)
            if queued:
                wake = min(wake, now + 0.25)
            done, _ = wait(pending, timeout=max(wake - now, 0),
                           return_when=FIRST_COMPLETED)
            for fut in done:
                pending.discard(fut)
                g = futures[fut]
                try:
                    entries = fut.result()
                except Exception:
                    logger.debug("nets_state: a probe group failed",
                                 exc_info=True)
                    entries = [_unknown(r, _unreadable("probe group failed"))
                               for r in g.recs]
                _settle_cooldown(g.key, g.recs, entries)
                logger.debug("nets_state: %s answered in %.0f ms", g.key,
                             (time.monotonic() - g.started) * 1000)
                yield ("states", entries)
            now = time.monotonic()
            for fut in list(pending):
                g = futures[fut]
                if g.started is None or now < g.started + g.budget:
                    continue
                if g.started + g.budget >= request_deadline:
                    # Its budget was the request's own remainder; leave it to
                    # the deadline below so it reads "deadline", not timeout.
                    continue
                pending.discard(fut)
                answered, rest = _cut_short(
                    g, lambda r, b=g.budget: _timed_out(r, b))
                logger.warning("nets_state: %s did not answer within %.1fs; "
                               "%d net(s) report timed out", g.key, g.budget,
                               len(rest))
                _settle_cooldown(g.key, g.recs, rest)
                yield ("states", answered + rest)

        if pending:
            answered = []
            for fut in pending:
                g = futures[fut]
                part, rest = _cut_short(
                    g, lambda r: _unknown(r, REASON_DEADLINE))
                answered.extend(part)
                cut_off.extend(rest)
            # Counts NETS, not groups -- see the comment this replaced in
            # nets_state: naming them "instrument groups" sent a real
            # diagnosis down the wrong path.
            logger.warning(
                "nets_state: %ss deadline reached; %d/%d nets answered, "
                "the rest report null: %s",
                _STATE_TIMEOUT, len(nets) - len(cut_off), len(nets),
                ", ".join(sorted(e["name"] for e in cut_off)) or "(none)",
            )
            if answered:
                yield ("states", answered)
    finally:
        # Do NOT wait. A `with` block (or a plain shutdown()) joins every
        # in-flight probe, so a hub blocked on its 10s lock held this
        # request -- and a box HTTP worker -- open for the full duration
        # even after the deadline had passed and we had stopped caring
        # about the answer. cancel_futures drops the queued work; anything
        # already running is left to finish and be discarded.
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    order = {rec.get("name", ""): i for i, rec in enumerate(nets)}
    cut_off.sort(key=lambda e: order.get(e["name"], 0))
    yield ("done", cut_off, int((time.monotonic() - started) * 1000))


# Keys accepted in a net's ``safety_limits`` record, mirroring what
# ``lager.safety`` actually enforces. The set is closed on purpose: a stored key
# nothing reads is indistinguishable, from the outside, from an enforced one.
_SAFETY_CEILING_KEYS = ('max_voltage', 'max_current')
_SAFETY_LIMIT_KEYS = _SAFETY_CEILING_KEYS + ('allow_destructive',)


def _validate_safety_limits(payload):
    """Validate a safety-limits body.

    Returns ``(limits, error)``. An empty ``limits`` dict means "clear", which
    is a legitimate request -- a net with no ``safety_limits`` key is
    unrestricted, and that is how a net goes back to being unrestricted.
    """
    if payload is None:
        return {}, None
    if not isinstance(payload, dict):
        return None, 'body must be a JSON object'

    limits = {}
    for key, value in payload.items():
        if key == 'max_power':
            return None, (
                'max_power is not supported: one setter call establishes either '
                'voltage or current, never both, so a power ceiling could not be '
                'evaluated honestly. Use max_voltage and max_current.'
            )
        if key not in _SAFETY_LIMIT_KEYS:
            return None, "unknown key '%s'; accepted: %s" % (
                key, ', '.join(_SAFETY_LIMIT_KEYS))
        if value is None:
            # Explicit null clears that one key while leaving the others.
            continue
        if key == 'allow_destructive':
            if not isinstance(value, bool):
                return None, 'allow_destructive must be a boolean'
            limits[key] = value
            continue
        # A ceiling that is not a positive number cannot be compared against a
        # setpoint. bool is a subclass of int, hence the explicit exclusion:
        # True would otherwise sail through as 1.0 V.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, '%s must be a number' % key
        if value <= 0:
            return None, '%s must be greater than zero' % key
        limits[key] = float(value)

    return limits, None


def register_nets_routes(app: Flask) -> None:
    """Register nets REST routes with the Flask app."""

    @app.route('/nets/list', methods=['GET'])
    def nets_list():
        """Return full saved nets details.

        Uses Net.list_saved() (same source the old `net.py list` exec used)
        so uart nets carry the `live_path` annotation the CLI display relies
        on; falls back to the raw file if the annotation pass fails.
        """
        try:
            nets = Net.list_saved()
            if not isinstance(nets, list):
                nets = []
            return jsonify(nets)
        except Exception:
            logger.exception("Net.list_saved failed; falling back to raw file")
        try:
            with open('/etc/lager/saved_nets.json', 'r') as f:
                nets = json.load(f)
            if not isinstance(nets, list):
                nets = []
            return jsonify(nets)
        except FileNotFoundError:
            return jsonify([])
        except (json.JSONDecodeError, TypeError):
            return jsonify([])

    @app.route('/nets/state', methods=['GET'])
    def nets_state():
        """Return brief live state for every saved net.

        Response: [{"name": "usb1", "role": "usb", "state": "enabled",
                    "enabled": true}, ...]

        ``enabled`` is a bool for roles with an on/off output (usb,
        power-supply, battery, eload) and is present only when that on/off was
        actually read. An unreadable on/off shows as ``?`` in the ``state``
        text (``"CH1/?/3.30V/0.120A"``) and leaves ``enabled`` out; it is never
        reported as off.

        One work unit per physical instrument, run in parallel, under a whole-
        request deadline of ``_STATE_TIMEOUT``. A net whose instrument is slow,
        wedged or absent comes back with ``state: null``; it never fails the
        request and never blocks another instrument's answer. Roles without a
        probe (uart, spi, ...) are also ``state: null``.

        A null entry carries a ``reason`` saying why, and where the fault has
        a class, a ``reason_code`` (see the compatibility rule above
        ``_unreadable``):

        - ``"deadline"`` -- the instrument had not answered when the budget for
          *all* of them ran out, not necessarily because it is slow itself.
        - ``"no probe for role"``.
        - ``"unreadable: <detail>"`` -- probed, no answer. ``device-busy`` when
          hardware_service could not get the device lock inside the budget.
        - ``"not connected: ..."``, code ``instrument-absent`` -- the net's USB
          address (or ``/dev/tty*`` path) is not on the bus, so it was not
          probed. Checked from one sysfs read per request; an address that
          cannot be checked this way is probed as before.
        - ``"timed out: ..."``, code ``instrument-timeout`` -- probed, and no
          answer inside the instrument's own budget (``_GROUP_BUDGET_S``, plus
          ``_GROUP_BUDGET_PER_NET_S`` per extra net on the one-net-at-a-time
          path). One slow instrument is cut short on its own. USB hubs keep
          the whole request deadline.
        - ``"not probed: ..."``, code ``probe-cooldown`` -- the last probe of
          this instrument timed out or found it busy within ``_COOLDOWN_S``;
          the reason quotes that answer. Ends early when the instrument next
          completes any operation through hardware_service.

        Entries with a state carry no ``reason``. The USB batch probe receives
        the instrument's budget and sub-budgets it per hub (issue #205): a hub
        the remaining budget cannot cover is skipped with its own reason and a
        ``hub-skipped`` code.

        Always answers 200 with one entry per saved net, in the saved order.

        **Streaming: ``GET /nets/state?stream=1``.** Advertised by the
        ``netsStateStream`` status capability. Answers
        ``application/x-ndjson``: one JSON object per line, each sent as soon
        as it exists::

            {"type": "states", "entries": [<entry>, ...]}
            ...
            {"type": "done", "entries": [<entry>, ...], "elapsed_ms": 2412}

        - Each ``<entry>`` has exactly the shape of an element of the array
          above.
        - A ``states`` line is sent per instrument as it answers or is cut
          short at its own budget. The first one carries every net that needed
          no instrument I/O (no probe, not connected, cooling down).
        - ``done`` is always the last line and is sent exactly once. Its
          ``entries`` are the nets the request deadline cut off, each with
          ``reason: "deadline"``; it is often empty.
        - Every saved net appears in exactly one line. Order across lines is
          arrival order, not saved order.
        - No ``done`` line means the stream was cut, not that it finished.
        """
        try:
            nets = Net.list_saved()
            if not isinstance(nets, list):
                nets = []
        except Exception:
            logger.exception("nets_state: list_saved failed")
            nets = []

        if request.args.get("stream") in ("1", "true"):
            def lines():
                for event in _sweep(nets):
                    if event[0] == "done":
                        line = {"type": "done", "entries": event[1],
                                "elapsed_ms": event[2]}
                    else:
                        line = {"type": "states", "entries": event[1]}
                    yield json.dumps(line) + "\n"

            return Response(
                lines(),
                mimetype="application/x-ndjson",
                headers={
                    # Mirrors the UART stream: no proxy may hold lines back.
                    "X-Accel-Buffering": "no",
                    "Cache-Control": "no-cache",
                },
            )

        if not nets:
            return jsonify([])

        by_name = {}
        for event in _sweep(nets):
            for entry in event[1]:
                by_name[entry["name"]] = entry

        # One entry per saved net, saved order, whatever happened above.
        return jsonify([by_name.get(rec.get("name", ""))
                        or _unknown(rec, REASON_DEADLINE)
                        for rec in nets])

    @app.route('/nets/<name>', methods=['PUT'])
    def nets_update(name):
        """Create or replace a net by name."""
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({'error': 'Invalid JSON body'}), 400
        if not data.get('name') or not data.get('role') or not data.get('instrument'):
            return jsonify({'error': 'name, role, and instrument are required'}), 400

        error = _ud_pin_span_error(data)
        if error:
            return jsonify({'error': error}), 400

        # If the name is changing, delete the old entry first
        if data['name'] != name:
            Net.delete_local_net(name)

        Net.save_local_net(data)
        return jsonify({'ok': True})

    @app.route('/nets/<name>/safety-limits', methods=['PUT'])
    def nets_set_safety_limits(name):
        """Set or clear the safety limits on a saved net.

        Merges rather than replaces, so no other field on the net is disturbed.
        ``PUT /nets/<name>`` cannot serve this purpose: it takes a whole net
        definition, rederives ``mappings`` and ``scope_points`` from it, and
        would require the caller to round-trip every field it does not model.

        Every record sharing this name is updated, not just the first one found.
        ``lager.safety`` reads limits through ``NetsCache.find_by_name``, which
        indexes one record per name, so leaving a same-named sibling untouched
        would make enforcement depend on which record the index happened to
        keep -- a limit that applies or not depending on file order is worse
        than none.

        This route confers no authority that this port did not already grant:
        ``PUT /nets/<name>`` replaces a net wholesale, limits included, and
        ``DELETE /nets/<name>`` removes it. The interlock's guarantee is that a
        *test script* cannot raise its own ceiling through the hardware service,
        not that the saved-net file is unwritable.
        """
        payload = request.get_json(force=True, silent=True)
        limits, error = _validate_safety_limits(payload)
        if error:
            return jsonify({'error': error}), 400

        # Copy before mutating: get_local_nets hands back the cache's own dicts,
        # and a failed write would otherwise leave raised limits live in memory
        # until something invalidated the cache.
        nets = [dict(n) for n in Net.get_local_nets()]
        matched = [n for n in nets if n.get('name') == name]
        if not matched:
            return jsonify({'error': "no saved net named '%s'" % name}), 404

        for record in matched:
            if limits:
                record['safety_limits'] = dict(limits)
            else:
                record.pop('safety_limits', None)

        Net.save_local_nets(nets)
        logger.info(
            "safety limits for net '%s' set to %s across %d record(s)",
            name, limits or None, len(matched),
        )
        return jsonify({'ok': True, 'name': name, 'safety_limits': limits or None})

    @app.route('/nets', methods=['DELETE'])
    def nets_delete_all():
        """Delete all saved nets in a single atomic write."""
        Net.delete_all_local_nets()
        return jsonify({'ok': True})

    @app.route('/nets/<name>', methods=['DELETE'])
    def nets_delete(name):
        """Delete a net by name."""
        role = request.args.get('role') or None
        deleted = Net.delete_local_net(name, role)
        if not deleted:
            return jsonify({'error': 'Net not found'}), 404
        return jsonify({'ok': True})
