# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
HTTP handlers package for the Lager Box HTTP+WebSocket Server.

This package contains modular handlers for different hardware subsystems:
- uart: UART communication
- supply: Power supply monitoring and control (future)
- battery: Battery simulator monitoring and control (future)
"""
from .uart import (
    active_uart_sessions,
    active_uart_sessions_lock,
    register_uart_routes,
    register_uart_socketio,
    cleanup_uart_sessions,
)

__all__ = [
    'active_uart_sessions',
    'active_uart_sessions_lock',
    'register_uart_routes',
    'register_uart_socketio',
    'cleanup_uart_sessions',
]
