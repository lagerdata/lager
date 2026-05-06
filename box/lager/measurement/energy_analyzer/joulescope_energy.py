# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Joulescope JS220 energy analyzer implementation.

Reuses the JoulescopeJS220 singleton instance (via its __new__ cache) so no
second device.open() call is needed.
"""

from __future__ import annotations
import threading
from typing import Dict, Optional

import numpy as np

from .energy_analyzer_net import EnergyAnalyzerBase
from lager.exceptions import EnergyAnalyzerBackendError


class JoulescopeEnergyAnalyzer(EnergyAnalyzerBase):
    """
    Energy analyzer backed by a Joulescope JS220.

    Shares the cached JoulescopeJS220 device handle so the instrument is
    opened only once regardless of how many net types reference it.
    """

    # Singleton cache keyed by serial string (mirrors JoulescopeJS220._instances)
    _instances: Dict[Optional[str], 'JoulescopeEnergyAnalyzer'] = {}
    _instance_lock = threading.Lock()

    def __new__(cls, name: str, pin: int | str, location) -> 'JoulescopeEnergyAnalyzer':
        from lager.measurement.watt.joulescope_js220 import _parse_serial
        serial = _parse_serial(location)

        with cls._instance_lock:
            if serial in cls._instances:
                inst = cls._instances[serial]
                inst._name = name
                inst._pin = pin
                return inst

            inst = super().__new__(cls)
            inst._initializing = threading.Lock()
            inst._initialized = False
            cls._instances[serial] = inst
            return inst

    def __init__(self, name: str, pin: int | str, location) -> None:
        from lager.measurement.watt.joulescope_js220 import JoulescopeJS220

        with self._initializing:
            if self._initialized:
                return

            super().__init__(name, pin)

            # Obtain the shared JS220 instance (opens device if not already open)
            try:
                self._js220 = JoulescopeJS220(name, pin, location)
            except Exception as e:
                raise EnergyAnalyzerBackendError(
                    f"Failed to acquire Joulescope device: {e}"
                ) from e

            self._initialized = True

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
            data = self._js220.read_raw(duration)
        except Exception as e:
            raise EnergyAnalyzerBackendError(
                f"Failed to read energy from Joulescope '{self.name}': {e}"
            ) from e

        current = data[:, 0].astype(np.float64)
        voltage = data[:, 1].astype(np.float64)
        n = len(current)
        dt = duration / n  # seconds per sample

        charge_c = float(np.sum(current) * dt)
        energy_j = float(np.sum(current * voltage) * dt)

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
            data = self._js220.read_raw(duration)
        except Exception as e:
            raise EnergyAnalyzerBackendError(
                f"Failed to read stats from Joulescope '{self.name}': {e}"
            ) from e

        current = data[:, 0].astype(np.float64)
        voltage = data[:, 1].astype(np.float64)
        power = current * voltage

        def _stats(arr):
            return {
                "mean": float(np.mean(arr)),
                "min": float(np.min(arr)),
                "max": float(np.max(arr)),
                "std": float(np.std(arr)),
            }

        return {
            "current": _stats(current),
            "voltage": _stats(voltage),
            "power": _stats(power),
            "duration_s": duration,
        }

    @classmethod
    def clear_cache(cls) -> None:
        """Clear all cached instances."""
        with cls._instance_lock:
            cls._instances.clear()
