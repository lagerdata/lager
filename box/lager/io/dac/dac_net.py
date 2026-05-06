# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Abstract DAC interface for digital-to-analog conversion operations.

Defines the interface that hardware-specific DAC implementations must follow.
"""

from __future__ import annotations
from abc import ABC, abstractmethod


class UnsupportedInstrumentError(RuntimeError):
    """Raised when attempting to use an unsupported DAC instrument."""
    pass


class DACBase(ABC):
    """
    Abstract base class for DAC (Digital-to-Analog Converter) operations.
    
    This class defines the interface for voltage output operations on hardware pins.
    Concrete implementations handle device-specific communication and conversion.
    
    Do NOT instantiate directly - use hardware-specific subclasses.
    """
    
    def __init__(self, name: str, pin: int | str) -> None:
        """
        Initialize DAC interface.
        
        Args:
            name: Human-readable name for this DAC net
            pin: Hardware pin identifier (number or string)
        """
        self._name = name
        self._pin = pin

    @property
    def name(self) -> str:
        """Get the human-readable name of this DAC net."""
        return self._name

    @property
    def pin(self) -> int | str:
        """Get the hardware pin identifier."""
        return self._pin

    @abstractmethod
    def get_voltage(self) -> float:
        """
        Read the current voltage output from the DAC pin.

        Returns:
            Current voltage output in volts as a float
        """
        raise NotImplementedError

    @abstractmethod
    def output(self, voltage: float) -> None:
        """
        Set the voltage output of the DAC pin.
        
        Args:
            voltage: Desired output voltage in volts
        """
        raise NotImplementedError

    # Alias for backward compatibility
    def input(self) -> float:
        """Alias for get_voltage() for backward compatibility."""
        return self.get_voltage()
