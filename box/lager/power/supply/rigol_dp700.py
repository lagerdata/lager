# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Backend driver for the Rigol DP700 series (DP711 / DP712).

Unlike the DP800 series (USB-TMC, multi-channel, channel-scoped SCPI), the
DP700 is a **single-channel** supply with an **RS-232-only** control interface.
On a Lager box it is reached through a generic USB-serial adapter (e.g. a
Prolific 067b:23a3 cable) that enumerates as ``/dev/ttyUSB*`` — the box cannot
auto-detect the instrument, so the net is created via the manual
custom-device association flow (see ``lager.devices.catalog``).

This driver therefore opens an **ASRL/serial** VISA resource over the pyvisa-py
(``@py``) backend, the same fallback path the EA PSB driver uses, rather than a
``USB0::...::INSTR`` USB-TMC resource. It exposes the same method surface as
``RigolDP800`` so the existing supply dispatcher, HTTP/WebSocket handlers and
the DP800 net mapper can drive it unchanged.

SCPI NOTES (verify against the DP700 Programming Guide on first bench bring-up):
  - The DP700 is single-channel; commands are sent without a CH/SOURce prefix.
  - Core set/measure/output commands below are high-confidence.
  - The OVP/OCP mnemonics mirror the DP800 (``:OUTP:OVP...``); every protection
    call is wrapped so a mnemonic mismatch degrades to a safe default instead
    of breaking the core voltage/current/enable path.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

try:
    import pyvisa  # type: ignore
except (ImportError, ModuleNotFoundError):  # pragma: no cover - box always has it
    pyvisa = None

from lager.devices.catalog import get_device, serial_params, limits_for
from lager.devices.serial_id import is_serial_address, resolve_address_to_tty
from lager.instrument_wrappers.instrument_wrap import InstrumentWrap
from lager.util.device_lock import device_lock
from .supply_net import (
    SupplyNet,
    SupplyBackendError,
    LibraryMissingError,
    DeviceNotFoundError,
)

# ANSI color codes (match the other supply drivers' output formatting)
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

CATALOG_NAME = "Rigol_DP711"


# ----------------------------- address helpers -----------------------------

def _asrl_address(address: Optional[str]) -> Optional[str]:
    """Coerce a saved-net address into an ASRL VISA resource string.

    Accepted forms:
      - ``serial://<vid>:<pid>/serial/<s>`` or ``.../port/<p>`` — durable
        identity resolved to the live ``/dev/ttyUSB*`` at open time (survives
        renumber/replug). Returns None if the cable isn't currently plugged in.
      - ``ASRL/dev/ttyUSB0::INSTR``    (used as-is)
      - ``/dev/ttyUSB0``               (wrapped as ASRL...::INSTR)
    """
    addr = (address or "").strip()
    if not addr:
        return None
    if is_serial_address(addr):
        tty = resolve_address_to_tty(addr)
        return f"ASRL{tty}::INSTR" if tty else None
    if addr.upper().startswith("ASRL"):
        return addr
    if addr.startswith("/dev/"):
        return f"ASRL{addr}::INSTR"
    return None


def _assignment_serial_settings(address: str) -> Optional[dict]:
    """Per-assignment serial settings for a durable ``serial://`` address.

    The assignment record can carry a ``--baud`` override on top of the
    catalog defaults (the DP711's rate is front-panel-configurable, so it can
    differ per unit). Guarded: returns None for non-durable addresses or when
    the store is unavailable, and the caller falls back to catalog defaults.
    """
    if not is_serial_address(address):
        return None
    try:
        from lager.devices.custom_store import serial_settings_for_address
        return serial_settings_for_address(address)
    except Exception:
        return None


