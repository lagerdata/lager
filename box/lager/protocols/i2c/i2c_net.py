# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
I2C Net wrapper class for the lager Python API.

Provides clean object-based access to I2C nets from Python scripts running on the box.

Example usage:
    from lager import Net, NetType

    # Get the I2C net
    my_i2c = Net.get('MY_I2C_NET', NetType.I2C)

    # Configure I2C parameters
    my_i2c.config(frequency_hz=400_000, pull_ups=True)

    # Scan for devices
    devices = my_i2c.scan()

    # Read from a device
    data = my_i2c.read(address=0x48, num_bytes=2)

    # Write to a device
    my_i2c.write(address=0x48, data=[0x0A, 0x03])

    # Write then read (register read pattern)
    reg_value = my_i2c.write_read(address=0x48, data=[0x0A], num_bytes=2)
"""
from __future__ import annotations

from typing import List, Dict, Any, Optional

from lager.exceptions import I2CBackendError


class I2CNet:
    """
    Represents an I2C net configuration.

    Provides clean object-oriented API for I2C communication from Python scripts.
    Wraps the dispatcher functions to provide instance methods that don't require
    passing the netname on every call.

    Attributes:
        name: Net name from saved_nets.json
        params: I2C configuration parameters from net config
    """

    def __init__(self, name: str, net_config: dict):
        self.name = name
        self._config = net_config
        self.params = net_config.get("params", {})
        self._driver = None

    def _get_driver(self, overrides: Optional[Dict[str, Any]] = None):
        """Get or create the I2C driver instance."""
        from .dispatcher import _resolve_net_and_driver
        return _resolve_net_and_driver(self.name, overrides)

    def config(
        self,
        frequency_hz: Optional[int] = None,
        pull_ups: Optional[bool] = None,
    ) -> None:
        """
        Configure I2C bus parameters.

        Only explicitly-provided params are changed. Omitted params
        retain their stored values from saved_nets.json.

        Args:
            frequency_hz: Clock frequency in Hz (e.g., 100_000, 400_000).
                          None keeps the stored value.
            pull_ups: Enable/disable internal pull-ups (Aardvark only).
                      None keeps the stored value.

        Example:
            i2c.config(frequency_hz=400_000)
            i2c.config(frequency_hz=100_000, pull_ups=True)
        """
        from .dispatcher import config as dispatcher_config
        dispatcher_config(self.name, frequency_hz=frequency_hz, pull_ups=pull_ups)

    def scan(
        self,
        start_addr: int = 0x08,
        end_addr: int = 0x77,
    ) -> List[int]:
        """
        Scan I2C bus for connected devices.

        Args:
            start_addr: First 7-bit address to probe (default 0x08)
            end_addr: Last 7-bit address to probe (default 0x77)

        Returns:
            List of 7-bit addresses that responded with ACK

        Example:
            devices = i2c.scan()
            print(f"Found devices at: {[hex(a) for a in devices]}")
        """
        drv = self._get_driver()
        return drv.scan(start_addr=start_addr, end_addr=end_addr)

    def read(
        self,
        address: int,
        num_bytes: int,
        output_format: str = "list",
        overrides: Optional[Dict[str, Any]] = None,
    ) -> List[int]:
        """
        Read bytes from an I2C device.

        Args:
            address: 7-bit I2C device address (0x00-0x7F)
            num_bytes: Number of bytes to read
            output_format: Output format - "list" (default), "hex", "bytes", or "json"
            overrides: Optional dict of per-call overrides (e.g. {"frequency_hz": 400000})

        Returns:
            List of received bytes as integers

        Example:
            data = i2c.read(address=0x48, num_bytes=2)
            temp = (data[0] << 8) | data[1]
        """
        drv = self._get_driver(overrides)
        result = drv.read(address, num_bytes)

        if output_format == "list":
            return result
        return self._format_output(result, output_format)

    def write(
        self,
        address: int,
        data: List[int],
        overrides: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Write bytes to an I2C device.

        Args:
            address: 7-bit I2C device address (0x00-0x7F)
            data: List of bytes to write
            overrides: Optional dict of per-call overrides (e.g. {"frequency_hz": 400000})

        Example:
            # Write register address and value
            i2c.write(address=0x48, data=[0x0A, 0x03])
        """
        drv = self._get_driver(overrides)
        drv.write(address, data)

    def write_read(
        self,
        address: int,
        data: List[int],
        num_bytes: int,
        output_format: str = "list",
        overrides: Optional[Dict[str, Any]] = None,
    ) -> List[int]:
        """
        Write then read in a single I2C transaction (repeated start).

        Common pattern: write register address, read register value.

        Args:
            address: 7-bit I2C device address (0x00-0x7F)
            data: List of bytes to write before reading
            num_bytes: Number of bytes to read after writing
            output_format: Output format - "list" (default), "hex", "bytes", or "json"
            overrides: Optional dict of per-call overrides (e.g. {"frequency_hz": 400000})

        Returns:
            List of received bytes as integers

        Example:
            # Read 2-byte temperature register at 0x00
            temp_bytes = i2c.write_read(address=0x48, data=[0x00], num_bytes=2)
            temperature = (temp_bytes[0] << 8) | temp_bytes[1]
        """
        drv = self._get_driver(overrides)
        result = drv.write_read(address, data, num_bytes)

        if output_format == "list":
            return result
        return self._format_output(result, output_format)

    def _format_output(self, data: List[int], fmt: str) -> Any:
        """Format output data according to the requested format."""
        import json as json_mod

        if fmt == "json":
            return {"data": data}
        elif fmt == "bytes":
            return " ".join(str(b) for b in data)
        else:  # hex
            return " ".join(f"{b:02x}" for b in data)

    def get_config(self) -> dict:
        """Get the raw net configuration dict."""
        return self._config.copy()

    def __str__(self):
        return f'<I2CNet name="{self.name}">'

    def __repr__(self):
        return str(self)
