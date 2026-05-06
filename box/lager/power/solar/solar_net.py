# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations
import abc

# Import unified exceptions from centralized module
from lager.exceptions import (
    SolarBackendError,
    LibraryMissingError,
    DeviceNotFoundError,
    DeviceLockError,
)

# Re-export for backward compatibility
__all__ = ['SolarBackendError', 'LibraryMissingError', 'DeviceNotFoundError', 'DeviceLockError', 'SolarNet']


class SolarNet(abc.ABC):
    """Abstract base class defining the interface for a solar simulator backend."""
    @abc.abstractmethod
    def enable(self) -> None:
        """Connect to the instrument (establish communication and configure mode)."""
        raise NotImplementedError

    @abc.abstractmethod
    def disable(self) -> None:
        """Disconnect from the instrument and release any locks if needed."""
        raise NotImplementedError

    @abc.abstractmethod
    def irradiance(self, value: float | None = None) -> str:
        """Get or set the irradiance (W/m^2). Return current irradiance as string."""
        raise NotImplementedError

    @abc.abstractmethod
    def mpp_current(self) -> str:
        """Return the current at the maximum power point (A) as a string."""
        raise NotImplementedError

    @abc.abstractmethod
    def mpp_voltage(self) -> str:
        """Return the voltage at the maximum power point (V) as a string."""
        raise NotImplementedError

    @abc.abstractmethod
    def resistance(self, value: float | None = None) -> str:
        """Get or set the dynamic panel resistance (Ω) as a string."""
        raise NotImplementedError

    @abc.abstractmethod
    def temperature(self) -> str:
        """Return the cell temperature (°C) as a string."""
        raise NotImplementedError

    @abc.abstractmethod
    def voc(self) -> str:
        """Return the open-circuit voltage (Voc) as a string."""
        raise NotImplementedError
