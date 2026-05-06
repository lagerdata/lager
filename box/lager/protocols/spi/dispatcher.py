# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
SPI dispatcher - loads net configs and creates SPI driver instances.

Uses shared helpers from lager.dispatchers.helpers for common patterns.
"""
from __future__ import annotations

import json
import sys
import threading
from typing import Any, Dict, List, Optional

from lager.dispatchers import helpers
from lager.exceptions import SPIBackendError

# Re-export for backward compatibility with modules that import from here
__all__ = [
    'SPIBackendError',
    'config',
    'read',
    'read_write',
    'transfer',
    '_resolve_net_and_driver',
]

# Role constant for SPI nets
ROLE = "spi"

# Driver cache to avoid recreating drivers for each call
_driver_cache: Dict[str, 'SPIBase'] = {}
_driver_cache_lock = threading.Lock()


def _persist_params(netname: str, **kwargs) -> None:
    """
    Persist SPI configuration params into saved_nets.json.

    Only updates the keys passed as keyword arguments, leaving other
    params untouched.
    """
    from lager.nets.net import Net

    all_nets = Net.get_local_nets()
    for rec in all_nets:
        if rec.get("name") == netname:
            params = rec.setdefault("params", {})
            params.update(kwargs)
            break
    else:
        raise SPIBackendError(f"Net '{netname}' not found in saved nets")

    Net.save_local_nets(all_nets)


def _get_pin_config(rec: Dict[str, Any]) -> Dict[str, int]:
    """
    Extract SPI pin configuration from net record.

    Supports two formats:
    1. Standard pin field: "pin": "FIO0-FIO3" (uses default mapping)
    2. Custom params: "params": {"cs_pin": 0, "clk_pin": 1, "mosi_pin": 2, "miso_pin": 3}
    """
    # Aardvark and FT232H have fixed hardware pins - no pin config needed
    instrument = rec.get("instrument", "").lower()
    if instrument in ("aardvark_spi", "aardvark", "totalphase_aardvark"):
        return {}
    if instrument in ("ft232h", "ftdi_ft232h", "ft232h_spi"):
        return {}

    params = rec.get("params", {})
    pin_field = rec.get("pin", "")
    netname = rec.get("name", "<unknown>")

    # Check for standard FIO0-FIO3 pin configuration (4-pin SPI with CS)
    if pin_field == "FIO0-FIO3":
        return {
            "cs_pin": 0,    # FIO0 for chip select
            "clk_pin": 1,   # FIO1 for clock
            "mosi_pin": 2,  # FIO2 for MOSI
            "miso_pin": 3,  # FIO3 for MISO
        }

    # Check for FIO1-FIO3 pin configuration (3-pin SPI, no CS - manual mode)
    if pin_field == "FIO1-FIO3":
        return {
            "clk_pin": 1,   # FIO1 for clock
            "mosi_pin": 2,  # FIO2 for MOSI
            "miso_pin": 3,  # FIO3 for MISO
            # cs_pin intentionally omitted
        }

    # Fall back to params-based configuration
    # cs_pin is optional (absent when cs_mode is manual)
    required_pins = ["clk_pin", "mosi_pin", "miso_pin"]
    pin_config = {}

    for pin_name in required_pins:
        value = params.get(pin_name)
        if value is None:
            raise SPIBackendError(
                f"Net '{netname}' missing required SPI pin configuration: {pin_name}. "
                f"Required params: {required_pins}"
            )
        try:
            pin_config[pin_name] = int(value)
        except (ValueError, TypeError):
            raise SPIBackendError(
                f"Invalid pin value for '{pin_name}': {value}. Must be an integer."
            )

    # cs_pin is optional
    cs_value = params.get("cs_pin")
    if cs_value is not None:
        try:
            pin_config["cs_pin"] = int(cs_value)
        except (ValueError, TypeError):
            raise SPIBackendError(
                f"Invalid pin value for 'cs_pin': {cs_value}. Must be an integer."
            )

    return pin_config


def _get_spi_params(rec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract SPI configuration parameters from net record.

    These are optional parameters with sensible defaults.
    Auto-detects cs_mode default: "manual" for 3-pin nets (FIO1-FIO3 or
    no cs_pin in params), "auto" otherwise.
    """
    params = rec.get("params", {})
    pin_field = rec.get("pin", "")

    # Auto-detect default cs_mode based on pin configuration
    if "cs_mode" in params:
        default_cs_mode = params["cs_mode"]
    elif pin_field == "FIO1-FIO3" or (pin_field not in ("FIO0-FIO3", "") and "cs_pin" not in params):
        default_cs_mode = "manual"
    else:
        default_cs_mode = "auto"

    return {
        "mode": params.get("mode", 0),
        "bit_order": params.get("bit_order", "msb"),
        "frequency_hz": params.get("frequency_hz", 1_000_000),
        "word_size": params.get("word_size", 8),
        "cs_active": params.get("cs_active", "low"),
        "cs_mode": default_cs_mode,
    }


