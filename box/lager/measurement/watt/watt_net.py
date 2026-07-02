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
    def read(self, duration: float = 0.1) -> float:
        """
        Read the current power consumption.

        Args:
            duration: Averaging window in seconds. Longer windows reduce noise
                for a steadier reading. Instruments that return an instantaneous
                value (e.g. Yocto-Watt) may ignore this.

        Returns:
            Power reading in watts as a float
        """
        raise NotImplementedError

    def read_current(self, duration: float = 0.1) -> float:
        """
        Read current in amps.

        Default implementation for instruments that cannot report current
        independently of power. Subclasses backed by a current-sensing
        instrument (Joulescope, PPK2) override this.

        Args:
            duration: Averaging window in seconds.

        Returns:
            Current reading in amps as a float
        """
        raise UnsupportedInstrumentError(
            f"Watt meter '{self.name}' does not support reading current "
            f"(use a Joulescope JS220 or Nordic PPK2)"
        )

    def read_voltage(self, duration: float = 0.1) -> float:
        """
        Read voltage in volts.

        Default implementation for instruments that cannot report voltage
        independently of power. Subclasses backed by a voltage-sensing
        instrument (Joulescope, PPK2) override this.

        Args:
            duration: Averaging window in seconds.

        Returns:
            Voltage reading in volts as a float
        """
        raise UnsupportedInstrumentError(
            f"Watt meter '{self.name}' does not support reading voltage "
            f"(use a Joulescope JS220 or Nordic PPK2)"
        )

    def read_all(self, duration: float = 0.1) -> dict:
        """
        Read current, voltage, and power in a single operation.

        Default implementation for instruments that cannot report current and
        voltage independently of power. Subclasses backed by a current/voltage
        sensing instrument (Joulescope, PPK2) override this.

        Args:
            duration: Averaging window in seconds.

        Returns:
            Dictionary with 'current' (amps), 'voltage' (volts), 'power' (watts)
        """
        raise UnsupportedInstrumentError(
            f"Watt meter '{self.name}' does not support reading current/voltage "
            f"(use a Joulescope JS220 or Nordic PPK2)"
        )
