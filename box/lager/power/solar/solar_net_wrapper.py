# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
What ``Net.get(name, type=NetType.Solar)`` returns.

A solar net drives an EA PSB supply in its photovoltaic simulation mode. The
calls go to the hardware service's ``solar_hs`` adapter, the same one
``lager solar`` uses, so a script serializes under the instrument's VISA
address lock with the CLI and with any supply net on the same unit, rather
than opening a second session to it.

The adapter answers with the driver's display strings ("1.234 A", "25.0 C",
"n/a"). The reads here return the number, or ``None`` when the instrument gave
no number, so a script can compare them without parsing.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from lager.exceptions import SolarBackendError

_NUMBER = re.compile(r"^\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")

# The same bounds `lager solar` applies before it touches the instrument.
_MAX_IRRADIANCE = 1500.0


def _number(text: Any) -> Optional[float]:
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    m = _NUMBER.match(str(text or ""))
    return float(m.group(1)) if m else None


class SolarNetWrapper:
    """A saved solar net, driven through the hardware service."""

    # Starting and stopping PV simulation retries while the unit settles, so
    # it can take far longer than a read; these match the `lager solar` route.
    _MODE_TIMEOUT_S = 90.0
    _READ_TIMEOUT_S = 60.0

    def __init__(self, name: str, record: dict):
        from lager.dispatchers.helpers import _solar_module_for_instrument
        from lager.nets.device_identity import address_from_rec

        self.name = name
        self.record = dict(record or {})
        self.instrument = self.record.get("instrument") or ""
        self.address = address_from_rec(self.record)
        if not self.address:
            raise SolarBackendError("Net '%s' has no device address." % name)
        self._module = _solar_module_for_instrument(self.instrument)
        if self._module is None:
            raise SolarBackendError(
                "Unsupported solar instrument '%s' for net '%s'." % (self.instrument, name))

    def _device(self, timeout: float):
        from lager.nets.device import Device
        net_info = {"name": self.name, "address": self.address,
                    "instrument": self.instrument}
        return Device(self._module, net_info, timeout=timeout)

    # -- simulation mode ---------------------------------------------------
    def enable(self) -> None:
        """Connect and start photovoltaic simulation mode."""
        self._device(self._MODE_TIMEOUT_S).set_mode()

    def disable(self) -> None:
        """Stop the simulation and disconnect."""
        self._device(self._MODE_TIMEOUT_S).stop_mode()

    # The module functions' names, so a script can move between the two.
    set_to_solar_mode = enable
    stop_solar_mode = disable

    # -- settings and readings ----------------------------------------------
    def irradiance(self, value: Optional[float] = None) -> Optional[float]:
        """Set the irradiance (W/m^2) when given; return the irradiance."""
        if value is not None:
            value = float(value)
            if value < 0:
                raise ValueError("Irradiance cannot be negative, got: %s W/m^2" % value)
            if value > _MAX_IRRADIANCE:
                raise ValueError(
                    "Irradiance too high (max %g W/m^2), got: %s W/m^2"
                    % (_MAX_IRRADIANCE, value))
        return _number(self._device(self._READ_TIMEOUT_S).irradiance(value))

    def resistance(self, value: Optional[float] = None) -> Optional[float]:
        """Set the dynamic panel resistance (ohm) when given; return it."""
        if value is not None:
            value = float(value)
            if value <= 0:
                raise ValueError("Resistance must be positive, got: %s Ohm" % value)
        return _number(self._device(self._READ_TIMEOUT_S).resistance(value))

    def mpp_current(self) -> Optional[float]:
        """The current at the maximum power point (A)."""
        return _number(self._device(self._READ_TIMEOUT_S).mpp_current())

    def mpp_voltage(self) -> Optional[float]:
        """The voltage at the maximum power point (V)."""
        return _number(self._device(self._READ_TIMEOUT_S).mpp_voltage())

    def temperature(self) -> Optional[float]:
        """The simulated cell temperature (C)."""
        return _number(self._device(self._READ_TIMEOUT_S).temperature())

    def voc(self) -> Optional[float]:
        """The open-circuit voltage (V)."""
        return _number(self._device(self._READ_TIMEOUT_S).voc())

    def __repr__(self) -> str:
        return "<SolarNetWrapper name=%r instrument=%r>" % (self.name, self.instrument)
