# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for SPI nets (create_device factory).

Thin shim over the SPI dispatcher's cached, config-aware driver. Each bus
transaction is one /invoke under the net's shared ``device_id`` lock, so a full
transfer can't interleave with another request on the same device. Config
persistence stays in the /net/command handler (a saved_nets.json write); this
adapter only applies the effective config to the live driver and runs transfers.

Transfers return ``{"words": [...], "word_size": N}`` so the CLI can format
multi-byte words correctly.
"""
from __future__ import annotations


class SPIHardwareAdapter:
    def __init__(self, netname: str) -> None:
        self._netname = netname

    def config(self, cfg=None) -> None:
        from lager.protocols.spi import dispatcher as _disp
        _disp._resolve_net_and_driver(self._netname, cfg or None)

    def _result(self, drv, words):
        return {
            "words": [int(w) for w in words],
            "word_size": int(getattr(drv, "_word_size", 8)),
        }

    def read(self, n_words, fill=0xFF, keep_cs=False, overrides=None) -> dict:
        from lager.protocols.spi import dispatcher as _disp
        drv = _disp._resolve_net_and_driver(self._netname, overrides)
        result = drv.read(int(n_words), fill=int(fill), keep_cs=bool(keep_cs))
        return self._result(drv, result)

    def read_write(self, data, keep_cs=False, overrides=None) -> dict:
        from lager.protocols.spi import dispatcher as _disp
        drv = _disp._resolve_net_and_driver(self._netname, overrides)
        result = drv.read_write([int(b) for b in (data or [])],
                                keep_cs=bool(keep_cs))
        return self._result(drv, result)

    def transfer(self, data, n_words=None, fill=0xFF, keep_cs=False,
                 overrides=None) -> dict:
        from lager.protocols.spi import dispatcher as _disp
        buf = [int(b) for b in (data or [])]
        n = int(n_words) if n_words is not None else len(buf)
        if len(buf) < n:
            buf = buf + [int(fill)] * (n - len(buf))
        elif len(buf) > n:
            buf = buf[:n]
        drv = _disp._resolve_net_and_driver(self._netname, overrides)
        result = drv.read_write(buf, keep_cs=bool(keep_cs))
        return self._result(drv, result)


def create_device(net_info, **_):
    netname = (net_info or {}).get("name")
    return SPIHardwareAdapter(netname)
