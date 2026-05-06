# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations
from abc import ABC, abstractmethod

class ThermocoupleBase(ABC):
    """Abstract Thermocouple net; do NOT instantiate directly."""

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
    def read(self) -> float:
        """Read the thermocouple temperature."""
        raise NotImplementedError

    
