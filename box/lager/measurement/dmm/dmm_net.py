# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Bench digital multimeter (DMM) interface.

A DMM net is one meter. Every reading is one ``measure()`` call that
configures the function (and optional range, NPLC, temperature sensor) and
takes a single reading, so concurrent requests on the same meter cannot
interleave a configure from one caller with a read from another.

Function names are the same short tokens across drivers, the box HTTP
handler and the CLI (``FUNCTIONS``). A driver that cannot do a function, a
range, an NPLC value or a sensor raises ``DMMUnsupportedError``; it never
substitutes something close.

An over-range or open-input reading is returned as ``None``, never as the
meter's sentinel number.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from lager.nets.device import Device

#: Measurement functions, in the order the CLI lists them.
FUNCTIONS = (
    "dcv", "acv", "dci", "aci", "res", "fres", "cont", "diode",
    "freq", "period", "cap", "temp",
)

#: Unit of the value ``measure()`` returns for each function. Temperature is
#: always degrees Celsius; drivers set the meter's unit to match.
UNITS = {
    "dcv": "V", "acv": "V", "dci": "A", "aci": "A",
    "res": "ohm", "fres": "ohm", "cont": "ohm", "diode": "V",
    "freq": "Hz", "period": "s", "cap": "F", "temp": "C",
}

#: hardware_service proxy timeout for one measure() call, in seconds. Sized
#: past the driver's 30 s VISA timeout: a 100 PLC reading at 50 Hz is 2 s,
#: auto-ranging can take several readings, and the top capacitance ranges
#: take seconds per reading.
PROXY_TIMEOUT_S = 60.0


class DMMUnsupportedError(ValueError):
    """The meter cannot do the requested function, range, NPLC or sensor.

    A ValueError so the box's /net/command handler answers 400 with the
    message instead of reporting a hardware fault.
    """


class DMMBase(ABC):
    """Driver interface every DMM implements; do not instantiate directly."""

    @classmethod
    @abstractmethod
    def validate(
        cls,
        function: str,
        range: float | str | None = None,
        nplc: float | None = None,
        sensor: str | None = None,
    ) -> None:
        """Check a ``measure()`` request without opening the instrument.

        Called before a request reaches hardware_service, which reports a
        driver exception as ``DeviceError``.

        Raises:
            DMMUnsupportedError: The meter cannot do what was asked.
        """

    @abstractmethod
    def measure(
        self,
        function: str,
        range: float | str | None = None,
        nplc: float | None = None,
        sensor: str | None = None,
    ) -> float | None:
        """Configure ``function`` and return one reading in ``UNITS[function]``.

        ``range`` is the full-scale value in the function's unit, or None or
        "auto" for auto-range. ``nplc`` is the integration time in power-line
        cycles. ``sensor`` selects the temperature probe ("rtd:pt100",
        "ther:k", ...). Returns None when the meter reports overload or an
        open input.

        Raises:
            DMMUnsupportedError: The meter cannot do what was asked.
            DMMBackendError: The meter rejected a command or did not answer.
        """

    @abstractmethod
    def errors(self) -> list[str]:
        """Drain and return the meter's error queue; empty when it is clear."""

    @abstractmethod
    def identify(self) -> str:
        """Return the meter's ``*IDN?`` reply."""

    def close(self) -> None:
        """Release the instrument session, if the driver holds one."""


class DMMNet:
    """The object ``Net.get(name, NetType.DMM)`` returns.

    Forwards to the driver inside hardware_service, which owns the VISA
    session and serializes calls per instrument address, so a ``lager
    python`` script and a CLI read never open the meter twice.

    ``validate`` checks a request against the instrument before the proxy
    call. An exception raised inside hardware_service reaches the caller as
    a ``DeviceError``, so this is what lets ``measure()`` raise
    ``DMMUnsupportedError`` itself. A meter failure (``DMMBackendError`` in
    the driver) still arrives as ``DeviceError``.
    """

    def __init__(
        self,
        name: str,
        device: Device,
        validate: Callable[..., None] | None = None,
    ) -> None:
        self.name = name
        self._device = device
        self._validate = validate

    def measure(
        self,
        function: str,
        range: float | str | None = None,
        nplc: float | None = None,
        sensor: str | None = None,
    ) -> float | None:
        """Take one reading; see ``DMMBase.measure``."""
        if function not in FUNCTIONS:
            raise DMMUnsupportedError(
                f"Unknown DMM function '{function}'. Use one of: {', '.join(FUNCTIONS)}")
        if self._validate is not None:
            self._validate(function, range=range, nplc=nplc, sensor=sensor)
        value = self._device.measure(function, range=range, nplc=nplc, sensor=sensor)
        return None if value is None else float(value)

    def errors(self) -> list[str]:
        """Drain the meter's error queue."""
        return list(self._device.errors())

    def identify(self) -> str:
        """Return the meter's ``*IDN?`` reply."""
        return str(self._device.identify())

    def __str__(self) -> str:
        return f"<DMMNet {self.name}>"
