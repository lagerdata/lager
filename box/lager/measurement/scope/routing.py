# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Which oscilloscope daemon drives a scope net.

Every supported scope is driven by a daemon, and every surface -- the CLI,
the web UI, Python scripts, the MCP server -- reaches it the same way, so the
make of scope matters in exactly one place: here, where a net is turned into
the URL of its daemon. A PicoScope is found by the box's own daemon on
:8085; a Rigol MSO5000 has a daemon of its own, started for its address.
"""
from __future__ import annotations

PICOSCOPE = "picoscope"
RIGOL_MSO5000 = "rigol_mso5000"


def scope_family(instrument) -> str | None:
    """The driver family behind an instrument string, or None."""
    lowered = (instrument or "").lower()
    if "pico" in lowered:
        return PICOSCOPE
    if "rigol" in lowered or "mso5" in lowered:
        return RIGOL_MSO5000
    return None


def is_daemon_backed(instrument) -> bool:
    return scope_family(instrument) is not None


def address_of(rec: dict, info: dict | None = None):
    """The instrument address a net carries, preferring its mapping."""
    for mapping in (rec or {}).get("mappings") or []:
        if mapping.get("device_override"):
            return mapping["device_override"]
    return (rec or {}).get("address") or (info or {}).get("address")


def daemon_url_for(rec: dict, info: dict | None = None) -> str:
    """WebSocket URL of the daemon driving this net's scope.

    Starts a Rigol's daemon if it is not running yet, which is why this is
    called when a command is about to be sent rather than when a net is read.
    """
    from lager.measurement.scope import daemon_client

    instrument = (rec or {}).get("instrument") or (info or {}).get("instrument")
    family = scope_family(instrument)
    if family == PICOSCOPE:
        return daemon_client.daemon_url()
    if family == RIGOL_MSO5000:
        from lager.measurement.scope import rigol_daemons
        return rigol_daemons.ensure(address_of(rec, info), capture_of(rec, info))
    raise daemon_client.ScopeDaemonUnavailable(
        "no oscilloscope daemon drives a %s" % (instrument or "scope of unknown make"))


# Why a Rigol with no card cannot stream. The trace streams from the HDMI
# output; without a card the daemon would never publish a frame.
NO_LIVE_VIEW = (
    "no HDMI capture card is paired with this scope, so it cannot stream; "
    "pair one with `lager nets set-capture`. Single captures and "
    "measurements work without it")


def has_live_view(rec: dict) -> bool:
    """Whether the net's scope can stream: a PicoScope always can, a Rigol
    only through the HDMI capture card paired with it."""
    family = scope_family((rec or {}).get("instrument"))
    if family == PICOSCOPE:
        return True
    if family == RIGOL_MSO5000:
        return bool(capture_of(rec))
    return False


def capture_of(rec: dict, info: dict | None = None) -> str | None:
    """The capture card a Rigol net's scope is paired with: the net's own,
    else the one another net on the same scope names."""
    from lager.measurement.scope import rigol_daemons
    return (rigol_daemons.capture_spec(rec)
            or rigol_daemons.capture_for_address(address_of(rec, info)))
