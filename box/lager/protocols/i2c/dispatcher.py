# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
I2C dispatcher - loads net configs and creates I2C driver instances.

Uses shared helpers from lager.dispatchers.helpers for common patterns.
"""
from __future__ import annotations

import json
import sys
import threading
from typing import Any, Dict, List, Optional

from lager.dispatchers import helpers
from lager.exceptions import I2CBackendError

__all__ = [
    'I2CBackendError',
    'config',
    'scan',
    'read',
    'write',
    'transfer',
    '_resolve_net_and_driver',
]

# Role constant for I2C nets
ROLE = "i2c"

# Driver cache to avoid recreating drivers for each call
_driver_cache: Dict[str, 'I2CBase'] = {}
_driver_cache_lock = threading.Lock()


def _get_pin_config(rec: Dict[str, Any]) -> Dict[str, int]:
    """
    Extract I2C pin configuration from net record.

    Supports two formats:
    1. Standard pin field: "pin": "FIO4-FIO5" (uses default mapping)
    2. Custom params: "params": {"sda_pin": 4, "scl_pin": 5}
    """
    # Aardvark and FT232H have fixed hardware pins - no pin config needed
    instrument = rec.get("instrument", "").lower()
    if instrument in ("aardvark_i2c", "aardvark", "totalphase_aardvark"):
        return {}
    if instrument in ("ft232h", "ftdi_ft232h", "ft232h_i2c"):
        return {}

    params = rec.get("params", {})
    pin_field = rec.get("pin", "")
    netname = rec.get("name", "<unknown>")

    # Check for standard FIO pin configuration like "FIO4-FIO5"
    if pin_field.startswith("FIO") and "-" in pin_field:
        parts = pin_field.split("-")
        if len(parts) == 2:
            try:
                sda_pin = int(parts[0].replace("FIO", ""))
                scl_pin = int(parts[1].replace("FIO", ""))
                return {"sda_pin": sda_pin, "scl_pin": scl_pin}
            except ValueError:
                pass

    # Fall back to params-based configuration
    required_pins = ["sda_pin", "scl_pin"]
    pin_config = {}

    for pin_name in required_pins:
        value = params.get(pin_name)
        if value is None:
            raise I2CBackendError(
                f"Net '{netname}' missing required I2C pin configuration: "
                f"{pin_name}. Required params: {required_pins}"
            )
        try:
            pin_config[pin_name] = int(value)
        except (ValueError, TypeError):
            raise I2CBackendError(
                f"Invalid pin value for '{pin_name}': {value}. "
                f"Must be an integer."
            )

    return pin_config


def _get_i2c_params(rec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract I2C configuration parameters from net record.
    """
    params = rec.get("params", {})

    return {
        "frequency_hz": params.get("frequency_hz", 100_000),
    }


