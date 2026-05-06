# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Energy analyzer dispatcher that routes operations to the appropriate implementation.

The energy-analyzer role is currently exclusive to the Joulescope JS220.
"""

from __future__ import annotations

from typing import Any, Dict, Type

from lager.dispatchers import BaseDispatcher
from lager.exceptions import EnergyAnalyzerBackendError

from .energy_analyzer_net import EnergyAnalyzerBase
from .joulescope_energy import JoulescopeEnergyAnalyzer


class EnergyAnalyzerDispatcher(BaseDispatcher):
    """
    Dispatcher that routes energy analyzer operations to the JS220 implementation.
    """

    ROLE = "energy-analyzer"
    ERROR_CLASS = EnergyAnalyzerBackendError
    _driver_cache: Dict[str, Any] = {}

    def _choose_driver(self, instrument_name: str) -> Type[EnergyAnalyzerBase]:
        instrument_lower = instrument_name.lower()
        if 'joulescope' in instrument_lower or 'js220' in instrument_lower:
            return JoulescopeEnergyAnalyzer
        if 'ppk2' in instrument_lower or 'ppk' in instrument_lower or 'nordic' in instrument_lower:
            from .ppk2_energy import PPK2EnergyAnalyzer
            return PPK2EnergyAnalyzer
        raise EnergyAnalyzerBackendError(
            f"Unsupported energy-analyzer instrument: '{instrument_name}'. "
            "Supported instruments: Joulescope JS220, Nordic PPK2."
        )

    def _make_error(self, message: str) -> EnergyAnalyzerBackendError:
        return EnergyAnalyzerBackendError(message)

    def _make_driver(
        self, rec: Dict[str, Any], netname: str, channel: int
    ) -> EnergyAnalyzerBase:
        instrument = rec.get("instrument") or "joulescope"
        location = rec.get("address") or f"joulescope:{channel}"

        cache_key = self._get_cache_key(instrument, location, netname, channel)
        cached = self._get_cached_driver(cache_key)
        if cached is not None:
            return cached

        Driver = self._choose_driver(instrument)

        try:
            driver = Driver(name=netname, pin=channel, location=location)
            self._cache_driver(cache_key, driver)
            return driver
        except Exception as exc:
            raise self._make_error(str(exc)) from exc

    @classmethod
    def from_net_name(cls, netname: str) -> EnergyAnalyzerBase:
        dispatcher = cls()
        driver, _ = dispatcher._resolve_net_and_driver(netname)
        return driver


# Module-level singleton dispatcher
_dispatcher = EnergyAnalyzerDispatcher()


def read_energy(netname: str, duration: float = 10.0) -> dict:
    """
    Integrate energy and charge from an energy-analyzer net.

    Args:
        netname: Name of the saved energy-analyzer net
        duration: Integration duration in seconds (default 10.0)

    Returns:
        dict with energy_j, energy_wh, charge_c, charge_ah, duration_s
    """
    driver, _ = _dispatcher._resolve_net_and_driver(netname)
    return driver.read_energy(duration)


def read_stats(netname: str, duration: float = 1.0) -> dict:
    """
    Compute statistics from an energy-analyzer net.

    Args:
        netname: Name of the saved energy-analyzer net
        duration: Measurement duration in seconds (default 1.0)

    Returns:
        Nested dict with current/voltage/power statistics
    """
    driver, _ = _dispatcher._resolve_net_and_driver(netname)
    return driver.read_stats(duration)


__all__ = [
    'EnergyAnalyzerDispatcher',
    'read_energy',
    'read_stats',
]