def _apply_serial_settings(inst: Any, cfg: dict) -> None:
    """Best-effort application of baud/data/stop/parity to an ASRL resource."""
    try:
        inst.read_termination = "\n"
        inst.write_termination = "\n"
        inst.timeout = 5000
    except Exception:
        pass
    if not cfg:
        return
    try:
        if "baud" in cfg and hasattr(inst, "baud_rate"):
            inst.baud_rate = int(cfg["baud"])
        if "bytesize" in cfg and hasattr(inst, "data_bits"):
            inst.data_bits = int(cfg["bytesize"])
        if "stopbits" in cfg and hasattr(inst, "stop_bits"):
            try:
                from pyvisa.constants import StopBits  # type: ignore
                inst.stop_bits = {
                    1: StopBits.one,
                    1.5: StopBits.one_and_a_half,
                    2: StopBits.two,
                }.get(cfg["stopbits"], StopBits.one)
            except Exception:
                inst.stop_bits = int(cfg["stopbits"])
        if "parity" in cfg and hasattr(inst, "parity"):
            try:
                from pyvisa.constants import Parity  # type: ignore
                inst.parity = {
                    "N": Parity.none,
                    "E": Parity.even,
                    "O": Parity.odd,
                }.get(str(cfg["parity"]).upper(), Parity.none)
            except Exception:
                inst.parity = str(cfg["parity"])
    except Exception:
        # Serial knobs are advisory; a failure here shouldn't block the open.
        pass


def _tty_from_asrl(asrl: Optional[str]) -> Optional[str]:
    """Extract the ``/dev/tty*`` path from an ``ASRL/dev/tty*::INSTR`` string."""
    if not asrl:
        return None
    m = re.match(r"ASRL(/dev/[^:]+)(?:::INSTR)?$", asrl.strip(), re.IGNORECASE)
    return m.group(1) if m else None


def _resolve_or_raise(address: str) -> str:
    """Resolve a saved address to a live ASRL resource string, or raise."""
    asrl = _asrl_address(address)
    if asrl:
        return asrl
    if is_serial_address(address):
        raise DeviceNotFoundError(
            f"DP700 cable for '{address}' is not currently connected "
            f"(no matching /dev/ttyUSB* found). Check the USB-serial adapter."
        )
    raise DeviceNotFoundError(
        f"DP700 net address '{address}' is not a serial/ASRL resource. "
        f"Expected 'serial://<vid>:<pid>/serial/<s>', 'ASRL/dev/ttyUSB0::INSTR', "
        f"or '/dev/ttyUSB0'."
    )


def _open_asrl(asrl: str, cfg: dict) -> Any:
    """Open a known ASRL resource string, preferring the pyvisa-py backend."""
    if pyvisa is None:
        raise LibraryMissingError("PyVISA is not installed on this box.")

    last_exc: Optional[Exception] = None
    # ASRL is served by pyvisa-py; try it first, then the default backend.
    for backend in ("@py", None):
        try:
            rm = pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()
            with device_lock(asrl, timeout=2.0):
                inst = rm.open_resource(asrl)
            _apply_serial_settings(inst, cfg)
            inst._lager_rm = rm  # keep RM alive to protect the session handle
            return inst
        except Exception as exc:  # noqa: BLE001 - try the next backend
            last_exc = exc
            continue

    raise DeviceNotFoundError(f"Could not open DP700 at {asrl}: {last_exc}")


# --------------------------------- driver ---------------------------------

