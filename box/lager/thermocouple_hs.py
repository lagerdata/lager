# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for thermocouple nets (create_device factory).

Thin shim over the thermocouple dispatcher's cached driver; hardware_service
serializes every call under the net's shared ``device_id`` lock.
"""
from __future__ import annotations


class ThermocoupleHardwareAdapter:
    def __init__(self, netname: str) -> None:
        self._netname = netname

    def read(self) -> float:
        from lager.measurement.thermocouple import dispatcher as _disp
        return float(_disp.read(self._netname))


def create_device(net_info, **_):
    netname = (net_info or {}).get("name")
    return ThermocoupleHardwareAdapter(netname)
