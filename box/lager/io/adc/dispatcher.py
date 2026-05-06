# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
ADC dispatcher for routing CLI commands to appropriate hardware backends.

This module provides the dispatcher for routing ADC (analog-to-digital converter)
operations to the appropriate hardware backend drivers. Uses the BaseDispatcher
pattern for consistency with other hardware dispatchers.

Supported instruments:
- LabJack T7: Multi-channel ADC with AIN0-AIN13
- MCC USB-202: 8-channel ADC with CH0-CH7

Command formats supported:
- adc <NET> -> reads voltage, prints voltage value
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, Optional, Type

from lager.dispatchers.base import BaseDispatcher
from lager.dispatchers import helpers
from lager.exceptions import ADCBackendError

from lager.io.adc.labjack_t7 import LabJackADC
from lager.io.adc.usb202 import USB202ADC


DEBUG = bool(os.environ.get("LAGER_ADC_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_ADC_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"DEBUG: {msg}\n")


class ADCDispatcher(BaseDispatcher):
    """
    Dispatcher for ADC operations.

    Routes ADC read commands to the appropriate hardware backend based on
    the instrument type configured for each net.
    """

    ROLE = "adc"
    ERROR_CLASS = ADCBackendError
    _driver_cache: Dict[str, Any] = {}  # Class-level cache for driver instances

    def _choose_driver(self, instrument_name: str) -> Type[Any]:
        """
        Return the driver class based on the instrument string stored in the local net.
        """
        inst = (instrument_name or "").strip()

        # LabJack T7
        if re.search(r"labjack[_\-\s]*t7", inst, re.IGNORECASE):
            return LabJackADC

        # MCC USB-202
        if re.search(r"mcc[_\-\s]*usb[_\-]?202", inst, re.IGNORECASE):
            return USB202ADC

        raise self._make_error(f"Unsupported instrument for ADC nets: '{instrument_name}'.")

    def _make_error(self, message: str) -> Exception:
        """Create an ADCBackendError with the given message."""
        return self.ERROR_CLASS(message)

    def _make_driver(self, rec: Dict[str, Any], netname: str, channel: int) -> Any:
        """
        Construct the correct backend with the constructor signature it expects.

        This overrides the base class method to handle driver-specific
        constructor signatures for ADC backends.
        """
        instrument = rec.get("instrument") or ""
        address = self._resolve_address(rec, netname)

        # Check cache first
        cache_key = self._get_cache_key(instrument, address, netname, channel)
        cached = self._get_cached_driver(cache_key)
        if cached is not None:
            return cached

        Driver = self._choose_driver(instrument)

        try:
            if Driver is LabJackADC:
                # LabJack: pass (name, pin)
                driver = Driver(name=netname, pin=channel)

            elif Driver is USB202ADC:
                # USB-202: pass (name, pin, unique_id)
                # address may be a VISA string or serial number
                driver = Driver(name=netname, pin=channel, unique_id=address)

            else:
                # Future-proof fallback
                driver = Driver(name=netname, pin=channel)

            self._cache_driver(cache_key, driver)
            return driver

        except Exception as exc:
            raise self._make_error(str(exc)) from exc

    def _resolve_address(self, rec: Dict[str, Any], netname: str) -> str:
        """
        Resolve the VISA/device address for the net.

        For ADC, the address may be optional (LabJack auto-discovers).
        Returns empty string if no address configured.
        """
        try:
            return helpers.resolve_address(rec, netname, self.ERROR_CLASS)
        except self.ERROR_CLASS:
            # Address is optional for some ADC devices (LabJack auto-discovers)
            return ""


# Module-level singleton dispatcher instance
_dispatcher = ADCDispatcher()


# --------- actions (called from adc.py impl scripts) ---------

def read(netname: str, **_) -> float:
    """
    Read voltage from the specified ADC net.

    Args:
        netname: The name of the ADC net to read from.

    Returns:
        Voltage reading in volts as a float.

    Raises:
        ADCBackendError: If the net is not found or reading fails.
    """
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    return drv.input()


def voltage(netname: str, **_) -> float:
    """
    Alias for read() - reads voltage from the specified ADC net.

    Args:
        netname: The name of the ADC net to read from.

    Returns:
        Voltage reading in volts as a float.
    """
    return read(netname)


# Backward compatibility with old dispatcher interface
def _do_adc_read(net_name: str) -> float:
    """
    Perform ADC voltage measurement operation.

    This is kept for backward compatibility with existing code that uses
    the old dispatcher interface.

    Args:
        net_name: Name of the ADC net to read

    Returns:
        Voltage reading in volts as a float
    """
    return read(net_name)


# --------- CLI dispatcher entry point ---------

def _usage(exit_code: int = 2) -> None:
    """Print usage information and exit."""
    sys.stderr.write(
        "Usage:\n"
        "  adc <NET>\n"
        "  <BOX_ID> adc <NET>\n"
    )
    sys.exit(exit_code)


def _parse_args(argv: list[str]) -> tuple[str, str]:
    """
    Parse command line arguments.

    Returns:
        Tuple of (mode, net_name) where:
        - mode: "adc"
        - net_name: the ADC net name
    """
    if not argv:
        _usage()

    # Check if first argument is a command or box ID
    if argv[0] == "adc":
        start_idx = 1
    else:
        # Assume first argument is box ID, second is command
        if len(argv) < 2 or argv[1] != "adc":
            _usage()
        start_idx = 2
        _debug(f"Using box ID: {argv[0]}")

    # Extract net name
    if len(argv) <= start_idx:
        _usage()
    net_name = argv[start_idx]

    return "adc", net_name


def main(argv: Optional[list[str]] = None) -> int:
    """
    Main dispatcher entry point.

    Args:
        argv: Command line arguments (uses sys.argv if None)

    Returns:
        0 on success, 1 on error
    """
    args = sys.argv[1:] if argv is None else list(argv)

    try:
        mode, net_name = _parse_args(args)
        _debug(f"Parsed: mode={mode}, net={net_name}")

        voltage_val = read(net_name)
        sys.stdout.write(f"{voltage_val}\n")
        sys.stdout.flush()

        return 0

    except Exception as e:
        sys.stderr.write(f"adc dispatcher error: {e}\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