class RigolDP700(SupplyNet):
    """Single-channel RS-232 Rigol DP700-series (DP711) supply backend."""

    def __init__(self, address: Optional[str] = None, channel: int = 1,
                 device_path: Optional[str] = None, serial_cfg: Optional[dict] = None,
                 **_: Any):
        addr = address or (f"ASRL{device_path}::INSTR" if device_path else None)
        if not addr:
            raise SupplyBackendError("DP700 requires a serial address or device path.")

        # Saved-net address as stored. When it's a durable serial:// identity we
        # re-resolve it to the live tty on demand (the cable can move ports and
        # the cached driver would otherwise hold a stale /dev/ttyUSB* session).
        self._raw_address = addr
        self._durable = is_serial_address(addr)
        if serial_cfg is not None:
            self._cfg = serial_cfg
        else:
            # Assignment-level overrides (e.g. --baud) win over catalog defaults.
            self._cfg = (_assignment_serial_settings(addr)
                         or serial_params(CATALOG_NAME) or {})
        self._rm = None
        self.instr = None
        self._opened_tty: Optional[str] = None
        # DP700 is single-channel; channel is fixed at 1 regardless of input.
        self.channel = 1

        self._connect()
        self.check_instrument()

    # --------------------------- connection lifecycle ---------------------------

    def _connect(self) -> None:
        """Resolve the address to a live ASRL resource and open it."""
        asrl = _resolve_or_raise(self._raw_address)
        raw = _open_asrl(asrl, self._cfg)
        self._rm = getattr(raw, "_lager_rm", None)
        try:
            self.instr = InstrumentWrap(raw)
        except Exception:
            self.instr = raw
        self._opened_tty = _tty_from_asrl(asrl)

    def _close(self) -> None:
        """Best-effort teardown of the current pyvisa session."""
        inst = self.instr
        raw = inst.instr if isinstance(inst, InstrumentWrap) else inst
        for obj in (raw, self._rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self.instr = None
        self._rm = None

    def _reopen(self) -> bool:
        """Close and re-resolve/re-open the serial session. True on success."""
        self._close()
        try:
            self._connect()
            return True
        except Exception:
            return False

    def _ensure_open(self) -> None:
        """Cheap pre-op check: if a durable cable's tty vanished, reopen.

        A failed reopen must surface as DeviceNotFoundError here: ignoring it
        leaves ``self.instr`` as None, and the next IO would raise a raw
        NoneType AttributeError — which ``_safe_query`` deliberately re-raises
        as a programming error instead of reporting a missing cable.
        """
        if self._durable and self._opened_tty and not os.path.exists(self._opened_tty):
            if not self._reopen():
                raise DeviceNotFoundError(
                    f"DP700 cable for '{self._raw_address}' is not currently "
                    f"connected (tty vanished and could not be re-resolved)."
                )

    def _is_connection_alive(self) -> bool:
        """Liveness hook for the dispatcher driver cache.

        For durable serial nets the underlying tty can renumber when the cable
        moves; report dead so the cache reconstructs (and re-resolves) instead
        of reusing a session bound to a gone tty.
        """
        if self._durable and self._opened_tty:
            return os.path.exists(self._opened_tty)
        return True

    # ------------------------------ IO shims ------------------------------

    def _raw_write(self, cmd: str) -> None:
        if self.instr is None:
            # Closed/never-reopened session (e.g. a retry after a failed
            # _reopen): report the missing device, not a NoneType access.
            raise DeviceNotFoundError(
                f"DP700 at '{self._raw_address}' has no open session."
            )
        try:
            self.instr.write(cmd, check_errors=False)
        except TypeError:
            self.instr.write(cmd)

    def _raw_query(self, cmd: str) -> str:
        if self.instr is None:
            raise DeviceNotFoundError(
                f"DP700 at '{self._raw_address}' has no open session."
            )
        try:
            return self.instr.query(cmd, check_errors=False)
        except TypeError:
            return str(self.instr.query(cmd))

    def _write(self, cmd: str) -> None:
        # check_errors=False: the DP700 ``:SYST:ERR?`` round-trip that
        # InstrumentWrap performs adds a serial round-trip per write and isn't
        # needed for the simple command set we use.
        self._ensure_open()
        try:
            self._raw_write(cmd)
        except Exception:
            # Stale session (e.g. cable replugged to the same tty number):
            # reopen once and retry before giving up.
            if self._durable and self._reopen():
                self._raw_write(cmd)
            else:
                raise

    def _query(self, cmd: str) -> str:
        self._ensure_open()
        try:
            return self._raw_query(cmd)
        except Exception:
            if self._durable and self._reopen():
                return self._raw_query(cmd)
            raise

    def _safe_query(self, cmd: str, default: str = "n/a") -> str:
        try:
            resp = self._query(cmd)
            return resp.strip() if isinstance(resp, str) else str(resp)
        except (NameError, AttributeError, TypeError, ImportError):
            # Programming errors (bad import/attr) must surface, not be silently
            # collapsed into a default that looks like a dead instrument.
            raise
        except Exception:
            return default

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            # Tolerate trailing units (e.g. "3.300 V") that some Rigols append.
            m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(value))
            return float(m.group(0)) if m else default
        except Exception:
            return default

    # --------------------------- identity ---------------------------

    def get_identification(self) -> str:
        return self._query("*IDN?").strip()

    def check_instrument(self) -> None:
        idn = self._safe_query("*IDN?", "")
        entry = get_device(CATALOG_NAME) or {}
        pattern = entry.get("idn_match", r"RIGOL\s+TECHNOLOGIES,\s*DP7")
        if not re.search(pattern, idn, re.IGNORECASE):
            raise SupplyBackendError(
                f"Unexpected instrument identification for DP700 net:\n{idn or '<no response>'}"
            )

    def __str__(self) -> str:
        return self._safe_query("*IDN?", "Rigol DP700")

    # --------------------------- setpoints ---------------------------
    # DP700 is single-channel: no CH/SOURce prefix. ``source``/``channel``
    # arguments are accepted for API parity with RigolDP800 and ignored.

    def set_channel_voltage(self, value: float, source: Any = None) -> None:
        self._write(f":VOLT {value}")

    def get_channel_voltage(self, source: Any = None, channel: Any = None) -> float:
        return self._safe_float(self._safe_query(":VOLT?"))

    def set_channel_current(self, value: float, source: Any = None) -> None:
        self._write(f":CURR {value}")

    def get_channel_current(self, source: Any = None, channel: Any = None) -> float:
        return self._safe_float(self._safe_query(":CURR?"))

    # --------------------------- measurements ---------------------------

    def measure_voltage(self, channel: Any = None) -> float:
        return self._safe_float(self._safe_query(":MEAS:VOLT?"))

    def measure_current(self, channel: Any = None) -> float:
        return self._safe_float(self._safe_query(":MEAS:CURR?"))

    def measure_power(self, channel: Any = None) -> float:
        return self._safe_float(self._safe_query(":MEAS:POWE?"))

    # --------------------------- output ---------------------------

    def enable_output(self, channel: Any = None) -> None:
        self._write(":OUTP ON")
        time.sleep(0.2)

    def disable_output(self, channel: Any = None) -> None:
        self._write(":OUTP OFF")
        time.sleep(0.2)

    def output_is_enabled(self, channel: Any = None) -> bool:
        resp = self._safe_query(":OUTP?", "OFF").strip().upper()
        return resp in ("ON", "1")

    def get_output_mode(self, channel: Any = None) -> str:
        # DP700 reports CV/CC via :OUTP:MODE? on supported firmware; fall back
        # to CV when the query isn't recognized.
        mode = self._safe_query(":OUTP:MODE?", "CV").strip().upper()
        if "CC" in mode:
            return "CC"
        if "CV" in mode:
            return "CV"
        if "UR" in mode:
            return "UR"
        return "CV"

    def enable_sense(self, channel: Any = None) -> None:
        # DP711 has no remote-sense SCPI; no-op kept for DP800 API parity.
        return

    def get_channel_limits(self, channel: Any = None) -> dict:
        limits = limits_for(CATALOG_NAME, "power-supply") or {}
        return {
            "voltage_max": float(limits.get("voltage_max", 30.0)),
            "current_max": float(limits.get("current_max", 5.0)),
        }

    # --------------------------- OCP ---------------------------

    def set_overcurrent_protection_value(self, value: float, channel: Any = None) -> None:
        self._write(f":OUTP:OCP:VAL {value}")

    def get_overcurrent_protection_value(self, channel: Any = None) -> float:
        return self._safe_float(self._safe_query(":OUTP:OCP:VAL?"))

    def enable_overcurrent_protection(self, channel: Any = None) -> None:
        self._write(":OUTP:OCP ON")

    def disable_overcurrent_protection(self, channel: Any = None) -> None:
        self._write(":OUTP:OCP OFF")

    def overcurrent_protection_is_tripped(self, channel: Any = None) -> bool:
        return self._safe_query(":OUTP:OCP:QUES?", "NO").strip().upper() in ("YES", "1", "ON")

    def clear_overcurrent_protection_trip(self, channel: Any = None) -> None:
        self._write(":OUTP:OCP:CLEAR")
        time.sleep(0.1)

    # --------------------------- OVP ---------------------------

    def set_overvoltage_protection_value(self, value: float, channel: Any = None) -> None:
        self._write(f":OUTP:OVP:VAL {value}")

    def get_overvoltage_protection_value(self, channel: Any = None) -> float:
        return self._safe_float(self._safe_query(":OUTP:OVP:VAL?"))

    def enable_overvoltage_protection(self, channel: Any = None) -> None:
        self._write(":OUTP:OVP ON")

    def disable_overvoltage_protection(self, channel: Any = None) -> None:
        self._write(":OUTP:OVP OFF")

    def overvoltage_protection_is_tripped(self, channel: Any = None) -> bool:
        return self._safe_query(":OUTP:OVP:QUES?", "NO").strip().upper() in ("YES", "1", "ON")

    def clear_overvoltage_protection_trip(self, channel: Any = None) -> None:
        self._write(":OUTP:OVP:CLEAR")
        time.sleep(0.1)

    # ----------------------- SupplyNet interface -----------------------

    def voltage(self, value: float | None = None,
                ocp: float | None = None,
                ovp: float | None = None) -> None:
        if ocp is not None:
            self.set_overcurrent_protection_value(ocp)
            self.enable_overcurrent_protection()
        if ovp is not None:
            self.set_overvoltage_protection_value(ovp)
            self.enable_overvoltage_protection()
        if value is not None:
            if value < 0:
                raise SupplyBackendError(f"Voltage must be positive, got {value}V")
            self.set_channel_voltage(value)
            time.sleep(0.1)
            print(f"{GREEN}Voltage set to: {value:.4f}V{RESET}")
            return
        v = self.get_channel_voltage()
        print(f"{GREEN}Voltage: {v:.4f}{RESET}")

    def current(self, value: float | None = None,
                ocp: float | None = None,
                ovp: float | None = None) -> None:
        if ocp is not None:
            self.set_overcurrent_protection_value(ocp)
            self.enable_overcurrent_protection()
        if ovp is not None:
            self.set_overvoltage_protection_value(ovp)
            self.enable_overvoltage_protection()
        if value is not None:
            if value < 0:
                raise SupplyBackendError(f"Current must be positive, got {value}A")
            self.set_channel_current(value)
            time.sleep(0.1)
            print(f"{GREEN}Current set to: {value:.4f}A{RESET}")
            return
        c = self.get_channel_current()
        print(f"{GREEN}Current: {c:.4f}{RESET}")

    def enable(self) -> None:
        self.enable_output()

    def disable(self) -> None:
        self.disable_output()

    def set_mode(self) -> None:
        # DP700 is a DC supply; nothing to switch.
        return

    def clear_ocp(self) -> None:
        self.clear_overcurrent_protection_trip()

    def clear_ovp(self) -> None:
        self.clear_overvoltage_protection_trip()

    def ocp(self, value: float | None = None) -> None:
        if value is not None:
            if value < 0:
                raise SupplyBackendError(f"OCP limit must be positive, got {value}A")
            self.set_overcurrent_protection_value(value)
            self.enable_overcurrent_protection()
            return
        print(f"{GREEN}OCP Limit: {self.get_overcurrent_protection_value()}{RESET}")

    def ovp(self, value: float | None = None) -> None:
        if value is not None:
            if value < 0:
                raise SupplyBackendError(f"OVP limit must be positive, got {value}V")
            self.set_overvoltage_protection_value(value)
            self.enable_overvoltage_protection()
            return
        print(f"{GREEN}OVP Limit: {self.get_overvoltage_protection_value()}{RESET}")

    def state(self) -> None:
        enabled = self.output_is_enabled()
        v = self.measure_voltage() if enabled else self.get_channel_voltage()
        i = self.measure_current() if enabled else self.get_channel_current()
        p = self.measure_power() if enabled else 0.0
        ocp_limit = self.get_overcurrent_protection_value()
        ovp_limit = self.get_overvoltage_protection_value()
        ocp_tripped = self.overcurrent_protection_is_tripped()
        ovp_tripped = self.overvoltage_protection_is_tripped()

        print(f"{GREEN}Channel: 1{RESET}")
        print(f"{GREEN}Enabled: {'ON' if enabled else 'OFF'}{RESET}")
        print(f"{GREEN}Mode: {self.get_output_mode()}{RESET}")
        print(f"{GREEN}Voltage: {v}{RESET}")
        print(f"{GREEN}Current: {i}{RESET}")
        print(f"{GREEN}Power: {p}{RESET}")
        print(f"{GREEN}OCP Limit: {ocp_limit}{RESET}")
        print(f"    OCP Tripped: {f'{RED}YES{RESET}' if ocp_tripped else f'{GREEN}NO{RESET}'}")
        print(f"{GREEN}OVP Limit: {ovp_limit}{RESET}")
        print(f"    OVP Tripped: {f'{RED}YES{RESET}' if ovp_tripped else f'{GREEN}NO{RESET}'}")

    def get_full_state(self) -> None:
        enabled = self.output_is_enabled()
        v_set = self.get_channel_voltage()
        i_set = self.get_channel_current()
        v = self.measure_voltage() if enabled else v_set
        i = self.measure_current() if enabled else i_set
        p = self.measure_power() if enabled else 0.0
        ocp_limit = self.get_overcurrent_protection_value()
        ovp_limit = self.get_overvoltage_protection_value()
        ocp_tripped = self.overcurrent_protection_is_tripped()
        ovp_tripped = self.overvoltage_protection_is_tripped()
        hw = self.get_channel_limits()

        print(f"{GREEN}Channel: 1{RESET}")
        print(f"{GREEN}Enabled: {'ON' if enabled else 'OFF'}{RESET}")
        print(f"{GREEN}Mode: {self.get_output_mode()}{RESET}")
        print(f"{GREEN}Voltage: {v}{RESET}")
        print(f"{GREEN}Current: {i}{RESET}")
        print(f"{GREEN}Power: {p}{RESET}")
        print(f"{GREEN}Voltage_Set: {v_set}{RESET}")
        print(f"{GREEN}Current_Set: {i_set}{RESET}")
        print(f"{GREEN}OCP Limit: {ocp_limit}{RESET}")
        print(f"    OCP Tripped: {f'{RED}YES{RESET}' if ocp_tripped else f'{GREEN}NO{RESET}'}")
        print(f"{GREEN}OVP Limit: {ovp_limit}{RESET}")
        print(f"    OVP Tripped: {f'{RED}YES{RESET}' if ovp_tripped else f'{GREEN}NO{RESET}'}")
        print(f"{GREEN}Voltage_Max: {hw['voltage_max']}{RESET}")
        print(f"{GREEN}Current_Max: {hw['current_max']}{RESET}")


def create_device(net_info: dict) -> RigolDP700:
    """Factory used by hardware_service.py:/invoke to instantiate this driver.

    ``net_info`` carries {address, channel, instrument} as assembled by
    ``lager.dispatchers.helpers.resolve_net_proxy``.
    """
    return RigolDP700(address=net_info.get("address"), channel=net_info.get("channel", 1))
