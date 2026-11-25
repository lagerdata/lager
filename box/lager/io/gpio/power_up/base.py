# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
What every power-up backend shares.

A gpio net can declare ``power_up: "low" | "high"``: the level its pin should
take when the instrument behind it powers up. How that level is made true
depends on the instrument family, so each family supplies a backend:

* ``storage = "flash"``: the instrument keeps the level itself and applies it
  as it boots, before any host software runs (LabJack T7 and U3).
* ``storage = "host"``: the instrument keeps nothing, so the box drives the
  level once the instrument is back on USB.

Either way the pin is undriven for a while after power is applied. A hardware
pull-down (or pull-up) on the line is the only cover for that window, and each
backend's ``coverage`` text says how long the window lasts.

This module imports nothing from the backends, so a backend's own vendor module
can import these helpers without an import cycle.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

LEVELS = ("low", "high")


class PowerUpError(RuntimeError):
    """A power-up level cannot be checked or applied as asked."""


def normalize_level(value) -> Optional[str]:
    """``"low"``/``"high"`` for a declared level, ``None`` for unset.

    Raises ValueError for anything else. No default: LOW is the asserting
    level of an active-low line, and an input net must stay undeclared.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in LEVELS:
        return text
    raise ValueError(f"power_up must be 'low' or 'high', got {value!r}")


def iter_declared(entries: Iterable[Tuple[int, str, str]]):
    """``(declared {pin: level}, nets {pin: name})``; refuses two nets on one pin
    that declare different levels."""
    declared: Dict[int, str] = {}
    names: Dict[int, str] = {}
    for pin, level, name in entries:
        if pin in declared and declared[pin] != level:
            raise PowerUpError(
                f"nets '{names[pin]}' and '{name}' share a pin but declare "
                f"different power-up levels")
        declared[pin] = level
        names.setdefault(pin, name)
    return declared, names


def public(plan: Dict[str, Any], nets: Dict[int, str]) -> Dict[str, Any]:
    """The JSON-safe report for one device: pins keyed by net, not pin number."""
    out = {k: v for k, v in plan.items() if not k.startswith("_") and k != "pins"}
    out["pins"] = [dict(info, net=nets.get(dio), dio=dio)
                   for dio, info in sorted(plan["pins"].items())]
    return out


class PowerUpBackend:
    """One instrument family's way of making declared power-up levels true.

    ``run`` executes inside hardware_service, under the device's ``/invoke``
    lock, with ``nets`` = ``[{"name", "power_up"}, ...]`` for one physical
    device. It returns that device's report: ``model``, ``serial``, ``pins``
    (``[{"net", "dio", "dio_name", "declared", "stored"}, ...]``),
    ``in_sync`` and ``written``, plus any family-specific keys.
    """

    #: Short family key, reported as ``backend``.
    family: str = ""
    #: What the CLI calls a device of this family ("LabJack").
    label: str = ""
    #: ``"flash"`` or ``"host"``; see the module docstring.
    storage: str = ""
    #: One sentence on what covers the pin, and when it does not.
    coverage: str = ""

    def validate(self, record: Dict[str, Any]) -> Optional[str]:
        """Why this gpio net cannot declare a level, or None. Runs at save time."""
        return None

    def run(self, mode: str, nets: List[Dict[str, Any]],
            allow_undeclared: bool = False) -> Dict[str, Any]:
        raise NotImplementedError

    def describe(self) -> Dict[str, str]:
        return {"backend": self.family, "storage": self.storage,
                "coverage": self.coverage}


__all__ = ["LEVELS", "PowerUpError", "normalize_level", "iter_declared",
           "public", "PowerUpBackend"]
