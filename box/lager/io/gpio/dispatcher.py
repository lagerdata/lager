# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
GPIO dispatcher module for digital input/output control.

This module provides the dispatcher for routing GPIO operations
to the appropriate hardware backend drivers (LabJack T7, USB-202).
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, Optional, Type

from lager.dispatchers.base import BaseDispatcher
from lager.dispatchers import helpers
from lager.exceptions import GPIOBackendError, LibraryMissingError, DeviceNotFoundError

from .labjack_t7 import LabJackGPIO
from .usb202 import USB202GPIO
from .ft232h_gpio import FT232HGPIO
from .aardvark_gpio import AardvarkGPIO

DEBUG = bool(os.environ.get("LAGER_GPIO_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_GPIO_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"DEBUG: {msg}\n")
        sys.stderr.flush()


class GPIODispatcher(BaseDispatcher):
    """
    Dispatcher for GPIO operations.

    Routes GPIO commands (gpi/read, gpo/write) to the appropriate hardware
    backend based on the instrument type configured for each net.
    """

    ROLE = "gpio"
    ERROR_CLASS = GPIOBackendError
    _driver_cache: Dict[str, Any] = {}  # Class-level cache for driver instances

    def _choose_driver(self, instrument_name: str) -> Type[Any]:
        """
        Return the driver class based on the instrument string stored in the local net.
        """
        inst = (instrument_name or "").strip().lower()

        # LabJack T7
        if re.search(r"labjack[_\-\s]*t7", inst, re.IGNORECASE) or inst == "labjack_t7":
            return LabJackGPIO

        # USB-202 / MCC USB-202
        if re.search(r"(usb[_\-]?202|mcc.*usb.*202)", inst, re.IGNORECASE):
            return USB202GPIO

        if re.search(r"(ft232h|ftdi)", inst, re.IGNORECASE):
            return FT232HGPIO

        if re.search(r"(aardvark|totalphase)", inst, re.IGNORECASE):
            return AardvarkGPIO

        # Default to LabJack T7 if no instrument specified
        if not inst:
            _debug("No instrument specified, defaulting to LabJack T7")
            return LabJackGPIO

        raise self._make_error(f"Unsupported instrument for GPIO nets: '{instrument_name}'.")

    def _make_error(self, message: str) -> Exception:
        """Create a GPIOBackendError with the given message."""
        return self.ERROR_CLASS(message)

    def _make_driver(self, rec: Dict[str, Any], netname: str, channel) -> Any:
        """
        Construct the correct backend with the constructor signature it expects.

        GPIO drivers take (name, pin) as constructor arguments.
        """
        instrument = rec.get("instrument") or ""
        # For GPIO, use pin as the channel identifier
        pin = channel

        # Check cache first using netname and pin
        cache_key = self._get_cache_key(instrument, netname, netname, pin)
        cached = self._get_cached_driver(cache_key)
        if cached is not None:
            return cached

        Driver = self._choose_driver(instrument)

        try:
            if Driver is LabJackGPIO:
                # LabJack GPIO: (name, pin)
                driver = Driver(netname, pin)

            elif Driver is USB202GPIO:
                # USB-202 GPIO: (name, pin, unique_id)
                # Get unique_id from address field if available
                address = rec.get("address")
                driver = Driver(netname, pin, unique_id=address)

            elif Driver is FT232HGPIO:
                # FT232H GPIO: (name, pin, serial)
                # Extract serial from VISA-format address if available
                address = rec.get("address") or ""
                serial = None
                if "::" in address:
                    parts = address.split("::")
                    if len(parts) >= 4:
                        serial = parts[3]
                elif address and not address.startswith("ftdi://"):
                    serial = address
                driver = Driver(netname, pin, serial=serial)

            elif Driver is AardvarkGPIO:
                # Aardvark GPIO: (name, pin, port, serial, target_power)
                params = rec.get("params") or {}
                port = int(params.get("port", 0))
                target_power = bool(params.get("target_power", False))
                serial = rec.get("address") or None
                driver = Driver(netname, pin, port=port, serial=serial,
                                target_power=target_power)

            else:
                # Future-proof fallback
                driver = Driver(netname, pin)

            self._cache_driver(cache_key, driver)
            return driver

        except (LibraryMissingError, DeviceNotFoundError):
            # Bubble known exceptions for consistent exit codes
            raise
        except Exception as exc:
            # Wrap any other init error
            raise self._make_error(str(exc)) from exc

    def _resolve_channel(self, rec: Dict[str, Any], netname: str):
        """
        Override channel resolution to handle GPIO pin extraction.

        For GPIO, we extract the pin from the net config. Returns int for
        numeric pins, or str for named pins (e.g., "FIO0", "CIO0", "SCL").
        Named string pins are passed through directly so hardware drivers
        can parse them with their own pin-naming logic.
        """
        mapping = self._find_mapping_for_net(rec, netname)
        pin = (mapping or {}).get("pin", rec.get("pin"))

        if pin is not None:
            try:
                return int(pin)
            except (TypeError, ValueError):
                # Return string pin directly for named channels
                # (e.g., "FIO0", "CIO0", "SCL", "MOSI")
                return str(pin)

        # Fall back to extracting numeric suffix from net name
        # e.g., "gpio16" -> 16
        match = re.search(r'(\d+)$', netname)
        if match:
            return int(match.group(1))

        # If no pin can be determined, raise error
        raise self._make_error(f"Cannot determine GPIO pin for net '{netname}'. "
                               f"Set 'pin' in net configuration.")


# Module-level singleton dispatcher instance
_dispatcher = GPIODispatcher()


# --------- actions (called from gpio/__init__.py) ---------

def gpi(netname: str) -> int:
    """
    Read the current state of a GPIO input pin.

    Args:
        netname: Name of the GPIO net to read

    Returns:
        0 for LOW, 1 for HIGH
    """
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    return drv.input()


def gpo(netname: str, level: str) -> None:
    """
    Set the output state of a GPIO pin.

    Args:
        netname: Name of the GPIO net to set
        level: Output level (0/1, low/high, off/on)
    """
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.output(level)


def _normalize_level(level) -> int:
    """Normalize a level value (int, str) to 0 or 1."""
    if isinstance(level, str):
        return 1 if level.strip().lower() in ("1", "high", "on", "true") else 0
    return 1 if int(level) else 0


def wait_for_level(
    netname: str,
    level,
    timeout: float | None = None,
    scan_rate: int | None = None,
    scans_per_read: int | None = None,
    poll_interval: float | None = None,
) -> float:
    """
    Block until a GPIO pin reaches the target level.

    Routes to the appropriate driver's ``wait_for_level()`` method.
    LabJack drivers receive ``scan_rate`` / ``scans_per_read``; polling
    drivers receive ``poll_interval``.

    Args:
        netname: Name of the GPIO net.
        level: Target level (0/1, "high"/"low").
        timeout: Maximum seconds to wait (None = forever).
        scan_rate: LabJack streaming Hz (ignored for non-LabJack).
        scans_per_read: LabJack batch size (ignored for non-LabJack).
        poll_interval: Seconds between polls for non-streaming drivers.

    Returns:
        Elapsed time in seconds.

    Raises:
        TimeoutError: If timeout exceeded.
    """
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    int_level = _normalize_level(level)

    kwargs: Dict[str, Any] = {}
    if timeout is not None:
        kwargs["timeout"] = timeout

    if isinstance(drv, LabJackGPIO):
        if scan_rate is not None:
            kwargs["scan_rate"] = scan_rate
        if scans_per_read is not None:
            kwargs["scans_per_read"] = scans_per_read
    else:
        if poll_interval is not None:
            kwargs["poll_interval"] = poll_interval

    return drv.wait_for_level(int_level, **kwargs)


# --------- Legacy API functions ---------

def _do_gpi(net_name: str) -> int:
    """Legacy API: Perform GPIO input operation."""
    return gpi(net_name)


def _do_gpo(net_name: str, level: str) -> None:
    """Legacy API: Perform GPIO output operation."""
    gpo(net_name, level)


# --------- CLI entry point ---------

def _usage(exit_code: int = 2) -> None:
    """Print usage information and exit."""
    sys.stderr.write(
        "Usage:\n"
        "  gpi <NET>\n"
        "  gpo <NET> <LEVEL>\n"
        "  <BOX_ID> gpi <NET>\n"
        "  <BOX_ID> gpo <NET> <LEVEL>\n\n"
        "LEVEL options: 0/1, low/high, off/on\n"
    )
    sys.exit(exit_code)


def _parse_args(argv: list[str]) -> tuple[str, str, Optional[str]]:
    """
    Parse command line arguments.

    Returns:
        Tuple of (mode, net_name, level_or_none) where:
        - mode: "gpi" or "gpo"
        - net_name: the GPIO net name
        - level_or_none: required for "gpo", None for "gpi"
    """
    if not argv:
        _usage()

    # Check if first argument is a command or box ID
    if argv[0] in ("gpi", "gpo"):
        mode = argv[0]
        start_idx = 1
    else:
        # Assume first argument is box ID, second is command
        if len(argv) < 2 or argv[1] not in ("gpi", "gpo"):
            _usage()
        mode = argv[1]
        start_idx = 2
        _debug(f"Using box ID: {argv[0]}")

    # Extract net name
    if len(argv) <= start_idx:
        _usage()
    net_name = argv[start_idx]

    # Extract level for gpo commands
    level = None
    if mode == "gpo":
        if len(argv) <= start_idx + 1:
            _usage()
        level = argv[start_idx + 1]

    return mode, net_name, level


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
        mode, net_name, level = _parse_args(args)
        _debug(f"Parsed: mode={mode}, net={net_name}, level={level}")

        if mode == "gpi":
            value = gpi(net_name)
            sys.stdout.write(f"{value}\n")
            sys.stdout.flush()
        else:  # mode == "gpo"
            gpo(net_name, level or "")
            # No output on successful gpo operation

        return 0

    except Exception as e:
        sys.stderr.write(f"gpio dispatcher error: {e}\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
