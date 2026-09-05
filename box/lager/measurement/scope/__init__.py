# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Oscilloscope modules for Lager.

Provides interfaces for oscilloscope control and measurement. Supports Rigol
MSO5000 series over SCPI/VISA and PicoScope units through the oscilloscope
daemon; both drivers expose the same method surface, so callers do not branch
on which scope is attached.

``create_device`` remains the Rigol factory for backward compatibility --
existing net records name the module directly. Scope nets registered through
the ``scope`` role go through ``lager.scope_hs``, which picks the driver.
"""

from .rigol_mso5000 import RigolMso5000, create_device

# Alias for backward compatibility (supports both case variants)
RigolMSO5000 = RigolMso5000

__all__ = [
    'RigolMso5000',
    'RigolMSO5000',
    'PicoScope',
    'create_device',
]


def __getattr__(name):
    # Imported lazily so that `from lager.measurement.scope import ...` in a
    # Rigol-only context does not pull in the daemon client (and its
    # simple_websocket dependency), which a box image may not carry.
    if name == "PicoScope":
        from .picoscope import PicoScope
        return PicoScope
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
