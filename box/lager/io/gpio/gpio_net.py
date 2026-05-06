# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Abstract GPIO interface for digital input/output operations.

Defines the interface that hardware-specific GPIO implementations must follow.
"""

from __future__ import annotations
import time
from abc import ABC, abstractmethod


class UnsupportedInstrumentError(RuntimeError):
    """Raised when attempting to use an unsupported GPIO instrument."""
    pass


class GPIOBase(ABC):
    """
    Abstract base class for GPIO (General Purpose Input/Output) operations.
    
    This class defines the interface for digital I/O operations on hardware pins.
    Concrete implementations handle device-specific communication.
    
    Do NOT instantiate directly - use hardware-specific subclasses.
    """
    
    def __init__(self, name: str, pin: int | str) -> None:
        """
        Initialize GPIO interface.
        
        Args:
            name: Human-readable name for this GPIO net
            pin: Hardware pin identifier (number or string)
        """
        self._name = name
        self._pin = pin

    @property
    def name(self) -> str:
        """Get the human-readable name of this GPIO net."""
        return self._name

    @property
    def pin(self) -> int | str:
        """Get the hardware pin identifier."""
        return self._pin

    @abstractmethod
    def input(self) -> int:
        """
        Read the current state of the GPIO pin.
        
        Returns:
            0 for LOW/False, 1 for HIGH/True
        """
        raise NotImplementedError

    @abstractmethod
    def output(self, level: int | str) -> None:
        """
        Set the output state of the GPIO pin.

        Args:
            level: Output level - accepts:
                   - int: 0 = LOW, non-zero = HIGH
                   - str: "0"/"low"/"off" = LOW, "1"/"high"/"on" = HIGH
        """
        raise NotImplementedError

    def wait_for_level(
        self,
        level: int,
        timeout: float | None = None,
        poll_interval: float = 0.01,
        **kwargs,
    ) -> float:
        """
        Block until the pin reaches the target level.

        This default implementation polls ``self.input()`` in a loop.
        Subclasses (e.g. LabJackGPIO) may override with a hardware-
        accelerated streaming approach.

        Args:
            level: Target level (0 or 1).
            timeout: Maximum seconds to wait.  ``None`` means wait forever.
            poll_interval: Seconds between polls (default 10 ms).

        Returns:
            Elapsed time in seconds until the level was detected.

        Raises:
            TimeoutError: If *timeout* is exceeded before the level is seen.
        """
        start = time.monotonic()
        while True:
            if self.input() == level:
                return time.monotonic() - start
            elapsed = time.monotonic() - start
            if timeout is not None and elapsed >= timeout:
                raise TimeoutError(
                    f"GPIO '{self._name}' did not reach level {level} "
                    f"within {timeout}s"
                )
            time.sleep(poll_interval)
