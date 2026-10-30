# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Backend driver for the Siglent SPD1000X series (SPD1168X, SPD1305X).

Single-channel linear supplies with SCPI over USB-TMC or LAN (VXI-11
``TCPIP::<ip>::INSTR``, or a raw socket on port 5025). The method surface
matches ``RigolDP800`` / ``RigolDP700`` so the supply dispatcher, the
HTTP/WebSocket handlers and the DP800 net mapper drive it unchanged.

SCPI dialect (SPD1000X programming guide; cross-checked against pymeasure's
SPD1168X driver):

  - Setpoints: ``CH1:VOLTage <v>``, ``CH1:CURRent <i>`` and their ``?`` forms.
  - Readback: ``MEASure:VOLTage? CH1``, ``MEASure:CURRent? CH1``,
    ``MEASure:POWEr? CH1``.
  - Output: ``OUTPut CH1,ON|OFF``. There is no output query; on/off and CV/CC
    come from ``SYSTem:STATus?``, a hex bitfield (``STATUS_*`` below).
  - ``SYSTem:ERRor?`` replies ``0  No Error`` style, without the SCPI comma,
    so ``InstrumentWrap``'s error check cannot parse it and is not used.
  - No OVP/OCP subsystem. The protection methods raise ``SupplyBackendError``
    and the state reports say "unsupported" instead of showing a limit.

Timing: Siglent supplies drop or garble commands sent back to back, and some
firmware does not implement ``*OPC?``, so completion cannot be polled. Every
transaction is spaced at least ``COMMAND_INTERVAL_S`` after the previous one
(Siglent recommends 10-100 ms), and an output toggle waits
``OUTPUT_SETTLE_S`` before the next command. The pacing is per driver
instance: the supply dispatcher cache and hardware_service's ``create_device``
can each hold an instance for the same address, and commands from the two are
not paced against each other.

Readback lags setpoints: on an SPD1168X (firmware 2.1.1.9R1) the measured
values update about every 0.34 s, so a ``MEASure`` sent within that time of a
setpoint change returns the old value. A falling step into no load takes
1-1.4 s to reach the new value.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

try:
    import pyvisa  # type: ignore
except (ImportError, ModuleNotFoundError):  # pragma: no cover - box always has it
    pyvisa = None

from lager.util.device_lock import device_lock

from .supply_net import (
    DeviceNotFoundError,
    LibraryMissingError,
    SupplyBackendError,
    SupplyNet,
)

logger = logging.getLogger(__name__)

# ANSI color codes (match the other supply drivers' output formatting)
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

# Minimum gap between two SCPI transactions, in seconds.
COMMAND_INTERVAL_S = 0.05
# Extra wait after OUTPut ON/OFF before the next command, in seconds.
OUTPUT_SETTLE_S = 0.2

# SYSTem:STATus? bits. Bit 0 clear means CV.
STATUS_CC = 1 << 0
STATUS_OUTPUT_ON = 1 << 4
STATUS_FOUR_WIRE = 1 << 5

IDN_PATTERN = r"siglent.*,\s*(SPD1\d{3}X)\s*,"

# Rated output per model, from the IDN model field.
MODEL_LIMITS = {
    "SPD1168X": {"voltage_max": 16.0, "current_max": 8.0},
    "SPD1305X": {"voltage_max": 30.0, "current_max": 5.0},
}

UNSUPPORTED_PROTECTION = "is not supported: the SPD1000X has no OVP/OCP SCPI commands"


def _protection_unsupported(feature: str) -> SupplyBackendError:
    return SupplyBackendError(f"{feature} {UNSUPPORTED_PROTECTION}")


class _PacedSession:
    """pyvisa resource wrapper that enforces ``COMMAND_INTERVAL_S``.

    Exposed as the driver's ``instr`` so the ``*IDN?`` liveness probe in
    ``SupplyNet.get_monitor_state`` is paced like every other command.
    ``check_errors`` is accepted for ``InstrumentWrap`` call compatibility
    and ignored.
    """

    def __init__(self, raw: Any, interval_s: float = COMMAND_INTERVAL_S):
        self.instr = raw
        self._interval_s = interval_s
        self._last = 0.0

    def _pace(self) -> None:
        wait = self._last + self._interval_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def write(self, cmd: str, *, check_errors: bool = False) -> None:
        self._pace()
        try:
            self.instr.write(cmd)
        finally:
            self._last = time.monotonic()

    def query(self, cmd: str, *, check_errors: bool = False) -> str:
        self._pace()
        try:
            return str(self.instr.query(cmd)).strip()
        finally:
            self._last = time.monotonic()

    def close(self) -> None:
        self.instr.close()


