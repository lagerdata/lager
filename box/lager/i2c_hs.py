# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for I2C nets (create_device factory).

Thin shim over the I2C dispatcher's cached, config-aware driver. Each bus
transaction is one /invoke under the net's shared ``device_id`` lock. Config
persistence stays in the /net/command handler; this adapter applies the
effective config to the live driver and runs scan/read/write/transfer.
"""
from __future__ import annotations


class I2CHardwareAdapter:
    def __init__(self, netname: str) -> None:
        self._netname = netname

    def config(self, frequency_hz, pull_ups) -> None:
        from lager.protocols.i2c import dispatcher as _disp
        drv = _disp._resolve_net_and_driver(
            self._netname,
            {"frequency_hz": frequency_hz, "pull_ups": pull_ups})
        drv.config(frequency_hz=frequency_hz, pull_ups=pull_ups)

    def scan(self, start_addr=None, end_addr=None, overrides=None) -> list:
        from lager.protocols.i2c import dispatcher as _disp
        drv = _disp._resolve_net_and_driver(self._netname, overrides)
        kwargs = {}
        if start_addr is not None:
            kwargs["start_addr"] = int(start_addr)
        if end_addr is not None:
            kwargs["end_addr"] = int(end_addr)
        return [int(a) for a in drv.scan(**kwargs)]

    def read(self, address, num_bytes, overrides=None) -> list:
        from lager.protocols.i2c import dispatcher as _disp
        drv = _disp._resolve_net_and_driver(self._netname, overrides)
        return [int(b) for b in drv.read(int(address), int(num_bytes))]

    def write(self, address, data, overrides=None) -> None:
        from lager.protocols.i2c import dispatcher as _disp
        drv = _disp._resolve_net_and_driver(self._netname, overrides)
        drv.write(int(address), [int(b) for b in (data or [])])

    def write_read(self, address, data, num_bytes, overrides=None) -> list:
        from lager.protocols.i2c import dispatcher as _disp
        drv = _disp._resolve_net_and_driver(self._netname, overrides)
        return [int(b) for b in drv.write_read(
            int(address), [int(b) for b in (data or [])], int(num_bytes))]


def create_device(net_info, **_):
    netname = (net_info or {}).get("name")
    return I2CHardwareAdapter(netname)
