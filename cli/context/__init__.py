# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.context

    Context management subpackage
"""
from .ci_detection import CIEnvironment, is_container_ci, get_ci_environment, _CONTAINER_CI
from .error_handlers import (
    DOCKER_ERROR_CODES,
    CANBUS_ERROR_CODES,
    ElfHashMismatch,
    print_docker_error,
    print_canbus_error,
)
from .core import (
    LagerContext,
    get_default_box,
    get_impl_path,
    get_default_net,
)
from .session import (
    DirectHTTPSession,
)
def is_ip_address(address):
    """Check if the address is an IP address (instead of box ID)"""
    try:
        import ipaddress
        ipaddress.ip_address(address)
        return True
    except ValueError:
        return False


__all__ = [
    # CI detection
    'CIEnvironment',
    'is_container_ci',
    'get_ci_environment',
    '_CONTAINER_CI',
    # Error handlers
    'DOCKER_ERROR_CODES',
    'CANBUS_ERROR_CODES',
    'ElfHashMismatch',
    'print_docker_error',
    'print_canbus_error',
    # Core context
    'LagerContext',
    'get_default_box',
    'get_impl_path',
    'get_default_net',
    # Sessions
    'DirectHTTPSession',
    # Utilities
    'is_ip_address',
]