def _make_driver(rec: Dict[str, Any], overrides: Dict[str, Any] = None):
    """
    Construct an SPI driver with the net configuration and overrides.

    Currently only supports LabJack T7. Future versions will support
    Aardvark and FTDI based on the 'instrument' field.
    """
    netname = rec.get("name", "<unknown>")
    instrument = rec.get("instrument", "labjack_t7").lower()

    # Get SPI parameters with defaults
    spi_params = _get_spi_params(rec)

    # Apply overrides
    if overrides:
        spi_params.update(overrides)

    # Select driver based on instrument type
    if instrument in ("labjack_t7", "labjack", "t7"):
        from .labjack_spi import LabJackSPI

        pin_config = _get_pin_config(rec)
        try:
            return LabJackSPI(
                cs_pin=pin_config.get("cs_pin"),
                clk_pin=pin_config["clk_pin"],
                mosi_pin=pin_config["mosi_pin"],
                miso_pin=pin_config["miso_pin"],
                **spi_params,
            )
        except Exception as exc:
            raise SPIBackendError(f"Failed to create SPI driver: {exc}") from exc
    elif instrument in ("aardvark_spi", "aardvark", "totalphase_aardvark"):
        from .aardvark_spi import AardvarkSPI

        # Extract port/serial from net record
        port = rec.get("params", {}).get("port", 0)
        target_power = rec.get("params", {}).get("target_power", False)
        serial = None
        address = rec.get("address", "")
        parts = address.split("::")
        if len(parts) > 3 and parts[3]:
            serial = parts[3]
        try:
            return AardvarkSPI(port=port, serial=serial, target_power=target_power, **spi_params)
        except Exception as exc:
            raise SPIBackendError(f"Failed to create Aardvark SPI driver: {exc}") from exc
    elif instrument in ("ft232h", "ftdi_ft232h", "ft232h_spi"):
        from .ft232h_spi import FT232HSPI

        # cs_mode is not supported by FT232H (uses hardware CS); remove before passing
        spi_params.pop("cs_mode", None)

        serial = None
        address = rec.get("address", "")
        parts = address.split("::")
        if len(parts) > 3 and parts[3]:
            serial = parts[3]
        cs_pin = rec.get("params", {}).get("cs_pin", 3)
        try:
            return FT232HSPI(
                serial=serial,
                cs_pin=cs_pin,
                **spi_params,
            )
        except Exception as exc:
            raise SPIBackendError(f"Failed to create FT232H SPI driver: {exc}") from exc
    else:
        raise SPIBackendError(
            f"Unsupported SPI instrument '{instrument}' for net '{netname}'. "
            f"Supported: labjack_t7, aardvark_spi, ft232h"
        )


def _resolve_net_and_driver(netname: str, overrides: Dict[str, Any] = None):
    """
    Load net config and create/retrieve driver instance.

    Uses shared helpers for net lookup and role validation.
    Caches drivers for efficiency. Thread-safe via _driver_cache_lock.
    """
    if overrides is None:
        overrides = {}

    with _driver_cache_lock:
        # Check cache first
        cache_key = netname
        if cache_key in _driver_cache:
            drv = _driver_cache[cache_key]
            # Apply any configuration overrides
            if overrides:
                drv.config(**overrides)
            return drv

        # Use shared helpers for common patterns
        rec = helpers.find_saved_net(netname, SPIBackendError)
        helpers.ensure_role(rec, ROLE, SPIBackendError)

        drv = _make_driver(rec, overrides)

        # Cache the driver
        _driver_cache[cache_key] = drv

        return drv


def _format_output(data: List[int], fmt: str = "hex", word_size: int = 8) -> str:
    """
    Format output data according to the requested format.

    Args:
        data: List of words to format
        fmt: Output format - "hex", "bytes", or "json"
        word_size: Word size for formatting (8, 16, or 32)

    Returns:
        Formatted string
    """
    if fmt == "json":
        return json.dumps({"data": data})
    elif fmt == "bytes":
        # Format as space-separated decimal values
        return " ".join(str(w) for w in data)
    else:
        # Default to hex format
        if word_size == 8:
            return " ".join(f"{w:02x}" for w in data)
        elif word_size == 16:
            return " ".join(f"{w:04x}" for w in data)
        else:
            return " ".join(f"{w:08x}" for w in data)


# --------- actions (called from spi.py implementation script) ---------

