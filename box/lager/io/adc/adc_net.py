# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Abstract ADC interface for analog-to-digital conversion operations.

Defines the interface that hardware-specific ADC implementations must follow.
"""

from __future__ import annotations
from abc import ABC, abstractmethod


class UnsupportedInstrumentError(RuntimeError):
    """Raised when attempting to use an unsupported ADC instrument."""
    pass


class ADCBase(ABC):
    """
    Abstract base class for ADC (Analog-to-Digital Converter) operations.

    This class defines the interface for voltage measurement operations on hardware pins.
    Concrete implementations handle device-specific communication and conversion.

    Do NOT instantiate directly - use hardware-specific subclasses.
    """

    def __init__(self, name: str, pin: int | str) -> None:
        """
        Initialize ADC interface.

        Args:
            name: Human-readable name for this ADC net
            pin: Hardware pin identifier (number or string)
        """
        self._name = name
        self._pin = pin

    @property
    def name(self) -> str:
        """Get the human-readable name of this ADC net."""
        return self._name

    @property
    def pin(self) -> int | str:
        """Get the hardware pin identifier."""
        return self._pin

    @abstractmethod
    def input(self) -> float:
        """
        Read the current voltage on the ADC pin.

        Returns:
            Voltage reading in volts as a float
        """
        raise NotImplementedError
