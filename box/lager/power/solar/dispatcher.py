# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Solar simulator dispatcher module using BaseDispatcher pattern."""
from __future__ import annotations

import re
from typing import Any, Type

from lager.dispatchers.base import BaseDispatcher
from lager.exceptions import SolarBackendError, LibraryMissingError, DeviceNotFoundError
from .ea import EA

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'


class SolarDispatcher(BaseDispatcher):
    """
    Dispatcher for solar simulator operations.

    Uses BaseDispatcher pattern for consistent net resolution and driver caching.
    """

    ROLE = "solar"
    ERROR_CLASS = SolarBackendError
    _driver_cache = {}  # Class-level cache for solar drivers

    def _choose_driver(self, instrument_name: str) -> Type[Any]:
        """
        Return the driver class based on the instrument string.

        Currently supports EA PSB series solar simulators.
        """
        inst = (instrument_name or "").strip()
        # All supported EA photovoltaic simulator models
        if re.match(r"EA_PSB", inst) or inst in ("EA_PSB_10080_60", "EA_PSB_10060_60"):
            return EA
        raise self._make_error(f"Unsupported instrument for solar nets: '{instrument_name}'.")

    def _make_error(self, message: str) -> Exception:
        """Create a SolarBackendError."""
        return self.ERROR_CLASS(message)

    def _make_driver(self, rec: dict[str, Any], netname: str, channel: int) -> Any:
        """
        Construct or retrieve a cached EA driver instance.

        EA solar simulators are single-channel and use a different constructor
        signature than the default BaseDispatcher pattern.
        """
        instrument = rec.get("instrument") or ""
        address = self._resolve_address(rec, netname)

        # Check cache first (EA is single-channel, so no channel in cache key)
        cache_key = self._get_cache_key(instrument, address, netname)
        cached = self._get_cached_driver(cache_key)
        if cached is not None:
            return cached

        Driver = self._choose_driver(instrument)

        try:
            # EA driver uses 'instr' parameter for VISA address
            if Driver is EA:
                driver = Driver(instr=address)
            else:
                # For future backends, attempt a generic initialization
                driver = Driver(address=address)

            # Cache the driver for reuse
            self._cache_driver(cache_key, driver)
            return driver
        except (LibraryMissingError, DeviceNotFoundError):
            # Propagate known exceptions for correct handling
            raise
        except Exception as exc:
            # Wrap any other initialization errors
            raise self._make_error(str(exc)) from exc

    def resolve_driver(self, netname: str) -> Any:
        """
        Resolve net and return the driver instance.

        This is a simplified version of _resolve_net_and_driver that doesn't
        return the channel (since solar simulators are single-channel).
        """
        rec = self._find_net(netname)
        # Solar nets don't use channel, but we need to resolve it for the method signature
        channel = 1
        return self._make_driver(rec, netname, channel)


# Module-level dispatcher singleton
_dispatcher = SolarDispatcher()


# ---------- Action functions (called from solar.py) ----------

def set_to_solar_mode(netname: str, **_) -> None:
    """Initialize and start the solar simulation mode."""
    drv = _dispatcher.resolve_driver(netname)

    # Try connection with retry logic for rapid set/stop cycling
    max_retries = 3
    last_error = None

    for attempt in range(max_retries):
        try:
            drv.connect_instrument()
            # The EA driver handles all initialization in connect_instrument()
            print(f"{GREEN}Solar simulator '{netname}' initialized and started in PV simulation mode{RESET}")
            return
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                # Wait before retry - EA devices need time to settle after stop
                import time
                time.sleep(1.0 + attempt * 0.5)
            else:
                # Final attempt failed
                raise SolarBackendError(
                    f"Failed to initialize solar simulator after {max_retries} attempts. "
                    f"This may happen if the device was recently stopped. "
                    f"Wait a few seconds and try again. Error: {e}"
                )


def stop_solar_mode(netname: str, **_) -> None:
    """Stop the solar simulation mode and disconnect."""
    drv = _dispatcher.resolve_driver(netname)

    # Try to connect first (might already be connected)
    try:
        drv.connect_instrument()  # Ensure we can communicate
    except Exception:
        # If connection fails, device might already be stopped - continue anyway
        pass

    try:
        drv.disconnect_instrument()  # This will stop the simulation
        print(f"{GREEN}Solar simulator '{netname}' stopped{RESET}")

        # Give the device time to fully stop and release resources
        # This is critical for rapid set/stop cycling
        import time
        time.sleep(0.5)
    except Exception as e:
        # Even if disconnect fails, report success if device is already stopped
        # Check device state to verify
        try:
            status = drv.instr.query("FUNCtion:PHOTovoltaics:STATe?")
            if "STOP" in str(status) or "OFF" in str(status):
                print(f"{GREEN}Solar simulator '{netname}' stopped{RESET}")
                return
        except Exception:
            pass
        # If we can't verify, raise the original error
        raise SolarBackendError(f"Failed to stop solar simulator: {e}")


def irradiance(netname: str, value: float | None = None, **_) -> None:
    """Get or set the solar irradiance (W/m^2)."""
    drv = _dispatcher.resolve_driver(netname)
    # Validate irradiance value if setting
    if value is not None:
        if value < 0:
            raise SolarBackendError(f"Irradiance cannot be negative, got: {value} W/m^2")
        if value > 1500:
            raise SolarBackendError(f"Irradiance too high (max 1500 W/m^2), got: {value} W/m^2")
    # Ensure connection and simulation are running
    drv.connect_instrument()
    result = drv.irradiance(value=value)
    # Always print result for both GET and SET operations
    if result is not None and result.strip():
        print(f"{GREEN}{result}{RESET}")


def mpp_current(netname: str, **_) -> None:
    """Get the maximum power point current."""
    drv = _dispatcher.resolve_driver(netname)
    drv.connect_instrument()
    print(f"{GREEN}{drv.mpp_current()}{RESET}")


def mpp_voltage(netname: str, **_) -> None:
    """Get the maximum power point voltage."""
    drv = _dispatcher.resolve_driver(netname)
    drv.connect_instrument()
    print(f"{GREEN}{drv.mpp_voltage()}{RESET}")


def resistance(netname: str, value: float | None = None, **_) -> None:
    """Get or set the panel resistance."""
    drv = _dispatcher.resolve_driver(netname)
    drv.connect_instrument()
    if value is None:
        print(f"{GREEN}{drv.resistance()}{RESET}")
    else:
        # Validate resistance value
        if value <= 0:
            raise SolarBackendError(f"Resistance must be positive, got: {value} Ohm")
        print(f"{GREEN}{drv.resistance(value)}{RESET}")


def temperature(netname: str, **_) -> None:
    """Get the cell temperature."""
    drv = _dispatcher.resolve_driver(netname)
    drv.connect_instrument()
    print(f"{GREEN}{drv.temperature()}{RESET}")


def voc(netname: str, **_) -> None:
    """Get the open-circuit voltage."""
    drv = _dispatcher.resolve_driver(netname)
    drv.connect_instrument()
    print(f"{GREEN}{drv.voc()}{RESET}")
