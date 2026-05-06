# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Oscilloscope modules for Lager.

Provides interfaces for oscilloscope control and measurement.
Currently supports Rigol MSO5000 series oscilloscopes.
"""

from .rigol_mso5000 import RigolMso5000, create_device

# Alias for backward compatibility (supports both case variants)
RigolMSO5000 = RigolMso5000

__all__ = [
    'RigolMso5000',
    'RigolMSO5000',
    'create_device',
]