def _open_resource(address: str) -> tuple[Any, Any]:
    """Open ``address``, trying the default VISA backend and then pyvisa-py."""
    if pyvisa is None:
        raise LibraryMissingError("PyVISA is not installed on this box.")
    last_exc: Optional[Exception] = None
    for backend in (None, "@py"):
        try:
            rm = pyvisa.ResourceManager() if backend is None else pyvisa.ResourceManager(backend)
            with device_lock(address, timeout=2.0):
                raw = rm.open_resource(address)
            break
        except Exception as exc:  # noqa: BLE001 - try the next backend
            last_exc = exc
    else:
        raise DeviceNotFoundError(f"Could not open SPD1000X at {address}: {last_exc}") from last_exc

    # The SPD rejects any terminator other than a bare LF.
    try:
        raw.write_termination = "\n"
        raw.read_termination = "\n"
        raw.timeout = 5000
    except Exception:
        pass
    return raw, rm


def parse_error_reply(reply: str) -> tuple[int, str]:
    """Split a ``SYSTem:ERRor?`` reply into (code, message).

    Accepts the SPD's ``0  No Error`` form and the SCPI ``0,"No error"`` form.
    An unparseable reply returns code -1 with the reply as the message.
    """
    m = re.match(r"\s*([-+]?\d+)\s*,?\s*(.*)$", reply or "")
    if not m:
        return -1, (reply or "").strip()
    return int(m.group(1)), m.group(2).strip().strip('"')


def parse_status(reply: str) -> Optional[int]:
    """Parse a ``SYSTem:STATus?`` hex reply (``0x10`` or ``10``); None if garbled."""
    try:
        return int(str(reply).strip(), 16)
    except (TypeError, ValueError):
        return None


