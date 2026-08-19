# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Callable, Any

# Import USB exceptions for export
from .usb_net import (
    USBBackendError,
    LibraryMissingError,
    DeviceNotFoundError,
    PortStateError,
    HubOperationTimeout,
)

# Import driver classes for export
from .acroname import AcronameUSBNet
from .ykush import YKUSHUSBNet
from .plugable import PlugableUSBNet

# Backward-compatible aliases
AcronameUSB = AcronameUSBNet  # Shorter alias for convenience


__all__ = [
    "enable",
    "disable",
    "toggle",
    "state",
    "states",
    "USBBackendError",
    "LibraryMissingError",
    "DeviceNotFoundError",
    "PortStateError",
    "HubOperationTimeout",
    # Driver classes
    "AcronameUSBNet",
    "AcronameUSB",  # Alias for backward compatibility
    "YKUSHUSBNet",
    "PlugableUSBNet",
]


_dispatcher: ModuleType | None = None


def _load_dispatcher() -> ModuleType:
    """Import lager.automation.usb_hub.dispatcher exactly once (lazy singleton)."""
    global _dispatcher  # pylint: disable=global-statement
    if _dispatcher is None:
        _dispatcher = importlib.import_module("lager.automation.usb_hub.dispatcher")
    return _dispatcher

def _make_proxy(attr: str) -> Callable[..., Any]:  # helper factory
    def _proxy(*args, **kwargs):                   # noqa: D401
        return getattr(_load_dispatcher(), attr)(*args, **kwargs)

    _proxy.__name__ = attr
    _proxy.__doc__ = f"Deferred wrapper for dispatcher.{attr}()"
    return _proxy


enable = _make_proxy("enable")    # type: ignore[assignment]
disable = _make_proxy("disable")  # type: ignore[assignment]
toggle = _make_proxy("toggle")    # type: ignore[assignment]
state = _make_proxy("state")      # type: ignore[assignment]
states = _make_proxy("states")    # type: ignore[assignment]