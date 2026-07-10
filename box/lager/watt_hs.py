# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for watt-meter nets (create_device factory).

Thin shim over the watt-meter dispatcher's cached driver. ``measure`` performs
the timed averaging window in one /invoke under the net's shared ``device_id``
lock (shared with an energy-analyzer net on the same Joulescope/PPK2, so the two
serialize). Current/voltage/all raise UnsupportedInstrumentError on power-only
meters (Yocto-Watt); that propagates to a 502 on the CLI.
"""
from __future__ import annotations


class WattHardwareAdapter:
    def __init__(self, netname: str) -> None:
        self._netname = netname

    def measure(self, action: str, duration) -> dict:
        from lager.measurement.watt import dispatcher as _disp
        drv, _ = _disp._dispatcher._resolve_net_and_driver(self._netname)
        d = float(duration)
        if action in ("read", "power"):
            return {"value": float(drv.read(d))}
        if action == "current":
            return {"value": float(drv.read_current(d))}
        if action == "voltage":
            return {"value": float(drv.read_voltage(d))}
        if action == "all":
            r = drv.read_all(d)
            return {
                "current": float(r["current"]),
                "voltage": float(r["voltage"]),
                "power": float(r["power"]),
            }
        raise ValueError("Unknown watt-meter action: %r" % action)


def create_device(net_info, **_):
    netname = (net_info or {}).get("name")
    return WattHardwareAdapter(netname)
