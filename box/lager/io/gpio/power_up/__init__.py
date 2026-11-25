# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Declared power-up levels for gpio nets, by instrument family.

``power_up`` is a property of the net. Which instruments can honour it, and
how, is decided here: each family names the instruments it matches and the
module holding its backend (see ``base.PowerUpBackend``). Matching needs no
backend import, so the nets HTTP handler can gate a save without loading any
vendor library; the backend module is imported only when a check or an apply
runs in hardware_service.
"""
from __future__ import annotations

import importlib
import re
from typing import NamedTuple, Optional

from .base import (LEVELS, PowerUpBackend, PowerUpError, iter_declared,
                   normalize_level, public)


class Family(NamedTuple):
    name: str
    #: How the error message names the instruments ("LabJack T7 and U3").
    label: str
    pattern: "re.Pattern[str]"
    module: str


FAMILIES = (
    # Matches the GPIO dispatcher's own LabJack tests.
    Family("labjack", "LabJack T7 and U3",
           re.compile(r"labjack[_\-\s]*(t7|u3)", re.IGNORECASE),
           "lager.io.gpio.power_up.labjack"),
)


def family_for(instrument) -> Optional[Family]:
    """The family that can hold a power-up level for ``instrument``, or None."""
    text = str(instrument or "")
    for family in FAMILIES:
        if family.pattern.search(text):
            return family
    return None


def backend_for(instrument) -> Optional[PowerUpBackend]:
    """The backend for ``instrument``'s family, or None if it has none."""
    family = family_for(instrument)
    if family is None:
        return None
    return importlib.import_module(family.module).BACKEND


def supported_label() -> str:
    """Every family's label, for "power_up is supported on ..." messages."""
    return ", ".join(f.label for f in FAMILIES)


__all__ = ["FAMILIES", "Family", "family_for", "backend_for", "supported_label",
           "LEVELS", "PowerUpBackend", "PowerUpError", "iter_declared",
           "normalize_level", "public"]
