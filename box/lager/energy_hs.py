# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for energy-analyzer nets (create_device factory).

Thin shim over the energy-analyzer dispatcher's cached driver. ``measure``
integrates (read_energy) or averages (read_stats) over the requested window in
one /invoke under the net's shared ``device_id`` lock (shared with a watt-meter
net on the same Joulescope/PPK2, so the two serialize).
"""
from __future__ import annotations


class EnergyAnalyzerHardwareAdapter:
    def __init__(self, netname: str) -> None:
        self._netname = netname

    def measure(self, action: str, duration) -> dict:
        from lager.measurement.energy_analyzer import dispatcher as _disp
        d = float(duration)
        if action == "read_energy":
            return _disp.read_energy(self._netname, d)
        return _disp.read_stats(self._netname, d)


def create_device(net_info, **_):
    netname = (net_info or {}).get("name")
    return EnergyAnalyzerHardwareAdapter(netname)
