# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Electronic Load dispatcher module using BaseDispatcher pattern."""
from __future__ import annotations

from typing import Any, Type

from lager.dispatchers.base import BaseDispatcher
from lager.exceptions import ELoadBackendError, DeviceNotFoundError
from .eload_net import ELoadNet


class ELoadDispatcher(BaseDispatcher):
    """
    Dispatcher for electronic load operations.

    Uses BaseDispatcher pattern for consistent net resolution and driver caching.
    """

    ROLE = "eload"
    ERROR_CLASS = ELoadBackendError
    _driver_cache = {}  # Class-level cache for eload drivers

    def _choose_driver(self, instrument_name: str) -> Type[Any]:
        """
        Return the driver class based on the instrument string.

        Currently supports Rigol DL3021 electronic loads.
        """
        instrument = (instrument_name or "").lower()
        if "rigol" in instrument and ("dl3021" in instrument or "dl3000" in instrument):
            from .rigol_dl3021 import RigolDL3021
            return RigolDL3021
        raise self._make_error(f"Unsupported electronic load instrument: {instrument_name}")

    def _make_error(self, message: str) -> Exception:
        """Create an ELoadBackendError."""
        return self.ERROR_CLASS(message)

    def _make_driver(self, rec: dict[str, Any], netname: str, channel: int) -> Any:
        """
        Construct or retrieve a cached driver instance.

        Electronic loads like the RigolDL3021 use a net_info dict pattern
        for construction rather than address/channel.
        """
        instrument = rec.get("instrument") or ""
        address = self._resolve_address(rec, netname)

        # Check cache first
        cache_key = self._get_cache_key(instrument, address, netname)
        cached = self._get_cached_driver(cache_key)
        if cached is not None:
            return cached

        Driver = self._choose_driver(instrument)

        try:
            # RigolDL3021 expects a net_info dict
            net_info = {"name": netname, "address": address}
            driver = Driver(net_info)

            # Cache the driver for reuse
            self._cache_driver(cache_key, driver)
            return driver
        except DeviceNotFoundError:
            raise
        except Exception as exc:
            raise self._make_error(str(exc)) from exc

    def resolve_driver(self, netname: str) -> ELoadNet:
        """
        Resolve net and return the driver instance.

        This is a simplified version of _resolve_net_and_driver that doesn't
        return the channel (since electronic loads are typically single-channel).
        """
        rec = self._find_net(netname)
        # ELoads don't use channel, but we need to resolve it for the method signature
        channel = 1
        return self._make_driver(rec, netname, channel)


# Module-level dispatcher singleton
_dispatcher = ELoadDispatcher()


# ---------- Action functions (called from eload commands) ----------

def set_constant_current(net_name: str, current: float) -> dict:
    """Set constant current mode and current level."""
    device = _dispatcher.resolve_driver(net_name)
    device.set_mode("CC")
    device.set_current(current)
    return {"mode": "CC", "current": current}


def get_constant_current(net_name: str) -> dict:
    """Get current setting."""
    device = _dispatcher.resolve_driver(net_name)
    current = device.get_current()
    return {"mode": "CC", "current": current}


def set_constant_voltage(net_name: str, voltage: float) -> dict:
    """Set constant voltage mode and voltage level."""
    device = _dispatcher.resolve_driver(net_name)
    device.set_mode("CV")
    device.set_voltage(voltage)
    return {"mode": "CV", "voltage": voltage}


def get_constant_voltage(net_name: str) -> dict:
    """Get voltage setting."""
    device = _dispatcher.resolve_driver(net_name)
    voltage = device.get_voltage()
    return {"mode": "CV", "voltage": voltage}


def set_constant_resistance(net_name: str, resistance: float) -> dict:
    """Set constant resistance mode and resistance level."""
    device = _dispatcher.resolve_driver(net_name)
    device.set_mode("CR")
    device.set_resistance(resistance)
    return {"mode": "CR", "resistance": resistance}


def get_constant_resistance(net_name: str) -> dict:
    """Get resistance setting."""
    device = _dispatcher.resolve_driver(net_name)
    resistance = device.get_resistance()
    return {"mode": "CR", "resistance": resistance}


def set_constant_power(net_name: str, power: float) -> dict:
    """Set constant power mode and power level."""
    device = _dispatcher.resolve_driver(net_name)
    device.set_mode("CW")
    device.set_power(power)
    return {"mode": "CP", "power": power}


def get_constant_power(net_name: str) -> dict:
    """Get power setting."""
    device = _dispatcher.resolve_driver(net_name)
    power = device.get_power()
    return {"mode": "CP", "power": power}


def get_state(net_name: str) -> dict:
    """Get comprehensive electronic load state."""
    device = _dispatcher.resolve_driver(net_name)
    device.print_state()
    return {"status": "success"}
