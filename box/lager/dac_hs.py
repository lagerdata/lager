# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for DAC nets (create_device factory).

See ``adc_hs`` for why this is a role-unique top-level module. Thin shim over the
DAC dispatcher's cached driver; hardware_service serializes every call under the
net's shared ``device_id`` lock.
"""
from __future__ import annotations


class DACHardwareAdapter:
    def __init__(self, netname: str) -> None:
        self._netname = netname

    def input(self) -> float:
        from lager.io.dac import dispatcher as _disp
        return float(_disp.read_voltage(self._netname))

    def output(self, value) -> float:
        from lager.io.dac import dispatcher as _disp
        v = float(value)
        _disp.write_voltage(self._netname, v)
        return v


def create_device(net_info, **_):
    netname = (net_info or {}).get("name")
    return DACHardwareAdapter(netname)
