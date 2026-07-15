# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Nordic PPK2 energy analyzer implementation.

Reuses the PPK2Watt singleton instance (via its __new__ cache) so no
second device open is needed.
"""

from __future__ import annotations
import threading
from typing import Dict, Optional

import numpy as np

from .energy_analyzer_net import EnergyAnalyzerBase
from lager.exceptions import EnergyAnalyzerBackendError


class PPK2EnergyAnalyzer(EnergyAnalyzerBase):
    """
    Energy analyzer backed by a Nordic PPK2.

    Shares the cached PPK2Watt device handle so the instrument is
    opened only once regardless of how many net types reference it.
    """

    _instances: Dict[Optional[str], 'PPK2EnergyAnalyzer'] = {}
    _instance_lock = threading.Lock()

    def __new__(cls, name: str, pin: int | str, location) -> 'PPK2EnergyAnalyzer':
        from lager.measurement.watt.ppk2_watt import _parse_location
        serial, _ = _parse_location(location)

        with cls._instance_lock:
            if serial in cls._instances:
                inst = cls._instances[serial]
                inst._name = name
                inst._pin = pin
                return inst

            inst = super().__new__(cls)
            inst._initializing = threading.Lock()
            inst._initialized = False
            inst._serial = serial
            cls._instances[serial] = inst
            return inst

    def __init__(self, name: str, pin: int | str, location) -> None:
        from lager.measurement.watt.ppk2_watt import PPK2Watt

        with self._initializing:
            if self._initialized:
                return

            super().__init__(name, pin)

            self._location = location

            try:
                self._ppk2 = PPK2Watt(name, pin, location)
            except Exception as e:
                raise EnergyAnalyzerBackendError(
                    f"Failed to acquire PPK2 device: {e}"
                ) from e

            self._initialized = True

    def _acquire_ppk2(self):
        """Return a live shared PPK2, re-acquiring it if it was closed.

        The physical PPK2 is shared with the watt-meter role, whose reads
        close the device afterwards to release it. That close discards the
        PPK2Watt from its per-serial cache, so constructing again here yields
        a freshly probed device instead of the closed one we still reference.
        """
        from lager.measurement.watt.ppk2_watt import PPK2Watt

        ppk2 = getattr(self, "_ppk2", None)
        if ppk2 is None or not ppk2._is_connection_alive():
            self._ppk2 = PPK2Watt(self._name, self._pin, self._location)
        return self._ppk2

    def read_energy(self, duration: float) -> dict:
        """
        Integrate current and power over `duration` seconds.

        Returns dict with:
            energy_j    - energy in joules
            energy_wh   - energy in watt-hours
            charge_c    - charge in coulombs
            charge_ah   - charge in amp-hours
            duration_s  - actual duration requested
        """
        try:
            current_amps, voltage_v = self._acquire_ppk2().read_raw(duration)
        except Exception as e:
            raise EnergyAnalyzerBackendError(
                f"Failed to read energy from PPK2 '{self.name}': {e}"
            ) from e

        n = len(current_amps)
        dt = duration / n  # seconds per sample

        charge_c = float(np.sum(current_amps) * dt)
        power = current_amps * voltage_v
        energy_j = float(np.sum(power) * dt)

        return {
            "energy_j": energy_j,
            "energy_wh": energy_j / 3600.0,
            "charge_c": charge_c,
            "charge_ah": charge_c / 3600.0,
            "duration_s": duration,
        }

    def read_stats(self, duration: float) -> dict:
        """
        Compute mean/min/max/std for current, voltage, and power over `duration` seconds.

        Returns nested dict:
            {
                "current": {"mean": ..., "min": ..., "max": ..., "std": ...},
                "voltage": {...},
                "power":   {...},
                "duration_s": ...
            }
        """
        try:
            current_amps, voltage_v = self._acquire_ppk2().read_raw(duration)
        except Exception as e:
            raise EnergyAnalyzerBackendError(
                f"Failed to read stats from PPK2 '{self.name}': {e}"
            ) from e

        # Voltage is constant (source mode), but create array for consistent stats
        voltage = np.full_like(current_amps, voltage_v)
        power = current_amps * voltage_v

        def _stats(arr):
            return {
                "mean": float(np.mean(arr)),
                "min": float(np.min(arr)),
                "max": float(np.max(arr)),
                "std": float(np.std(arr)),
            }

        return {
            "current": _stats(current_amps),
            "voltage": _stats(voltage),
            "power": _stats(power),
            "duration_s": duration,
        }

    @classmethod
    def clear_cache(cls) -> None:
        """Clear all cached instances."""
        with cls._instance_lock:
            cls._instances.clear()
