# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
import sys
import time

try:
    import pyvisa  # optional; only used when we receive a VISA address
except (ModuleNotFoundError, ImportError):
    pyvisa = None

from lager.instrument_wrappers.instrument_wrap import InstrumentWrapKeithley
from lager.instrument_wrappers.keithley_defines import Mode, SimMethod
from lager.instrument_wrappers.util import InvalidEnumError

from .battery_net import (
    BatteryNet,
    LibraryMissingError,
    DeviceNotFoundError,
    BatteryBackendError,
)

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

# The five battery models built into 2281S firmware, recallable by name via
# :BATT:MOD:RCL (reference manual 077114601, section 7-35). They live in
# firmware, not in the numbered memory slots. NOTE: the manual prints two of
# them with hyphens (LI-ION4_2, LEAD-ACID12), but a hyphen is unparseable as
# SCPI character data (-102 "Syntax error;  - :BATT:MOD:RCL LI-",
# hardware-verified) — the underscore spellings below are what the parser
# accepts and what :BATT:MOD:RCL? reports back.
BUILTIN_MODELS = ("LI_ION4_2", "NIMH1_2", "NICD1_2", "LEAD_ACID12", "NIMH12")


class KeithleyBattery(BatteryNet):
    """
    Battery-simulator backend for Keithley 2281S-20-6.

    UX improvements:
      • Enter Battery entry function first, then clear/arm => avoids 700.
      • Quiet ID check with caching; no repeated warnings.
      • Auto-reenter Battery EF on clear/enable/write if needed.
      • Tolerant writes (ignore one -222 quantization) with error-drain.
      • Model index validated as >= 1 with friendly error.
    """

    # ----------------------------- lifecycle -----------------------------

    def __init__(self, instr=None, address=None, channel: int = 1, reset: bool = False,
                 _owns_resource: bool = True, **_):
        """
        `_owns_resource`: if False, close() will NOT close the underlying
        pyvisa session. Set by hardware_service.py when sharing one pyvisa
        session between this battery driver and the sibling supply driver
        on the same Keithley 2281S — fixes the v0.16.7 dual-role known
        limitation.
        """
        raw = None
        addr = None
        self._rm = None  # Keep RM alive to prevent GC from invalidating session handles
        self._owns_resource = _owns_resource

        if instr is not None:
            if isinstance(instr, str):
                addr = instr
            else:
                raw = instr

        if raw is None and addr is None and address:
            addr = address

        if raw is None and addr is None:
            raise BatteryBackendError("Keithley_2281S requires a VISA address or an open VISA resource.")

        if raw is None and isinstance(addr, str):
            if pyvisa is None:
                raise LibraryMissingError("PyVISA library is not installed.")
            try:
                # Cross-process advisory lock — defends against an ad-hoc
                # `docker exec python3 -c "import pyvisa; ..."` debug script or
                # any other in-box process racing for this Keithley's libusb
                # interface. hardware_service serializes subsequent SCPI calls
                # via its in-process per-address lock; this lock only spans
                # the open_resource call itself.
                from lager.util.device_lock import device_lock
                rm = pyvisa.ResourceManager()
                with device_lock(addr, timeout=2.0):
                    raw = rm.open_resource(addr)
                self._rm = rm
                try:
                    raw.read_termination = "\n"
                    raw.write_termination = "\n"
                    raw.timeout = 5000  # ms
                except Exception:
                    pass
            except Exception as e:
                raise DeviceNotFoundError(f"Could not open instrument at {addr}: {e}")

        self.instr = InstrumentWrapKeithley(raw)
        self.channel = 1

        if reset:
            self._reset_instrument()

        self._idn_cache = None
        self._check_instrument()
        # Do NOT force entry function here; require explicit `set_to_battery_mode()`.

    # ----------------------------- public API (dispatcher calls) -----------------------------

    def set_to_battery_mode(self) -> None:
        """
        Enter the Battery entry function and enable continuous initiation.
        Bound to CLI: `lager battery <NET> set`.
        """
        # 1) Enter Battery EF first (prevents "Command not permitted in this mode")
        self._enter_battery_ef()

        # 2) Then do safety clears/arming (now permitted in this EF)
        for cmd in (":ABOR", ":BATT:OUTP OFF", ":OUTP OFF", ":BATT:OUTP:PROT:CLE", ":OUTP:PROT:CLE", "*CLS"):
            try:
                self._write(cmd, check_errors=False)
            except Exception:
                pass

        # 3) Arm continuous initiation (helps some readbacks react quickly)
        try:
            self._write(":INIT:CONT ON", check_errors=False)
        except Exception:
            pass

        # Drain residual errors from the entry sequence; ignore quantization and mode errors
        self._drain_error_queue(ignore_codes=(-222, 700))

    def set_mode(self, mode_type: str | None) -> None:
        """CLI 'mode' -> Keithley SimMethod."""
        if mode_type is None:
            return
        cli = mode_type.strip().lower()
        if cli == "static":
            method = SimMethod.Static
        elif cli == "dynamic":
            method = SimMethod.Dynamic
        else:
            raise InvalidEnumError(f"Unsupported mode '{mode_type}'. Use 'static' or 'dynamic'.")
        self._ensure_batt_mode()
        self._write(f":BATT:SIM:METH {method.to_cmd()}")

    def enable(self) -> None:
        """Enable battery simulator output with auto-reentry on mode errors."""
        try:
            self._ensure_batt_mode()
            self._write(":BATT:OUTP ON")
        except Exception as e:
            msg = str(e)
            if "Command not permitted in this mode" in msg or "700" in msg:
                self._enter_battery_ef()
                self._write(":BATT:OUTP ON")
            else:
                raise

    def disable(self) -> None:
        """Disable battery simulator output."""
        try:
            self._write(":BATT:OUTP OFF")
        except Exception:
            self._write(":OUTP OFF", check_errors=False)

    def enable_battery(self) -> None:
        self.enable()

    def disable_battery(self) -> None:
        self.disable()

    # ----- getters/setters used by CLI actions -----

    def mode(self, mode_type: str | None = None) -> None:
        if mode_type is None:
            try:
                method = SimMethod.from_cmd(self._safe_query(":BATT:SIM:METH?", "STAT"))
                print(f"{GREEN}{'static' if method == SimMethod.Static else 'dynamic'}{RESET}")
            except Exception:
                print(f"{GREEN}unknown{RESET}")
            return
        self.set_mode(mode_type)

    def soc(self, value: float | None = None) -> None:
        if value is None:
            print(f"{GREEN}{self._safe_query(':BATT:SIM:SOC?', '0')}%{RESET}")
            return
        self.set_soc(value)

    def voc(self, value: float | None = None) -> None:
        if value is None:
            print(f"{GREEN}{self._safe_query(':BATT:SIM:VOC?', '0')} V{RESET}")
            return
        self.set_voc(value)

    def voltage_full(self, value: float | None = None) -> None:
        if value is None:
            print(f"{GREEN}{self._safe_query(':BATT:SIM:VOC:FULL?', '0')} V{RESET}")
            return
        self.set_volt_full(value)

    def voltage_empty(self, value: float | None = None) -> None:
        if value is None:
            print(f"{GREEN}{self._safe_query(':BATT:SIM:VOC:EMPT?', '0')} V{RESET}")
            return
        self.set_volt_empty(value)

    def capacity(self, value: float | None = None) -> None:
        if value is None:
            print(f"{GREEN}{self._safe_query(':BATT:SIM:CAP:LIM?', '0')} Ah{RESET}")
            return
        self.set_capacity(value)
        # Verify if value was clamped by instrument
        if value is not None:
            actual = float(self._safe_query(':BATT:SIM:CAP:LIM?', str(value)))
            if abs(actual - value) > 0.01:  # Allow small quantization differences
                print(f"{RED}WARNING: Capacity clamped from {value:.3g}Ah to {actual:.3g}Ah (instrument limit){RESET}", file=sys.stderr)

    def current_limit(self, value: float | None = None) -> None:
        if value is None:
            print(f"{GREEN}{self._safe_query(':BATT:SIM:CURR:LIM?', '0')} A{RESET}")
            return
        self.set_current_limit(value)
        # Verify if value was clamped by instrument
        if value is not None:
            actual = float(self._safe_query(':BATT:SIM:CURR:LIM?', str(value)))
            if abs(actual - value) > 0.001:  # Allow small quantization differences
                print(f"{RED}WARNING: Current limit clamped from {value:.3f}A to {actual:.3f}A (instrument limit){RESET}", file=sys.stderr)

    def ovp(self, value: float | None = None) -> None:
        if value is None:
            print(f"{GREEN}{self._safe_query(':BATT:SIM:TVOL:PROT?', '0')} V{RESET}")
            return
        self.set_ovp(value)

    def ocp(self, value: float | None = None) -> None:
        if value is None:
            print(f"{GREEN}{self._safe_query(':BATT:SIM:CURR:PROT?', '0')} A{RESET}")
            return
        self.set_ocp(value)

    def model(self, partnumber: str | None = None) -> None:
        if partnumber is None:
            print(f"{GREEN}{self.current_model()}{RESET}")
            return
        self.set_model(partnumber)

    def current_model(self) -> str:
        """Name of the active battery model, via :BATT:MOD:RCL?.

        (:BATT:STAT? — which older code used here — reports charge/discharge
        status per the reference manual, NOT the model, so it shows
        'DISCHARGE' whenever the output is idle regardless of what is
        loaded.)

        RCL? answers with the slot number while a numbered slot is active and
        with the model name while a firmware built-in is active. If it gives
        no reply (transient — e.g. right after a rejected command corrupts
        the parser's input buffer), fall back to the name cached by the last
        successful set_model.
        """
        raw = self._active_model_raw()
        if raw:
            name = ("DISCHARGE" if raw == "0" else f"slot {raw}") if raw.isdigit() else raw
            self._active_model_name = name
            return name
        return getattr(self, "_active_model_name", None) or "Custom"

    def _active_model_raw(self) -> str:
        """Raw :BATT:MOD:RCL? reply, '' when the instrument does not answer.

        Hardware-verified 2281S behavior: the query answers with the slot
        number while a numbered slot (0-9) is active and with the model name
        while a firmware built-in is active. It can transiently give no reply
        (e.g. a previously rejected command corrupting the parser's input
        buffer leaves the next query unanswered) — shrink the VISA timeout
        around the query so that silence costs <1 s instead of the full 5 s
        (this runs inside the monitor tick and the Device proxy's 10 s
        /invoke budget).
        """
        old_timeout = None
        try:
            old_timeout = self.instr.timeout
            self.instr.timeout = 800  # ms
        except Exception:
            pass
        try:
            return self._safe_query(":BATT:MOD:RCL?", "").strip().strip('"')
        finally:
            if old_timeout is not None:
                try:
                    self.instr.timeout = old_timeout
                except Exception:
                    pass

    def model_catalog(self) -> list[dict]:
        """
        Read the catalog of battery models available on the instrument.

        The 2281S reference manual (077114601, March 2019) defines no catalog
        query — :BATT:MODel:CATalog? does not exist — so the catalog is
        assembled from the documented read-only model-memory queries instead:

        - Slot 0 (DISCHARGE, basic constant-voltage simulation) is always
          available.
        - Slots 1-9 are probed with :BATT:MOD<n>:VOC:STEPs? (query only),
          which returns the number of values stored in that model. A slot is
          occupied when the length is non-zero. Internal memory stores models
          by index only, so occupied slots have no retrievable name.
        - The five firmware built-in models (recallable by name via
          :BATT:MOD:RCL) are appended with slot None.

        Read-only: issues only queries and never forces the Battery entry
        function — the model memory is not documented as mode-restricted.

        Returns:
            List of {"slot": int | None, "name": str | None} dicts.

        Raises:
            BatteryBackendError: If the instrument rejects the model-memory
                query (older firmware) or does not answer it at all.
        """
        models = [{"slot": 0, "name": "DISCHARGE"}]
        for idx in range(1, 10):
            try:
                raw = self._query(f":BATT:MOD{idx}:VOC:STEP?")
            except Exception as exc:
                code = self._scpi_error_code(exc)
                if code is not None and -199 <= code <= -100:
                    # SCPI command errors (-113 "Undefined header", etc.):
                    # this firmware has no model-memory queries.
                    raise BatteryBackendError(
                        "This instrument does not support the battery model "
                        f"memory query (:BATT:MOD{idx}:VOC:STEPs?): {exc}"
                    ) from exc
                if code is None:
                    # No response at all (e.g. VISA timeout): bail out on the
                    # first probe instead of timing out on all nine slots.
                    raise BatteryBackendError(
                        f"Battery model catalog query failed: {exc}"
                    ) from exc
                # Any other instrument error just means this slot holds no
                # usable model (e.g. empty slot); keep probing the rest.
                continue
            try:
                steps = int(float(raw.strip() or "0"))
            except (TypeError, ValueError):
                steps = 0
            if steps > 0:
                models.append({"slot": idx, "name": None})
        models.extend({"slot": None, "name": name} for name in BUILTIN_MODELS)
        return models

    # Model memory geometry (2281S reference manual 077114601): a complete
    # model holds exactly 101 points per element; :SIMPlify takes exactly 11
    # points and interpolates to 101; each SCPI program message is capped at
    # 2048 characters, so full curves are written in :APPend chunks.
    MODEL_POINTS_FULL = 101
    MODEL_POINTS_SIMPLE = 11
    # Values per staged write: 25 values is ~200-350 characters, far under
    # the 2048-character message cap even with worst-case float formatting.
    _MODEL_CHUNK_POINTS = 25

    def read_model(self, slot: int) -> dict:
        """Read a saved battery model's curve out of a memory slot.

        Read-only, hardware-verified: :BATT:MOD<n>:VOC? / :RES? answer with
        the SAVED slot's points directly (one comma-separated reply per
        element), independent of which model is active — no :BATT:MOD:RCL is
        needed, so exporting never changes the active model. An empty slot
        answers all-zeros rather than erroring, so occupancy is checked with
        the same :BATT:MOD<n>:VOC:STEPs? probe model_catalog uses.

        Returns:
            {"slot": slot, "points": [{"voc": float, "resistance": float},
            ...]} with exactly STEPs? entries, in SOC order (index 0 = empty).

        Raises:
            BatteryBackendError: If the slot is invalid or empty, or an
                element reply cannot be parsed into the expected point count.
        """
        slot = self._validate_model_slot(slot)
        steps = self._model_steps(slot)
        if steps <= 0:
            raise BatteryBackendError(
                f"Battery model slot {slot} is empty — there is nothing to export.\n"
                f"  Use 'models' to list the slots that hold a saved model."
            )
        curves = {}
        for element in ("VOC", "RES"):
            try:
                raw = self._query(f":BATT:MOD{slot}:{element}?")
            except Exception as exc:
                raise BatteryBackendError(
                    f"Could not read battery model slot {slot} ({element}): {exc}"
                ) from exc
            try:
                values = [float(v) for v in raw.strip().strip('"').split(",") if v.strip()]
            except ValueError as exc:
                raise BatteryBackendError(
                    f"Unexpected {element} reply for battery model slot {slot}: {raw!r}"
                ) from exc
            if len(values) != steps:
                raise BatteryBackendError(
                    f"Battery model slot {slot} {element} readback is incomplete: "
                    f"expected {steps} points, got {len(values)}."
                )
            curves[element] = values
        points = [
            {"voc": voc, "resistance": res}
            for voc, res in zip(curves["VOC"], curves["RES"])
        ]
        return {"slot": slot, "points": points}

    def define_model(self, slot: int, voc: list, resistance: list) -> None:
        """Write a custom battery model into a memory slot (create/overwrite).

        The 2281S builds a model from two curves indexed by SOC (index 0 =
        empty): open-circuit voltage and internal resistance. A complete
        model is exactly 101 points per element; exactly 11 points are also
        accepted via :SIMPlify, which interpolates them to 101 on the
        instrument. :BATT:MOD:SAVE:INTernal then persists the staged model to
        the slot — SILENTLY OVERWRITING whatever was there. There is no SCPI
        to delete/empty a slot (verified against the full reference manual);
        a slot can only be overwritten. Callers gate overwrites (--force).

        Everything is validated here before any instrument write so users get
        line-of-sight errors instead of raw SCPI 701/702/710 codes. Writes
        run with the output disabled and restored afterwards (error 704:
        config writes are not permitted while the model is running), matching
        _set_with_output_management.

        Args:
            slot: Target memory slot, 1-9 (slot 0 is DISCHARGE, not writable).
            voc: Open-circuit voltages in volts, non-decreasing, 0 < v <= 60.
            resistance: Internal resistances in ohms, non-increasing,
                0 < r <= 100. Same length as voc: exactly 11 or 101 points.

        Raises:
            BatteryBackendError: On validation failure, an instrument-rejected
                write, or a post-save STEPs? verification mismatch.
        """
        import math

        slot = self._validate_model_slot(slot)
        try:
            voc = [float(v) for v in (voc or [])]
            resistance = [float(r) for r in (resistance or [])]
        except (TypeError, ValueError) as exc:
            raise BatteryBackendError(
                f"Battery model points must be numbers: {exc}") from exc
        if len(voc) != len(resistance):
            raise BatteryBackendError(
                f"Battery model needs one resistance per VOC point: got "
                f"{len(voc)} VOC and {len(resistance)} resistance values.")
        if len(voc) not in (self.MODEL_POINTS_SIMPLE, self.MODEL_POINTS_FULL):
            raise BatteryBackendError(
                f"Battery model must have exactly {self.MODEL_POINTS_SIMPLE} points "
                f"(interpolated on the instrument) or {self.MODEL_POINTS_FULL} points; "
                f"got {len(voc)}.")
        for name, values, limit, unit in (
                ("VOC", voc, 60.0, "V"), ("resistance", resistance, 100.0, "Ω")):
            for i, v in enumerate(values):
                if not math.isfinite(v):
                    raise BatteryBackendError(
                        f"Battery model {name} point {i + 1} is not a finite number.")
                if v <= 0 or v > limit:
                    raise BatteryBackendError(
                        f"Battery model {name} point {i + 1} ({v:g} {unit}) is out of "
                        f"range: must be > 0 and <= {limit:g} {unit}.")
        for i in range(1, len(voc)):
            if voc[i] < voc[i - 1]:
                raise BatteryBackendError(
                    f"Battery model VOC must be non-decreasing (SOC index 0 is the "
                    f"empty battery): point {i + 1} ({voc[i]:g} V) is below point "
                    f"{i} ({voc[i - 1]:g} V).")
            if resistance[i] > resistance[i - 1]:
                raise BatteryBackendError(
                    f"Battery model resistance must be non-increasing: point {i + 1} "
                    f"({resistance[i]:g} Ω) is above point {i} "
                    f"({resistance[i - 1]:g} Ω).")

        # All validation passed — now touch the instrument. Mirror
        # _set_with_output_management around the whole write sequence (one
        # disable/restore, not one per chunk).
        was_enabled = self._is_batt_output_on()
        if was_enabled:
            try:
                self._write(":BATT:OUTP OFF", check_errors=False)
            except Exception:
                pass
        try:
            self._ensure_batt_mode()
            for element, values in (("VOC", voc), ("RES", resistance)):
                if len(values) == self.MODEL_POINTS_SIMPLE:
                    payload = ",".join(self._fmt_model_value(v) for v in values)
                    self._checked_model_write(
                        f':BATT:MOD{slot}:{element}:SIMP "{payload}"')
                else:
                    # Full :APPEND spelling — the standard SCPI short form
                    # (:APP) is -113 "Undefined header" on firmware 01.08b
                    # (hardware-verified), unlike :SIMP/:STEP which abbreviate
                    # fine. The plain (non-append) opener resets the staging
                    # area, so a previous partial write can't accumulate.
                    first = True
                    for chunk in self._model_chunks(values):
                        cmd = (f':BATT:MOD{slot}:{element} "{chunk}"' if first
                               else f':BATT:MOD{slot}:{element}:APPEND "{chunk}"')
                        self._checked_model_write(cmd)
                        first = False
            # Full :INTERNAL spelling for the same reason (:INT is -113).
            self._checked_model_write(f":BATT:MOD:SAVE:INTERNAL {slot}")
        finally:
            if was_enabled:
                try:
                    self._write(":BATT:OUTP ON", check_errors=False)
                except Exception:
                    pass

        # Verify both elements report a complete 101-point curve (the
        # 11-point :SIMPlify path interpolates to 101). Note STEPs? also
        # counts staged-but-unsaved data (hardware-verified), so persistence
        # itself is confirmed by the SAVE write draining no error above;
        # this catches an element that never reached 101 points.
        for element in ("VOC", "RES"):
            steps = self._model_steps(slot, element=element)
            if steps != self.MODEL_POINTS_FULL:
                raise BatteryBackendError(
                    f"Battery model save to slot {slot} did not verify: the "
                    f"instrument reports {steps} {element} points instead of "
                    f"{self.MODEL_POINTS_FULL}. The slot may hold an incomplete model."
                )

    @staticmethod
    def _validate_model_slot(slot) -> int:
        """Validate a writable/exportable model slot index (1-9)."""
        try:
            value = int(slot)
        except (TypeError, ValueError):
            value = None
        if value is None or str(slot).strip() != str(value) or not 1 <= value <= 9:
            raise BatteryBackendError(
                f"Battery model slot must be a number from 1 to 9, got {slot!r}. "
                f"(Slot 0 is the built-in DISCHARGE mode and cannot be exported "
                f"or overwritten.)")
        return value

    def _model_steps(self, slot: int, element: str = "VOC") -> int:
        """Stored point count for one element of a slot (0 = empty slot).

        Same read-only :BATT:MOD<n>:<el>:STEPs? probe model_catalog uses.
        """
        try:
            raw = self._query(f":BATT:MOD{slot}:{element}:STEP?")
        except Exception as exc:
            raise BatteryBackendError(
                f"Battery model memory query failed for slot {slot}: {exc}"
            ) from exc
        try:
            return int(float(raw.strip() or "0"))
        except (TypeError, ValueError):
            return 0

    def _checked_model_write(self, cmd: str) -> None:
        """Write one model-memory command and surface queued errors friendly.

        The drain doubles as the post-rejection buffer flush: a rejected
        command can corrupt the parser's input buffer so the NEXT query times
        out — :SYST:ERR? right after the write clears it either way.
        """
        self._write(cmd, check_errors=False)
        try:
            self._drain_error_queue()
        except BatteryBackendError as exc:
            code = self._scpi_error_code(exc)
            friendly = {
                701: "the instrument reports the model data is too short "
                     "(fewer points arrived than expected)",
                702: "the instrument reports the model data is too long "
                     "(more points arrived than expected)",
                710: "the instrument rejected the model data (VOC must be "
                     "non-decreasing and resistance non-increasing)",
            }.get(code)
            if friendly:
                raise BatteryBackendError(
                    f"Battery model write failed: {friendly}.") from exc
            raise

    @staticmethod
    def _fmt_model_value(v: float) -> str:
        """Compact float formatting for SCPI model payloads (%.6g)."""
        return f"{v:.6g}"

    def _model_chunks(self, values: list[float]):
        """Split values into comma-joined strings of _MODEL_CHUNK_POINTS."""
        for i in range(0, len(values), self._MODEL_CHUNK_POINTS):
            yield ",".join(self._fmt_model_value(v)
                           for v in values[i:i + self._MODEL_CHUNK_POINTS])

    @staticmethod
    def _scpi_error_code(exc) -> int | None:
        """Extract the SCPI error code from an instrument exception.

        InstrumentError carries (code, message, response) in args; other
        wrappers stringify to '<code>,"<message>"', possibly behind a
        prefix (BatteryBackendError renders as '[Battery] <code>,"..."').
        Returns None when no code can be found (e.g. a transport-level
        timeout).
        """
        args = getattr(exc, "args", ())
        if args and isinstance(args[0], int):
            return args[0]
        match = re.search(r'(-?\d+)\s*,\s*"', str(exc))
        if match:
            return int(match.group(1))
        return None

    def terminal_voltage(self) -> float:
        try:
            return float(self._safe_query(":BATT:SIM:TVOL?", "0"))
        except Exception:
            return 0.0

    def current(self) -> float:
        try:
            return float(self._safe_query(":BATT:SIM:CURR?", "0"))
        except Exception:
            return 0.0

    def esr(self, value: float | None = None):
        """Get or set the battery simulator series resistance.

        With no argument, returns the real-time internal resistance (ohms)
        reported by the active battery model (``:BATT:SIM:RES?``).

        With a value, sets the simulator's series-resistance OFFSET
        (``:BATT:SIM:RES:OFFSet``). The Keithley 2281S has no SCPI command to
        set an absolute simulated resistance — the intrinsic resistance comes
        from the active battery model as a function of SOC. The offset
        (-100 to +100 Ω) is added in series on top of that model resistance,
        so a positive value injects additional series resistance into the
        output path (e.g. ``esr(0.4)`` adds 400 mΩ). Read back the resulting
        live total with ``esr()``.
        """
        if value is None:
            try:
                return float(self._safe_query(":BATT:SIM:RES?", "0.067"))
            except Exception:
                return 0.067
        self.set_esr(value)
        return None

    def set_mode_battery(self) -> None:
        self.set_to_battery_mode()

    # ----- setters with tolerant writes / clamping -----

    def set_voc(self, value: float | None) -> None:
        if value is None:
            return
        if float(value) < 0:
            raise BatteryBackendError("Voltage cannot be negative")
        if float(value) > 60.0:  # Reasonable max voltage for battery simulation
            raise BatteryBackendError("VOC voltage exceeds reasonable maximum (60.0V)")
        self._set_with_output_management(f":BATT:SIM:VOC {value}", ignore_codes=(-222,))
        # Longer delay to allow instrument to process and settle the VOC value
        # In dynamic mode with battery model, VOC may be constrained by model parameters
        time.sleep(0.25)

    def set_volt_full(self, value: float | None) -> None:
        if value is None:
            return
        if float(value) < 0:
            raise BatteryBackendError("Voltage cannot be negative")
        self._set_with_output_management(f":BATT:SIM:VOC:FULL {value}", ignore_codes=(-222,))
        time.sleep(0.15)

    def set_volt_empty(self, value: float | None) -> None:
        if value is None:
            return
        if float(value) < 0:
            raise BatteryBackendError("Voltage cannot be negative")
        self._set_with_output_management(f":BATT:SIM:VOC:EMPT {value}", ignore_codes=(-222,))
        time.sleep(0.15)

    def set_capacity(self, value: float | None) -> None:
        if value is None:
            return
        if float(value) <= 0:
            raise BatteryBackendError("Capacity must be positive")
        self._set_with_output_management(f":BATT:SIM:CAP:LIM {value}", ignore_codes=(-222,))
        time.sleep(0.05)

    def set_current_limit(self, value: float | None) -> None:
        if value is None:
            return
        if float(value) < 0.001:  # Minimum practical current limit
            raise BatteryBackendError("Current limit must be at least 1mA (0.001A)")
        if float(value) > 6.0:  # 2281S-20-6 max current
            raise BatteryBackendError("Current limit exceeds instrument maximum (6.0A)")
        self._set_with_output_management(f":BATT:SIM:CURR:LIM {value}", ignore_codes=(-222,))
        time.sleep(0.05)

    def set_esr(self, value: float | None) -> None:
        """Set the battery simulator's series-resistance offset (ohms).

        Maps to ``:BATT:SIM:RESistance:OFFSet``. Per the 2281S reference the
        offset range is -100 to +100 Ω, and it cannot be changed while the
        battery model is running (error 704) — ``_set_with_output_management``
        disables the simulator output around the write to avoid that. There is
        no command to set an absolute simulated resistance; this offset is
        added in series on top of the active model's intrinsic resistance.
        """
        if value is None:
            return
        v = float(value)
        if v < -100.0 or v > 100.0:
            raise BatteryBackendError("ESR offset out of range (must be -100 to 100 Ω)")
        self._set_with_output_management(f":BATT:SIM:RES:OFFS {v}", ignore_codes=(-222,))
        time.sleep(0.05)
        # Verify and clarify the offset semantics (no absolute-ESR SCPI exists).
        actual = self._safe_query(":BATT:SIM:RES:OFFS?", str(v))
        try:
            if abs(float(actual) - v) > 0.001:
                print(f"{RED}WARNING: ESR offset clamped from {v:.3f}Ω to {float(actual):.3f}Ω "
                      f"(instrument limit){RESET}", file=sys.stderr)
        except (TypeError, ValueError):
            pass
        print(f"{GREEN}ESR offset set to: {self._fmt_ohm(actual)} "
              f"(added in series on top of the active battery model){RESET}")

    def set_ovp(self, value: float | None) -> None:
        if value is None:
            return
        if float(value) < 0:
            raise BatteryBackendError("Voltage cannot be negative")
        self._set_with_output_management(f":BATT:SIM:TVOL:PROT {value}", ignore_codes=(-222,))
        time.sleep(0.05)

    def set_ocp(self, value: float | None) -> None:
        if value is None:
            return
        if float(value) < 0.001:  # Minimum practical current limit
            raise BatteryBackendError("OCP limit must be at least 1mA (0.001A)")
        if float(value) > 6.0:  # 2281S-20-6 max current
            raise BatteryBackendError("OCP limit exceeds instrument maximum (6.0A)")
        self._set_with_output_management(f":BATT:SIM:CURR:PROT {value}", ignore_codes=(-222,))
        time.sleep(0.05)

    def clear_ovp(self) -> None:
        for cmd in (":BATT:OUTP:PROT:CLE", ":BATT:PROT:CLE", ":OUTP:PROT:CLE", "*CLS"):
            try:
                self._write(cmd, check_errors=False)
            except Exception:
                pass
        self._enter_battery_ef()  # be sure we’re back in the right EF

    def clear_ocp(self) -> None:
        for cmd in (":BATT:OUTP:PROT:CLE", ":BATT:PROT:CLE", ":OUTP:PROT:CLE", "*CLS"):
            try:
                self._write(cmd, check_errors=False)
            except Exception:
                pass
        self._enter_battery_ef()

    def clear(self) -> None:
        for cmd in (":BATT:OUTP:PROT:CLE", ":BATT:PROT:CLE", ":OUTP:PROT:CLE", "*CLS"):
            try:
                self._write(cmd, check_errors=False)
            except Exception:
                pass
        self._enter_battery_ef()

    def set_soc(self, value: float | None) -> None:
        if value is None:
            return
        p = int(round(float(value)))
        if p < 0 or p > 100:
            raise BatteryBackendError("SOC must be between 0 and 100%")
        self._ensure_batt_mode()
        self._write(f":BATT:SIM:SOC {p}")
        time.sleep(0.05)

    def set_model(self, partnumber_or_index) -> None:
        """
        Set the battery model by name or numeric slot index.

        The Keithley 2281S stores battery models in numbered memory slots (0-9).
        Each model defines voltage-SOC discharge curves and internal resistance.

        Model Storage Mechanism:
        - Slot 0: DISCHARGE mode (basic constant-voltage simulation, always available)
        - Slots 1-9: Pre-configured or custom battery models
        - Factory-shipped instruments typically have common battery types in slots 1-4
        - Custom models can be created and saved via the instrument's front panel
        - Empty slots will return an error with guidance on available alternatives

        Supported model name aliases:
        - Lithium-ion: '18650', 'liion', 'li-ion', 'lithium' -> Slot 1
        - Nickel-Metal Hydride: 'nimh', 'ni-mh', 'nickel' -> Slot 2
        - Nickel-Cadmium: 'nicd', 'ni-cd', 'nicad' -> Slot 3
        - Lead-Acid: 'lead', 'leadacid', 'lead-acid', 'sla' -> Slot 4
        - Discharge mode: 'discharge' -> Slot 0

        Args:
            partnumber_or_index: Battery model name (str) or numeric slot (0-9)

        Raises:
            BatteryBackendError: If model slot is empty or invalid
        """
        if partnumber_or_index is None:
            return

        # Map common model names directly to numeric indices
        # Most Keithley units ship with default models in these slots
        model_map = {
            "18650": 1,          # Slot 1: Typically Li-ion 18650
            "liion": 1,
            "li-ion": 1,
            "lithium": 1,
            "nimh": 2,           # Slot 2: Typically NiMH
            "ni-mh": 2,
            "nickel": 2,
            "nicd": 3,           # Slot 3: Typically NiCd
            "ni-cd": 3,
            "nicad": 3,
            "lead": 4,           # Slot 4: Typically Lead-acid
            "leadacid": 4,
            "lead-acid": 4,
            "sla": 4,
            "discharge": 0       # Slot 0: DISCHARGE mode (always available)
        }
        
        cmd = None
        
        # Initialize name variable for proper scope
        name = str(partnumber_or_index).lower().replace("-", "").replace("_", "").strip()
        
        # Try numeric first
        try:
            idx = int(partnumber_or_index)
            if idx < 0:
                raise BatteryBackendError("Model index must be non-negative")
            cmd = f":BATT:MOD:RCL {idx}"
        except ValueError:
            # Try model name mapping
            
            # Handle empty string case
            if not name:
                raise BatteryBackendError(
                    "Empty model name. Use numeric index (0-9) or model name like '18650', 'liion', 'lead-acid'"
                )
                
            if name in model_map:
                cmd = f":BATT:MOD:RCL {model_map[name]}"
            else:
                # Check if it's already a valid Keithley built-in. Accept the
                # manual's hyphenated spellings (LI-ION4_2) but always SEND
                # the underscore form — the SCPI parser rejects hyphens.
                builtin_token = partnumber_or_index.upper().replace("-", "_")
                if builtin_token in BUILTIN_MODELS + ("DISCHARGE",):
                    cmd = f':BATT:MOD:RCL {builtin_token}'
                else:
                    # Provide helpful suggestions for common typos
                    suggestions = []
                    if "li" in name or "lithium" in name:
                        suggestions.append("For lithium-ion, try: '18650', 'liion', or 'li-ion'")
                    elif "lead" in name or "acid" in name:
                        suggestions.append("For lead-acid, try: 'lead-acid' or 'sla'")
                    elif "ni" in name:
                        suggestions.append("For nickel-based, try: 'nimh' or 'nicd'")

                    suggestion_text = f"\n  {'. '.join(suggestions)}" if suggestions else ""
                    common_names = ['18650', 'liion', 'nimh', 'nicd', 'lead-acid', 'discharge']
                    raise BatteryBackendError(
                        f"Unknown battery model '{partnumber_or_index}'.{suggestion_text}\n"
                        f"  Common model aliases: {common_names}\n"
                        f"  Or use numeric slot (0-9) for custom models saved in instrument memory.\n"
                        f"  Note: Model slots may be empty if no custom model was saved."
                    )
        
        if not cmd:
            raise BatteryBackendError("Unable to determine model command")
            
        # Use output management since model changes may require output to be off
        self._set_with_output_management(cmd, ignore_codes=(711, -222))

        # Verify via the active-model query (:BATT:MOD:RCL?). Hardware-verified
        # 2281S behavior: recalling an EMPTY slot fails silently (no error
        # queued, previous model stays active), so readback is the only
        # detection; RCL? answers with the slot number when a numbered slot is
        # active and with the model NAME when a firmware built-in is active.
        # (The old check read :BATT:STAT?, which per the manual reports
        # charge/discharge status — it said "DISCHARGE" for every idle output
        # and made every successful numbered-slot load raise "slot is empty".)
        target = cmd.split()[-1]
        actual = self._active_model_raw()
        if target.lstrip("+-").isdigit():
            if actual == str(int(target)):
                self._active_model_name = (
                    "DISCHARGE" if int(target) == 0 else f"slot {int(target)}"
                )
            else:
                shown = actual if actual else "unchanged (no reply)"
                raise BatteryBackendError(
                    f"Battery model slot '{partnumber_or_index}' appears to be empty — "
                    f"the recall did not take effect (active model: {shown}).\n"
                    f"  The Keithley 2281S stores battery models in numbered memory slots (0-9).\n"
                    f"  Options:\n"
                    f"    • Use 'models' to list the slots that hold a saved model\n"
                    f"    • Use 'discharge' for basic constant-voltage simulation\n"
                    f"    • Use '18650', 'liion', 'nimh', 'nicd', or 'lead-acid' for common battery types\n"
                    f"    • Save a custom battery model to this slot using the instrument front panel"
                )
        else:
            # Built-in recall: RCL? echoes the built-in's name on success; a
            # numbered answer (or anything else) means the recall did not
            # take effect and the previous model is still loaded.
            if actual.upper() != target.upper():
                shown = (f"model slot {actual}" if actual.isdigit()
                         else (actual or "no reply"))
                raise BatteryBackendError(
                    f"Battery model '{partnumber_or_index}' did not load — "
                    f"the instrument reports: {shown}."
                )
            self._active_model_name = actual


    # ----------------------------- state dump -----------------------------

    def print_state(self) -> None:
        enabled = self._is_batt_output_on()
        mode_str = self._mode_string()
        model_str = self.current_model()

        tvol = self._safe_query(":BATT:SIM:TVOL?", "0")
        curr = self._safe_query(":BATT:SIM:CURR?", "0")
        esr = self._safe_query(":BATT:SIM:RES?", "0.067")
        soc = self._safe_query(":BATT:SIM:SOC?", "0")
        voc = self._safe_query(":BATT:SIM:VOC?", "0")
        cap_lim = self._safe_query(":BATT:SIM:CAP:LIM?", "1.0")
        curr_lim = self._safe_query(":BATT:SIM:CURR:LIM?", "1.0")
        ocp_lim = self._safe_query(":BATT:SIM:CURR:PROT?", "2.0")
        ovp_lim = self._safe_query(":BATT:SIM:TVOL:PROT?", "4.5")

        trip = (self._safe_query(":OUTP:PROT:TRIP?", "").upper() or "")
        ocp_tripped = (trip == "OCP")
        ovp_tripped = (trip == "OVP")

        print(f"{GREEN}Channel: 1{RESET}")
        print(f"{GREEN}Enabled: {'ON' if enabled else 'OFF'}{RESET}")
        print(f"{GREEN}Output: {'ON' if enabled else 'OFF'}{RESET}")
        print(f"{GREEN}Mode: {mode_str}{RESET}")
        print(f"{GREEN}Model: {model_str}{RESET}")
        print(f"{GREEN}Terminal Voltage: {self._fmt_v(tvol)}{RESET}")
        print(f"{GREEN}Current: {self._fmt_i(curr)}{RESET}")
        print(f"{GREEN}ESR: {self._fmt_ohm(esr)}{RESET}")
        print(f"{GREEN}SOC: {self._fmt_pct(soc)}{RESET}")
        print(f"{GREEN}VOC: {self._fmt_v(voc, digits=4)}{RESET}")
        print(f"{GREEN}Capacity: {self._fmt_ah(cap_lim)}{RESET}")
        print(f"{GREEN}Current Limit: {self._fmt_i(curr_lim)}{RESET}")
        print(f"{GREEN}OCP Limit: {self._fmt_i(ocp_lim)}{RESET}")
        ocp_status = f"{RED}YES{RESET}" if ocp_tripped else f"{GREEN}NO{RESET}"
        print(f"    OCP Tripped: {ocp_status}")
        print(f"{GREEN}OVP Limit: {self._fmt_v(ovp_lim)}{RESET}")
        ovp_status = f"{RED}YES{RESET}" if ovp_tripped else f"{GREEN}NO{RESET}"
        print(f"    OVP Tripped: {ovp_status}")

    def get_monitor_state(self, channel=None) -> dict:
        """Single-call monitor state for the battery TUI WebSocket.

        Same queries as ``print_state`` but returned as a dict in the
        ``battery_state_update`` wire shape (minus ``netname``/``channel``,
        which the monitor adds). One hardware_service ``/invoke`` — and one
        per-device lock acquisition — per monitor tick instead of ~17, so
        polling cannot starve interactive commands on slow instruments.
        """
        # Liveness probe — deliberately NOT swallowed. If the cached pyvisa
        # session is stale (instrument power-cycled / USB re-enumerated), this
        # raises a session/ENODEV error that propagates to the hardware
        # service's /invoke handler, which evicts the stale session,
        # reconnects, and retries this call on a fresh session. Without it the
        # swallowing _safe_query calls below would return 0s and the stale
        # session would never be evicted -- the TUI shows 0s (and the monitor
        # spams DeviceError) until the box is rebooted. *IDN? is read-only and
        # valid in any instrument mode, so it never perturbs state.
        _probe = getattr(self, "instr", None)
        if _probe is not None:
            _probe.query("*IDN?", check_errors=False)

        enabled = self._is_batt_output_on()
        mode_str = self._mode_string()
        model_str = self.current_model()

        trip = (self._safe_query(":OUTP:PROT:TRIP?", "").upper() or "")

        def q(cmd: str, default: str) -> float:
            # Defensive parse: some 2281S firmware can answer with trailing
            # units or multi-value strings; a bare float() would turn one
            # odd reply into a failed monitor tick. Take the first
            # comma-separated field and strip unit suffixes.
            raw = self._safe_query(cmd, default)
            try:
                return float(raw)
            except (TypeError, ValueError):
                try:
                    return float(str(raw).split(',')[0].strip().rstrip('VAWs%Ω'))
                except (TypeError, ValueError):
                    return float(default)

        return {
            'terminal_voltage': q(":BATT:SIM:TVOL?", "0"),
            'current': q(":BATT:SIM:CURR?", "0"),
            'esr': q(":BATT:SIM:RES?", "0.067"),
            'soc': q(":BATT:SIM:SOC?", "0"),
            'voc': q(":BATT:SIM:VOC?", "0"),
            'enabled': enabled,
            'mode': mode_str,
            'model': model_str,
            'capacity': q(":BATT:SIM:CAP:LIM?", "1.0"),
            'current_limit': q(":BATT:SIM:CURR:LIM?", "1.0"),
            'ocp_limit': q(":BATT:SIM:CURR:PROT?", "2.0"),
            'ovp_limit': q(":BATT:SIM:TVOL:PROT?", "4.5"),
            'volt_full': q(":BATT:SIM:VOC:FULL?", "4.2"),
            'volt_empty': q(":BATT:SIM:VOC:EMPT?", "3.0"),
            'ocp_tripped': trip == "OCP",
            'ovp_tripped': trip == "OVP",
        }

    # ----------------------------- helpers -----------------------------

    def _enter_battery_ef(self) -> None:
        """Enter Battery entry function; raise if clearly unsupported."""
        entered = False
        for tok in ("BATT", "SIM", "BATTERY", '"BATT"', '"SIM"'):
            try:
                self._write(f":ENTR:FUNC {tok}", check_errors=False)
                mode = self._safe_query(":ENTR:FUNC?", "").upper()
                if "BATT" in mode or "SIM" in mode:
                    entered = True
                    break
            except Exception:
                continue
        if not entered:
            try:
                self._write(f":ENTR:FUNC {Mode.BatterySimulator.to_cmd()}", check_errors=False)
                entered = True
            except Exception:
                pass
        if not entered:
            raise BatteryBackendError("Unable to enter Battery entry function (:ENTR:FUNC BATT).")

    def _ensure_batt_mode(self) -> None:
        try:
            ef = self._safe_query(":ENTR:FUNC?", "").upper()
            if "BATT" in ef or "SIM" in ef:
                return
        except Exception:
            pass
        try:
            self._enter_battery_ef()
        except Exception:
            pass  # allow continuation; some ops may still work

    def _is_batt_output_on(self) -> bool:
        try:
            val = self._safe_query(":BATT:OUTP?", "")
            return val.strip().upper() in ("1", "ON")
        except Exception:
            try:
                val = self._safe_query("OUTP?", "")
                return val.strip().upper() in ("1", "ON")
            except Exception:
                return False

    def _mode_string(self) -> str:
        try:
            ef = (self._safe_query(":ENTR:FUNC?", "") or "").upper()
            meth = SimMethod.from_cmd(self._safe_query(":BATT:SIM:METH?", "STAT"))
            pretty = "Static" if (meth == SimMethod.Static) else "Dynamic"
            if "BATT" in ef or "SIM" in ef:
                return pretty
            return "OFF"
        except Exception:
            return "OFF"

    # ----- output management for config commands -----

    def _set_with_output_management(self, cmd: str, ignore_codes: tuple[int, ...] = ()) -> None:
        """
        Set parameter with automatic output disable/restore to avoid Error 704.
        Many Keithley configuration commands require output to be OFF.
        """
        was_enabled = self._is_batt_output_on()
        
        # Disable output if it was enabled
        if was_enabled:
            try:
                self._write(":BATT:OUTP OFF", check_errors=False)
            except Exception:
                pass
        
        try:
            # Execute the actual command  
            self._ensure_batt_mode()
            if ignore_codes:
                self._tolerant_write(cmd, ignore_codes=ignore_codes)
            else:
                # Default to ignoring common quantization errors
                self._tolerant_write(cmd, ignore_codes=(-222,))
        finally:
            # Restore output state if it was originally enabled
            if was_enabled:
                try:
                    self._write(":BATT:OUTP ON", check_errors=False)
                except Exception:
                    pass

    # ----- tolerant I/O -----

    def _tolerant_write(self, cmd: str, ignore_codes: tuple[int, ...] = ()) -> None:
        """
        Write without raising on known/benign errors (e.g., -222 Data out of range).
        Drains one or more errors from the queue and ignores listed codes.
        """
        self._write(cmd, check_errors=False)
        self._drain_error_queue(ignore_codes=ignore_codes)

    def _drain_error_queue(self, ignore_codes: tuple[int, ...] = ()) -> None:
        # Consume a few errors to avoid surfacing them on the next checked call
        for _ in range(4):
            try:
                err = self.instr.query(":SYST:ERR?", check_errors=False).strip()
            except Exception:
                break
            if not err:
                break
            try:
                code_str, _msg = err.split(",", 1)
                code = int(code_str, 10)
            except Exception:
                break
            if code == 0:
                break
            if code in ignore_codes:
                continue
            # Add special handling for common Keithley errors
            if code == 704:
                raise BatteryBackendError(f'{code},"Not permitted while battery model is running"')
            raise BatteryBackendError(err)

    # ----- low-level I/O -----

    def _write(self, cmd: str, check_errors: bool = True) -> None:
        self.instr.write(cmd, check_errors=check_errors)

    def _query(self, cmd: str) -> str:
        return self.instr.query(cmd)

    def _safe_query(self, cmd: str, default: str = "0") -> str:
        try:
            return self._query(cmd).strip()
        except Exception:
            return default

    # ----- formatting -----

    @staticmethod
    def _fmt_v(v: str | float, digits: int = 1) -> str:
        try:
            return f"{float(v):.{digits}f} V"
        except Exception:
            return f"{v} V"

    @staticmethod
    def _fmt_i(i: str | float) -> str:
        try:
            return f"{float(i):.1f} A"
        except Exception:
            return f"{i} A"

    @staticmethod
    def _fmt_ohm(r: str | float) -> str:
        try:
            return f"{float(r):.3f} Ω"
        except Exception:
            return f"{r} Ω"

    @staticmethod
    def _fmt_pct(p: str | float) -> str:
        try:
            return f"{float(p):.2f}%"
        except Exception:
            return f"{p}%"

    @staticmethod
    def _fmt_ah(a: str | float) -> str:
        try:
            return f"{float(a):.3g} Ah"
        except Exception:
            return f"{a} Ah"

    # ----- ID & reset -----

    def _check_instrument(self) -> None:
        # Cache IDN; stay quiet unless it's clearly not Keithley
        idn = self.get_identification()
        self._idn_cache = idn
        if not idn:
            return  # transient blanks are fine
        if re.search(r"KEITHLEY\s+INSTRUMENTS.*2281S", idn, re.IGNORECASE):
            return
        if "keithley" in idn.lower():
            return
        raise BatteryBackendError(f"Unknown device identification: {idn}")

    def get_identification(self) -> str:
        return self._safe_query("*IDN?", default="")

    def _reset_instrument(self) -> None:
        try:
            self._write(":OUTP:PROT:CLE", check_errors=False)
        except Exception:
            pass
        self._write("*RST", check_errors=False)
        self._write("*CLS", check_errors=False)
        time.sleep(0.3)

    def __str__(self) -> str:
        return self._idn_cache or self._safe_query("*IDN?", "Keithley 2281S")

    def close(self) -> None:
        """Close the VISA connection and release resources.

        When `_owns_resource` is False, leaves the underlying pyvisa session
        alone (it's owned by hardware_service.py's shared resource cache and
        is also held by the sibling Keithley supply driver). Just drops our
        reference to the wrapper.
        """
        if hasattr(self, 'instr') and self.instr is not None:
            if getattr(self, '_owns_resource', True):
                try:
                    if hasattr(self.instr, 'instr') and hasattr(self.instr.instr, 'close'):
                        self.instr.instr.close()
                    elif hasattr(self.instr, 'close'):
                        self.instr.close()
                except Exception:
                    pass
            self.instr = None

    def __del__(self) -> None:
        """Cleanup when instance is garbage collected."""
        self.close()


# Alias for hardware service class discovery (looks for "Keithley" class)
Keithley = KeithleyBattery


def create_device(net_info, *, raw_resource=None):
    """Factory function for hardware_service.

    Extracts the required parameters from net_info dict and creates a KeithleyBattery instance.
    This allows hardware_service to instantiate the device without knowing the constructor signature.

    Args:
        net_info: Dictionary containing device configuration:
            - address: VISA resource string
            - channel: Channel number (1-3)
            - reset: Whether to reset device on init (default False)
        raw_resource: Optional already-open pyvisa session to share with the
            sibling Keithley supply driver. When provided, the returned driver
            wraps it instead of opening a new session, and close() will not
            release the underlying USB claim. Used by hardware_service.py to
            fix the v0.16.7 dual-role known limitation.

    Returns:
        KeithleyBattery instance configured with the provided parameters
    """
    address = net_info.get('address')
    channel = net_info.get('channel') or net_info.get('pin') or 1
    reset = net_info.get('reset', False)
    if raw_resource is not None:
        return KeithleyBattery(instr=raw_resource, channel=int(channel), reset=reset, _owns_resource=False)
    return KeithleyBattery(address=address, channel=int(channel), reset=reset)