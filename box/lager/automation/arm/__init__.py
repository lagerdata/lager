# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Arm module for robotic arm control.

This module provides interfaces for controlling robotic arms,
currently supporting the Rotrics Dexarm.

Example usage:
    from lager.automation.arm import Dexarm

    # Create arm instance
    arm = Dexarm(port="/dev/ttyUSB0")

    # Get current position
    x, y, z = arm.position()

    # Move to a specific position
    arm.move_to(200, 0, 100)

    # Move relative
    arm.move_relative(dx=10, dy=0, dz=0)
"""

from .arm_net import (
    ArmBase,
    ArmBackendError,
    MovementTimeoutError,
    LibraryMissingError,
    DeviceNotFoundError,
)
from .rotrics import Dexarm

# Backward-compatible alias: RotricsArm -> Dexarm
# Rotrics is the company name, Dexarm is the product name
RotricsArm = Dexarm

__all__ = [
    # Base class
    'ArmBase',
    # Exceptions
    'ArmBackendError',
    'MovementTimeoutError',
    'LibraryMissingError',
    'DeviceNotFoundError',
    # Implementations
    'Dexarm',
    'RotricsArm',  # Alias for backward compatibility
]
