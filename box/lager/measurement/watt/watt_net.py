# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Abstract watt meter interface for power measurement operations.

Defines the interface that hardware-specific watt meter implementations must follow.
"""

from __future__ import annotations
from abc import ABC, abstractmethod

# Use centralized exception from lager.exceptions
from lager.exceptions import WattBackendError


class UnsupportedInstrumentError(RuntimeError):
    """Raised when attempting to use an unsupported watt meter instrument."""
    pass


# Backward compatibility alias - use WattBackendError from lager.exceptions
WattMeterBackendError = WattBackendError


class WattMeterBase(ABC):
    """
    Abstract base class for watt meter operations.

    This class defines the interface for power measurement operations.
    Concrete implementations handle device-specific communication and measurement.

    Do NOT instantiate directly - use hardware-specific subclasses.
    """

    def __init__(self, name: str, pin: int | str) -> None:
        """
        Initialize watt meter interface.

        Args:
            name: Human-readable name for this watt meter net
            pin: Hardware pin/channel identifier (number or string)
        """
        self._name = name
        self._pin = pin

    @property
    def name(self) -> str:
        """Get the human-readable name of this watt meter net."""
        return self._name

    @property
    def pin(self) -> int | str:
        """Get the hardware pin/channel identifier."""
        return self._pin

    @abstractmethod
    def read(self) -> float:
        """
        Read the current power consumption.

        Returns:
            Power reading in watts as a float
        """
        raise NotImplementedError