class SiglentSPD1000X(SupplyNet):
    """Single-channel Siglent SPD1000X (SPD1168X / SPD1305X) supply backend."""

    def __init__(self, address: Optional[str] = None, channel: int = 1,
                 instr: Any = None, **_: Any):
        if int(channel or 1) != 1:
            raise SupplyBackendError(
                f"Requested channel {channel} not available on instrument (supports 1 channel)."
            )
        self.channel = 1
        self._rm = None
        if instr is not None:
            raw = instr
        elif address:
            raw, self._rm = _open_resource(address)
        else:
            raise SupplyBackendError("SPD1000X requires a VISA address or an open VISA resource.")
        self.instr = _PacedSession(raw)
        self._model = ""
        self.check_instrument()

    # IO

    def _write(self, cmd: str) -> None:
        self.instr.write(cmd)

    def _query(self, cmd: str) -> str:
        return self.instr.query(cmd)

    def _safe_query(self, cmd: str, default: str = "n/a") -> str:
        try:
            return self._query(cmd)
        except (NameError, AttributeError, TypeError, ImportError):
            # Programming errors must surface, not read as a dead instrument.
            raise
        except Exception:
            return default

    def _float_query(self, cmd: str) -> float:
        """Query a number; raises on IO failure or a non-numeric reply."""
        reply = self._query(cmd)
        m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", reply)
        if not m:
            raise SupplyBackendError(f"Non-numeric reply to {cmd!r}: {reply!r}")
        return float(m.group(0))

    def _report_query(self, cmd: str) -> Any:
        """A number for a state report, or ``n/a``: a failed read is not 0."""
        try:
            return self._float_query(cmd)
        except (NameError, AttributeError, TypeError, ImportError):
            raise
        except Exception:
            return "n/a"

    def check_error(self) -> None:
        """Raise ``SupplyBackendError`` if the error queue holds an error."""
        code, message = parse_error_reply(self._query("SYSTem:ERRor?"))
        if code != 0:
            raise SupplyBackendError(f"SPD1000X error {code}: {message}")

    # Identity

    def get_identification(self) -> str:
        return self._query("*IDN?")

    def check_instrument(self) -> None:
        idn = self._safe_query("*IDN?", "")
        m = re.search(IDN_PATTERN, idn, re.IGNORECASE)
        if not m:
            raise SupplyBackendError(
                f"Unexpected instrument identification for SPD1000X net:\n{idn or '<no response>'}"
            )
        self._model = m.group(1).upper()

    def __str__(self) -> str:
        return self._safe_query("*IDN?", "Siglent SPD1000X")

    def set_active_channel(self, channel: Any) -> None:
        # Single channel; kept for parity with the multi-channel drivers.
        return

    # Setpoints. ``source``/``channel`` are accepted for RigolDP800 API parity.

    def _check_setpoint(self, value: float, kind: str, unit: str) -> None:
        # The SPD drops an out-of-range setpoint and only queues "9  Data out
        # of range", which no write reads back, so a bad value must be refused
        # here or the call returns with the old setpoint still in place.
        if value < 0:
            raise SupplyBackendError(f"{kind.capitalize()} must be positive, got {value}{unit}")
        limit = self.get_channel_limits()[f"{kind}_max"]
        if value > limit:
            raise SupplyBackendError(
                f"{kind.capitalize()} {value}{unit} exceeds the {self._model or 'SPD1000X'} "
                f"limit of {limit}{unit}"
            )

    def set_channel_voltage(self, value: float, source: Any = None) -> None:
        self._check_setpoint(value, "voltage", "V")
        self._write(f"CH1:VOLTage {value:.3f}")

    def get_channel_voltage(self, source: Any = None, channel: Any = None) -> float:
        return self._float_query("CH1:VOLTage?")

    def set_channel_current(self, value: float, source: Any = None) -> None:
        self._check_setpoint(value, "current", "A")
        self._write(f"CH1:CURRent {value:.3f}")

    def get_channel_current(self, source: Any = None, channel: Any = None) -> float:
        return self._float_query("CH1:CURRent?")

    # Measurements

    def measure_voltage(self, channel: Any = None) -> float:
        return self._float_query("MEASure:VOLTage? CH1")

    def measure_current(self, channel: Any = None) -> float:
        return self._float_query("MEASure:CURRent? CH1")

    def measure_power(self, channel: Any = None) -> float:
        return self._float_query("MEASure:POWEr? CH1")

    # Output and status

    def get_status(self) -> Optional[int]:
        """``SYSTem:STATus?`` as an int, or None if the read failed."""
        return parse_status(self._safe_query("SYSTem:STATus?", ""))

    def enable_output(self, channel: Any = None) -> None:
        self._write("OUTPut CH1,ON")
        time.sleep(OUTPUT_SETTLE_S)

    def disable_output(self, channel: Any = None) -> None:
        self._write("OUTPut CH1,OFF")
        time.sleep(OUTPUT_SETTLE_S)

    def output_state(self, channel: Any = None) -> Optional[bool]:
        """True / False / None (unknown) -- see ``SupplyNet.output_state``."""
        status = self.get_status()
        if status is None:
            return None
        return bool(status & STATUS_OUTPUT_ON)

    def output_is_enabled(self, channel: Any = None) -> bool:
        return self.output_state(channel) is True

    def get_output_mode(self, channel: Any = None) -> str:
        """``CC`` or ``CV``; raises if the status read failed."""
        status = self.get_status()
        if status is None:
            raise SupplyBackendError("Could not read SYSTem:STATus?")
        return "CC" if status & STATUS_CC else "CV"

    def enable_sense(self, channel: Any = None) -> None:
        # 2W/4W sense is a front-panel choice here; the DP800 mapper calls
        # this only for channel 2, which this supply does not have.
        return

    def get_channel_limits(self, channel: Any = None) -> dict:
        limits = MODEL_LIMITS.get(self._model, MODEL_LIMITS["SPD1168X"])
        return dict(limits)

    # OVP/OCP: not present on the SPD1000X. Getters raise too, so the monitor
    # gather reports n/a rather than a limit of 0.

    def set_overcurrent_protection_value(self, value: float, channel: Any = None) -> None:
        raise _protection_unsupported("OCP")

    def get_overcurrent_protection_value(self, channel: Any = None) -> float:
        raise _protection_unsupported("OCP")

    def enable_overcurrent_protection(self, channel: Any = None) -> None:
        raise _protection_unsupported("OCP")

    def disable_overcurrent_protection(self, channel: Any = None) -> None:
        raise _protection_unsupported("OCP")

    def overcurrent_protection_is_tripped(self, channel: Any = None) -> bool:
        raise _protection_unsupported("OCP")

    def clear_overcurrent_protection_trip(self, channel: Any = None) -> None:
        raise _protection_unsupported("OCP")

    def set_overvoltage_protection_value(self, value: float, channel: Any = None) -> None:
        raise _protection_unsupported("OVP")

    def get_overvoltage_protection_value(self, channel: Any = None) -> float:
        raise _protection_unsupported("OVP")

    def enable_overvoltage_protection(self, channel: Any = None) -> None:
        raise _protection_unsupported("OVP")

    def disable_overvoltage_protection(self, channel: Any = None) -> None:
        raise _protection_unsupported("OVP")

    def overvoltage_protection_is_tripped(self, channel: Any = None) -> bool:
        raise _protection_unsupported("OVP")

    def clear_overvoltage_protection_trip(self, channel: Any = None) -> None:
        raise _protection_unsupported("OVP")

    # SupplyNet interface

    @staticmethod
    def _reject_protection(ocp: float | None, ovp: float | None) -> None:
        # Refuse before any setpoint is written, so a request that asked for
        # protection never leaves the output reconfigured without it.
        if ocp is not None:
            raise _protection_unsupported("OCP")
        if ovp is not None:
            raise _protection_unsupported("OVP")

    def voltage(self, value: float | None = None,
                ocp: float | None = None,
                ovp: float | None = None) -> None:
        self._reject_protection(ocp, ovp)
        if value is not None:
            self.set_channel_voltage(value)
            print(f"{GREEN}Voltage set to: {value:.4f}V{RESET}")
            return
        print(f"{GREEN}Voltage: {self.get_channel_voltage():.4f}{RESET}")

    def current(self, value: float | None = None,
                ocp: float | None = None,
                ovp: float | None = None) -> None:
        self._reject_protection(ocp, ovp)
        if value is not None:
            self.set_channel_current(value)
            print(f"{GREEN}Current set to: {value:.4f}A{RESET}")
            return
        print(f"{GREEN}Current: {self.get_channel_current():.4f}{RESET}")

    def enable(self) -> None:
        self.enable_output()

    def disable(self) -> None:
        self.disable_output()

    def set_mode(self) -> None:
        # DC supply only; nothing to switch.
        return

    def clear_ocp(self) -> None:
        self.clear_overcurrent_protection_trip()

    def clear_ovp(self) -> None:
        self.clear_overvoltage_protection_trip()

    def ocp(self, value: float | None = None) -> None:
        raise _protection_unsupported("OCP")

    def ovp(self, value: float | None = None) -> None:
        raise _protection_unsupported("OVP")

    def _print_state(self, full: bool) -> None:
        state = self.output_state()
        enabled_text = {True: "ON", False: "OFF", None: "unknown"}[state]
        # Measured values whatever the output state, as RigolDP800 reports,
        # so a setpoint is never shown as a reading.
        v = self._report_query("MEASure:VOLTage? CH1")
        i = self._report_query("MEASure:CURRent? CH1")
        p = self._report_query("MEASure:POWEr? CH1")
        v_set = self._report_query("CH1:VOLTage?")
        i_set = self._report_query("CH1:CURRent?")
        try:
            mode = self.get_output_mode()
        except SupplyBackendError:
            mode = "unknown"

        print(f"{GREEN}Channel: 1{RESET}")
        print(f"{GREEN}Enabled: {enabled_text}{RESET}")
        print(f"{GREEN}Mode: {mode}{RESET}")
        print(f"{GREEN}Voltage: {v}{RESET}")
        print(f"{GREEN}Current: {i}{RESET}")
        print(f"{GREEN}Power: {p}{RESET}")
        if full:
            print(f"{GREEN}Voltage_Set: {v_set}{RESET}")
            print(f"{GREEN}Current_Set: {i_set}{RESET}")
        print(f"{GREEN}OCP Limit: unsupported{RESET}")
        print(f"{GREEN}OVP Limit: unsupported{RESET}")
        if full:
            hw = self.get_channel_limits()
            print(f"{GREEN}Voltage_Max: {hw['voltage_max']}{RESET}")
            print(f"{GREEN}Current_Max: {hw['current_max']}{RESET}")

    def state(self) -> None:
        self._print_state(full=False)

    def get_full_state(self) -> None:
        self._print_state(full=True)


def create_device(net_info: dict) -> SiglentSPD1000X:
    """Factory used by hardware_service.py:/invoke to instantiate this driver.

    ``net_info`` carries {address, channel, instrument} as assembled by
    ``lager.dispatchers.helpers.resolve_net_proxy``.
    """
    return SiglentSPD1000X(address=net_info.get("address"), channel=net_info.get("channel", 1))
