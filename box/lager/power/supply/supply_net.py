# Copyright 2024-2026 Lager Data
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

    def get_monitor_state(self, channel=None) -> dict:
        """Gather the supply TUI's full monitor state in ONE call.

        The supply WebSocket monitor (box/lager/http_handlers/supply.py)
        previously issued ~12 separate hardware_service ``/invoke`` calls
        per poll tick, each taking and releasing the per-device lock.
        Composing the same queries here means one ``/invoke`` — and one
        lock acquisition — per tick, so interactive TUI commands are not
        starved behind monitor traffic on slow instruments.

        The returned keys match the ``supply_state_update`` wire shape
        (minus ``netname``/``channel``, which the monitor adds).

        Drivers for mode-sensitive instruments (e.g. the Keithley 2281S,
        whose supply function shares hardware with a battery function)
        should override this with a non-intrusive implementation that
        never enforces or switches the instrument mode.
        """
        state = {
            'voltage': float(self.measure_voltage(channel)),
            'current': float(self.measure_current(channel)),
            'power': float(self.measure_power(channel)),
            'enabled': self.output_is_enabled(channel),
            'mode': self.get_output_mode(channel) if hasattr(self, 'get_output_mode') else 'CV',
            'voltage_set': float(self.get_channel_voltage(source=channel)),
            'current_set': float(self.get_channel_current(source=channel)),
        }

        try:
            limits = self.get_channel_limits(channel)
            state['voltage_max'] = limits.get('voltage_max', 0)
            state['current_max'] = limits.get('current_max', 0)
        except (AttributeError, NotImplementedError):
            state['voltage_max'] = 0
            state['current_max'] = 0

        try:
            state['ocp_limit'] = float(self.get_overcurrent_protection_value(channel))
            state['ocp_tripped'] = self.overcurrent_protection_is_tripped(channel)
        except (AttributeError, NotImplementedError):
            state['ocp_limit'] = None
            state['ocp_tripped'] = None

        try:
            state['ovp_limit'] = float(self.get_overvoltage_protection_value(channel))
            state['ovp_tripped'] = self.overvoltage_protection_is_tripped(channel)
        except (AttributeError, NotImplementedError):
            state['ovp_limit'] = None
            state['ovp_tripped'] = None

        return state
