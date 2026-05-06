# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
UART module for Lager box.
Provides drivers for UART bridge devices.

This module is part of the protocols package and handles serial
communication with devices via UART bridges.
"""
from .dispatcher import monitor, monitor_interactive, _resolve_net_and_driver
from .uart_bridge import UARTBridge
from .uart_net import UARTNet
from lager.exceptions import UARTBackendError

__all__ = [
    'monitor',
    'monitor_interactive',
    'UARTBridge',
    'UARTNet',
    'UARTBackendError',
    '_resolve_net_and_driver',
]