def config(
    netname: str,
    mode: int = None,
    bit_order: str = None,
    frequency_hz: int = None,
    word_size: int = None,
    cs_active: str = None,
    cs_mode: str = None,
    **_,
) -> None:
    """
    Configure SPI parameters for a net.

    Only explicitly-provided parameters are persisted; omitted parameters
    retain their previously-stored values.

    Args:
        netname: Name of the SPI net
        mode: SPI mode (0-3)
        bit_order: "msb" or "lsb"
        frequency_hz: Clock frequency in Hz
        word_size: Bits per word (8, 16, or 32)
        cs_active: "low" or "high"
        cs_mode: "auto" (hardware SS) or "manual" (user-managed GPIO)
    """
    if frequency_hz is not None and frequency_hz <= 0:
        raise SPIBackendError(
            f"Invalid SPI frequency: {frequency_hz}Hz. "
            f"Must be a positive value (e.g., 1000000 for 1MHz)."
        )

    # Build overrides with only explicitly-provided values
    overrides = {}
    if mode is not None:
        overrides["mode"] = mode
    if bit_order is not None:
        overrides["bit_order"] = bit_order
    if frequency_hz is not None:
        overrides["frequency_hz"] = frequency_hz
    if word_size is not None:
        overrides["word_size"] = word_size
    if cs_active is not None:
        overrides["cs_active"] = cs_active
    if cs_mode is not None:
        overrides["cs_mode"] = cs_mode

    drv = _resolve_net_and_driver(netname, overrides if overrides else None)

    # Persist only explicitly-provided values
    if overrides:
        _persist_params(netname, **overrides)

    # Resolve effective values using the same auto-detection logic as the driver.
    # _get_spi_params() correctly auto-detects cs_mode for 3-pin nets, etc.
    rec = helpers.find_saved_net(netname, SPIBackendError)
    effective = _get_spi_params(rec)

    print(f"SPI configured: mode={effective['mode']}, freq={effective['frequency_hz']}Hz, "
          f"word_size={effective['word_size']}, bit_order={effective['bit_order']}, "
          f"cs_active={effective['cs_active']}, cs_mode={effective['cs_mode']}")


def read(
    netname: str,
    n_words: int,
    fill: int = 0xFF,
    keep_cs: bool = False,
    output_format: str = "hex",
    overrides: Dict[str, Any] = None,
    **_,
) -> None:
    """
    Read data from SPI slave.

    Args:
        netname: Name of the SPI net
        n_words: Number of words to read
        fill: Fill byte/word to send while reading
        keep_cs: If True, keep CS asserted after transfer
        output_format: Output format - "hex", "bytes", or "json"
        overrides: Optional configuration overrides
    """
    drv = _resolve_net_and_driver(netname, overrides)
    result = drv.read(n_words, fill=fill, keep_cs=keep_cs)

    # Get word size from driver (reflects stored config + any overrides)
    effective_ws = getattr(drv, '_word_size', 8)

    output = _format_output(result, output_format, effective_ws)
    print(output)


def read_write(
    netname: str,
    data: List[int],
    keep_cs: bool = False,
    output_format: str = "hex",
    overrides: Dict[str, Any] = None,
    **_,
) -> None:
    """
    Perform simultaneous read/write SPI transfer.

    Args:
        netname: Name of the SPI net
        data: List of words to transmit
        keep_cs: If True, keep CS asserted after transfer
        output_format: Output format - "hex", "bytes", or "json"
        overrides: Optional configuration overrides
    """
    drv = _resolve_net_and_driver(netname, overrides)
    result = drv.read_write(data, keep_cs=keep_cs)

    # Get word size from driver (reflects stored config + any overrides)
    effective_ws = getattr(drv, '_word_size', 8)

    output = _format_output(result, output_format, effective_ws)
    print(output)


def transfer(
    netname: str,
    n_words: int,
    data: Optional[List[int]] = None,
    fill: int = 0xFF,
    keep_cs: bool = False,
    output_format: str = "hex",
    overrides: Dict[str, Any] = None,
    **_,
) -> None:
    """
    Perform SPI transfer (combined read/write with padding/truncation).

    This is the main transfer function that handles the CLI interface:
    - If data is shorter than n_words, pad with fill value
    - If data is longer than n_words, truncate

    Args:
        netname: Name of the SPI net
        n_words: Number of words to transfer
        data: Optional list of words to transmit
        fill: Fill byte/word for padding
        keep_cs: If True, keep CS asserted after transfer
        output_format: Output format - "hex", "bytes", or "json"
        overrides: Optional configuration overrides
    """
    # Prepare data with padding/truncation
    if data is None:
        data = []

    # Pad or truncate to match n_words
    if len(data) < n_words:
        data = data + [fill] * (n_words - len(data))
    elif len(data) > n_words:
        data = data[:n_words]

    drv = _resolve_net_and_driver(netname, overrides)
    result = drv.read_write(data, keep_cs=keep_cs)

    # Get word size from driver (reflects stored config + any overrides)
    effective_ws = getattr(drv, '_word_size', 8)

    output = _format_output(result, output_format, effective_ws)
    print(output)
