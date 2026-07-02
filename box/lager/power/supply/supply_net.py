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


def _safe(fn, default=None):
    """Run a single monitor-field read, degrading to `default` on any error.

    One flaky field (e.g. :OUTP:MODE? unsupported by a DP711 firmware, a
    measure_power overflow, a transient bus error) must cost only that field,
    not the whole get_monitor_state() gather — the WebSocket monitor and the
    HTTP 'state' action both drop the entire structured state if this raises.
    """
    try:
        return fn()
    except Exception:
        return default


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
        # Liveness probe — deliberately NOT swallowed (mirrors the Keithley
        # overrides). If the cached pyvisa session is stale (instrument
        # power-cycled / USB re-enumerated), this raises a session/ENODEV
        # error that propagates to the hardware service's /invoke handler,
        # which evicts the stale session, reconnects, and retries this call
        # on a fresh session. Without it the _safe-guarded reads below would
        # return defaults forever and the stale session would never be
        # evicted. *IDN? is read-only and valid in any instrument mode.
        _probe = getattr(self, "instr", None)
        if _probe is not None:
            try:
                _probe.query("*IDN?", check_errors=False)
            except TypeError:
                # raw pyvisa resource without the check_errors kwarg
                _probe.query("*IDN?")

        # Every field read is individually _safe-guarded: one failing query
        # (unsupported SCPI on a given firmware, a transient bus error) costs
        # only that field, never the whole gather.
        limits = _safe(lambda: self.get_channel_limits(channel), {}) or {}
        return {
            'voltage': _safe(lambda: float(self.measure_voltage(channel)), 0.0),
            'current': _safe(lambda: float(self.measure_current(channel)), 0.0),
            'power': _safe(lambda: float(self.measure_power(channel)), 0.0),
            'enabled': _safe(lambda: self.output_is_enabled(channel)),
            'mode': _safe(lambda: self.get_output_mode(channel), 'CV')
                    if hasattr(self, 'get_output_mode') else 'CV',
            'voltage_set': _safe(lambda: float(self.get_channel_voltage(source=channel))),
            'current_set': _safe(lambda: float(self.get_channel_current(source=channel))),
            'voltage_max': _safe(lambda: limits.get('voltage_max', 0), 0),
            'current_max': _safe(lambda: limits.get('current_max', 0), 0),
            'ocp_limit': _safe(lambda: float(self.get_overcurrent_protection_value(channel))),
            'ocp_tripped': _safe(lambda: self.overcurrent_protection_is_tripped(channel)),
            'ovp_limit': _safe(lambda: float(self.get_overvoltage_protection_value(channel))),
            'ovp_tripped': _safe(lambda: self.overvoltage_protection_is_tripped(channel)),
        }
