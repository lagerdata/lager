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

# How long to wait AFTER re-enabling before returning, so the downstream device
# has time to re-enumerate on the USB bus. A J-Link takes ~2-3s to reappear; if
# we returned immediately a caller that re-checks probe presence right away would
# still see it absent and wrongly conclude the power-cycle failed.
_HUB_REENUM_SECONDS = 4.0


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


_SYSFS_USB_ROOT = "/sys/bus/usb/devices"


def _probe_present_via_sysfs(vid: str, pid: str, serial: str | None,
                             root: str = _SYSFS_USB_ROOT) -> bool | None:
    """Check probe presence by scanning sysfs, the live USB topology.

    sysfs is re-read on every call, so a probe that just re-enumerated (e.g.
    right after ``power_cycle_hub``) is seen immediately. An in-process
    ``usb.core.find`` cannot be trusted here: the long-running MCP server holds
    a libusb device cache that goes stale across a hub power-cycle, producing a
    false "probe not found" even though the device is back on the bus.

    Returns True/False on Linux, or None when sysfs is absent (non-Linux host)
    so the caller can fall back to pyusb.
    """
    import os

    if not os.path.isdir(root):
        return None

    def _norm(value: str | None) -> str:
        return (value or "").strip().lower().lstrip("0")

    want_vid, want_pid = _norm(vid), _norm(pid)
    want_serial = _norm(serial) if serial else None

    def _read(path: str) -> str | None:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return None

    for entry in os.listdir(root):
        dev = os.path.join(root, entry)
        if _norm(_read(os.path.join(dev, "idVendor"))) != want_vid:
            continue
        if _norm(_read(os.path.join(dev, "idProduct"))) != want_pid:
            continue
        if want_serial and _norm(_read(os.path.join(dev, "serial"))) != want_serial:
            continue
        return True
    return False


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
        # Prefer sysfs (always current). Only fall back to an in-process pyusb
        # scan on a non-Linux host, where pyusb's freshness caveat doesn't bite
        # the way it does for the long-running server after a power-cycle.
        present = _probe_present_via_sysfs(vid, pid, serial)
        source = "sysfs"
        if present is None:
            import usb.core

            kwargs = {"idVendor": int(vid, 16), "idProduct": int(pid, 16)}
            if serial:
                kwargs["serial_number"] = serial
            present = usb.core.find(**kwargs) is not None
            source = "pyusb"
        detail = (
            f"probe enumerated on USB bus ({source})" if present
            else f"probe not found on USB bus ({source})"
        )
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
        # Wait for the downstream device to re-enumerate before returning, so a
        # caller that re-checks probe presence immediately sees the recovered
        # device rather than a still-absent one.
        time.sleep(_HUB_REENUM_SECONDS)
    except (KeyError, FileNotFoundError, RuntimeError) as exc:
        return json.dumps({"error": f"Cannot power-cycle '{hub}': {exc}"})
    except Exception as exc:  # hub/library errors surface as data, not a crash
        return json.dumps({"error": f"Power-cycle of '{hub}' failed: {exc}"})

    return json.dumps({
        "hub": hub,
        "actions": ["disable", "enable"],
        "settled_ms": int(_HUB_SETTLE_SECONDS * 1000),
        "reenum_wait_ms": int(_HUB_REENUM_SECONDS * 1000),
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
