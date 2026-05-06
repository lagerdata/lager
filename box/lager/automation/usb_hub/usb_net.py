# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from abc import ABC, abstractmethod

class USBNet(ABC):
    """Abstract base class for USB network controllers."""
    @abstractmethod
    def enable(self, net_name, port):
        """Enable (power on) the specified port on the given USB net."""
        raise NotImplementedError()

    @abstractmethod
    def disable(self, net_name, port):
        """Disable (power off) the specified port on the given USB net."""
        raise NotImplementedError()

    @abstractmethod
    def toggle(self, net_name, port):
        """Toggle the power state of the specified port on the given USB net."""
        raise NotImplementedError()
    
    # ─────────────  Common backend exceptions  ─────────────

class USBBackendError(RuntimeError):
    """Base class for all lager.usb_hub backend failures."""


class LibraryMissingError(USBBackendError):
    """Required vendor SDK (BrainStem, pykush, …) is not present."""


class DeviceNotFoundError(USBBackendError):
    """Requested hub (by serial) could not be opened."""


class PortStateError(USBBackendError):
    """Hub reported an error while reading or changing port state."""
