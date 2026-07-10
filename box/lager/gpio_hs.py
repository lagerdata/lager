# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for GPIO nets (create_device factory).

See ``adc_hs`` for why this is a role-unique top-level module. Thin shim over the
GPIO dispatcher's cached driver; hardware_service serializes every call under the
net's shared ``device_id`` lock — critically the LabJack's single LJM handle,
shared across GPIO/ADC/DAC/SPI/I2C nets.

``output`` and ``wait_for_level`` are composite (read+write / blocking poll) but
run as a single /invoke, so they complete atomically under the device lock.
"""
from __future__ import annotations


class GPIOHardwareAdapter:
    def __init__(self, netname: str) -> None:
        self._netname = netname

    def input(self) -> int:
        from lager.io.gpio import dispatcher as _disp
        return int(_disp.gpi(self._netname))

    def output(self, level) -> int:
        """Drive the pin and return the resulting level (0/1).

        Supports an explicit level (0/1, high/low, on/off) and "toggle", which
        reads the current level and writes its inverse — both under one lock.
        """
        from lager.io.gpio import dispatcher as _disp
        drv, _ = _disp._dispatcher._resolve_net_and_driver(self._netname)
        if str(level).strip().lower() == "toggle":
            new = 0 if int(drv.input()) else 1
        else:
            new = _disp._normalize_level(level)
        drv.output(new)
        return int(new)

    def wait_for_level(self, level, timeout=None, scan_rate=None,
                       scans_per_read=None, poll_interval=None) -> float:
        from lager.io.gpio import dispatcher as _disp
        return float(_disp.wait_for_level(
            self._netname, level, timeout=timeout, scan_rate=scan_rate,
            scans_per_read=scans_per_read, poll_interval=poll_interval))


def create_device(net_info, **_):
    netname = (net_info or {}).get("name")
    return GPIOHardwareAdapter(netname)
