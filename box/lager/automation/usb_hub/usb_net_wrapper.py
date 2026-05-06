# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
USB Net wrapper class for the lager Python API.

Provides clean access to USB hub nets (Acroname, YKUSH) from Python scripts.
"""
from __future__ import annotations

from typing import Optional

from .dispatcher import _controller_for


class USBNetWrapper:
    """
    Represents a USB net configuration (USB hub port control).

    Provides clean API for controlling USB hub ports from Python scripts.

    Example:
        from lager import Net, NetType

        # Get the USB net
        charge_net = Net.get('CHARGE', NetType.Usb)

        # Enable the port
        charge_net.enable()

        # Disable the port
        charge_net.disable()

        # Toggle the port
        charge_net.toggle()
    """

    def __init__(self, name: str, net_config: dict):
        """
        Initialize USB net.

        Args:
            name: Net name
            net_config: Net configuration dict from saved_nets.json
        """
        self.name = name
        self._config = net_config

        # Extract configuration
        port = net_config.get('pin') or net_config.get('channel')
        self.port = int(port) if port is not None else None
        self.instrument = net_config.get('instrument', '')
        self.address = net_config.get('address', '')

        # Get the controller for this USB hub type
        self._controller = _controller_for(net_config)

    def enable(self) -> None:
        """Enable (power on) this USB port."""
        if self.port is None:
            raise ValueError(f"USB net '{self.name}' has no port configured")
        self._controller.enable(self.name, self.port)

    def disable(self) -> None:
        """Disable (power off) this USB port."""
        if self.port is None:
            raise ValueError(f"USB net '{self.name}' has no port configured")
        self._controller.disable(self.name, self.port)

    def toggle(self) -> None:
        """Toggle the power state of this USB port."""
        if self.port is None:
            raise ValueError(f"USB net '{self.name}' has no port configured")
        self._controller.toggle(self.name, self.port)

    def get_config(self) -> dict:
        """Get the raw net configuration dict."""
        return self._config.copy()

    def __str__(self):
        return f'<USBNetWrapper name="{self.name}" port={self.port} instrument="{self.instrument}">'

    def __repr__(self):
        return str(self)
