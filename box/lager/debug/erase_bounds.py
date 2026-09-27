# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The bounds of a flash erase, shared by every path that erases through a probe.

``lager debug <net> erase`` and the pre-erase in ``lager debug <net> flash``
take ``--erase-start`` / ``--erase-size``; ``DebugNet.erase()`` takes
``start`` / ``length``. The J-Link path (``api.chip_erase``) and the OpenOCD
path (``openocd_flash.erase_target``) both check a requested range here, so
the two backends and the HTTP service cannot disagree about what a valid
range is, and both report the range they erased in one format.

``jlink.py`` does not import this module: tests load it standalone, so it may
not import from ``lager.*``. It resolves the DA1469x default and the
``LAGER_ERASE_RANGE`` script line on its own (``jlink.resolve_erase_range``).
"""

import math

from .da1469x_loader import QSPI_XIP_BASE, QSPI_XIP_END

KIB = 1 << 10
MIB = 1 << 20

#: The largest ``start + length`` a 32-bit target can address.
ADDRESS_SPACE = 1 << 32


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_bounds(start, length, *, da1469x):
    """Raise ``ValueError`` unless ``[start, start + length)`` is a range a target can erase.

    *da1469x* selects the family rule: that part erases only inside its QSPI
    XIP window, ``0x16000000-0x17FFFFFF``, the window the flash_loader refuses
    to write outside of.
    """
    if not _is_int(start) or not _is_int(length):
        raise ValueError('erase start and size must be integers')
    if start < 0:
        raise ValueError(f'erase start must not be negative, got {start}')
    if length <= 0:
        raise ValueError(f'erase size must be greater than 0, got {length}')
    if start + length > ADDRESS_SPACE:
        raise ValueError(
            f'erase range {format_bounds(start, length)} runs past the end '
            f'of the 32-bit address space'
        )
    if da1469x and not (QSPI_XIP_BASE <= start and start + length <= QSPI_XIP_END):
        raise ValueError(
            f'erase range {format_bounds(start, length)} is outside the '
            f'DA1469x QSPI XIP window '
            f'(0x{QSPI_XIP_BASE:08X}-0x{QSPI_XIP_END - 1:08X})'
        )


def format_size(length):
    """``2 MiB``, ``512 KiB`` or ``4100 bytes``: exact, never rounded."""
    if length % MIB == 0:
        return f'{length // MIB} MiB'
    if length % KIB == 0:
        return f'{length // KIB} KiB'
    return f'{length} bytes'


def format_bounds(start, length):
    """``0x16000000-0x161FFFFF (2 MiB)``: inclusive end, size in words."""
    end = start + length - 1
    return f'0x{start:08X}-0x{end:08X} ({format_size(length)})'


def bounds_dict(start, length, source):
    """The range a backend erased, as ``/debug/erase`` reports it.

    *source* says where the range came from: ``request`` (the caller),
    ``script`` (a ``LAGER_ERASE_RANGE`` line, J-Link only) or ``default``
    (the family default).
    """
    return {
        'start': start,
        'end': start + length - 1,
        'length': length,
        'source': source,
        'text': format_bounds(start, length),
    }


def scaled_timeout_s(base_s, length):
    """*base_s* per MiB of *length*, and never less than *base_s*.

    Every erase timeout was sized for the 1 MiB DA1469x default; a range n
    times larger gets n times the budget.
    """
    return base_s * max(1, math.ceil(length / MIB))
