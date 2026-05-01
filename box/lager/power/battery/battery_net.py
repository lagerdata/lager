# Copyright 2024-2026 Lager Data LLC
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
    def esr(self) -> float:
        """
        Read the battery equivalent series resistance.
        
        Returns:
            ESR in ohms.
        """
        pass

    def read_state_fields(self):
        """Return structured state for cli_output.print_state, or None for legacy.

        See ``lager.power.supply.supply_net.SupplyNet.read_state_fields`` for
        the contract — same shape applies to battery drivers.
        """
        return None

    def __str__(self) -> str:
        """
        Return a string representation of the battery device.
        Default implementation returns the class name.
        """
        return f"{self.__class__.__name__} Battery Simulator"