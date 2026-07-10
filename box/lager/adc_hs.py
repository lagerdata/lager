# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for ADC nets (create_device factory).

hardware_service resolves ``device`` names against ``lager.{name}`` first, so
this lives at the top of the package as ``adc_hs`` — a role-unique name (the raw
driver module ``labjack_t7`` is ambiguous across io.adc / io.dac / io.gpio).

The adapter is a thin shim over the existing ADC dispatcher: it delegates to the
dispatcher's cached driver so behavior is identical to the ``lager adc`` path,
while hardware_service now owns the single process that touches the device and
serializes every call under the net's shared ``device_id`` lock.
"""
from __future__ import annotations


class ADCHardwareAdapter:
    def __init__(self, netname: str) -> None:
        self._netname = netname

    def input(self) -> float:
        from lager.io.adc import dispatcher as _disp
        return float(_disp.read(self._netname))


def create_device(net_info, **_):
    netname = (net_info or {}).get("name")
    return ADCHardwareAdapter(netname)
