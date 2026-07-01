# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Shared formatting helpers for measurement output.
"""

from __future__ import annotations


def fmt_si(value: float, unit: str) -> str:
    """
    Format a value with an appropriate SI prefix.

    Scales sub-unit magnitudes into milli/micro/nano so small readings stay
    readable (e.g. ``52.340 µW`` instead of ``0.000 W``). An exact zero is
    shown in the base unit.

    Args:
        value: The numeric value (in base units, e.g. watts/amps/volts).
        unit: The base unit symbol (e.g. ``"W"``, ``"A"``, ``"V"``).

    Returns:
        A formatted string with three decimal places and an SI-prefixed unit.
    """
    abs_val = abs(value)
    if abs_val == 0.0:
        return f"{value:.3f} {unit}"
    if abs_val >= 1.0:
        return f"{value:.3f} {unit}"
    elif abs_val >= 1e-3:
        return f"{value * 1e3:.3f} m{unit}"
    elif abs_val >= 1e-6:
        return f"{value * 1e6:.3f} µ{unit}"
    else:
        return f"{value * 1e9:.3f} n{unit}"
