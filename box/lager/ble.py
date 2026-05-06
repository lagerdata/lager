# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
BLE public API.

Provides ``from lager.ble import Central, Client, ...`` as a top-level
import path.  Delegates to :mod:`lager.protocols.ble`.
"""
from lager.protocols.ble import (
    Central,
    Client,
    noop_handler,
    notify_handler,
    waiter,
)

__all__ = ["Central", "Client", "noop_handler", "notify_handler", "waiter"]
