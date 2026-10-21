# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
HSM dispatcher: resolves a saved ``hsm`` net to its driver.

The drivers hold no device handle between calls, so a cached driver is only
a parsed address and is safe to keep.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple, Type

from lager.dispatchers import BaseDispatcher
from lager.exceptions import HsmBackendError

from .hsm_net import HsmBase
from .yubihsm2 import YubiHsm2


class HsmDispatcher(BaseDispatcher):
    """Routes ``hsm`` nets to the driver for their instrument."""

    ROLE = "hsm"
    ERROR_CLASS = HsmBackendError
    _driver_cache: Dict[str, Any] = {}

    def _choose_driver(self, instrument_name: str) -> Type[HsmBase]:
        inst = (instrument_name or "").lower()
        if "yubihsm" in inst:
            return YubiHsm2
        raise self._make_error("Unsupported HSM instrument '%s'" % instrument_name)

    def _make_error(self, message: str) -> HsmBackendError:
        return HsmBackendError(message)

    def _resolve_net_and_driver(self, netname: str) -> Tuple[HsmBase, None]:
        # An HSM net has one channel, so the saved pin is not needed.
        rec = self._find_net(netname)
        return self._make_driver(rec, netname, None), None

    def _make_driver(self, rec: Dict[str, Any], netname: str, channel: Any) -> HsmBase:
        instrument = rec.get("instrument") or ""
        address = self._resolve_address(rec, netname)
        cache_key = self._get_cache_key(instrument, address, netname)
        cached = self._get_cached_driver(cache_key)
        if cached is not None:
            return cached
        Driver = self._choose_driver(instrument)
        driver = Driver(name=netname, address=address)
        self._cache_driver(cache_key, driver)
        return driver


_dispatcher = HsmDispatcher()


def get_driver(netname: str) -> HsmBase:
    """Return the driver for a saved ``hsm`` net."""
    driver, _ = _dispatcher._resolve_net_and_driver(netname)
    return driver


__all__ = [
    'HsmDispatcher',
    'get_driver',
]
