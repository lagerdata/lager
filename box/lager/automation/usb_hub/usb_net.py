# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import os
import time
from abc import ABC, abstractmethod

_SETTLE_ENV = "LAGER_USB_SETTLE"


def _coerce_settle(value, default=0.0):
    """Best-effort non-negative float; bad/None input -> *default*.

    Kept forgiving on purpose so a malformed env var or arg can never hang a
    caller — garbage and negatives clamp to the default (0.0 = no wait).
    """
    if value is None or value == "":
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


class USBNet(ABC):
    """Abstract base class for USB network controllers.

    ``enable``/``disable``/``toggle`` accept an optional ``settle`` delay
    (seconds) so a caller can have the box block until the port state has had
    time to take effect (the device powers down / re-enumerates) before
    returning. ``settle=None`` (the default) consults ``LAGER_USB_SETTLE`` and
    otherwise waits 0s — byte-for-byte the historical fire-and-forget behaviour.
    """
    @abstractmethod
    def enable(self, net_name, port, settle=None):
        """Enable (power on) the specified port, then settle for *settle* s."""
        raise NotImplementedError()

    @abstractmethod
    def disable(self, net_name, port, settle=None):
        """Disable (power off) the specified port, then settle for *settle* s."""
        raise NotImplementedError()

    @abstractmethod
    def toggle(self, net_name, port, settle=None):
        """Toggle the port power state, then settle for *settle* s."""
        raise NotImplementedError()

    def _apply_settle(self, settle=None):
        """Sleep for the resolved settle delay after a successful state change.

        Precedence: explicit *settle* arg > ``LAGER_USB_SETTLE`` env > 0.0.
        Returns the delay actually applied (useful for tests/logging).
        """
        if settle is None:
            settle = os.environ.get(_SETTLE_ENV)
        delay = _coerce_settle(settle, 0.0)
        if delay > 0:
            time.sleep(delay)
        return delay

    # ─────────────  Common backend exceptions  ─────────────

class USBBackendError(RuntimeError):
    """Base class for all lager.usb_hub backend failures."""


class LibraryMissingError(USBBackendError):
    """Required vendor SDK (BrainStem, pykush, …) is not present."""


class DeviceNotFoundError(USBBackendError):
    """Requested hub (by serial) could not be opened."""


class PortStateError(USBBackendError):
    """Hub reported an error while reading or changing port state."""
