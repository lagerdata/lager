# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Pure helpers for on-device statistics accumulator math (no hardware deps).

Kept separate from the device driver so the arithmetic can be unit-tested
without the ``joulescope`` package or a live instrument.
"""

from __future__ import annotations


def average_from_accumulators(
    charge0: float, energy0: float, sample0: float,
    charge1: float, energy1: float, sample1: float,
    sample_freq: float, fallback_voltage: float = 0.0,
) -> dict:
    """
    Compute gapless average current/voltage/power from two accumulator snapshots.

    The JS220 statistics stream exposes running integrals of charge (coulombs)
    and energy (joules). Given two snapshots taken ``sample1 - sample0`` device
    samples apart at ``sample_freq`` Hz:

        avg_current = Δcharge / Δt
        avg_power   = Δenergy / Δt
        avg_voltage = Δenergy / Δcharge   (charge-weighted mean voltage)

    ``avg_voltage`` falls back to ``fallback_voltage`` when no charge moved
    (e.g. an open circuit), since Δenergy/Δcharge is then undefined.

    Raises:
        ValueError: if the window is non-positive.
    """
    elapsed = (sample1 - sample0) / sample_freq
    if elapsed <= 0:
        raise ValueError("non-positive statistics window")
    d_charge = charge1 - charge0
    d_energy = energy1 - energy0
    current = d_charge / elapsed
    power = d_energy / elapsed
    voltage = (d_energy / d_charge) if d_charge != 0.0 else fallback_voltage
    return {
        "current": float(current),
        "voltage": float(voltage),
        "power": float(power),
    }
