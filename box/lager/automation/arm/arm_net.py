# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional, Tuple, Any


class ArmBackendError(Exception):
    """Base exception for arm backend errors."""
    pass

class MovementTimeoutError(ArmBackendError):
    """Raised when the arm did not reach the target in time.

    Can optionally include target coordinates and bounds information
    to help users understand why the movement failed.
    """

    def __init__(
        self,
        message: str = "Movement timed out",
        *,
        target_x: Optional[float] = None,
        target_y: Optional[float] = None,
        target_z: Optional[float] = None,
        bounds_hint: Optional[str] = None,
    ):
        # Build enhanced message if coordinates are provided
        parts = [message]

        if target_x is not None or target_y is not None or target_z is not None:
            coords = []
            if target_x is not None:
                coords.append(f"X={target_x}")
            if target_y is not None:
                coords.append(f"Y={target_y}")
            if target_z is not None:
                coords.append(f"Z={target_z}")
            parts.append(f"Requested position: {', '.join(coords)}.")

        if bounds_hint:
            parts.append(f"Approximate bounds: {bounds_hint}")

        full_message = " ".join(parts)
        super().__init__(full_message)

        # Store attributes for programmatic access
        self.target_x = target_x
        self.target_y = target_y
        self.target_z = target_z
        self.bounds_hint = bounds_hint

class LibraryMissingError(ArmBackendError):
    """Raised when a required library (e.g., VISA library) is missing."""
    pass

class DeviceNotFoundError(ArmBackendError):
    """Raised when the specified arm device cannot be found or opened."""
    pass

class ArmBase(ABC):
    """Abstract Arm net; do NOT instantiate directly."""

    def __init__(self, name: str, pin: int | str, location: dict[str, Any] | None = None) -> None:
        self._name = name
        self._pin = pin
        self._location = location or {}
    @property
    def name(self) -> str:
        return self._name

    @property
    def pin(self) -> int | str:
        return self._pin

    # Core reads/moves
    @property
    def location(self) -> dict[str, Any]:
        return self._location

    @abstractmethod
    def position(self) -> Tuple[float, float, float]:
        """Read the robot arm position."""
        raise NotImplementedError

    @abstractmethod
    def move_to(self, x: float, y: float, z: float, *, timeout: float = 15.0) -> None:
        """Move the robot arm to a position.

        Args:
            x, y, z: Target coordinates in mm
            timeout: Timeout in seconds (default: 15.0)
        """
        raise NotImplementedError

    # Quality-of-life ops
    @abstractmethod
    def move_relative(self, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0, *, timeout: float = 15.0) -> Tuple[float, float, float]:
        """Apply a delta move and return new position.

        Args:
            dx, dy, dz: Delta coordinates in mm
            timeout: Timeout in seconds (default: 15.0)
        """
        raise NotImplementedError

    @abstractmethod
    def go_home(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def enable_motor(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def disable_motor(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def save_position(self) -> None:
        raise NotImplementedError