# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
import time

try:
    import pyvisa  # type: ignore
except (ModuleNotFoundError, ImportError):
    pyvisa = None

from lager.instrument_wrappers.instrument_wrap import InstrumentWrapKeithley
from .supply_net import (
    SupplyNet,
    LibraryMissingError,
    DeviceNotFoundError,
    SupplyBackendError,
)

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

# Conservative default current limit (amps) to keep things safe after connect
LAGER_CURRENT_LIMIT = 0.3

# Keithley 2281S-20-6 specifications
# Model: 2281S-20-6 = 20V, 6A, 120W
KEITHLEY_2281S_MAX_VOLTAGE = 20.0  # Volts
KEITHLEY_2281S_MAX_CURRENT = 6.0   # Amps
KEITHLEY_2281S_MIN_VOLTAGE = 0.0   # Volts
KEITHLEY_2281S_MIN_CURRENT = 0.0   # Amps


class Keithley2281S(SupplyNet):
    """
    SupplyNet implementation for Keithley 2281S-20-6 (single channel).

    Hardenings:
      • Tolerant, cached *IDN? check (avoids transient mis-ID after trips).
      • Proper OCP readback via CURR:PROT? (falls back to CURR? if N/A).
      • Tolerant setters with clamp/quantization notes on readback.
      • Explicit Power-Supply entry-function handling for set/enable.
      • Safe protection clear that preserves setpoints.
    """

    def __init__(self, instr=None, address=None, channel: int = 1, reset: bool = False,
                 _owns_resource: bool = True, **_):
        """
        Accepts:
          • instr:   an open VISA resource or a VISA address string
          • address: a VISA address string (used if 'instr' is not a resource)
          • channel: ignored (2281S is single-channel)
          • reset:   perform *RST on connect if True
          • _owns_resource: if False, close() will NOT close the underlying pyvisa
                            session. Set by hardware_service.py when sharing one
                            pyvisa session between this supply driver and the
                            sibling battery driver on the same Keithley 2281S.
        """
        raw = None
        addr = None
        self._rm = None  # Keep RM alive to prevent GC from invalidating session handles
        self._owns_resource = _owns_resource

        # 1) prefer 'instr' if provided
        if instr is not None:
            if isinstance(instr, str):
                addr = instr
            else:
                raw = instr

        # 2) otherwise 'address'
        if raw is None and addr is None and address:
            addr = address

        if raw is None and addr is None:
            raise SupplyBackendError("Keithley2281S requires a VISA address or an open VISA resource.")

        # open VISA if we only have an address string
        if raw is None and isinstance(addr, str):
            if pyvisa is None:
                raise LibraryMissingError("PyVISA library is not installed.")
            try:
                rm = pyvisa.ResourceManager()
                raw = rm.open_resource(addr)
                self._rm = rm
                # best-effort VISA tuning
                try:
                    raw.read_termination = "\n"
                    raw.write_termination = "\n"
                    raw.timeout = 5000  # ms
                except Exception:
                    pass
            except Exception as e:
                raise DeviceNotFoundError(f"Could not open instrument at {addr}: {e}")

        # wrap the resource
        try:
            self.instr = InstrumentWrapKeithley(raw)
        except Exception:
            # fallback to raw VISA if wrapper construction fails
            self.instr = raw  # type: ignore

        self.channel = 1  # single-channel abstraction
        self._idn_cache: str | None = None

        if reset:
            self._reset_instrument()

        self._check_instrument()
        self._apply_lager_safety()

    # -------------------------- SupplyNet API --------------------------

    def voltage(self, value: float | None = None,
                ocp: float | None = None,
                ovp: float | None = None) -> None:
        """
        Set/read voltage. If protections provided, apply them safely.
        """
        # When increasing bounds it is safe to set protections first
        if ocp is not None:
            self._set_ocp(ocp)
        if ovp is not None and value is None:
            # Check for conflict: OVP below current voltage setpoint
            current_vset = self._safe_float(self._get_vset())
            if ovp < current_vset:
                raise SupplyBackendError(f"Cannot set OVP ({ovp}V) below current voltage setpoint ({current_vset}V). Lower voltage first or use voltage command with both values.")
            self._set_ovp(ovp)

        # Tightening OVP while raising/holding V needs careful ordering
        if value is not None and ovp is not None:
            self._apply_voltage_and_ovp_safely(value, ovp)
            print(f"{GREEN}Voltage set to: {value:.4f}V{RESET}")
        elif value is not None:
            self._set_vset(value)
            print(f"{GREEN}Voltage set to: {value:.4f}V{RESET}")

        if value is None and ocp is None and ovp is None:
            v_raw = self._meas_v() if self.output_is_enabled() else self._get_vset()
            # Parse voltage from response which may be verbose format
            v = self._parse_voltage_from_response(v_raw)
            # Format consistently as a simple decimal number
            print(f"{GREEN}Voltage: {float(v):.4f}{RESET}")

    def current(self, value: float | None = None,
                ocp: float | None = None,
                ovp: float | None = None) -> None:
        """
        Set/read current limit. If protections provided, apply them safely.
        """
        if ovp is not None:
            self._set_ovp(ovp)

        if value is not None and ocp is not None:
            self._apply_current_and_ocp_safely(value, ocp)
            print(f"{GREEN}Current set to: {value:.4f}A{RESET}")
        else:
            if value is not None:
                self._set_iset(value)
                print(f"{GREEN}Current set to: {value:.4f}A{RESET}")
            if ocp is not None:
                self._set_ocp(ocp)

        if value is None and ocp is None and ovp is None:
            i = self._meas_i() if self.output_is_enabled() else self._get_iset()
            print(f"{GREEN}Current: {i}{RESET}")

    def enable(self) -> None:
        """
        Turn output ON. Ensures PS mode and proper initialization.
        Handles OVP recovery when voltage setpoint is at or near protection limit.
        """
        self._ensure_ps_mode()

        # Clear any protection states
        try:
            self._write(":OUTP:PROT:CLE", check_errors=False)
            time.sleep(0.05)
        except Exception:
            pass

        # Handle case where voltage setpoint would cause OVP trip
        try:
            current_vset = self._safe_float(self._get_vset())
            current_ovp = self._safe_float(self._safe_query(":SOUR1:VOLT:PROT?", default=str(KEITHLEY_2281S_MAX_VOLTAGE)))

            if current_vset >= current_ovp:
                # V >= OVP requires special recovery: lower voltage, clear trip, raise OVP
                self._write(":OUTP OFF", check_errors=False)
                time.sleep(0.05)

                safe_temp_v = min(current_ovp * 0.9, 1.0) if current_ovp > 0 else 1.0
                self._write(f":SOUR1:VOLT {safe_temp_v}", check_errors=False)
                time.sleep(0.1)

                # Aggressively clear trip state
                self._write(":OUTP:PROT:CLE", check_errors=False)
                time.sleep(0.05)
                self._write("*CLS", check_errors=False)
                time.sleep(0.05)
                self._write(":OUTP:PROT:CLE", check_errors=False)
                time.sleep(0.05)

                # Raise OVP to max
                self._write(f":SOUR1:VOLT:PROT {KEITHLEY_2281S_MAX_VOLTAGE}", check_errors=False)
                time.sleep(0.1)

                # Restore voltage if it has headroom, otherwise keep at safe level
                if current_vset >= KEITHLEY_2281S_MAX_VOLTAGE * 0.99:
                    safe_voltage = KEITHLEY_2281S_MAX_VOLTAGE * 0.9
                    self._write(f":SOUR1:VOLT {safe_voltage}", check_errors=False)
                else:
                    self._write(f":SOUR1:VOLT {current_vset}", check_errors=False)
                time.sleep(0.05)

                # Final trip verification and recovery if needed
                trip_check = self._safe_query_no_mode(":OUTP:PROT:TRIP?", default="")
                if trip_check in ("OVP", "OCP"):
                    self._write(":OUTP OFF", check_errors=False)
                    time.sleep(0.1)
                    self._write("*CLS", check_errors=False)
                    time.sleep(0.1)
                    self._write(":OUTP:PROT:CLE", check_errors=False)
                    time.sleep(0.1)

            elif current_ovp > 0 and current_vset >= current_ovp * 0.95:
                # Near OVP limit - raise temporarily for safety margin
                temp_ovp = min(current_ovp * 1.1, KEITHLEY_2281S_MAX_VOLTAGE)
                self._write(f":SOUR1:VOLT:PROT {temp_ovp}", check_errors=False)
                time.sleep(0.1)
                self._write(":OUTP:PROT:CLE", check_errors=False)
                time.sleep(0.05)
        except Exception:
            pass

        # Ensure continuous initiation is enabled
        try:
            self._write(":INIT:CONT ON", check_errors=False)
            time.sleep(0.05)
        except Exception:
            pass

        # Enable output
        self._write(":OUTP ON", check_errors=False)
        time.sleep(0.15)

        # Final clear of any OVP trip and retry enable if needed
        try:
            trip = self._safe_query_no_mode(":OUTP:PROT:TRIP?", default="")
            if trip in ("OVP", "OCP"):
                self._write(":OUTP:PROT:CLE", check_errors=False)
                time.sleep(0.05)
                self._write(":OUTP ON", check_errors=False)
                time.sleep(0.1)
        except Exception:
            pass

        # Verify output is enabled - retry if needed
        for retry in range(3):
            if self.output_is_enabled():
                break
            try:
                self._write(":OUTP:PROT:CLE", check_errors=False)
                time.sleep(0.05)
                self._write(":OUTP ON", check_errors=False)
                time.sleep(0.15)
            except Exception:
                pass

        # Final verification with error reporting
        if not self.output_is_enabled():
            # Check for protection trips
            try:
                trip = self._safe_query_no_mode(":OUTP:PROT:TRIP?", default="")
                if trip:
                    raise SupplyBackendError(f"Failed to enable output: Protection trip detected ({trip})")
                else:
                    raise SupplyBackendError("Failed to enable output after 3 retries (no protection trip detected)")
            except SupplyBackendError:
                raise
            except Exception:
                raise SupplyBackendError("Failed to enable output after 3 retries")

    def disable(self) -> None:
        """
        Turn output OFF (mode-aware).
        """
        self._write_ps(":OUTP OFF")
        # Allow time for state to propagate
        time.sleep(0.1)

    def set_mode(self) -> None:
        """
        CLI 'set' entry point: ensure Power-Supply entry function (PS mode).
        """
        self._ensure_ps_mode()

    def clear_ocp(self) -> None:
        """
        Clear latched protection (mode-aware and wrapper-safe).
        """
        self._force_clear_protection()

    def clear_ovp(self) -> None:
        """
        Clear latched protection (mode-aware and wrapper-safe).
        """
        self._force_clear_protection()

    def ocp(self, value: float | None = None) -> None:
        """Set or read over-current protection limit"""
        if value is not None:
            # Validate positive value
            if value < 0:
                raise SupplyBackendError(f"OCP limit must be positive, got {value}A")
            self._set_ocp(value)
            return

        # Read current OCP limit
        ocp_limit = self._safe_query_no_mode(":SOUR1:CURR:PROT?", default="n/a")
        print(f"{GREEN}OCP Limit: {ocp_limit}{RESET}")

    def ovp(self, value: float | None = None) -> None:
        """Set or read over-voltage protection limit"""
        if value is not None:
            # Validate positive value
            if value < 0:
                raise SupplyBackendError(f"OVP limit must be positive, got {value}V")

            # Check against current voltage setpoint
            current_vset = self._safe_float(self._get_vset())
            if value < current_vset:
                raise SupplyBackendError(f"OVP limit ({value}V) cannot be less than current voltage setpoint ({current_vset}V)")

            self._set_ovp(value)
            return

        # Read current OVP limit
        ovp_limit = self._safe_query_no_mode(":SOUR1:VOLT:PROT?", default="n/a")
        print(f"{GREEN}OVP Limit: {ovp_limit}{RESET}")

    def state(self) -> None:
        """
        Friendly state dump. Uses measurement if output ON; else setpoints.
        Non-intrusive - does not change instrument mode or state.
        """
        # Check if output is enabled first to avoid disrupting it
        enabled = self.output_is_enabled()
        
        # Get basic setpoints without forcing mode changes - use direct query without mode enforcement
        v_set = self._safe_query_no_mode(":SOUR1:VOLT?", default="0.0")
        i_set = self._safe_query_no_mode(":SOUR1:CURR?", default=f"{LAGER_CURRENT_LIMIT}")

        if enabled:
            # Use actual measurements when output is enabled
            v = self._safe_query_no_mode(":MEAS:VOLT?", default=v_set)
            i = self._safe_query_no_mode(":MEAS:CURR?", default=i_set)
            # Determine if in CV or CC mode by checking QIE register
            mode = self._determine_operating_mode_no_mode()
        else:
            # Use setpoints when output is disabled
            v, i = v_set, i_set
            # When disabled, default to CV mode (status badge already shows OFF)
            mode = "CV"

        # power (best-effort)
        try:
            p = float(v) * float(i)
            p_str = f"{p:.6g}"
        except Exception:
            p_str = "0"

        # Get protection limits and trip status - use non-mode-enforcing queries
        ocp_raw = self._safe_query_no_mode(":SOUR1:CURR:PROT?", default="n/a")
        ovp_raw = self._safe_query_no_mode(":SOUR1:VOLT:PROT?", default="n/a")
        trip = self._safe_query_no_mode(":OUTP:PROT:TRIP?", default="")
        
        ocp_trip = (trip == "OCP")
        ovp_trip = (trip == "OVP")

        print(f"{GREEN}Channel: 1{RESET}")
        print(f"{GREEN}Enabled: {'ON' if enabled else 'OFF'}{RESET}")
        print(f"{GREEN}Mode: {mode}{RESET}")
        print(f"{GREEN}Voltage: {v}{RESET}")
        print(f"{GREEN}Current: {i}{RESET}")
        print(f"{GREEN}Power: {p_str}{RESET}")
        print(f"{GREEN}OCP Limit: {ocp_raw}{RESET}")
        ocp_status = f"{RED}YES{RESET}" if ocp_trip else f"{GREEN}NO{RESET}"
        print(f"    OCP Tripped: {ocp_status}")
        print(f"{GREEN}OVP Limit: {ovp_raw}{RESET}")
        ovp_status = f"{RED}YES{RESET}" if ovp_trip else f"{GREEN}NO{RESET}"
        print(f"    OVP Tripped: {ovp_status}")

    def get_full_state(self) -> None:
        """
        Get full state including measurements, setpoints, and limits.
        Format matches Rigol implementation for TUI compatibility.
        """
        # Check if output is enabled
        enabled = self.output_is_enabled()

        # Get setpoints
        v_set = self._safe_query_no_mode(":SOUR1:VOLT?", default="0.0")
        i_set = self._safe_query_no_mode(":SOUR1:CURR?", default=f"{LAGER_CURRENT_LIMIT}")

        # Get measurements or use setpoints if disabled
        if enabled:
            v = self._safe_query_no_mode(":MEAS:VOLT?", default=v_set)
            i = self._safe_query_no_mode(":MEAS:CURR?", default=i_set)
            mode = self._determine_operating_mode_no_mode()
        else:
            v, i = v_set, i_set
            # When disabled, default to CV mode (status badge already shows OFF)
            mode = "CV"

        # Calculate power
        try:
            p = float(v) * float(i)
            p_str = f"{p:.6g}"
        except Exception:
            p_str = "0"

        # Get protection limits and trip status
        ocp_limit = self._safe_query_no_mode(":SOUR1:CURR:PROT?", default="n/a")
        ovp_limit = self._safe_query_no_mode(":SOUR1:VOLT:PROT?", default="n/a")
        trip = self._safe_query_no_mode(":OUTP:PROT:TRIP?", default="")

        ocp_tripped = (trip == "OCP")
        ovp_tripped = (trip == "OVP")

        # Hardware limits from constants
        v_max = KEITHLEY_2281S_MAX_VOLTAGE
        i_max = KEITHLEY_2281S_MAX_CURRENT

        # Print formatted output matching Rigol format for TUI parsing
        print(f"{GREEN}Channel: 1{RESET}")
        print(f"{GREEN}Enabled: {'ON' if enabled else 'OFF'}{RESET}")
        print(f"{GREEN}Mode: {mode}{RESET}")
        print(f"{GREEN}Voltage: {v}{RESET}")
        print(f"{GREEN}Current: {i}{RESET}")
        print(f"{GREEN}Power: {p_str}{RESET}")
        print(f"{GREEN}Voltage_Set: {v_set}{RESET}")
        print(f"{GREEN}Current_Set: {i_set}{RESET}")
        print(f"{GREEN}OCP Limit: {ocp_limit}{RESET}")
        ocp_status = f"{RED}YES{RESET}" if ocp_tripped else f"{GREEN}NO{RESET}"
        print(f"    OCP Tripped: {ocp_status}")
        print(f"{GREEN}OVP Limit: {ovp_limit}{RESET}")
        ovp_status = f"{RED}YES{RESET}" if ovp_tripped else f"{GREEN}NO{RESET}"
        print(f"    OVP Tripped: {ovp_status}")
        print(f"{GREEN}Voltage_Max: {v_max}{RESET}")
        print(f"{GREEN}Current_Max: {i_max}{RESET}")

    def __str__(self) -> str:
        return self._idn_cache or self._safe_query("*IDN?", default="Keithley 2281S")

    # ------------------------ Low-level helpers ------------------------

    def _check_instrument(self) -> None:
        """
        Tolerant, cached identification. Avoids transient mis-ID during heavy activity.
        """
        idn = self._safe_query("*IDN?", "")
        self._idn_cache = idn or self._idn_cache  # keep prior good value if blank
        if not idn:
            # transient blank; assume correct unit to avoid spurious failures
            return
        if re.search(r"KEITHLEY\s+INSTRUMENTS.*2281S", idn, re.IGNORECASE):
            return
        if "keithley" in idn.lower():
            # Allow related strings quietly
            return
        # Only raise error if we have a non-empty identification that doesn't match
        if idn.strip():
            raise SupplyBackendError(f"Unknown device identification:\n{idn}")

    def _reset_instrument(self) -> None:
        # Clear protection, reset, clear status; brief pause
        try:
            self._write(":OUTP:PROT:CLE", check_errors=False)
        except Exception:
            pass
        self._write("*RST", check_errors=False)
        self._write("*CLS", check_errors=False)
        time.sleep(0.3)

    def _apply_lager_safety(self) -> None:
        """
        Make the instrument safe without provoking errors. Do not force PS mode here.

        NOTE: This method intentionally does NOT:
        - Turn off the output (would interfere with enable/disable state)
        - Set protection limits (would overwrite user-configured OCP/OVP values)

        Safety is achieved by:
        - Initial instrument identification (_check_instrument)
        - User-controlled protection limits via voltage/current commands
        """
        # Previously, this method would turn off output and reset OCP limit,
        # but this interfered with state persistence across CLI commands.
        # Safety is now handled by explicit user commands and protection limits.
        pass

    # --- set/get primitives (prefer tolerant setters) ---

    def _clamp_voltage(self, value: float) -> float:
        """Clamp voltage to instrument limits and warn if clamped."""
        if value < KEITHLEY_2281S_MIN_VOLTAGE:
            print(f"{RED}WARNING: Voltage {value}V below minimum {KEITHLEY_2281S_MIN_VOLTAGE}V, clamping to{RESET} minimum")
            return KEITHLEY_2281S_MIN_VOLTAGE
        if value > KEITHLEY_2281S_MAX_VOLTAGE:
            print(f"{RED}WARNING: Voltage {value}V above maximum {KEITHLEY_2281S_MAX_VOLTAGE}V, clamping to{RESET} maximum")
            return KEITHLEY_2281S_MAX_VOLTAGE
        return value

    def _clamp_current(self, value: float) -> float:
        """Clamp current to instrument limits and warn if clamped."""
        if value < KEITHLEY_2281S_MIN_CURRENT:
            print(f"{RED}WARNING: Current {value}A below minimum {KEITHLEY_2281S_MIN_CURRENT}A, clamping to{RESET} minimum")
            return KEITHLEY_2281S_MIN_CURRENT
        if value > KEITHLEY_2281S_MAX_CURRENT:
            print(f"{RED}WARNING: Current {value}A above maximum {KEITHLEY_2281S_MAX_CURRENT}A, clamping to{RESET} maximum")
            return KEITHLEY_2281S_MAX_CURRENT
        return value

    def _set_vset(self, value: float) -> None:
        # Clamp to valid range before sending to instrument
        clamped_value = self._clamp_voltage(value)

        # Use tolerant_set which now handles OVP conflicts during output state restoration
        self._tolerant_set(":SOUR1:VOLT", clamped_value, unit="V")

    def _get_vset(self) -> str:
        return self._safe_query(":SOUR1:VOLT?", default="0.0")

    def _set_iset(self, value: float) -> None:
        # Clamp to valid range before sending to instrument
        clamped_value = self._clamp_current(value)
        self._tolerant_set(":SOUR1:CURR", clamped_value, unit="A")

    def _get_iset(self) -> str:
        return self._safe_query(":SOUR1:CURR?", default=f"{LAGER_CURRENT_LIMIT}")

    def _set_ovp(self, value: float) -> None:
        """
        Set overvoltage protection. Uses _tolerant_set which handles output state automatically.
        """
        # Clamp to valid range before sending to instrument
        clamped_value = self._clamp_voltage(value)
        self._tolerant_set(":SOUR1:VOLT:PROT", clamped_value, unit="V")

    def _set_ocp(self, value: float) -> None:
        """
        Set overcurrent protection. The 2281S uses SOUR1:CURR:PROT for protection limits.

        Note: Some firmware versions may require output OFF when setting protection limits.
        Protection trips also prevent setting values - we clear them automatically.
        """
        # Clamp to valid range before sending to instrument
        clamped_value = self._clamp_current(value)

        # Ensure we're in PS mode before setting protection
        self._ensure_ps_mode()

        # Check if there's a protection trip condition that needs clearing
        # Use _safe_query_no_mode to avoid triggering QIE error checks
        trip = self._safe_query_no_mode(":OUTP:PROT:TRIP?", default="")
        if trip in ("OCP", "OVP"):
            # Clear protection before attempting to set values
            try:
                self._write(":OUTP OFF", check_errors=False)
                time.sleep(0.05)
                self._write(":OUTP:PROT:CLE", check_errors=False)
                time.sleep(0.05)
                self._drain_error_queue(ignore_codes=(-222, -200, 300, 200))
            except Exception:
                pass

        # Check if output is currently enabled
        output_was_enabled = self.output_is_enabled()

        # Set the overcurrent protection limit with error checks disabled
        self._write(f":SOUR1:CURR:PROT {clamped_value}", check_errors=False)
        time.sleep(0.05)

        # Check for -200 execution error which may indicate output needs to be off
        try:
            self._drain_error_queue(ignore_codes=(-222,))
        except SupplyBackendError as e:
            error_str = str(e)
            # If we got -200/300 error, try with output off and protection cleared
            if ("-200" in error_str or "300" in error_str):
                # Turn output off and clear protections
                self._write(":OUTP OFF", check_errors=False)
                time.sleep(0.05)
                self._write(":OUTP:PROT:CLE", check_errors=False)
                time.sleep(0.05)
                self._drain_error_queue(ignore_codes=(-222, -200, 300, 200))

                # Retry the set command
                self._write(f":SOUR1:CURR:PROT {clamped_value}", check_errors=False)
                time.sleep(0.05)

                # Restore output state if it was enabled
                if output_was_enabled:
                    self._write(":OUTP ON", check_errors=False)
                    time.sleep(0.1)  # Extra delay for output state to stabilize
                    # Drain any errors from enabling output
                    self._drain_error_queue(ignore_codes=(-222, -200, 300, 200))

                # Final drain of any remaining errors after retry
                self._drain_error_queue(ignore_codes=(-222, -200, 300, 200))
            else:
                # Re-raise if it's a different error
                raise

        # Verify the setting took effect - use no-mode query to avoid error checks
        try:
            readback = self._safe_query_no_mode(":SOUR1:CURR:PROT?", default=str(clamped_value))
            rb_float = float(readback)
            if abs(float(clamped_value) - rb_float) > 1e-6:
                print(f"NOTE: requested {clamped_value} A; instrument accepted {rb_float} A")
        except Exception:
            pass

    def _meas_v(self) -> str:
        return self._safe_query(":MEAS:VOLT?", default=self._get_vset())

    def _meas_i(self) -> str:
        return self._safe_query(":MEAS:CURR?", default=self._get_iset())

    def output_is_enabled(self) -> bool:
        try:
            # Try multiple queries with slight delays for robustness
            for _ in range(3):
                val = self._safe_query(":OUTP?", "").strip().upper()
                if val in ("1", "ON"):
                    return True
                elif val in ("0", "OFF"):
                    return False
                # If we get an unclear response, wait and retry
                time.sleep(0.02)
            return False
        except Exception:
            return False

    # --- TUI-required methods (for WebSocket supply monitoring) ---

    def measure_voltage(self, channel=None) -> float:
        """Measure actual output voltage."""
        return self._safe_float(self._meas_v())

    def measure_current(self, channel=None) -> float:
        """Measure actual output current."""
        return self._safe_float(self._meas_i())

    def measure_power(self, channel=None) -> float:
        """Measure actual output power (V * I)."""
        v = self.measure_voltage(channel)
        i = self.measure_current(channel)
        return v * i

    def get_channel_voltage(self, source=None, channel=None) -> float:
        """Get voltage setpoint."""
        return self._safe_float(self._get_vset())

    def get_channel_current(self, source=None, channel=None) -> float:
        """Get current limit setpoint."""
        return self._safe_float(self._get_iset())

    def get_output_mode(self, channel=None) -> str:
        """Get output mode (CV or CC). Keithley 2281S doesn't report mode directly, return CV as default."""
        # 2281S doesn't have explicit CV/CC mode query, would need to compare setpoint vs measurement
        return "CV"

    def get_channel_limits(self, channel=None) -> dict:
        """Get hardware voltage and current limits for this channel."""
        return {
            'voltage_max': KEITHLEY_2281S_MAX_VOLTAGE,
            'current_max': KEITHLEY_2281S_MAX_CURRENT
        }

    def get_overcurrent_protection_value(self, channel=None) -> float:
        """Get OCP limit."""
        ocp_str = self._safe_query(":SOUR1:CURR:PROT?", default=str(KEITHLEY_2281S_MAX_CURRENT))
        return self._safe_float(ocp_str)

    def get_overvoltage_protection_value(self, channel=None) -> float:
        """Get OVP limit."""
        ovp_str = self._safe_query(":SOUR1:VOLT:PROT?", default=str(KEITHLEY_2281S_MAX_VOLTAGE))
        return self._safe_float(ovp_str)

    def overcurrent_protection_is_tripped(self, channel=None) -> bool:
        """Check if OCP is tripped."""
        trip = self._safe_query_no_mode(":OUTP:PROT:TRIP?", default="")
        return trip == "OCP"

    def overvoltage_protection_is_tripped(self, channel=None) -> bool:
        """Check if OVP is tripped."""
        trip = self._safe_query_no_mode(":OUTP:PROT:TRIP?", default="")
        return trip == "OVP"

    # --- tolerant setter / error drain ---

    def _tolerant_set(self, base: str, value: float, unit: str = "") -> None:
        """
        Write without raising on known/benign errors (e.g., -222 Data out of range).
        Then read back and print a note if the instrument accepted a different value.

        Note: Some Keithley 2281S firmware versions require output to be OFF when setting
        voltage/current. We handle this gracefully by retrying with output off if needed.
        Protection trips also prevent setting values - we clear them automatically.
        Output state is always preserved across this operation.
        """
        # Ensure we're in PS mode before setting values
        self._ensure_ps_mode()

        # Check if output is currently enabled - save state at the beginning
        output_was_enabled = self.output_is_enabled()

        # Check if there's a protection trip condition that needs clearing
        # Use _safe_query_no_mode to avoid triggering QIE error checks
        trip = self._safe_query_no_mode(":OUTP:PROT:TRIP?", default="")
        if trip in ("OCP", "OVP"):
            # Clear protection before attempting to set values
            try:
                self._write(":OUTP OFF", check_errors=False)
                time.sleep(0.05)
                self._write(":OUTP:PROT:CLE", check_errors=False)
                time.sleep(0.05)

                # If this is a voltage/protection command and OVP tripped, temporarily raise OVP limit
                if trip == "OVP" and ("VOLT" in base.upper()):
                    # Set OVP to max to allow any voltage setting
                    self._write(f":SOUR1:VOLT:PROT {KEITHLEY_2281S_MAX_VOLTAGE}", check_errors=False)
                    time.sleep(0.05)

                self._drain_error_queue(ignore_codes=(-222, -200, 300, 200))
            except Exception:
                pass

        # Issue write with error checks disabled so we can drain the queue ourselves
        self._write(f"{base} {value}", check_errors=False)

        # Allow time for setting to take effect
        time.sleep(0.05)

        # Check for -200/300 execution/protection errors which may indicate output needs to be off
        try:
            # Only ignore quantization errors on first attempt
            self._drain_error_queue(ignore_codes=(-222,))
        except SupplyBackendError as e:
            error_str = str(e)
            # If we got -200/300 error (execution error or OVP), try with output off and protection cleared
            if ("-200" in error_str or "300" in error_str):
                # Turn output off and clear protections
                self._write(":OUTP OFF", check_errors=False)
                time.sleep(0.05)
                self._write(":OUTP:PROT:CLE", check_errors=False)
                time.sleep(0.05)
                self._drain_error_queue(ignore_codes=(-222, -200, 300, 200))

                # Retry the set command
                self._write(f"{base} {value}", check_errors=False)
                time.sleep(0.05)

                # Final drain of any remaining errors after retry
                self._drain_error_queue(ignore_codes=(-222, -200, 300, 200))
            else:
                # Re-raise if it's a different error
                raise

        # Restore output state if it was disabled during operation
        if output_was_enabled:
            current_state = self.output_is_enabled()
            if not current_state:
                # Ensure OVP is high enough to prevent trip when re-enabling
                try:
                    vset = self._safe_float(self._safe_query_no_mode(f"{base}?", default="0"))
                    ovp = self._safe_float(self._safe_query_no_mode(":SOUR1:VOLT:PROT?", default=str(KEITHLEY_2281S_MAX_VOLTAGE)))
                    if ovp > 0 and vset >= ovp * 0.95:
                        safe_ovp = min(vset * 1.1, KEITHLEY_2281S_MAX_VOLTAGE)
                        self._write(f":SOUR1:VOLT:PROT {safe_ovp}", check_errors=False)
                        time.sleep(0.05)
                except Exception:
                    pass

                try:
                    self._write(":OUTP ON", check_errors=False)
                    time.sleep(0.1)
                    self._drain_error_queue(ignore_codes=(-222, -200, 300, 200))
                except Exception:
                    pass

        # Inform user if instrument quantized the value
        rb = self._safe_query_no_mode(f"{base}?", default=str(value))
        try:
            f_set, f_rb = float(value), float(self._safe_float(rb))
            if abs(f_set - f_rb) > 1e-9:
                u = f" {unit}" if unit else ""
                print(f"NOTE: requested {f_set}{u}; instrument accepted {f_rb}{u}")
        except Exception:
            pass

    def _drain_error_queue(self, ignore_codes: tuple[int, ...] = ()) -> None:
        """
        Consume up to a few errors to avoid surfacing them on the next checked call.
        Ignore codes listed in ignore_codes (e.g., -222 Data out of range).
        """
        for _ in range(4):
            try:
                err = self.instr.query(":SYST:ERR?", check_errors=False).strip()
            except Exception as e:
                # Handle query interruption or communication errors
                err_str = str(e)
                if "INTERRUPTED" in err_str or "interrupted" in err_str.lower():
                    # Query was interrupted - try to clear and recover
                    try:
                        time.sleep(0.1)
                        self._write("*CLS", check_errors=False)
                        time.sleep(0.05)
                    except Exception:
                        pass
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
                # keep draining; don't raise
                continue
            # -410 is Query INTERRUPTED error - can be safely ignored in most cases
            if code == -410:
                continue
            # Any other code is unexpected; raise with the original line
            raise SupplyBackendError(err)
    
    def _determine_operating_mode(self) -> str:
        """
        Determine if the power supply is in CV or CC mode by checking the QIE register.
        """
        try:
            # Query the questionable instrument event register
            qie_reg = self._safe_query(":STATus:QUEStionable:INSTrument:ISUMmary:CONDition?", "0")
            reg_val = int(float(qie_reg))
            
            # Bit 0: CC mode, Bit 1: CV mode
            if reg_val & (1 << 0):  # Bit 0 set = Constant Current
                return "CC"
            elif reg_val & (1 << 1):  # Bit 1 set = Constant Voltage
                return "CV"
            else:
                return "CV"  # Default to CV if unclear
        except Exception:
            return "CV"  # Default fallback

    def _determine_operating_mode_no_mode(self) -> str:
        """
        Determine if the power supply is in CV or CC mode without enforcing PS mode.
        """
        try:
            # Query the questionable instrument event register without mode enforcement
            qie_reg = self._safe_query_no_mode(":STATus:QUEStionable:INSTrument:ISUMmary:CONDition?", "0")
            reg_val = int(float(qie_reg))
            
            # Bit 0: CC mode, Bit 1: CV mode
            if reg_val & (1 << 0):  # Bit 0 set = Constant Current
                return "CC"
            elif reg_val & (1 << 1):  # Bit 1 set = Constant Voltage
                return "CV"
            else:
                return "CV"  # Default to CV if unclear
        except Exception:
            return "CV"  # Default fallback

    # --- protection clear / recovery ---

    def _force_clear_protection(self) -> None:
        """
        Best-effort clear that works even when wrapper pre-checks would block.
        Keeps setpoints (no *RST unless you pass reset=True to __init__).
        """
        # Send critical clears with error checks disabled so QIE/ERR doesn't block
        for cmd in (":OUTP OFF", ":OUTP:PROT:CLE", "*CLS"):
            try:
                self._write(cmd, check_errors=False)
            except Exception:
                pass

        # Ensure PS mode; some firmware paths require this post-trip
        try:
            self._ensure_ps_mode()
        except Exception:
            pass

        # Final clear pass
        try:
            self._write(":OUTP:PROT:CLE", check_errors=False)
        except Exception:
            pass

    # --- entry-mode control ---

    def _stop_activity(self) -> None:
        """Abort any running/armed operation in other entry functions."""
        for cmd in (":ABOR", ":BATT:OUTP OFF", ":OUTP OFF"):
            try:
                self._write(cmd, check_errors=False)
            except Exception:
                pass
        try:
            self._write("*CLS", check_errors=False)
        except Exception:
            pass

    def _ensure_ps_mode(self) -> None:
        """
        Ensure instrument is in Power-Supply entry function.
        Abort running activities; try multiple tokens; set INIT:CONT ON.
        """
        mode = self._safe_query(":ENTR:FUNC?", default="")
        up = mode.upper()
        if "POW" in up or "SUPPLY" in up:
            return

        # Check if output is enabled to avoid disrupting it
        output_enabled = self.output_is_enabled()
        if not output_enabled:
            self._stop_activity()
        else:
            # If output is enabled, try gentler mode switch without stopping activity
            try:
                self._write("*CLS", check_errors=False)
            except Exception:
                pass
        # Try standard Power Supply entry function tokens
        # Based on Keithley 2281S manual, POW is the standard abbreviation
        tokens = ("POW", "POWERSUPPLY")
        for tok in tokens:
            # Only try unquoted format - Keithley 2281S doesn't accept quoted entry function names
            try:
                self._write(f":ENTR:FUNC {tok}", check_errors=False)
                time.sleep(0.1)  # Increased delay for mode switching
                now = self._safe_query(":ENTR:FUNC?", default="")
                if "POW" in now.upper() or "SUPPLY" in now.upper():
                    # Clear any errors that may have accumulated during mode switching
                    try:
                        self._drain_error_queue(ignore_codes=(-104, -222))
                    except Exception:
                        pass
                    try:
                        self._write(":INIT:CONT ON", check_errors=False)
                        time.sleep(0.05)  # Allow INIT:CONT to take effect
                    except Exception:
                        pass
                    return
            except Exception:
                continue

        # Clear any errors from failed mode switching attempts
        try:
            self._drain_error_queue(ignore_codes=(-104, -222))
        except Exception:
            pass

        # If we still aren't in PS, try one gentle re-init of INIT:CONT
        try:
            self._write(":INIT:CONT ON", check_errors=False)
            time.sleep(0.05)
        except Exception:
            pass

    # --- write/query shims with mode-aware retry ---

    def _write_ps(self, cmd: str) -> None:
        """
        Write a PS command. If the unit complains it's not permitted in this mode,
        auto-switch to PS mode and retry once.
        """
        try:
            self._write(cmd)
        except Exception as e:
            msg = str(e)
            if ("Command not permitted in this mode" in msg) or ("700" in msg):
                self._ensure_ps_mode()
                self._write(cmd)
            else:
                raise

    # --- thin wrappers over the instrument I/O ---

    def _write(self, cmd: str, check_errors: bool | None = None) -> None:
        """
        Pass through to wrapper if it provides `write(cmd, check_errors=...)`,
        otherwise fall back to plain write.
        """
        try:
            self.instr.write(cmd, check_errors=False if check_errors is None else check_errors)
        except TypeError:
            self.instr.write(cmd)

    def _query(self, cmd: str) -> str:
        return self.instr.query(cmd)

    def _safe_query(self, cmd: str, default: str = "n/a") -> str:
        try:
            return self._query(cmd).strip()
        except Exception as e:
            # Handle interrupted queries gracefully
            if "INTERRUPTED" in str(e) or "-410" in str(e):
                try:
                    time.sleep(0.1)
                    self._write("*CLS", check_errors=False)
                    time.sleep(0.05)
                    # Retry once after clearing
                    return self._query(cmd).strip()
                except Exception:
                    return default
            return default

    def _safe_query_no_mode(self, cmd: str, default: str = "n/a") -> str:
        """
        Query the instrument without enforcing PS mode. Used for read-only state queries.
        """
        try:
            # Directly query without any mode checks or error handling that might change state
            # Use check_errors=False to avoid querying SYST:ERR which could surface stale errors
            return self.instr.query(cmd, check_errors=False).strip()
        except TypeError:
            # Fallback if wrapper doesn't support check_errors parameter
            try:
                return self.instr.query(cmd).strip()
            except Exception as e:
                # Handle interrupted queries gracefully
                if "INTERRUPTED" in str(e) or "-410" in str(e):
                    try:
                        time.sleep(0.1)
                        self._write("*CLS", check_errors=False)
                        time.sleep(0.05)
                        # Retry once after clearing
                        return self.instr.query(cmd).strip()
                    except Exception:
                        return default
                return default
        except Exception as e:
            # Handle interrupted queries gracefully
            if "INTERRUPTED" in str(e) or "-410" in str(e):
                try:
                    time.sleep(0.1)
                    self._write("*CLS", check_errors=False)
                    time.sleep(0.05)
                    # Retry once after clearing
                    return self.instr.query(cmd, check_errors=False).strip()
                except Exception:
                    return default
            return default

    # --- safe sequencing helpers ---

    def _apply_voltage_and_ovp_safely(self, vset: float, ovp: float) -> None:
        cur_v = self._safe_float(self._get_vset())
        if vset > ovp:
            tmp_ovp = vset + max(0.05, 0.05 * max(vset, 0.1))
            self._set_ovp(tmp_ovp)
            self._set_vset(vset)
            self._set_ovp(ovp)
        else:
            if cur_v > ovp:
                self._set_vset(ovp)
            self._set_vset(vset)
            self._set_ovp(ovp)

    def _apply_current_and_ocp_safely(self, iset: float, ocp: float) -> None:
        cur_i = self._safe_float(self._get_iset())
        if iset > ocp:
            # If requested current exceeds OCP, temporarily raise OCP
            tmp_ocp = iset + max(0.05, 0.05 * max(iset, 0.1))
            self._set_ocp(tmp_ocp)
            self._set_iset(iset)
            self._set_ocp(ocp)
        else:
            # Set OCP first to ensure it's not blocking the current setting
            self._set_ocp(ocp)
            # Then set the current
            self._set_iset(iset)

    @staticmethod
    def _parse_voltage_from_response(response: str) -> str:
        """
        Parse voltage from Keithley response which may be in verbose or simple format.
        Verbose format: "+1.500000E+00A,+3.299153E+00V,+3.369820E+05s"
        Simple format: "3.300"
        """
        response_str = str(response).strip()
        # Check if response contains commas (verbose format)
        if ',' in response_str:
            # Verbose format: extract voltage value (field ending with 'V')
            parts = response_str.split(',')
            for part in parts:
                if part.strip().endswith('V'):
                    # Remove 'V' suffix and return
                    return part.strip()[:-1]
        # Simple format or fallback
        return response_str

    @staticmethod
    def _safe_float(s: str) -> float:
        try:
            return float(str(s).strip().split()[0])
        except Exception:
            return 0.0

    def close(self) -> None:
        """Close the VISA connection and release resources.

        When `_owns_resource` is False, leaves the underlying pyvisa session
        alone (it's owned by hardware_service.py's shared resource cache and
        is also held by the sibling Keithley battery driver). Just drops our
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


def create_device(net_info, *, raw_resource=None):
    """Factory function for hardware_service.

    Extracts the required parameters from net_info dict and creates a Keithley2281S instance.
    This allows hardware_service to instantiate the device without knowing the constructor signature.

    When `raw_resource` is provided (an already-open pyvisa session), the
    returned driver wraps it instead of opening a new session, and is marked
    as a non-owner so close() won't release the underlying USB claim. Used
    by hardware_service.py to share one pyvisa session between the supply
    and battery drivers when both roles are configured on the same Keithley
    2281S USB device — fixes the v0.16.7 dual-role known limitation.
    """
    address = net_info.get('address')
    channel = net_info.get('channel') or net_info.get('pin') or 1
    if raw_resource is not None:
        return Keithley2281S(instr=raw_resource, channel=int(channel), _owns_resource=False)
    return Keithley2281S(instr=address, channel=int(channel))