def _persist_params(netname: str, **kwargs) -> None:
    """
    Persist I2C configuration params into saved_nets.json.

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
        raise I2CBackendError(f"Net '{netname}' not found in saved nets")

    Net.save_local_nets(all_nets)


def _make_driver(rec: Dict[str, Any], overrides: Dict[str, Any] = None):
    """
    Construct an I2C driver with the net configuration and overrides.
    """
    netname = rec.get("name", "<unknown>")
    instrument = rec.get("instrument", "labjack_t7").lower()

    # Get I2C parameters with defaults
    i2c_params = _get_i2c_params(rec)

    # Apply overrides
    if overrides:
        i2c_params.update(overrides)

    # Select driver based on instrument type
    if instrument in ("labjack_t7", "labjack", "t7"):
        from .labjack_i2c import LabJackI2C

        pin_config = _get_pin_config(rec)
        try:
            return LabJackI2C(
                sda_pin=pin_config["sda_pin"],
                scl_pin=pin_config["scl_pin"],
                frequency_hz=i2c_params.get("frequency_hz", 100_000),
            )
        except Exception as exc:
            raise I2CBackendError(
                f"Failed to create I2C driver: {exc}"
            ) from exc
    elif instrument in ("aardvark_i2c", "aardvark", "totalphase_aardvark"):
        from .aardvark_i2c import AardvarkI2C

        port = rec.get("params", {}).get("port", 0)
        pull_ups = rec.get("params", {}).get("pull_ups", False)
        target_power = rec.get("params", {}).get("target_power", False)
        serial = None
        address = rec.get("address", "")
        parts = address.split("::")
        if len(parts) > 3 and parts[3]:
            serial = parts[3]
        try:
            return AardvarkI2C(
                port=port,
                serial=serial,
                frequency_hz=i2c_params.get("frequency_hz", 100_000),
                pull_ups=pull_ups,
                target_power=target_power,
            )
        except Exception as exc:
            raise I2CBackendError(
                f"Failed to create Aardvark I2C driver: {exc}"
            ) from exc
    elif instrument in ("ft232h", "ftdi_ft232h", "ft232h_i2c"):
        from .ft232h_i2c import FT232HI2C

        serial = None
        address = rec.get("address", "")
        parts = address.split("::")
        if len(parts) > 3 and parts[3]:
            serial = parts[3]
        try:
            return FT232HI2C(
                serial=serial,
                frequency_hz=i2c_params.get("frequency_hz", 100_000),
            )
        except Exception as exc:
            raise I2CBackendError(
                f"Failed to create FT232H I2C driver: {exc}"
            ) from exc
    else:
        raise I2CBackendError(
            f"Unsupported I2C instrument '{instrument}' for net '{netname}'. "
            f"Supported: labjack_t7, aardvark_i2c, ft232h"
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
                config_params = {}
                if "frequency_hz" in overrides:
                    config_params["frequency_hz"] = overrides["frequency_hz"]
                if "pull_ups" in overrides:
                    config_params["pull_ups"] = overrides["pull_ups"]
                if config_params:
                    drv.config(**config_params)
            return drv

        # Use shared helpers for common patterns
        rec = helpers.find_saved_net(netname, I2CBackendError)
        helpers.ensure_role(rec, ROLE, I2CBackendError)

        drv = _make_driver(rec, overrides)

        # Cache the driver
        _driver_cache[cache_key] = drv

        return drv


def _format_output(data: List[int], fmt: str = "hex") -> str:
    """
    Format output data according to the requested format.

    Args:
        data: List of bytes to format
        fmt: Output format - "hex", "bytes", or "json"

    Returns:
        Formatted string
    """
    if fmt == "json":
        return json.dumps({"data": data})
    elif fmt == "bytes":
        return " ".join(str(b) for b in data)
    else:
        return " ".join(f"{b:02x}" for b in data)


def _format_scan_output(found_addrs: List[int],
                         start_addr: int = 0x08,
                         end_addr: int = 0x77) -> str:
    """
    Format scan results as an i2cdetect-style grid.

    Output:
         0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
    00:          -- -- -- -- -- -- -- -- -- -- -- -- -- --
    10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    20: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    30: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    40: -- -- -- -- -- -- -- -- 48 -- -- -- -- -- -- --
    50: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    60: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    70: -- -- -- -- -- -- -- --
    """
    found_set = set(found_addrs)
    lines = []

    # Header
    lines.append("     " + "  ".join(f"{i:x}" for i in range(16)))

    for row in range(0, 0x80, 16):
        cols = []
        for col in range(16):
            addr = row + col
            if addr < start_addr or addr > end_addr:
                cols.append("  ")
            elif addr in found_set:
                cols.append(f"{addr:02x}")
            else:
                cols.append("--")
        line = f"{row:02x}: " + " ".join(cols)
        lines.append(line)

    return "\n".join(lines)


# --------- actions (called from i2c.py implementation script) ---------

def config(
    netname: str,
    frequency_hz: Optional[int] = None,
    pull_ups: Optional[bool] = None,
    **_,
) -> None:
    """
    Configure I2C parameters for a net.

    Only explicitly-provided params are persisted to saved_nets.json.
    Omitted params retain their stored values.
    """
    if frequency_hz is not None and frequency_hz <= 0:
        raise I2CBackendError(
            f"Invalid I2C frequency: {frequency_hz}Hz. "
            f"Must be a positive value (e.g., 100000 for 100kHz)."
        )

    # Read stored params to determine effective values
    rec = helpers.find_saved_net(netname, I2CBackendError)
    stored_params = rec.get("params", {})
    stored_freq = stored_params.get("frequency_hz", 100_000)
    stored_pull_ups = stored_params.get("pull_ups", False)

    effective_freq = frequency_hz if frequency_hz is not None else stored_freq
    effective_pull_ups = pull_ups if pull_ups is not None else stored_pull_ups

    # Apply to driver
    overrides = {
        "frequency_hz": effective_freq,
        "pull_ups": effective_pull_ups,
    }
    drv = _resolve_net_and_driver(netname, overrides)
    drv.config(frequency_hz=effective_freq, pull_ups=effective_pull_ups)

    # Persist only the explicitly-set params
    persist_kwargs = {}
    if frequency_hz is not None:
        persist_kwargs["frequency_hz"] = frequency_hz
    if pull_ups is not None:
        persist_kwargs["pull_ups"] = pull_ups
    if persist_kwargs:
        _persist_params(netname, **persist_kwargs)

    print(f"I2C configured: freq={effective_freq}Hz, "
          f"pull_ups={'on' if effective_pull_ups else 'off'}")


def scan(
    netname: str,
    start_addr: int = 0x08,
    end_addr: int = 0x77,
    overrides: Dict[str, Any] = None,
    **_,
) -> None:
    """
    Scan I2C bus for connected devices.
    """
    drv = _resolve_net_and_driver(netname, overrides)
    found = drv.scan(start_addr=start_addr, end_addr=end_addr)

    # Print i2cdetect-style grid
    print(_format_scan_output(found, start_addr, end_addr))

    # Print summary
    if found:
        addr_list = ", ".join(f"0x{a:02x}" for a in found)
        print(f"\nFound {len(found)} device(s): {addr_list}")
    else:
        print(f"\nNo devices found in range "
              f"0x{start_addr:02x}-0x{end_addr:02x}")


def read(
    netname: str,
    address: int,
    num_bytes: int,
    output_format: str = "hex",
    overrides: Dict[str, Any] = None,
    **_,
) -> None:
    """
    Read bytes from an I2C device.
    """
    drv = _resolve_net_and_driver(netname, overrides)
    result = drv.read(address, num_bytes)

    output = _format_output(result, output_format)
    print(output)


def write(
    netname: str,
    address: int,
    data: List[int],
    output_format: str = "hex",
    overrides: Dict[str, Any] = None,
    **_,
) -> None:
    """
    Write bytes to an I2C device.
    """
    drv = _resolve_net_and_driver(netname, overrides)
    drv.write(address, data)

    print(f"Wrote {len(data)} byte(s) to 0x{address:02x}")


def transfer(
    netname: str,
    address: int,
    num_bytes: int,
    data: Optional[List[int]] = None,
    output_format: str = "hex",
    overrides: Dict[str, Any] = None,
    **_,
) -> None:
    """
    Write then read in a single I2C transaction (repeated start).
    """
    if data is None:
        data = []

    drv = _resolve_net_and_driver(netname, overrides)
    result = drv.write_read(address, data, num_bytes)

    output = _format_output(result, output_format)
    print(output)
