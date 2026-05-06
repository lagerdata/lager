# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Abstract energy analyzer interface.

Defines the interface that hardware-specific energy analyzer implementations
must follow. The energy analyzer provides energy accumulation and statistics
over a specified duration.
"""

from __future__ import annotations
from abc import ABC, abstractmethod


class EnergyAnalyzerBase(ABC):
    """
    Abstract base class for energy analyzer operations.

    Concrete implementations handle device-specific communication and measurement.
    Do NOT instantiate directly - use hardware-specific subclasses.
    """

    def __init__(self, name: str, pin: int | str) -> None:
        self._name = name
        self._pin = pin

    @property
    def name(self) -> str:
        return self._name

    @property
    def pin(self) -> int | str:
        return self._pin

    @abstractmethod
    def read_energy(self, duration: float) -> dict:
        """
        Integrate current and power over `duration` seconds.

        Returns:
            dict with keys: energy_j, energy_wh, charge_c, charge_ah, duration_s
        """
        raise NotImplementedError

    @abstractmethod
    def read_stats(self, duration: float) -> dict:
        """
        Compute statistics for current, voltage, and power over `duration` seconds.

        Returns:
            Nested dict: {current: {mean, min, max, std}, voltage: {...}, power: {...}}
        """
        raise NotImplementedError

    def close(self) -> None:
        """Release resources. Default no-op; subclasses may override."""
        pass
