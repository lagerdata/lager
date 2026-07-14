# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

# Import unified exceptions from centralized module
from lager.exceptions import (
    BatteryBackendError,
    LibraryMissingError,
    DeviceNotFoundError,
)

# Re-export for backward compatibility
__all__ = ['BatteryBackendError', 'LibraryMissingError', 'DeviceNotFoundError', 'BatteryNet']


class BatteryNet(ABC):
    """
    Abstract base class for battery simulator backends.
    
    This class defines the interface that all battery simulator implementations
    must follow to be compatible with the Lager framework. It provides battery
    simulation functionality including state of charge, terminal voltage,
    capacity management, and protection features.
    """

    @abstractmethod
    def mode(self, mode_type: str | None = None) -> None:
        """
        Set or read the battery simulation mode.
        
        Args:
            mode_type: Battery simulation mode ("static" or "dynamic").
                      If None, reads and prints current mode.
        """
        pass

    @abstractmethod
    def set_mode_battery(self) -> None:
        """
        Set the instrument to battery simulation mode.
        This ensures the device is configured for battery simulation.
        """
        pass

    @abstractmethod
    def soc(self, value: float | None = None) -> None:
        """
        Set or read the battery state of charge.
        
        Args:
            value: State of charge percentage (0-100).
                  If None, reads and prints current SOC.
        """
        pass

    @abstractmethod
    def voc(self, value: float | None = None) -> None:
        """
        Set or read the battery open circuit voltage.
        
        Args:
            value: Open circuit voltage in volts.
                  If None, reads and prints current VOC.
        """
        pass

    @abstractmethod
    def voltage_full(self, value: float | None = None) -> None:
        """
        Set or read the fully charged battery voltage.
        
        Args:
            value: Fully charged voltage in volts.
                  If None, reads and prints current fully charged voltage.
        """
        pass

    @abstractmethod
    def voltage_empty(self, value: float | None = None) -> None:
        """
        Set or read the fully discharged battery voltage.
        
        Args:
            value: Fully discharged voltage in volts.
                  If None, reads and prints current fully discharged voltage.
        """
        pass

    @abstractmethod
    def capacity(self, value: float | None = None) -> None:
        """
        Set or read the battery capacity.
        
        Args:
            value: Battery capacity in amp-hours.
                  If None, reads and prints current capacity.
        """
        pass

    @abstractmethod
    def current_limit(self, value: float | None = None) -> None:
        """
        Set or read the maximum charge/discharge current.
        
        Args:
            value: Maximum current in amps.
                  If None, reads and prints current limit.
        """
        pass

    @abstractmethod
    def ovp(self, value: float | None = None) -> None:
        """
        Set or read the over-voltage protection threshold.
        
        Args:
            value: OVP threshold in volts.
                  If None, reads and prints current OVP setting.
        """
        pass

    @abstractmethod
    def ocp(self, value: float | None = None) -> None:
        """
        Set or read the over-current protection threshold.
        
        Args:
            value: OCP threshold in amps.
                  If None, reads and prints current OCP setting.
        """
        pass

    @abstractmethod
    def model(self, partnumber: str | None = None) -> None:
        """
        Set or read the battery model.

        Battery models define voltage-SOC discharge characteristics and internal
        resistance for different battery chemistries. Implementation varies by
        instrument:

        Keithley 2281S:
        - Stores models in numbered memory slots (0-9)
        - Supports model name aliases: '18650', 'liion', 'nimh', 'nicd', 'lead-acid'
        - Custom models can be created via instrument front panel
        - Slot 0 ('discharge') provides basic constant-voltage simulation

        Args:
            partnumber: Battery model identifier (name or numeric slot).
                       If None, reads and prints current model.
        """
        pass

    @abstractmethod
    def model_catalog(self) -> list:
        """
        Read the catalog of battery models available on the instrument.

        Lists the same slot-storage mechanism that model() loads from:
        numbered memory slots plus any firmware built-in models. Must be
        read-only — assembling the catalog must not change instrument state.

        Keithley 2281S:
        - Slot 0 ('discharge') is always available
        - Slots 1-9 are reported when a model has been saved there
        - The five firmware built-in models are listed without a slot

        Returns:
            List of {"slot": int | None, "name": str | None} dicts, one per
            available model. Slot-less entries are built-in model names;
            nameless entries are custom models addressed by slot index.
        """
        pass

    @abstractmethod
    def read_model(self, slot: int) -> dict:
        """
        Read a saved battery model's curve points out of a memory slot.

        Exports from the same slot-storage mechanism that model() loads from
        and model_catalog() lists. Must be read-only: exporting a model must
        not change which model is active.

        Keithley 2281S:
        - Slots 1-9 hold saved models; slot 0 (DISCHARGE) has no curve
        - Each model is 101 points per element (VOC and resistance)
        - Empty slots are rejected with guidance to the 'models' catalog

        Args:
            slot: Memory slot to export (1-9).

        Returns:
            {"slot": int, "points": [{"voc": float, "resistance": float},
            ...]} in SOC order (index 0 = empty battery).

        Raises:
            BatteryBackendError: If the slot is invalid or empty.
        """
        pass

    @abstractmethod
    def define_model(self, slot: int, voc: list, resistance: list) -> None:
        """
        Write a custom battery model into a memory slot (create/overwrite).

        Persists into the same slot-storage mechanism that model() loads
        from. Saving to an occupied slot silently overwrites it, and there
        is no way to delete/empty a slot afterwards (the Keithley 2281S has
        no SCPI for it — slots can only be overwritten), so callers are
        expected to gate overwrites behind explicit confirmation.

        Keithley 2281S:
        - Valid target slots are 1-9 (slot 0 is DISCHARGE, not writable)
        - A model is two curves indexed by SOC: VOC (non-decreasing) and
          internal resistance (non-increasing)
        - Exactly 101 points per element, or exactly 11 points which the
          instrument interpolates to 101

        Args:
            slot: Target memory slot (1-9).
            voc: Open-circuit voltage curve in volts.
            resistance: Internal resistance curve in ohms, same length.

        Raises:
            BatteryBackendError: On invalid input or a failed/unverified save.
        """
        pass

    @abstractmethod
    def enable(self) -> None:
        """
        Enable the battery simulator output.
        """
        pass

    @abstractmethod
    def disable(self) -> None:
        """
        Disable the battery simulator output.
        """
        pass

    @abstractmethod
    def clear_ovp(self) -> None:
        """
        Clear over-voltage protection trip condition.
        """
        pass

    @abstractmethod
    def clear_ocp(self) -> None:
        """
        Clear over-current protection trip condition.
        """
        pass

    @abstractmethod
    def print_state(self) -> None:
        """
        Print comprehensive battery simulator state including:
        - Terminal voltage
        - Current
        - ESR (Equivalent Series Resistance)
        - State of charge
        - Open circuit voltage
        - Capacity
        - Protection status
        """
        pass

    @abstractmethod
    def terminal_voltage(self) -> float:
        """
        Read the battery terminal voltage.
        
        Returns:
            Terminal voltage in volts.
        """
        pass

    @abstractmethod
    def current(self) -> float:
        """
        Read the battery current (positive for discharge, negative for charge).
        
        Returns:
            Current in amps.
        """
        pass

    @abstractmethod
    def esr(self, value: float | None = None) -> float:
        """
        Read or set the battery equivalent series resistance (ESR).

        With no argument, reads and returns the real-time series resistance.

        With a value, sets the simulator's series resistance. Note that some
        instruments expose this only as a series-resistance *offset* added to
        the active battery model rather than an absolute value; see the
        backend implementation for exact semantics and the supported range.

        Args:
            value: Series resistance (ohms) to set. If None, reads instead.

        Returns:
            ESR in ohms when reading; None when setting.
        """
        pass

    def __str__(self) -> str:
        """
        Return a string representation of the battery device.
        Default implementation returns the class name.
        """
        return f"{self.__class__.__name__} Battery Simulator"