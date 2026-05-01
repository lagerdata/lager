# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations
import abc

# Import unified exceptions from centralized module
from lager.exceptions import (
    SupplyBackendError,
    LibraryMissingError,
    DeviceNotFoundError,
)

# Re-export for backward compatibility
__all__ = ['SupplyBackendError', 'LibraryMissingError', 'DeviceNotFoundError', 'SupplyNet']


class SupplyNet(abc.ABC):
    """Abstract base class defining the interface for a power supply backend."""

    @abc.abstractmethod
    def voltage(self, value: float | None = None,
                ocp: float | None = None,
                ovp: float | None = None) -> None:
        """Set or read the net's voltage. If `value` is provided, set the output voltage; otherwise read and print the present voltage."""
        raise NotImplementedError

    @abc.abstractmethod
    def current(self, value: float | None = None,
                ocp: float | None = None,
                ovp: float | None = None) -> None:
        """Set or read the net's current. If `value` is provided, set the output current; otherwise read and print the present current."""
        raise NotImplementedError

    @abc.abstractmethod
    def enable(self) -> None:
        """Enable (turn on) the supply output for this net."""
        raise NotImplementedError

    @abc.abstractmethod
    def disable(self) -> None:
        """Disable (turn off) the supply output for this net."""
        raise NotImplementedError

    @abc.abstractmethod
    def set_mode(self) -> None:
        """Set the instrument mode to DC power supply (if applicable)."""
        raise NotImplementedError

    @abc.abstractmethod
    def state(self) -> None:
        """Print a comprehensive power state for this net (voltage, current, power, OCP/OVP status)."""
        raise NotImplementedError

    @abc.abstractmethod
    def clear_ocp(self) -> None:
        """Clear an over-current protection (OCP) trip on this net."""
        raise NotImplementedError

    @abc.abstractmethod
    def clear_ovp(self) -> None:
        """Clear an over-voltage protection (OVP) trip on this net."""
        raise NotImplementedError

    @abc.abstractmethod
    def ocp(self, value: float | None = None) -> None:
        """Set or read over-current protection limit. If `value` is provided, set the OCP limit; otherwise read and print the current OCP limit."""
        raise NotImplementedError

    @abc.abstractmethod
    def ovp(self, value: float | None = None) -> None:
        """Set or read over-voltage protection limit. If `value` is provided, set the OVP limit; otherwise read and print the current OVP limit."""
        raise NotImplementedError

    def get_full_state(self) -> None:
        """Get full state including measurements, setpoints, and limits. This is optional and can be overridden by specific drivers."""
        # Default implementation falls back to state()
        self.state()

    def read_state_fields(self):
        """Return structured state for cli_output.print_state, or None for legacy.

        Drivers that implement this opt into the unified output path: the
        dispatcher renders via lager.cli_output.print_state, which produces a
        human-readable aligned block in text mode and a structured envelope in
        JSON mode. Drivers that don't override return None and the dispatcher
        falls back to calling state().

        Return shape:
            {
                "instrument": "Rigol DP821",      # human label
                "channel":    1,                  # optional
                "severity":   "ok"|"warn"|"error",# optional, default "ok"
                "fields":     [Field, Field, ...] # box.lager.cli_output.Field
            }
        """
        return None
