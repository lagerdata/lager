# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Scoped, allowlisted box-control MCP tools.

The Lager MCP server is read-only by default. These tools are the *only*
mutating / hardware-probing surface it exposes, and they are registered solely
when ``LAGER_MCP_ALLOW_CONTROL`` is set (see ``config.control_tools_enabled``
and the gated import in ``server.py``).

Each tool does ONE safe, well-scoped action and returns structured JSON. Bad
input and missing hardware are reported as ``{"error": ...}`` rather than
raised, mirroring the existing read-only tools.

The intended consumer is an operator/agent performing *box-environment
recovery* — e.g. a debug probe that has fallen off the USB bus. The diagnostic
tools (``debug_probe_status``, ``net_status``) are read-only; ``power_cycle_hub``
is the single action tool and merely toggles a USB hub port off then on.
"""

from __future__ import annotations

import json
import time


# How long to leave the hub port off before re-enabling, so the downstream
# device fully de-enumerates before it is re-powered.
_HUB_SETTLE_SECONDS = 1.0


def _find_local_net(name: str) -> dict | None:
    """Return the raw saved-net dict for *name*, or None.

    The MCP bench descriptor (``NetDescriptor``) is enriched metadata and does
    not carry the probe's VISA ``address``; the raw saved-nets entry does, so
    probe-serial parsing reads from there.
    """
    from lager.nets.net import Net

    for net in Net.get_local_nets():
        if net.get("name") == name:
            return net
    return None


def debug_probe_status(net: str) -> str:
    """Report whether a debug net's probe is present on the USB bus.

    Read-only. Resolves the debug net's probe (J-Link / ST-Link / etc.) from
    its saved VISA address and checks the USB bus for that exact device by
    vendor/product id and serial. Use this to confirm a debug probe is alive
    before/after a recovery action.

    Args:
        net: Name of the debug net to inspect (e.g. 'debug', 'jlink0').

    Returns JSON: {net, backend, probe_serial, vid, pid, present, detail}.
    """
    from lager.debug.probes import parse_probe_address, resolve_backend

    raw = _find_local_net(net)
    if raw is None:
        return json.dumps({"error": f"Unknown net '{net}'."})

    address = raw.get("address") or ""
    vid, pid, serial = parse_probe_address(address)
    backend = resolve_backend(raw)

    if vid is None or pid is None:
        return json.dumps({
            "net": net,
            "backend": backend,
            "probe_serial": serial,
            "present": False,
            "detail": f"No parseable probe address for net '{net}' (address={address!r}).",
        })

    try:
        import usb.core

        kwargs = {"idVendor": int(vid, 16), "idProduct": int(pid, 16)}
        if serial:
            kwargs["serial_number"] = serial
        device = usb.core.find(**kwargs)
        present = device is not None
        detail = "probe enumerated on USB bus" if present else "probe not found on USB bus"
    except Exception as exc:  # pragma: no cover - environment/permission dependent
        return json.dumps({
            "net": net,
            "backend": backend,
            "probe_serial": serial,
            "vid": vid,
            "pid": pid,
            "present": False,
            "detail": f"USB enumeration unavailable: {exc}",
        })

    return json.dumps({
        "net": net,
        "backend": backend,
        "probe_serial": serial,
        "vid": vid,
        "pid": pid,
        "present": present,
        "detail": detail,
    })


def net_status(net: str) -> str:
    """Report a compact, read-only status for a net on this bench.

    Resolves the net from the live bench descriptor and returns its identity,
    type, and control/observe capability flags. Does not drive hardware.

    Args:
        net: Name of the net to inspect (e.g. 'debug', 'spi0', 'psu1').

    Returns JSON: {net, net_type, electrical_type, instrument, channel,
    controllable, observable, roles, purpose}.
    """
    from ..server_state import get_bench

    bench = get_bench()
    for descriptor in bench.nets:
        if descriptor.name == net:
            return json.dumps({
                "net": descriptor.name,
                "net_type": descriptor.net_type,
                "electrical_type": descriptor.electrical_type,
                "instrument": descriptor.instrument,
                "channel": descriptor.channel,
                "controllable": descriptor.controllable,
                "observable": descriptor.observable,
                "roles": descriptor.roles,
                "purpose": descriptor.purpose,
            })
    return json.dumps({"error": f"Unknown net '{net}'."})


def power_cycle_hub(hub: str) -> str:
    """Power-cycle an Acroname-controlled USB hub port (disable, settle, enable).

    The single *action* tool: toggles the named USB net's hub port off, waits
    for the downstream device to de-enumerate, then re-enables it. Used to
    recover a debug probe (or other USB device) that has hung on the bus.

    Args:
        hub: Name of the USB net whose hub port to power-cycle (e.g. 'usb0').

    Returns JSON: {hub, actions, settled_ms, ok} on success, {"error": ...} when
    the net is unknown or the hub is unavailable.
    """
    from lager.automation.usb_hub import dispatcher

    try:
        dispatcher.disable(hub)
        time.sleep(_HUB_SETTLE_SECONDS)
        dispatcher.enable(hub)
    except (KeyError, FileNotFoundError, RuntimeError) as exc:
        return json.dumps({"error": f"Cannot power-cycle '{hub}': {exc}"})
    except Exception as exc:  # hub/library errors surface as data, not a crash
        return json.dumps({"error": f"Power-cycle of '{hub}' failed: {exc}"})

    return json.dumps({
        "hub": hub,
        "actions": ["disable", "enable"],
        "settled_ms": int(_HUB_SETTLE_SECONDS * 1000),
        "ok": True,
    })


def register(mcp) -> None:
    """Register the control tools on *mcp*.

    Called from ``server.py`` only when ``LAGER_MCP_ALLOW_CONTROL`` is set, so
    the default tool surface stays read-only. Using an explicit register hook
    (rather than bare ``@mcp.tool()`` at import) keeps these tools off the
    global server unless opted in, and lets tests register onto a throwaway
    server without mutating the shared singleton.
    """
    mcp.add_tool(debug_probe_status)
    mcp.add_tool(net_status)
    mcp.add_tool(power_cycle_hub)
