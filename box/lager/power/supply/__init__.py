# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Supply module for power supply control.

Provides functions for controlling power supply nets (voltage, current, enable/disable, etc.).
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Optional

__all__ = ["voltage", "current", "enable", "disable", "state", "set_mode", "clear_ocp", "clear_ovp"]


_dispatcher: ModuleType | None = None


def _load_dispatcher() -> ModuleType:
    """Import lager.power.supply.dispatcher exactly once (lazy singleton)."""
    global _dispatcher  # pylint: disable=global-statement
    if _dispatcher is None:
        _dispatcher = importlib.import_module("lager.power.supply.dispatcher")
    return _dispatcher


def voltage(net_name: str, value: Optional[float] = None, ocp: Optional[float] = None, ovp: Optional[float] = None) -> None:
    """
    Set or read voltage for a power supply net.

    Args:
        net_name: Name of the power supply net
        value: Voltage to set (V). If None, reads and prints current voltage.
        ocp: Optional over-current protection limit (A)
        ovp: Optional over-voltage protection limit (V)

    Example:
        supply.voltage('MAIN_BOARD', 24.0)           # Set to 24V
        supply.voltage('MAIN_BOARD', 24.0, ovp=24.5) # Set to 24V with OVP at 24.5V
        supply.voltage('MAIN_BOARD')                 # Read current voltage
    """
    dispatcher = _load_dispatcher()
    dispatcher.voltage(net_name, value=value, ocp=ocp, ovp=ovp)


def current(net_name: str, value: Optional[float] = None, ocp: Optional[float] = None, ovp: Optional[float] = None) -> None:
    """
    Set or read current for a power supply net.

    Args:
        net_name: Name of the power supply net
        value: Current limit to set (A). If None, reads and prints current.
        ocp: Optional over-current protection limit (A)
        ovp: Optional over-voltage protection limit (V)

    Example:
        supply.current('MAIN_BOARD', 1.0)           # Set current limit to 1A
        supply.current('MOTOR', 3.0, ocp=3.0)       # Set to 3A with OCP at 3A
        supply.current('MAIN_BOARD')                # Read current
    """
    dispatcher = _load_dispatcher()
    dispatcher.current(net_name, value=value, ocp=ocp, ovp=ovp)


def enable(net_name: str) -> None:
    """
    Enable (turn on) a power supply net.

    Args:
        net_name: Name of the power supply net to enable

    Example:
        supply.enable('MAIN_BOARD')
    """
    dispatcher = _load_dispatcher()
    dispatcher.enable(net_name)


def disable(net_name: str) -> None:
    """
    Disable (turn off) a power supply net.

    Args:
        net_name: Name of the power supply net to disable

    Example:
        supply.disable('MAIN_BOARD')
    """
    dispatcher = _load_dispatcher()
    dispatcher.disable(net_name)


def state(net_name: str) -> None:
    """
    Print comprehensive state for a power supply net.

    Displays: channel, enabled status, mode, voltage, current, power,
    OCP limit, OCP tripped status, OVP limit, OVP tripped status.

    Args:
        net_name: Name of the power supply net

    Example:
        supply.state('MAIN_BOARD')
    """
    dispatcher = _load_dispatcher()
    dispatcher.state(net_name)


def set_mode(net_name: str) -> None:
    """
    Set the instrument mode to DC power supply (if applicable).

    Args:
        net_name: Name of the power supply net

    Example:
        supply.set_mode('MAIN_BOARD')
    """
    dispatcher = _load_dispatcher()
    dispatcher.set_mode(net_name)


def clear_ocp(net_name: str) -> None:
    """
    Clear an over-current protection (OCP) trip.

    Args:
        net_name: Name of the power supply net

    Example:
        supply.clear_ocp('MAIN_BOARD')
    """
    dispatcher = _load_dispatcher()
    dispatcher.clear_ocp(net_name)


def clear_ovp(net_name: str) -> None:
    """
    Clear an over-voltage protection (OVP) trip.

    Args:
        net_name: Name of the power supply net

    Example:
        supply.clear_ovp('MAIN_BOARD')
    """
    dispatcher = _load_dispatcher()
    dispatcher.clear_ovp(net_name)
