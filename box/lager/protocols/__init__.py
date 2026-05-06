# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Communication protocols for Lager box.

This package provides unified access to all communication-related hardware control:
- uart: Serial communication via UART bridges
- ble: Bluetooth Low Energy client for BLE devices
- wifi: WiFi management and router control
- spi: SPI (Serial Peripheral Interface) communication

Submodule imports:
    from lager.protocols import uart, ble, wifi, spi
    from lager.protocols.uart import UARTBridge, monitor
    from lager.protocols.ble import Client, Central
    from lager.protocols.wifi import Wifi, scan_wifi
    from lager.protocols.spi import SPINet, config, read, read_write

Note: Legacy imports (lager.uart, lager.wifi) have been removed.
    Use the canonical paths above instead.  lager.ble is available as a
    top-level shim (see box/lager/ble.py).
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import TYPE_CHECKING

# Lazy module loading to avoid circular imports
_uart: ModuleType | None = None
_ble: ModuleType | None = None
_wifi: ModuleType | None = None
_spi: ModuleType | None = None


def _load_uart() -> ModuleType:
    """Lazily load the UART module."""
    global _uart  # pylint: disable=global-statement
    if _uart is None:
        _uart = importlib.import_module("lager.protocols.uart")
    return _uart


def _load_ble() -> ModuleType:
    """Lazily load the BLE module."""
    global _ble  # pylint: disable=global-statement
    if _ble is None:
        _ble = importlib.import_module("lager.protocols.ble")
    return _ble


def _load_wifi() -> ModuleType:
    """Lazily load the WiFi module."""
    global _wifi  # pylint: disable=global-statement
    if _wifi is None:
        _wifi = importlib.import_module("lager.protocols.wifi")
    return _wifi


def _load_spi() -> ModuleType:
    """Lazily load the SPI module."""
    global _spi  # pylint: disable=global-statement
    if _spi is None:
        _spi = importlib.import_module("lager.protocols.spi")
    return _spi


# Provide module-level access via __getattr__ for lazy loading
def __getattr__(name: str):
    """Lazy load submodules and classes on first access."""
    # Submodule access
    if name == "uart":
        return _load_uart()
    elif name == "ble":
        return _load_ble()
    elif name == "wifi":
        return _load_wifi()
    elif name == "spi":
        return _load_spi()
    # UART exports
    elif name == "UARTBridge":
        return _load_uart().UARTBridge
    elif name == "UARTNet":
        return _load_uart().UARTNet
    elif name == "monitor":
        return _load_uart().monitor
    elif name == "monitor_interactive":
        return _load_uart().monitor_interactive
    # BLE exports
    elif name == "Client":
        return _load_ble().Client
    elif name == "Central":
        return _load_ble().Central
    elif name == "noop_handler":
        return _load_ble().noop_handler
    elif name == "notify_handler":
        return _load_ble().notify_handler
    elif name == "waiter":
        return _load_ble().waiter
    # WiFi exports
    elif name == "Wifi":
        return _load_wifi().Wifi
    elif name == "toggle_internet_access":
        return _load_wifi().toggle_internet_access
    elif name == "set_internet_access":
        return _load_wifi().set_internet_access
    elif name == "connect_to_wifi":
        return _load_wifi().connect_to_wifi
    elif name == "scan_wifi":
        return _load_wifi().scan_wifi
    elif name == "get_wifi_status":
        return _load_wifi().get_wifi_status
    # SPI exports
    elif name == "SPINet":
        return _load_spi().SPINet
    elif name == "SPIBase":
        return _load_spi().SPIBase
    elif name == "LabJackSPI":
        return _load_spi().LabJackSPI
    elif name == "AardvarkSPI":
        return _load_spi().AardvarkSPI
    elif name == "SPIBackendError":
        return _load_spi().SPIBackendError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    """List available submodules and exports."""
    return [
        # Submodules
        "uart",
        "ble",
        "wifi",
        "spi",
        # UART exports
        "UARTBridge",
        "UARTNet",
        "monitor",
        "monitor_interactive",
        # BLE exports
        "Client",
        "Central",
        "noop_handler",
        "notify_handler",
        "waiter",
        # WiFi exports
        "Wifi",
        "toggle_internet_access",
        "set_internet_access",
        "connect_to_wifi",
        "scan_wifi",
        "get_wifi_status",
        # SPI exports
        "SPINet",
        "SPIBase",
        "LabJackSPI",
        "AardvarkSPI",
        "SPIBackendError",
    ]


# Type hints for IDE support
if TYPE_CHECKING:
    from lager.protocols.uart import UARTBridge, UARTNet, monitor, monitor_interactive
    from lager.protocols.ble import Client, Central, noop_handler, notify_handler, waiter
    from lager.protocols.wifi import (
        Wifi,
        toggle_internet_access,
        set_internet_access,
        connect_to_wifi,
        scan_wifi,
        get_wifi_status,
    )
    from lager.protocols.spi import SPINet, SPIBase, LabJackSPI, AardvarkSPI, SPIBackendError


__all__ = [
    # Submodules
    "uart",
    "ble",
    "wifi",
    "spi",
    # UART exports
    "UARTBridge",
    "UARTNet",
    "monitor",
    "monitor_interactive",
    # BLE exports
    "Client",
    "Central",
    "noop_handler",
    "notify_handler",
    "waiter",
    # WiFi exports
    "Wifi",
    "toggle_internet_access",
    "set_internet_access",
    "connect_to_wifi",
    "scan_wifi",
    "get_wifi_status",
    # SPI exports
    "SPINet",
    "SPIBase",
    "LabJackSPI",
    "AardvarkSPI",
    "SPIBackendError",
]
