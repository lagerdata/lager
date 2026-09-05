# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for scope nets (create_device factory).

Dispatches on the net's instrument: a PicoScope goes through the oscilloscope
daemon, a Rigol MSO5000 over SCPI/VISA. Both expose the same method names (see
``measurement/scope/picoscope.py``), so this only picks the driver -- the
per-role handler in ``net_command.py`` calls the same methods either way.

Running under hardware_service matters for the PicoScope specifically: the
``device_id`` lock serializes a ``lager scope`` command against a browser
streaming the same unit, and a PicoScope's USB handle admits exactly one
owner, so two concurrent openers would otherwise fail rather than queue.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _is_picoscope(instrument: str) -> bool:
    lowered = (instrument or "").lower()
    return "picoscope" in lowered or "pico" in lowered


def _resolve_net(netname: str) -> dict:
    """Look up the saved-net record for a scope net.

    The caller's ``net_info`` carries only name/instrument/device_id, because
    that is all the shared proxy builder needs. A scope driver additionally
    needs the VISA address (Rigol) and the wired channel (both), so this reads
    them from the same saved nets the rest of the box uses rather than
    widening the proxy for every role.
    """
    if not netname:
        return {}
    try:
        from lager.nets.net import Net
        for entry in Net.get_local_nets():
            if entry.get("name") == netname and entry.get("role") in ("scope", "analog"):
                return entry
    except Exception as e:
        logger.warning("scope %s: could not read saved nets: %s", netname, e)
    return {}


def _channel_of(rec: dict):
    """The channel this net is wired to, from the record's pin or mapping."""
    if rec.get("pin") is not None:
        return rec["pin"]
    for mapping in rec.get("mappings") or []:
        if mapping.get("pin") is not None:
            return mapping["pin"]
        if mapping.get("channel") is not None:
            return mapping["channel"]
    return None


def _address_of(rec: dict, info: dict):
    for mapping in rec.get("mappings") or []:
        if mapping.get("device_override"):
            return mapping["device_override"]
    return rec.get("address") or info.get("address")


def create_device(net_info=None, **kwargs):
    info = net_info or {}
    netname = info.get("name")
    rec = _resolve_net(netname)
    instrument = info.get("instrument") or rec.get("instrument") or ""
    channel = _channel_of(rec) or info.get("pin") or info.get("channel")

    if _is_picoscope(instrument):
        from lager.measurement.scope.picoscope import PicoScope
        return PicoScope(
            address=_address_of(rec, info),
            pin=channel,
            netname=netname,
        )

    # Default to the Rigol, which is the only other supported scope and the
    # one that predates this dispatch.
    from lager.measurement.scope.rigol_mso5000 import RigolMso5000
    return RigolMso5000(address=_address_of(rec, info), pin=channel)
