# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Lager Debug Module

This module provides comprehensive debugging functionality for embedded devices,
focusing on JLinkGDBServer support for remote debugging.

Main Components:
    - gdbserver: JLinkGDBServer lifecycle management (start/stop/status)
    - api: High-level debug operations (connect, disconnect, reset, flash, erase)
    - jlink: J-Link commander interface
    - gdb: GDB integration and architecture detection
    - mappings: Device and interface mappings, status checking
    - service: HTTP service for debug operations (port 8765)

Quick Start:
    from lager.debug.gdbserver import start_jlink_gdbserver, stop_jlink_gdbserver
    from lager.debug import flash_device, reset_device

    # Start JLinkGDBServer
    result = start_jlink_gdbserver(device='R7FA0E107', speed='4000')

    # Flash firmware
    files = (['firmware.hex'], [], [])
    output = flash_device(files, verify=True)

    # Reset device
    reset_device(halt=False)

    # Stop JLinkGDBServer
    stop_jlink_gdbserver()
"""

# Core API - J-Link only
from .api import (
    connect,
    connect_jlink,
    disconnect,
    reset_device,
    erase_flash,
    chip_erase,
    flash_device,
    read_memory,
    RTT,
    # Exceptions
    DebugError,
    JLinkStartError,
    JLinkAlreadyRunningError,
    JLinkNotRunning,
)

# Status and mappings
from .mappings import (
    get_jlink_status,
)

# GDB utilities
from .gdb import (
    get_arch,
    get_controller,
    reset as gdb_reset,
    DebuggerNotConnectedError,
)

# Low-level interfaces (for advanced usage)
from .jlink import JLink

# GDBServer management (for JLinkGDBServer lifecycle)
from .gdbserver import (
    start_jlink_gdbserver,
    stop_jlink_gdbserver,
    get_jlink_gdbserver_status,
)

__all__ = [
    # Core API
    'connect',
    'connect_jlink',
    'disconnect',
    'reset_device',
    'erase_flash',
    'chip_erase',
    'flash_device',
    'read_memory',
    'RTT',
    # Exceptions
    'DebugError',
    'JLinkStartError',
    'JLinkAlreadyRunningError',
    'JLinkNotRunning',
    'DebuggerNotConnectedError',
    # Status
    'get_jlink_status',
    # GDB
    'get_arch',
    'get_controller',
    'gdb_reset',
    # Low-level
    'JLink',
    # GDBServer management
    'start_jlink_gdbserver',
    'stop_jlink_gdbserver',
    'get_jlink_gdbserver_status',
]

__version__ = '1.0.0'