# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
BLE public API.

Provides ``from lager.ble import Session, Central, Client, ...`` as a
top-level import path.  Delegates to :mod:`lager.protocols.ble`.
``Session``, ``adapter`` and ``scan`` go through the box's BLE service, so
they share the adapter with ``lager ble`` and remote sessions.
"""
from lager.protocols.ble import (
    Central,
    Client,
    noop_handler,
    notify_handler,
    waiter,
)
from lager.protocols.ble.session import (
    Notification,
    Session,
    SessionClosed,
    SessionError,
    adapter,
    scan,
)

__all__ = ["Central", "Client", "noop_handler", "notify_handler", "waiter",
           "Session", "SessionError", "SessionClosed", "Notification", "adapter", "scan"]
