# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Dispatcher classes for routing hardware operations to appropriate backends.

This module provides:
- BaseDispatcher: Abstract base class for creating hardware dispatchers
- Helper functions for net lookup, role validation, and address resolution
"""

from .base import BaseDispatcher
from .helpers import (
    ensure_role,
    find_saved_net,
    resolve_address,
    resolve_channel,
)

__all__ = [
    "BaseDispatcher",
    "ensure_role",
    "find_saved_net",
    "resolve_address",
    "resolve_channel",
]
