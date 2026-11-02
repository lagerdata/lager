# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Siglent SDM3065X 6.5-digit bench DMM over USB-TMC or LAN (VISA).

Command forms, ranges and NPLC values are from Siglent's "SDM Series Digital
Multimeter Programming Guide" (EN02A), SDM3065X columns. The dialect is
34401A-like: ``CONFigure:<func> [<range>|AUTO]`` resets the function to its
defaults (auto-range, 10 PLC, 1 sample, immediate trigger), then
``<func>:NPLC`` adjusts integration time and ``READ?`` takes the reading.

Every ``measure()`` sends the full configuration. A reading then never
depends on what the front panel or a previous caller left selected. Skipping
the configuration when it already matches was considered and not done:
``CONFigure?`` reports function, range and resolution but not whether
auto-range is on or the NPLC, so confirming the meter's state takes three
queries (``CONF?``, ``<func>:RANGe:AUTO?``, ``<func>:NPLC?``), each a full
round trip, to save two writes that need no reply. The cost CONFigure does
add is an auto-range search on each auto-ranged reading; a caller polling one
signal passes a fixed range to avoid it.

``SYSTem:ERRor?`` is not in the EN02A guide, but the meter keeps an error
queue (``*CLS`` clears it). The parser accepts both ``+0,"No error"`` and the
``0  No Error`` form other Siglent instruments send. The driver probes the
query once at open with a short timeout; if the meter does not answer, error
checking is off for the session, so no reading waits out the full I/O
timeout. Any timed-out query is followed by a device clear, so a late reply
cannot be read as the answer to the next query.
"""

from __future__ import annotations

import logging
import math
import re
import warnings
from typing import TYPE_CHECKING, Any

import pyvisa

from lager.exceptions import DeviceNotFoundError, DMMBackendError
from lager.measurement.dmm.dmm_net import FUNCTIONS, DMMBase, DMMUnsupportedError
from lager.util.device_lock import device_lock

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

# Same pyvisa-py USBTMC MsgID warning the Rigol drivers silence; harmless.
warnings.filterwarnings(
    "ignore",
    message="Unexpected MsgID format",
    category=UserWarning,
    module="pyvisa_py.protocols.usbtmc",
)

MODEL = "SDM3065X"

# SCPI function node for each Lager function token.
_SCPI_FUNCTION = {
    "dcv": "VOLT:DC", "acv": "VOLT:AC", "dci": "CURR:DC", "aci": "CURR:AC",
    "res": "RES", "fres": "FRES", "cont": "CONT", "diode": "DIOD",
    "freq": "FREQ", "period": "PER", "cap": "CAP", "temp": "TEMP",
}

# Fixed ranges per function, in base units (V, A, ohm, F). Functions missing
# here have a fixed range (cont 2 kohm, diode 2 V) or none (freq, period, temp).
_RANGES = {
    "dcv": (0.2, 2.0, 20.0, 200.0, 1000.0),
    "acv": (0.2, 2.0, 20.0, 200.0, 750.0),
    "dci": (200e-6, 2e-3, 20e-3, 200e-3, 2.0, 10.0),
    "aci": (200e-6, 2e-3, 20e-3, 200e-3, 2.0, 10.0),
    "res": (200.0, 2e3, 20e3, 200e3, 1e6, 10e6, 100e6),
    "fres": (200.0, 2e3, 20e3, 200e3, 1e6, 10e6, 100e6),
    "cap": (2e-9, 20e-9, 200e-9, 2e-6, 20e-6, 200e-6, 2e-3, 20e-3, 100e-3),
}

# NPLC applies to the integrating functions only; the guide lists no NPLC
# command for AC, frequency, capacitance or temperature.
_NPLC_VALUES = (0.005, 0.05, 0.5, 1.0, 10.0, 100.0)

# CONFigure:TEMPerature {RTD|THER},<type>. THER types are ITS-90 tables.
_TEMP_SENSORS = {
    "rtd:pt100": "RTD,PT100",
    "rtd:pt1000": "RTD,PT1000",
    **{f"ther:{t}": f"THER,{t.upper()}ITS90" for t in "bejknrst"},
}

# The meter answers +9.90000000E+37 (or 9.9E37) for overload and for an open
# continuity or diode input. Compare with a margin rather than for equality.
_OVERLOAD = 9.9e37
_OVERLOAD_MARGIN = 1e36

# Upper bound on SYSTem:ERRor? reads per drain, so a meter that never says
# "no error" cannot hang a request.
_MAX_ERROR_READS = 20

# VISA I/O timeout. See PROXY_TIMEOUT_S in dmm_net for the budget.
_VISA_TIMEOUT_MS = 30_000

# Timeout for the one SYST:ERR? probe at open. A meter that implements the
# query answers in milliseconds; this only bounds the wait when it does not.
_PROBE_TIMEOUT_MS = 2_000

_ERROR_REPLY = re.compile(r'^\s*([+-]?\d+)\s*,?\s*"?(.*?)"?\s*$')


def _close_to(value: float, candidates: tuple[float, ...]) -> float | None:
    """The candidate equal to ``value`` within float rounding, else None."""
    return next((c for c in candidates if math.isclose(value, c, rel_tol=1e-6)), None)


def _format_si(values: tuple[float, ...]) -> str:
    return ", ".join(f"{v:g}" for v in values)


def _parse_range(value: float | str | None) -> float | None:
    """Full-scale value from a request, or None for auto-range."""
    if value is None:
        return None
    if isinstance(value, str):
        if value.strip().lower() == "auto":
            return None
        try:
            return float(value)
        except ValueError as exc:
            raise DMMUnsupportedError(f"Range must be a number or 'auto', got '{value}'") from exc
    return float(value)


def _configure_command(node: str, function: str, range: float | str | None,
                       sensor: str | None) -> str:
    """The ``CONFigure`` command, with a validated range or temperature probe."""
    full_scale = _parse_range(range)
    if full_scale is not None:
        allowed = _RANGES.get(function)
        if allowed is None:
            raise DMMUnsupportedError(f"The {MODEL} has no selectable range for '{function}'")
        matched = _close_to(full_scale, allowed)
        if matched is None:
            raise DMMUnsupportedError(
                f"The {MODEL} has no {full_scale:g} range for '{function}'. "
                f"Ranges: {_format_si(allowed)}, or auto")
        return f"CONF:{node} {matched:g}"
    if sensor is None:
        return f"CONF:{node}"
    if function != "temp":
        raise DMMUnsupportedError("A sensor applies to temperature ('temp') only")
    probe = _TEMP_SENSORS.get(sensor.strip().lower())
    if probe is None:
        raise DMMUnsupportedError(
            f"Unknown temperature sensor '{sensor}'. Use one of: {', '.join(_TEMP_SENSORS)}")
    return f"CONF:{node} {probe}"


def _nplc_command(node: str, function: str, nplc: float | str) -> str:
    """The ``<func>:NPLC`` command for a validated integration time."""
    if function not in ("dcv", "dci", "res", "fres"):
        raise DMMUnsupportedError(
            f"The {MODEL} has no NPLC setting for '{function}'; "
            "it applies to dcv, dci, res and fres")
    try:
        cycles = float(nplc)
    except ValueError as exc:
        raise DMMUnsupportedError(f"NPLC must be a number, got '{nplc}'") from exc
    matched = _close_to(cycles, _NPLC_VALUES)
    if matched is None:
        raise DMMUnsupportedError(
            f"The {MODEL} does not support NPLC {cycles:g}. Values: {_format_si(_NPLC_VALUES)}")
    return f"{node}:NPLC {matched:g}"


def build_commands(
    function: str,
    range: float | str | None = None,
    nplc: float | None = None,
    sensor: str | None = None,
) -> list[str]:
    """SCPI configuration commands for one reading, after validating the request.

    Pure, so the box handler can reject a bad request before touching the
    instrument and tests can check command forms without a transport.

    Raises:
        DMMUnsupportedError: The SDM3065X cannot do what was asked.
    """
    if function not in FUNCTIONS:
        raise DMMUnsupportedError(
            f"Unknown DMM function '{function}'. Use one of: {', '.join(FUNCTIONS)}")
    node = _SCPI_FUNCTION[function]
    if sensor is not None and function != "temp":
        raise DMMUnsupportedError("A sensor applies to temperature ('temp') only")
    commands = [_configure_command(node, function, range, sensor)]
    if nplc is not None:
        commands.append(_nplc_command(node, function, nplc))

    if function == "temp":
        commands.append("UNIT:TEMP C")
    return commands


def parse_reading(reply: str) -> float | None:
    """A ``READ?`` reply as a float, or None for the overload/open sentinel.

    Raises:
        DMMBackendError: The reply is not a number.
    """
    try:
        value = float(reply.strip())
    except ValueError as exc:
        raise DMMBackendError(f"{MODEL} sent a reading that is not a number: {reply!r}") from exc
    if abs(abs(value) - _OVERLOAD) < _OVERLOAD_MARGIN or not math.isfinite(value):
        return None
    return value


def parse_error(reply: str) -> tuple[int, str]:
    """``(code, message)`` from a ``SYSTem:ERRor?`` reply; code 0 means no error.

    Accepts ``+0,"No error"`` and ``0  No Error``. An unparseable reply comes
    back as code -1 with the raw text, so it is reported rather than dropped.
    """
    match = _ERROR_REPLY.match(reply)
    if match is None:
        return -1, reply.strip()
    return int(match.group(1)), match.group(2).strip()


class SiglentSDM3065X(DMMBase):
    """Driver for one SDM3065X (also matches the SDM3065X-SC).

    ``resource`` is a pyvisa message-based resource; tests pass a fake one.
    Without it the driver opens ``net_info["address"]`` itself.
    """

    def __init__(self, net_info: Mapping[str, Any], resource: Any = None) -> None:
        self.net_info = dict(net_info)
        self.address = self.net_info.get("address")
        self._error_queue_ok = True
        if resource is None:
            if not self.address:
                raise DMMBackendError(f"No VISA address set for {MODEL} net "
                                      f"'{self.net_info.get('name')}'")
            resource = self._open(self.address)
        self._resource = resource
        idn = self.identify()
        if MODEL not in idn.upper():
            raise DeviceNotFoundError(f"Device at {self.address} is not a Siglent {MODEL}: {idn}")
        self._probe_error_queue()

    @classmethod
    def validate(
        cls,
        function: str,
        range: float | str | None = None,
        nplc: float | None = None,
        sensor: str | None = None,
    ) -> None:
        """Check a request against the SDM3065X; see ``DMMBase.validate``."""
        build_commands(function, range=range, nplc=nplc, sensor=sensor)

    @staticmethod
    def _open(address: str) -> Any:
        # Same cross-process advisory lock the other VISA drivers take around
        # the open (see power/battery/keithley.py).
        try:
            with device_lock(address, timeout=2.0):
                resource = pyvisa.ResourceManager().open_resource(address)
        except pyvisa.errors.VisaIOError as exc:
            raise DMMBackendError(f"Could not open {MODEL} at {address}: {exc}") from exc
        resource.timeout = _VISA_TIMEOUT_MS
        resource.read_termination = "\n"
        resource.write_termination = "\n"
        return resource

    def _clear(self) -> None:
        """Device clear after a timeout, discarding any reply still in flight."""
        try:
            self._resource.clear()
        except pyvisa.errors.VisaIOError as exc:
            logger.warning("Device clear on %s at %s failed: %s", MODEL, self.address, exc)

    def _probe_error_queue(self) -> None:
        """Ask ``SYST:ERR?`` once with a short timeout; turn checking off if unanswered."""
        saved = self._resource.timeout
        self._resource.timeout = _PROBE_TIMEOUT_MS
        try:
            self._resource.query("SYST:ERR?")
        except pyvisa.errors.VisaIOError as exc:
            self._error_queue_ok = False
            self._clear()
            logger.warning("%s at %s did not answer SYST:ERR? (%s); "
                           "error checking is off for this session", MODEL, self.address, exc)
        finally:
            self._resource.timeout = saved

    def _write(self, command: str) -> None:
        try:
            self._resource.write(command)
        except pyvisa.errors.VisaIOError as exc:
            raise DMMBackendError(f"{MODEL} write '{command}' failed: {exc}") from exc

    def _query(self, command: str) -> str:
        try:
            return str(self._resource.query(command)).strip()
        except pyvisa.errors.VisaIOError as exc:
            self._clear()
            raise DMMBackendError(f"{MODEL} query '{command}' failed: {exc}") from exc

    def identify(self) -> str:
        """Return the ``*IDN?`` reply."""
        return self._query("*IDN?")

    def errors(self) -> list[str]:
        """Drain ``SYSTem:ERRor?``; empty when clear or when the meter cannot answer it."""
        if not self._error_queue_ok:
            return []
        found = []
        for _ in range(_MAX_ERROR_READS):
            try:
                reply = str(self._resource.query("SYST:ERR?")).strip()
            except pyvisa.errors.VisaIOError as exc:
                self._error_queue_ok = False
                self._clear()
                logger.warning("%s at %s did not answer SYST:ERR? (%s); "
                               "error checking is off for this session", MODEL, self.address, exc)
                break
            code, message = parse_error(reply)
            if code == 0:
                break
            found.append(f"{code}, {message}" if message else str(code))
        return found

    def measure(
        self,
        function: str,
        range: float | str | None = None,
        nplc: float | None = None,
        sensor: str | None = None,
    ) -> float | None:
        """Configure and take one reading; see ``DMMBase.measure``."""
        commands = build_commands(function, range=range, nplc=nplc, sensor=sensor)
        self._write("*CLS")
        for command in commands:
            self._write(command)
        # Checked before READ? so a rejected range or NPLC is reported as the
        # meter's own error, not as a reading taken with the wrong settings.
        rejected = self.errors()
        if rejected:
            raise DMMBackendError(
                f"{MODEL} rejected '{'; '.join(commands)}': {'; '.join(rejected)}")
        return parse_reading(self._query("READ?"))

    def close(self) -> None:
        """Close the VISA session."""
        try:
            self._resource.close()
        except pyvisa.errors.VisaIOError as exc:
            logger.warning("Closing %s at %s failed: %s", MODEL, self.address, exc)
