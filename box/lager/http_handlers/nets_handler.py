# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Nets HTTP handler for the Lager Box HTTP server.

Provides endpoints to list, update, delete, and query live state of saved nets.
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError

from flask import Flask, jsonify, request

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


def _brief_supply(netname):
    """Power-supply: CH<n>/<on|off>/<V>/<I>."""
    from ..dispatchers.helpers import resolve_net_proxy
    from ..exceptions import SupplyBackendError
    from ..nets.device import Device
    try:
        device_name, net_info, channel = resolve_net_proxy(
            netname, "power-supply", SupplyBackendError)
        supply = Device(device_name, net_info)
        state = supply.get_monitor_state(channel)
        enabled = state.get("enabled")
        on_off = "on" if enabled else "off"
        v = state.get("voltage")
        i = state.get("current")
        v_s = "%.2fV" % v if v is not None else "?V"
        i_s = "%.3fA" % i if i is not None else "?A"
        parts = ["CH%s" % channel, on_off, v_s, i_s]
        return "/".join(parts)
    except Exception as e:
        logger.debug("brief_supply %s: %s", netname, e)
        return None


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
        enabled = state.get("enabled")
        on_off = "on" if enabled else "off"
        v = state.get("terminal_voltage")
        i = state.get("current")
        soc = state.get("soc")
        v_s = "%.2fV" % v if v is not None else "?V"
        i_s = "%.3fA" % i if i is not None else "?A"
        soc_s = "%d%%" % soc if soc is not None else "?%"
        return "/".join(["CH%s" % channel, on_off, v_s, i_s, soc_s])
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


def _brief_usb_batch(netnames):
    """USB hub ports for several nets, grouped by physical hub.

    The per-net probe costs a full hub open/read/close under that hub's lock, so
    a bench with a dozen USB nets on three hubs pays twelve enumerate/connect/
    disconnect cycles in three serialised lanes -- seconds, not milliseconds.
    ``usb_hub.states`` groups by hub and pays one cycle per hub.

    Returns:
        dict[str, str | None]: net name -> "enabled"/"disabled", or None.
    """
    from ..automation import usb_hub
    try:
        raw = usb_hub.states(netnames)
    except Exception as e:
        logger.debug("brief_usb_batch %s: %s", netnames, e)
        return {n: None for n in netnames}

    out = {}
    for name in netnames:
        value = raw.get(name)
        out[name] = None if value is None else ("enabled" if value else "disabled")
    return out


def _is_labjack(rec):
    """True if this net's instrument is a LabJack device."""
    return "labjack" in (rec.get("instrument") or "").lower()


_LABJACK_BATCH_ROLES = {"gpio", "adc", "dac"}


