# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
UART Net class for the lager Python API.

Provides clean access to UART nets from Python scripts running on the box.
"""
from __future__ import annotations

from typing import Optional
import serial


class UARTNet:
    """
    Represents a UART net configuration.

    Provides clean API for accessing UART serial ports from Python scripts.

    Example:
        from lager import Net, NetType

        # Get the UART net
        uart_net = Net.get('TARGET_SERIAL', NetType.UART)

        # Get the device path
        device_path = uart_net.get_path()  # Returns "/dev/ttyUSB0"

        # Use with pyserial
        import serial
        ser = serial.Serial(device_path, baudrate=115200)

        # Or use the convenience method
        ser = uart_net.connect(baudrate=115200)
    """

    def __init__(self, name: str, net_config: dict):
        """
        Initialize UART net.

        Args:
            name: Net name
            net_config: Net configuration dict from saved_nets.json
        """
        self.name = name
        self._config = net_config

        # Extract configuration
        self.usb_serial = net_config.get('pin', '')
        self.channel = net_config.get('channel', '0')
        self.params = net_config.get('params', {})

        # Cache the device path (lazy loaded)
        self._device_path: Optional[str] = None

    def get_path(self, force: bool = False) -> str:
        """
        Get the device path for this UART net.

        Args:
            force: Re-resolve the serial->tty mapping even if a path was cached.
                Use after a replug where the CP210x renumbered so a Python-API
                caller doesn't keep handing back a stale ``/dev/ttyUSB*`` node.

        Returns:
            Device path like "/dev/ttyUSB0"

        Raises:
            FileNotFoundError: If the UART device is not connected
        """
        if self._device_path is None or force:
            # Use the dispatcher to resolve the device path
            from .uart_bridge import UARTBridge

            # Create a temporary bridge instance just to get the device path
            # The bridge __init__ resolves the USB serial to a /dev path
            bridge = UARTBridge(
                bridge_serial=self.usb_serial,
                port=self.channel,
                device_path=self._config.get('device_path'),
                baudrate=self.params.get('baudrate', 115200)
            )
            self._device_path = bridge.device_path

        return self._device_path

    def connect(self, **overrides) -> serial.Serial:
        """
        Connect to the UART serial port with pyserial.

        Args:
            **overrides: Parameter overrides (baudrate, bytesize, parity, stopbits, etc.)

        Returns:
            Connected pyserial Serial object

        Example:
            ser = uart_net.connect(baudrate=115200, timeout=1.0)
            data = ser.read(100)
            ser.write(b"hello\\n")
        """
        from .uart_bridge import UARTBridge

        # Merge params with overrides
        final_params = {**self.params, **overrides}

        # Create bridge driver
        bridge = UARTBridge(
            bridge_serial=self.usb_serial,
            port=self.channel,
            device_path=self._config.get('device_path'),
            **final_params
        )

        # Connect and return the serial connection
        bridge._connect()
        return bridge.serial_conn

    def get_baudrate(self) -> int:
        """Get the configured baudrate for this net."""
        return self.params.get('baudrate', 115200)

    def get_config(self) -> dict:
        """Get the raw net configuration dict."""
        return self._config.copy()

    def __str__(self):
        return f'<UARTNet name="{self.name}" path="{self._device_path or "not connected"}">'

    def __repr__(self):
        return str(self)
