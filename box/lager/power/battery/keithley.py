# Copyright 2024-2026 Lager Data LLC
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
                rm = pyvisa.ResourceManager()
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
            model_str = self._safe_query(":BATT:STAT?", "")
            if not model_str:
                model_str = self._safe_query(":BATT:MOD?", "0")
            print(f"{GREEN}{model_str}{RESET}")
            return
        self.set_model(partnumber)

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

    def esr(self) -> float:
        try:
            return float(self._safe_query(":BATT:SIM:RES?", "0.067"))
        except Exception:
            return 0.067

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
                # Check if it's already a valid Keithley built-in
                keithley_builtins = ["LI-ION4_2", "NIMH1_2", "NICD1_2", "LEAD-ACID12", "NIMH12", "DISCHARGE"]
                if partnumber_or_index.upper() in keithley_builtins:
                    cmd = f':BATT:MOD:RCL {partnumber_or_index.upper()}'
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

        # Verify something actually loaded; provide helpful guidance
        model_str = self._safe_query(":BATT:STAT?", "")
        if not model_str or "DISCHARGE" in model_str:
            # DISCHARGE is the default/empty state
            # Only error if user didn't explicitly request DISCHARGE or slot 0
            requested_discharge = (
                str(partnumber_or_index).upper() == "DISCHARGE" or 
                str(partnumber_or_index) == "0" or
                name == "discharge"
            )
            if not requested_discharge:
                # Provide helpful guidance about model slots
                raise BatteryBackendError(
                    f"Battery model slot '{partnumber_or_index}' is empty.\n"
                    f"  The Keithley 2281S stores battery models in numbered memory slots (0-9).\n"
                    f"  This slot doesn't contain a saved model yet.\n"
                    f"  Options:\n"
                    f"    • Use 'discharge' for basic constant-voltage simulation\n"
                    f"    • Use '18650', 'liion', 'nimh', 'nicd', or 'lead-acid' for common battery types\n"
                    f"    • Save a custom battery model to this slot using the instrument front panel"
                )


    # ----------------------------- state dump -----------------------------

    def print_state(self) -> None:
        enabled = self._is_batt_output_on()
        mode_str = self._mode_string()
        model_str = self._safe_query(":BATT:STAT?", "") or "Custom"

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