def _brief_labjack_batch(recs):
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

    Returns dict[netname, brief_str | None].
    """
    import requests as _req

    from .net_command import _physical_device_id

    payload = [
        {"name": r.get("name", ""), "role": r.get("role", ""),
         "pin": r.get("pin") or r.get("channel") or ""}
        for r in recs
    ]
    device_id = _physical_device_id(
        recs[0].get("role", ""), recs[0].get("instrument", "") or "", recs[0])
    try:
        resp = _req.post("http://localhost:8080/labjack/batch_read",
                         json={"nets": payload, "device_id": device_id},
                         timeout=5.0)
        if resp.ok:
            return resp.json()
    except Exception as e:
        logger.debug("labjack batch_read call failed: %s", e)

    return {r.get("name", ""): None for r in recs}


# Roles that can answer for several nets in one instrument session. Anything
# absent here falls back to the per-net probe in _BRIEF_PROBES.
_BATCH_PROBES = {
    "usb": _brief_usb_batch,
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
        on_off = "on" if state.get("input_enabled") else "off"
        v = state.get("measured_voltage")
        i = state.get("measured_current")
        v_s = "%.2fV" % v if v is not None else "?V"
        i_s = "%.3fA" % i if i is not None else "?A"
        return "/".join([mode, on_off, v_s, i_s])
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


def _brief_watt(netname):
    """Watt-meter: quick 0.1s reading → I/V/P."""
    from .net_command import _proxy
    try:
        dev = _proxy(netname, "watt-meter", timeout=15.0)
        r = dev.measure("all", 0.1)
        i = float(r.get("current", 0))
        v = float(r.get("voltage", 0))
        p = float(r.get("power", 0))
        return "%.4fA/%.3fV/%.4fW" % (i, v, p)
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
        return "%.4fA/%.3fV/%.4fW" % (
            float(c.get("mean", 0)),
            float(v.get("mean", 0)),
            float(p.get("mean", 0)),
        )
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


def _unreadable(detail):
    """Reason string for a probe that ran and did not produce an answer."""
    detail = str(detail).strip()
    return f"unreadable: {detail}" if detail else "unreadable"


def _entry(name, role, state, reason=None):
    """One net's answer.

    ``reason`` is attached only when *state* is None, so its presence means
    "this is a null, and here is why". A net with a state carries no reason.
    """
    out = {"name": name, "role": role, "state": state}
    if state is None and reason:
        out["reason"] = reason
    return out


def _probe_net_state(net_rec):
    """Return {"name": ..., "role": ..., "state": <str|None>[, "reason": ...]}."""
    name = net_rec.get("name", "")
    role = net_rec.get("role", "")
    probe = _BRIEF_PROBES.get(role)
    if probe is None:
        return _entry(name, role, None, REASON_NO_PROBE)
    try:
        return _entry(name, role, probe(name))
    except Exception as e:
        logger.debug("probe %s (%s) failed: %s", name, role, e)
        return _entry(name, role, None, _unreadable(f"{type(e).__name__}: {e}"))


def _unknown(net_rec, reason):
    """The "we could not find out" answer for one net, and why."""
    return _entry(net_rec.get("name", ""), net_rec.get("role", ""), None, reason)


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
    if _is_labjack(net_rec) and role in _LABJACK_BATCH_ROLES:
        return ("_labjack_", instrument, address)
    return (role, instrument, address)


def _probe_group(recs):
    """Probe every net in one instrument group. Returns a list of results.

    Three dispatch paths, checked in order:

    1. **LabJack batch** — the group was keyed with ``"_labjack_"`` (see
       ``_group_key``), so it may contain GPIO + ADC + DAC nets on one T7.
       ``_brief_labjack_batch`` makes one HTTP call to hardware_service's
       ``/labjack/batch_read`` which reads registers without direction
       mutation.
    2. **Per-role batch** (``_BATCH_PROBES``) — e.g. USB hub ports.
    3. **Per-net fallback** — one ``_probe_net_state`` call per net.
    """
    if not recs:
        return []

    # Path 1: cross-role LabJack batch
    if recs[0].get("role", "") in _LABJACK_BATCH_ROLES and _is_labjack(recs[0]):
        try:
            states = _brief_labjack_batch(recs)
        except Exception as e:
            logger.debug("labjack batch probe failed: %s", e)
            reason = _unreadable(f"{type(e).__name__}: {e}")
            return [_unknown(rec, reason) for rec in recs]
        return [
            _entry(
                rec.get("name", ""),
                rec.get("role", ""),
                states.get(rec.get("name", "")),
                _unreadable("no value from instrument"),
            )
            for rec in recs
        ]

    # Path 2: per-role batch (e.g. USB)
    role = recs[0].get("role", "")
    batch = _BATCH_PROBES.get(role)
    if batch is None:
        # Path 3: per-net fallback
        return [_probe_net_state(rec) for rec in recs]

    names = [rec.get("name", "") for rec in recs]
    try:
        states = batch(names)
    except Exception as e:
        logger.debug("batch probe for role %s failed: %s", role, e)
        reason = _unreadable(f"{type(e).__name__}: {e}")
        return [_unknown(rec, reason) for rec in recs]

    return [
        _entry(
            rec.get("name", ""),
            role,
            states.get(rec.get("name", "")),
            _unreadable("no value from instrument"),
        )
        for rec in recs
    ]


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

        Response: [{"name": "usb1", "role": "usb", "state": "enabled"}, ...]

        One work unit per physical instrument, run in parallel, under a whole-
        request deadline of ``_STATE_TIMEOUT``. A net whose instrument is slow,
        wedged or absent comes back with ``state: null``; it never fails the
        request and never blocks another instrument's answer. Roles without a
        probe (uart, spi, i2c, ...) are also ``state: null``.

        A null entry carries a ``reason`` saying which of those it is --
        ``"deadline"``, ``"no probe for role"``, or ``"unreadable: <detail>"``.
        Entries with a state carry no ``reason``. The three used to be
        indistinguishable from outside the box, and they need different
        remedies (issue #196).

        Note the deadline is shared by the whole request, not per instrument, so
        ``reason: "deadline"`` means this net's instrument had not answered when
        the budget for *all* of them ran out -- not necessarily that this
        instrument is slow.

        Always answers 200 with one entry per saved net, in the saved order.
        """
        try:
            nets = Net.list_saved()
            if not isinstance(nets, list):
                nets = []
        except Exception:
            logger.exception("nets_state: list_saved failed")
            nets = []

        if not nets:
            return jsonify([])

        # Group by physical instrument: nets on one device serialise anyway, so
        # the pool should be spending its workers on distinct devices.
        groups = {}
        for rec in nets:
            groups.setdefault(_group_key(rec), []).append(rec)

        by_name = {}
        pool = ThreadPoolExecutor(max_workers=min(len(groups), 8))
        try:
            futures = {pool.submit(_probe_group, recs): recs
                       for recs in groups.values()}
            try:
                for fut in as_completed(futures, timeout=_STATE_TIMEOUT):
                    try:
                        for entry in fut.result():
                            by_name[entry["name"]] = entry
                    except Exception:
                        logger.debug("nets_state: a probe group failed",
                                     exc_info=True)
            except FuturesTimeoutError:
                # Deadline hit. as_completed raises out of the for statement, so
                # this must be caught HERE -- an except inside the loop body
                # never sees it, and letting it escape turned one wedged
                # instrument into a 500 for the whole bench.
                #
                # Counts NETS, not groups -- len(by_name) is nets answered and
                # len(nets) is nets asked for. Naming these "instrument groups"
                # read as "the grouping collapsed to one group per net", which
                # sent a real diagnosis down the wrong path; the slow instrument
                # it was actually reporting went unnoticed.
                unanswered = sorted(rec.get("name", "") for rec in nets
                                    if rec.get("name", "") not in by_name)
                logger.warning(
                    "nets_state: %ss deadline reached; %d/%d nets answered, "
                    "the rest report null: %s",
                    _STATE_TIMEOUT, len(by_name), len(nets),
                    ", ".join(unanswered) or "(none)",
                )
        finally:
            # Do NOT wait. A `with` block (or a plain shutdown()) joins every
            # in-flight probe, so a hub blocked on its 10s lock held this
            # request -- and a box HTTP worker -- open for the full duration
            # even after the deadline had passed and we had stopped caring
            # about the answer. cancel_futures drops the queued work; anything
            # already running is left to finish and be discarded.
            pool.shutdown(wait=False, cancel_futures=True)

        # One entry per saved net, saved order, whatever happened above. A net
        # with no entry never had one produced: its group is still running (or
        # was cancelled) when the shared deadline expired.
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

        # If the name is changing, delete the old entry first
        if data['name'] != name:
            Net.delete_local_net(name)

        Net.save_local_net(data)
        return jsonify({'ok': True})

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
