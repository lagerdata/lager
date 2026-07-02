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
    readable (e.g. ``52.340 µW`` instead of ``0.000 W``). For values so small
    that even the nano prefix would round to ``0.000``, scientific notation is
    used instead (e.g. ``3.000e-13 W``) so a nonzero reading is never lost to
    rounding. An exact zero is shown in the base unit.

    Args:
        value: The numeric value (in base units, e.g. watts/amps/volts).
        unit: The base unit symbol (e.g. ``"W"``, ``"A"``, ``"V"``).

    Returns:
        A formatted string with an SI-prefixed unit, or scientific notation for
        very small nonzero values.
    """
    abs_val = abs(value)
    if abs_val == 0.0:
        return f"{value:.3f} {unit}"

    # Choose an SI prefix that keeps the magnitude readable.
    if abs_val >= 1.0:
        scaled, prefix = value, ""
    elif abs_val >= 1e-3:
        scaled, prefix = value * 1e3, "m"
    elif abs_val >= 1e-6:
        scaled, prefix = value * 1e6, "µ"
    else:
        scaled, prefix = value * 1e9, "n"

    # If even the smallest prefix would round to 0.000 at three decimals,
    # fall back to scientific notation rather than lose the value to rounding.
    if abs(scaled) < 0.0005:
        return f"{value:.3e} {unit}"
    return f"{scaled:.3f} {prefix}{unit}"
