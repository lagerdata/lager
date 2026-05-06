# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

# Import unified exceptions from centralized module
from lager.exceptions import (
    ELoadBackendError,
    LibraryMissingError,
    DeviceNotFoundError,
)

# Re-export for backward compatibility
__all__ = ['ELoadBackendError', 'LibraryMissingError', 'DeviceNotFoundError', 'ELoadNet']


class ELoadNet(ABC):
    """
    Abstract base class for electronic load backends.

    This class defines the interface that all electronic load implementations
    must follow to be compatible with the Lager framework. It provides electronic
    load functionality including constant current, constant voltage, constant
    resistance, and constant power modes.
    """

    @abstractmethod
    def mode(self, mode_type: str | None = None) -> str | None:
        """
        Set or read the electronic load operation mode.

        Args:
            mode_type: Operation mode ("CC", "CV", "CR", "CW"/"CP").
                      If None, reads and returns current mode.

        Returns:
            Current mode string if mode_type is None, otherwise None.
        """
        pass

    @abstractmethod
    def current(self, value: float | None = None) -> float | None:
        """
        Set or read the constant current setting.

        Args:
            value: Current setting in amps.
                  If None, reads and returns current setting.

        Returns:
            Current setting in amps if value is None, otherwise None.
        """
        pass

    @abstractmethod
    def voltage(self, value: float | None = None) -> float | None:
        """
        Set or read the constant voltage setting.

        Args:
            value: Voltage setting in volts.
                  If None, reads and returns voltage setting.

        Returns:
            Voltage setting in volts if value is None, otherwise None.
        """
        pass

    @abstractmethod
    def resistance(self, value: float | None = None) -> float | None:
        """
        Set or read the constant resistance setting.

        Args:
            value: Resistance setting in ohms.
                  If None, reads and returns resistance setting.

        Returns:
            Resistance setting in ohms if value is None, otherwise None.
        """
        pass

    @abstractmethod
    def power(self, value: float | None = None) -> float | None:
        """
        Set or read the constant power setting.

        Args:
            value: Power setting in watts.
                  If None, reads and returns power setting.

        Returns:
            Power setting in watts if value is None, otherwise None.
        """
        pass

    @abstractmethod
    def enable(self) -> None:
        """
        Enable (turn on) the electronic load input.
        """
        pass

    @abstractmethod
    def disable(self) -> None:
        """
        Disable (turn off) the electronic load input.
        """
        pass

    @abstractmethod
    def print_state(self) -> None:
        """
        Print comprehensive electronic load state including:
        - Operation mode (CC/CV/CR/CW)
        - Current setting and measured value
        - Voltage setting and measured value
        - Power measured value
        - Input state (enabled/disabled)
        """
        pass

    @abstractmethod
    def measured_voltage(self) -> float:
        """
        Read the measured input voltage.

        Returns:
            Measured voltage in volts.
        """
        pass

    @abstractmethod
    def measured_current(self) -> float:
        """
        Read the measured input current.

        Returns:
            Measured current in amps.
        """
        pass

    @abstractmethod
    def measured_power(self) -> float:
        """
        Read the measured input power.

        Returns:
            Measured power in watts.
        """
        pass

    def __str__(self) -> str:
        """
        Return a string representation of the electronic load device.
        Default implementation returns the class name.
        """
        return f"{self.__class__.__name__} Electronic Load"
