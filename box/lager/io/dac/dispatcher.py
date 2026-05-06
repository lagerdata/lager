# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
DAC dispatcher for routing CLI commands to appropriate hardware backends.

Handles 'lager dac' commands by:
1. Looking up net configuration from saved_nets.json
2. Determining the appropriate backend (LabJack T7 or USB-202)
3. Routing the operation to the selected backend
4. Returning results in the expected format

Command formats supported:
- dac <NET> -> reads current voltage output, prints voltage value
- dac <NET> <VOLTAGE> -> sets voltage output to specified value
- <BOX_ID> dac <NET> -> optional box prefix for read
- <BOX_ID> dac <NET> <VOLTAGE> -> optional box prefix for write

Errors are printed as: 'dac dispatcher error: <message>'

Uses BaseDispatcher pattern for consistent net resolution and driver management.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional, Tuple, Type

from lager.dispatchers.base import BaseDispatcher
from lager.dispatchers import helpers
from lager.exceptions import DACBackendError

from .labjack_t7 import LabJackDAC
from .usb202 import USB202DAC

DEBUG = bool(os.environ.get("LAGER_DAC_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_DAC_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"DEBUG: {msg}\n")


class DACDispatcher(BaseDispatcher):
    """
    Dispatcher for DAC (Digital-to-Analog Converter) operations.
    
    Routes operations to the appropriate hardware backend based on
    instrument type configured in the net definition.
    """
    
    ROLE = "dac"
    ERROR_CLASS = DACBackendError
    _driver_cache: Dict[str, Any] = {}  # Class-level cache
    
    def _choose_driver(self, instrument_name: str) -> Type[Any]:
        """
        Return the driver class based on the instrument string.
        
        Args:
            instrument_name: The instrument identifier from the net configuration.
            
        Returns:
            The driver class to use for this instrument.
            
        Raises:
            DACBackendError: If the instrument is not supported.
        """
        inst_lower = (instrument_name or "").lower()
        
        if "usb-202" in inst_lower or "usb202" in inst_lower or "mcc" in inst_lower:
            return USB202DAC
        elif "labjack" in inst_lower or "t7" in inst_lower or not instrument_name:
            # Default to LabJack T7 for backward compatibility
            return LabJackDAC
        else:
            raise self._make_error(f"Unsupported DAC instrument: {instrument_name}")
    
    def _make_error(self, message: str) -> DACBackendError:
        """
        Create an appropriate error for this dispatcher type.
        
        Args:
            message: The error message.
            
        Returns:
            A DACBackendError instance.
        """
        return DACBackendError(message)
    
    def _make_driver(
        self, rec: Dict[str, Any], netname: str, channel: int
    ) -> Any:
        """
        Construct or retrieve a cached driver instance.
        
        Handles the different constructor signatures for LabJack vs USB-202 drivers.
        
        Args:
            rec: The net configuration record.
            netname: The net name.
            channel: The resolved channel number.
            
        Returns:
            A driver instance.
            
        Raises:
            DACBackendError: If driver construction fails.
        """
        instrument = rec.get("instrument") or ""
        
        # Get address - USB-202 needs unique_id, LabJack doesn't need address
        address = rec.get("address", "")
        
        # Check cache first
        cache_key = self._get_cache_key(instrument, address, netname, channel)
        cached = self._get_cached_driver(cache_key)
        if cached is not None:
            return cached
        
        # Create new driver
        Driver = self._choose_driver(instrument)
        
        try:
            if Driver == USB202DAC:
                # USB-202 needs unique_id for device selection
                driver = Driver(name=netname, pin=channel, unique_id=address)
            else:
                # LabJack T7 just needs name and pin
                driver = Driver(name=netname, pin=channel)
            
            self._cache_driver(cache_key, driver)
            return driver
        except Exception as exc:
            raise self._make_error(str(exc)) from exc
    
    def read_voltage(self, netname: str) -> float:
        """
        Read the current voltage output from a DAC net.
        
        Args:
            netname: Name of the DAC net to read.
            
        Returns:
            Current voltage output in volts.
            
        Raises:
            DACBackendError: If the operation fails.
        """
        driver, _ = self._resolve_net_and_driver(netname)
        return driver.get_voltage()
    
    def write_voltage(self, netname: str, voltage: float) -> None:
        """
        Set the voltage output of a DAC net.
        
        Args:
            netname: Name of the DAC net to set.
            voltage: Desired output voltage in volts.
            
        Raises:
            DACBackendError: If the operation fails.
        """
        driver, _ = self._resolve_net_and_driver(netname)
        driver.output(voltage)


# Module-level dispatcher singleton for backward compatibility
_dispatcher = DACDispatcher()


# Public API functions that use the dispatcher
def read_voltage(netname: str) -> float:
    """
    Read the current voltage output from a DAC net.
    
    Args:
        netname: Name of the DAC net to read.
        
    Returns:
        Current voltage output in volts.
    """
    return _dispatcher.read_voltage(netname)


def write_voltage(netname: str, voltage: float) -> None:
    """
    Set the voltage output of a DAC net.
    
    Args:
        netname: Name of the DAC net to set.
        voltage: Desired output voltage in volts.
    """
    _dispatcher.write_voltage(netname, voltage)


# Backward-compatible functions
def output(netname: str, voltage: float) -> None:
    """Set voltage output (alias for write_voltage)."""
    write_voltage(netname, voltage)


def input(netname: str) -> float:
    """Read voltage output (alias for read_voltage)."""
    return read_voltage(netname)


# =============================================================================
# CLI Entry Point
# =============================================================================

def _usage(exit_code: int = 2) -> None:
    """Print usage information and exit."""
    sys.stderr.write(
        "Usage:\n"
        "  dac <NET>\n"
        "  dac <NET> <VOLTAGE>\n"
        "  <BOX_ID> dac <NET>\n"
        "  <BOX_ID> dac <NET> <VOLTAGE>\n"
    )
    sys.exit(exit_code)


def _parse_args(argv: list[str]) -> Tuple[str, str, Optional[float]]:
    """
    Parse command line arguments.
    
    Returns:
        Tuple of (mode, net_name, voltage_or_none) where:
        - mode: "dac"
        - net_name: the DAC net name
        - voltage_or_none: voltage for output, None for input (read)
    """
    if not argv:
        _usage()

    # Check if first argument is a command or box ID
    if argv[0] == "dac":
        start_idx = 1
    else:
        # Assume first argument is box ID, second is command
        if len(argv) < 2 or argv[1] != "dac":
            _usage()
        start_idx = 2
        _debug(f"Using box ID: {argv[0]}")

    # Extract net name
    if len(argv) <= start_idx:
        _usage()
    net_name = argv[start_idx]
    
    # Extract voltage if provided
    voltage = None
    if len(argv) > start_idx + 1:
        try:
            voltage = float(argv[start_idx + 1])
        except ValueError:
            _usage()

    return "dac", net_name, voltage


def _get_dac_backend_legacy(net_name: str, pin: Optional[int | str] = None) -> LabJackDAC:
    """
    Legacy function to get DAC backend without net configuration lookup.
    
    Used for backward compatibility when no saved nets exist.
    In the future, this should be removed in favor of dispatcher-based lookup.
    
    Args:
        net_name: Name of the DAC net
        pin: Optional pin override (extracted from net name if not provided)
        
    Returns:
        Configured DAC backend instance
    """
    # For now, always use LabJack T7 backend
    # Pin will be determined from net configuration or default to net name
    if pin is None:
        # Try to extract numeric pin from net name (e.g., "dac1" -> 1)
        import re
        match = re.search(r'(\d+)$', net_name)
        pin = int(match.group(1)) if match else net_name
    
    _debug(f"Creating LabJack DAC backend for net '{net_name}', pin '{pin}'")
    return LabJackDAC(net_name, pin)


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
        mode, net_name, voltage = _parse_args(args)
        _debug(f"Parsed: mode={mode}, net={net_name}, voltage={voltage}")
        
        # Try to use dispatcher-based lookup first
        try:
            if voltage is None:
                # Read operation
                current_voltage = read_voltage(net_name)
                sys.stdout.write(f"{current_voltage}\n")
                sys.stdout.flush()
            else:
                # Write operation
                write_voltage(net_name, voltage)
                # No output on successful write operation
        except DACBackendError as e:
            # If net not found, fall back to legacy behavior
            if "not found" in str(e).lower():
                _debug(f"Net not found, using legacy backend lookup")
                dac = _get_dac_backend_legacy(net_name)
                if voltage is None:
                    current_voltage = dac.get_voltage()
                    sys.stdout.write(f"{current_voltage}\n")
                    sys.stdout.flush()
                else:
                    dac.output(voltage)
            else:
                raise
            
        return 0
        
    except Exception as e:
        sys.stderr.write(f"dac dispatcher error: {e}\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
