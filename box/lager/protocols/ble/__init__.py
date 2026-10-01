# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
BLE module for box

This module provides BLE functionality including:
- Client and Central classes for BLE operations
- Device scanning functionality
- Device connection helpers
"""
from .client import Client, Central, noop_handler, notify_handler, waiter
from .session import Notification, Session, SessionClosed, SessionError, adapter

__all__ = [
    # Core BLE classes
    'Client',
    'Central',
    # Held GATT sessions through the box's session service
    'Session',
    'SessionError',
    'SessionClosed',
    'Notification',
    'adapter',
    # Helpers
    'noop_handler',
    'notify_handler',
    'waiter',
]
