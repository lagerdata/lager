# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Debug commands subpackage.

This subpackage contains firmware debugging commands for embedded development:
- gdbserver: Start JLink GDB server for debugging
- flash: Flash firmware to target
- reset: Reset target device
- erase: Erase flash memory
- memrd: Read memory from target
- status: Show debug net status
- health: Check debug service health
- disconnect: Stop JLink GDB server

The main command group is exposed as `_debug` to avoid naming conflicts.

Modules:
- commands.py: Main CLI command definitions
- gdb.py: GDB-specific functionality
- net_cache.py: Debug net caching
- service_client.py: Debug service client
- service_helper.py: Service tunnel helpers
- tunnel.py: WebSocket tunnel support
"""

# Helper modules that don't have circular dependencies
from .net_cache import get_net_cache, DebugNetCache
from .service_client import DebugServiceClient
from .service_helper import (
    ServiceTunnel,
    is_service_available,
    ensure_service_tunnel,
    cleanup_tunnels,
)
from .tunnel import serve_tunnel

# Note: gdb and commands modules are lazily imported to avoid circular dependencies
# They import from cli/python/commands.py which imports from cli/debug/tunnel.py

__all__ = [
    # Main command group (lazy loaded)
    "_debug",
    # Helper modules (lazy loaded)
    "gdb_debug",
    # Eagerly loaded modules
    "get_net_cache",
    "DebugNetCache",
    "DebugServiceClient",
    "ServiceTunnel",
    "is_service_available",
    "ensure_service_tunnel",
    "cleanup_tunnels",
    "serve_tunnel",
]


def __getattr__(name):
    """Lazy import for modules that have circular dependencies."""
    if name == "_debug":
        from .commands import _debug
        return _debug
    elif name == "gdb_debug":
        from .gdb import debug as gdb_debug
        return gdb_debug
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
