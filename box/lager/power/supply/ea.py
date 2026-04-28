# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import re
import glob
import time
from typing import Any, Optional

try:
    import pyvisa  # type: ignore
except (ImportError, ModuleNotFoundError):
    pyvisa = None

from lager.instrument_wrappers.instrument_wrap import InstrumentWrap
from .supply_net import (
    SupplyNet,
    SupplyBackendError,
    LibraryMissingError,
    DeviceNotFoundError,
)

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

# Conservative default limit if/when we need to fall back
LAGER_CURRENT_LIMIT = 0.1


# ------------------------- address helpers -------------------------

_USBTMC_RE = re.compile(r"^USB\d*::0x?([0-9A-Fa-f]{4})::0x?([0-9A-Fa-f]{4})::([^:]+)::INSTR$")


def _is_usbtmc_address(addr: str) -> bool:
    return bool(_USBTMC_RE.match(addr or ""))


def _serial_from_usbtmc(addr: str) -> Optional[str]:
    m = _USBTMC_RE.match(addr or "")
    return m.group(3) if m else None


def _find_serial_device_path(serial_hint: Optional[str]) -> Optional[str]:
    """
    Prefer stable /dev/serial/by-id symlinks, else fall back to ttyACM*/ttyUSB*.
    """
    by_id = "/dev/serial/by-id"
    patterns: list[str] = []
    if serial_hint:
        patterns += [
            os.path.join(by_id, f"*{serial_hint}*"),
            os.path.join(by_id, f"*EA*{serial_hint}*"),
            os.path.join(by_id, f"*PSB*{serial_hint}*"),
        ]
    else:
        patterns += [os.path.join(by_id, "*EA*"), os.path.join(by_id, "*PSB*")]

    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            if os.path.islink(path) or os.path.exists(path):
                return path

    for pat in ("/dev/ttyACM*", "/dev/ttyUSB*"):
        found = sorted(glob.glob(pat))
        if found:
            return found[0]

    return None


def _open_visa_with_fallback(addr: str) -> Any:
    """
    Open EA via:
      1) VISA default backend (USBTMC),
      2) VISA '@py' backend,
      3) ASRL on CDC-ACM (/dev/ttyACM*) with a quick *IDN? smoke test.
    """
    if pyvisa is None:
        raise LibraryMissingError("PyVISA is not installed on this box.")

    last_exc: Optional[Exception] = None

    # Try the USBTMC address as-is
    for backend in (None, "@py"):
        try:
            rm = pyvisa.ResourceManager() if backend is None else pyvisa.ResourceManager(backend)
            inst = rm.open_resource(addr)
            try:
                inst.read_termination = "\n"
                inst.write_termination = "\n"
                inst.timeout = 5000
            except Exception:
                pass
            # Attach RM to prevent GC from invalidating session handles
            inst._lager_rm = rm
            return inst
        except Exception as e:
            last_exc = e

    # Fall back to serial (CDC-ACM)
    serial_hint = _serial_from_usbtmc(addr) if _is_usbtmc_address(addr) else None
    dev_path = _find_serial_device_path(serial_hint)
    if dev_path:
        asrl_addr = f"ASRL{dev_path}::INSTR"
        for backend in ("@py", None):
            try:
                rm = pyvisa.ResourceManager() if backend is None else pyvisa.ResourceManager(backend)
                inst = rm.open_resource(asrl_addr)
                try:
                    inst.read_termination = "\n"
                    inst.write_termination = "\n"
                    inst.timeout = 5000
                    # Best-effort serial hints (ignored by USBTMC)
                    if hasattr(inst, "baud_rate"):
                        inst.baud_rate = 115200
                    if hasattr(inst, "data_bits"):
                        inst.data_bits = 8
                    if hasattr(inst, "stop_bits"):
                        inst.stop_bits = 1
                    if hasattr(inst, "parity"):
                        try:
                            from pyvisa.constants import Parity  # type: ignore
                            inst.parity = Parity.none
                        except Exception:
                            inst.parity = "N"
                except Exception:
                    pass

                # Smoke test so we fail fast if we grabbed the wrong port
                try:
                    idn = inst.query("*IDN?").strip()
                    if "EA Elektro-Automatik" not in idn:
                        raise ValueError(f"Unexpected IDN: {idn}")
                except Exception as e:
                    last_exc = e
                    continue

                # Attach RM to prevent GC from invalidating session handles
                inst._lager_rm = rm
                return inst
            except Exception as e:
                last_exc = e

    # Clearer “missing backend” surface
    if last_exc and any(s in str(last_exc) for s in ("No matching interface", "failed to load", "could not open")):
        raise DeviceNotFoundError(f"Could not open EA at {addr}: {last_exc}")

    raise DeviceNotFoundError(f"Could not open EA at {addr}: No device found.")


# ------------------------------- EA backend -------------------------------

class EA(SupplyNet):
    """
    EA PSB (10060-60 / 10080-60) as a single-channel supply, with parity to Rigol/Keithley:
      - OVP/OCP set & read
      - latched trip reporting
      - clear_* that resets latched events (EA uses alarm counters + SCPI status)
    """

    def __init__(self, instr=None, address=None, channel: int = 1, reset: bool = False, **_):
        # Resolve resource (same pattern as Keithley)
        raw = None
        addr = None
        self._rm = None  # Keep RM alive to prevent GC from invalidating session handles

        if instr is not None:
            if isinstance(instr, str):
                addr = instr
            else:
                raw = instr

        if raw is None and addr is None and address:
            addr = address

        if raw is None and addr is None:
            raise SupplyBackendError("EA requires a VISA address or an open VISA resource.")

        if raw is None:
            try:
                raw = _open_visa_with_fallback(addr)
                # Preserve the RM reference if the helper attached one
                if hasattr(raw, '_lager_rm'):
                    self._rm = raw._lager_rm
            except LibraryMissingError:
                raise
            except Exception as e:
                raise DeviceNotFoundError(f"Could not open EA at {addr}: {e}")

        # Wrap like other backends (channel-less wrapper)
        try:
            self.instr = InstrumentWrap(raw)
        except Exception:
            self.instr = raw

        self.channel = 1  # single-channel abstraction

        if reset:
            self._reset_instrument()

        self._check_instrument()
        
        # EA instruments need to be locked to prevent conflicts with other control software
        try:
            self._write("SYSTem:LOCK ON")
        except Exception:
            # Continue if locking fails - might already be locked by us
            pass
            
        # Ensure EA is in standard UI/P mode (voltage/current/power) not resistance mode
        try:
            self._write("SYSTem:CONFig:MODe UIP")
        except Exception:
            pass

    # -------- tiny IO shims (for parity with other backends) --------

    def _write(self, cmd: str) -> None:
        try:
            self.instr.write(cmd)
            # EA supplies can be slow to process commands, especially protection-related ones
            if any(keyword in cmd.upper() for keyword in ['PROTECTION', 'ALARM', 'OUTPUT']):
                time.sleep(0.1)  # Longer delay for critical commands
        except Exception as e:
            # For critical commands, re-raise the exception with more context
            if any(keyword in cmd.upper() for keyword in ['OUTPUT', 'RST', 'CLS', 'PROTECTION']):
                # Check if this is a settings conflict that we can resolve
                if "Settings conflict" in str(e) and "OUTPut" in cmd:
                    # EA is refusing output command - try recovery sequence
                    self._force_hardware_reset()
                    return
                raise SupplyBackendError(f"EA command '{cmd}' failed: {e}")

    def _query(self, cmd: str) -> str:
        return self.instr.query(cmd)

    def _safe_query(self, cmd: str, default: str = "n/a") -> str:
        try:
            result = self._query(cmd).strip()
            # EA supplies sometimes return error codes in response
            # Check for common SCPI error responses and handle them gracefully
            if result.startswith('-') and ',' in result:
                # This looks like an error code (e.g., "-200,Execution error")
                return default
            return result
        except Exception as e:
            return default

    # -------------------------- SCPI primitives --------------------------

    def _check_instrument(self) -> None:
        idn = self._safe_query("*IDN?", "")
        if not re.match(r"EA\s+Elektro-Automatik", idn, re.IGNORECASE):
            raise SupplyBackendError(f"Unknown device identification:\n{idn}")

    def _reset_instrument(self) -> None:
        # Gentle reset + clear status; EA can be slow to settle after *RST
        try:
            self._write("*CLS")
            self._write("*RST")
        except Exception:
            pass
        time.sleep(0.2)
        
    def _force_hardware_reset(self) -> None:
        """
        Force EA hardware reset when stuck in settings conflict state.
        This is more aggressive than _reset_instrument().
        """
        try:
            # Step 1: Force clear all status and errors
            self.instr.write("*CLS")
            time.sleep(0.1)
            
            # Step 2: Reset to factory defaults 
            self.instr.write("*RST")
            time.sleep(0.5)  # EA needs more time after full reset
            
            # Step 3: Set safe defaults
            self.instr.write("SOURce:VOLTage 0")
            time.sleep(0.1)
            self.instr.write("SOURce:CURRent 0.1")
            time.sleep(0.1)
            
            # Step 4: Set reasonable protection limits
            self.instr.write("SOURce:VOLTage:PROTection:LEVel 60")
            time.sleep(0.1)
            self.instr.write("SOURce:CURRent:PROTection:LEVel 60")
            time.sleep(0.1)
            
            # Step 5: Force output off using hardware reset
            self.instr.write("OUTPut OFF")
            time.sleep(0.2)
            
        except Exception:
            # If force reset fails, the EA may need manual intervention
            pass

    def _enabled(self) -> bool:
        # EA might return "1"/"0" instead of "ON"/"OFF"
        resp = self._safe_query("OUTPut?", "OFF").strip().upper()
        return resp in ("ON", "1")

    # Limits - Use proper EA SCPI commands with :LEVel
    def _get_ovp(self) -> str:
        return self._safe_query("SOURce:VOLTage:PROTection:LEVel?", "n/a")

    def _set_ovp(self, volts: float) -> None:
        self._write(f"SOURce:VOLTage:PROTection:LEVel {volts}")

    def _get_ocp(self) -> str:
        return self._safe_query("SOURce:CURRent:PROTection:LEVel?", "n/a")

    def _set_ocp(self, amps: float) -> None:
        self._write(f"SOURce:CURRent:PROTection:LEVel {amps}")

    # Setpoints
    def _set_vset(self, volts: float) -> None:
        self._write(f"SOURce:VOLTage {volts}")

    def _get_vset(self) -> str:
        return self._safe_query("SOURce:VOLTage?", "n/a")

    def _set_iset(self, amps: float) -> None:
        self._write(f"SOURce:CURRent {amps}")

    def _get_iset(self) -> str:
        return self._safe_query("SOURce:CURRent?", "n/a")

    # Measurements
    def _meas_v(self) -> str:
        return self._safe_query("MEASure:SCALar:VOLTage:DC?", "n/a")

    def _meas_i(self) -> str:
        return self._safe_query("MEASure:SCALar:CURRent:DC?", "n/a")

    def _meas_p(self) -> str:
        return self._safe_query("MEASure:SCALar:POWer:DC?", "n/a")

    # Output
    def _out_on(self) -> None:
        # Check if already enabled to avoid conflicts
        if self._enabled():
            return
        self._write("OUTPut ON")

    def _out_off(self) -> None:
        # Check if already off to avoid settings conflicts
        if not self._enabled():
            return
        try:
            self._write("OUTPut OFF")
        except Exception as e:
            # EA might reject OFF command due to settings conflicts
            # Try alternative approach - set voltage to 0 then disable
            try:
                self._write("SOURce:VOLTage 0")
                time.sleep(0.1)
                self._write("OUTPut OFF")
            except Exception:
                # If still failing, the EA might be in a protection state
                # Clear protections first, then try again
                self._clear_latched_events()
                time.sleep(0.2)
                try:
                    self._write("OUTPut OFF")
                except Exception:
                    # Final fallback - EA might need to stay enabled
                    pass

    # -------- latched trips & clear (parity with Rigol/Keithley) --------
    # EA exposes alarm counters; treat counter>0 as “tripped” and provide a clear.

    def overvoltage_protection_is_tripped(self) -> bool:
        # EA power supplies only trip OVP when output is enabled and voltage exceeds protection limit
        # A voltage setpoint above OVP limit while output is disabled is NOT a trip condition
        try:
            # First check if output is enabled - OVP can only trip when output is on
            if not self._enabled():
                return False
                
            # Wait briefly after enable to let EA settle before checking alarm status
            time.sleep(0.1)
                
            # For EA PSB supplies, check the actual alarm counter
            # Only consider it tripped if the counter indicates a real protection event
            cnt = self._safe_query("SYSTem:ALARm:COUNt:OVOLtage?", "0")
            try:
                cnt_val = int(float(cnt))
                # Only report trip if alarm counter > 0 AND we can verify protection is active
                if cnt_val > 0:
                    # Double-check with questionable status register bit 0 (OVP condition)
                    status = self._safe_query("STATus:QUEStionable:CONDition?", "0")
                    status_val = int(float(status))
                    return (status_val & 0x01) != 0  # Bit 0 = OVP condition
                return False
            except (ValueError, TypeError):
                return False
        except Exception:
            # Most conservative fallback: never report false trips
            return False

    def overcurrent_protection_is_tripped(self) -> bool:
        # EA power supplies only trip OCP when output is enabled and current exceeds protection limit
        # A current setpoint above OCP limit while output is disabled is NOT a trip condition
        try:
            # First check if output is enabled - OCP can only trip when output is on
            if not self._enabled():
                return False
                
            # Wait briefly after enable to let EA settle before checking alarm status
            time.sleep(0.1)
                
            # For EA PSB supplies, check the actual alarm counter
            # Only consider it tripped if the counter indicates a real protection event
            cnt = self._safe_query("SYSTem:ALARm:COUNt:OCURrent?", "0")
            try:
                cnt_val = int(float(cnt))
                # Only report trip if alarm counter > 0 AND we can verify protection is active
                if cnt_val > 0:
                    # Double-check with questionable status register bit 1 (OCP condition)
                    status = self._safe_query("STATus:QUEStionable:CONDition?", "0")
                    status_val = int(float(status))
                    return (status_val & 0x02) != 0  # Bit 1 = OCP condition
                return False
            except (ValueError, TypeError):
                return False
        except Exception:
            # Most conservative fallback: never report false trips
            return False

    def _clear_latched_events(self) -> None:
        """
        EA protection clearing using proper SCPI commands from manual:
          1) Disable output and wait for stabilization
          2) Use PROTection:CLEar if available, or clear via status registers
          3) Clear system errors and status registers
          4) Reset status system to known state
        """
        # Step 1: Ensure output is disabled and stable
        try:
            self._write("OUTPut OFF")
            time.sleep(0.2)  # EA needs time to fully disable and settle
        except Exception:
            pass

        # Step 2: Try standard SCPI protection clear first
        try:
            self._write("PROTection:CLEar")
            time.sleep(0.1)
        except Exception:
            # If PROTection:CLEar not supported, fall back to manual clearing
            pass
            
        # Clear any system errors in the error queue
        try:
            # Read and discard all errors to clear the queue
            for _ in range(10):  # Limit to prevent infinite loop
                error = self._safe_query("SYSTem:ERRor?", "0")
                if "No error" in error or error.startswith("0"):
                    break
                time.sleep(0.05)
        except Exception:
            pass

        # Step 3: Clear SCPI standard status registers by reading event registers
        # Reading event registers clears the latched events
        status_regs = [
            "STATus:QUEStionable:EVENt",  # Clears questionable events (OVP, OCP)
            "STATus:OPERation:EVENt",     # Clears operation events
            "*ESR"                        # Standard Event Status Register
        ]
        
        for reg in status_regs:
            try:
                _ = self._safe_query(f"{reg}?", "0")
                time.sleep(0.05)  # Small delay between register clears
            except Exception:
                pass

        # Step 4: Reset status system and clear command error register
        try:
            self._write("*CLS")  # Clear status byte and event registers
            time.sleep(0.1)
        except Exception:
            pass
            
        try:
            self._write("STATus:PRESet")  # Reset status system to power-on state
            time.sleep(0.1)
        except Exception:
            pass

        # Step 5: Final verification - give EA time to fully process all clearing
        time.sleep(0.2)

    def clear_overvoltage_protection_trip(self) -> None:
        self._clear_latched_events()

    def clear_overcurrent_protection_trip(self) -> None:
        self._clear_latched_events()
        
    def _apply_safe_settings_sequence(self, voltage: float | None = None, 
                                     current: float | None = None,
                                     ocp: float | None = None, 
                                     ovp: float | None = None) -> None:
        """
        Apply EA settings with proper sequencing to avoid "Settings conflict" errors.
        The EA PSB supply requires protection limits to be set before setpoints if they
        conflict, and proper sequencing to avoid protection trips during configuration.
        
        Key EA behavior: If voltage setpoint > OVP limit, the EA will limit actual 
        output voltage to below the OVP threshold, not the setpoint. This was causing
        the issue where setting 12V with 10V OVP resulted in ~1.2V output.
        """
        # Only disable output if we're changing voltage and it's enabled
        # EA can change current limits while enabled without conflicts
        was_enabled = self._enabled()
        needs_disable = was_enabled and voltage is not None
        if needs_disable:
            try:
                self._out_off()
                time.sleep(0.2)
            except Exception:
                # If disable fails, continue anyway - EA might handle it
                pass
        
        try:
            # Get current values to determine sequence
            try:
                ovp_str = self._safe_query("SOURce:VOLTage:PROTection:LEVel?", "10")
                ocp_str = self._safe_query("SOURce:CURRent:PROTection:LEVel?", "60")
                vset_str = self._safe_query("SOURce:VOLTage?", "0")
                iset_str = self._safe_query("SOURce:CURRent?", "0")
                
                # Parse values and handle units (EA might return "10.00 V" instead of "10.00")
                current_ovp = float(ovp_str.replace(' V', '').replace('V', ''))
                current_ocp = float(ocp_str.replace(' A', '').replace('A', ''))
                current_vset = float(vset_str.replace(' V', '').replace('V', ''))
                current_iset = float(iset_str.replace(' A', '').replace('A', ''))
                
            except (ValueError, TypeError):
                # Use safe defaults if queries fail
                current_ovp, current_ocp, current_vset, current_iset = 10.0, 60.0, 0.0, 0.0
            
            # EA sequencing strategy (CRITICAL FIX):
            # 1. ALWAYS set protection limits FIRST and high enough for setpoints
            # 2. Then set setpoints
            # 3. Finally set user-requested protection values if specified
            
            # Step 1: Set protection limits high enough to accommodate setpoints
            # This is the key fix - we must ensure OVP/OCP are adequate BEFORE setting V/I
            
            if voltage is not None:
                # Calculate required OVP (setpoint + safety margin, or user value if higher)
                required_ovp = voltage + 5.0  # 5V safety margin
                if ovp is not None:
                    required_ovp = max(required_ovp, ovp)
                
                # EA PSB 10060-60 has max 60V, so cap OVP at reasonable limit
                required_ovp = min(required_ovp, 60.0)
                
                # Only update OVP if current limit is insufficient
                if required_ovp > current_ovp:
                    try:
                        self._set_ovp(required_ovp)
                        time.sleep(0.2)  # Give EA more time to process
                        current_ovp = required_ovp  # Update our tracking
                    except Exception as e:
                        raise SupplyBackendError(f"Failed to set OVP to {required_ovp}V for {voltage}V setpoint: {e}")
                
            if current is not None:
                # Calculate required OCP (setpoint + safety margin, or user value if higher)
                required_ocp = current + 2.0  # 2A safety margin
                if ocp is not None:
                    required_ocp = max(required_ocp, ocp)
                
                # EA PSB 10060-60 has max 60A, so cap OCP at reasonable limit
                required_ocp = min(required_ocp, 60.0)
                
                # Only update OCP if current limit is insufficient
                if required_ocp > current_ocp:
                    try:
                        self._set_ocp(required_ocp)
                        time.sleep(0.2)  # Give EA more time to process
                        current_ocp = required_ocp  # Update our tracking
                    except Exception as e:
                        raise SupplyBackendError(f"Failed to set OCP to {required_ocp}A for {current}A setpoint: {e}")
                
            # Step 2: Now safely set setpoints (protections are adequate)
            if voltage is not None:
                try:
                    # For EA hardware faults, try setting voltage to 0 first, then target
                    if voltage > 1.0:  # Only for non-zero voltages
                        try:
                            self._set_vset(0.0)
                            time.sleep(0.2)
                        except Exception:
                            pass
                    self._set_vset(voltage)
                    time.sleep(0.1)
                except Exception as e:
                    raise SupplyBackendError(f"Failed to set voltage to {voltage}V: {e}")
                
            if current is not None:
                try:
                    self._set_iset(current)
                    time.sleep(0.1)
                except Exception as e:
                    raise SupplyBackendError(f"Failed to set current to {current}A: {e}")
                
            # Step 3: Apply final user-requested protection values if specified
            # Only do this if user explicitly set different values than our calculated ones
            if ovp is not None:
                final_vset = voltage if voltage is not None else current_vset
                if ovp < final_vset:
                    # User wants OVP below setpoint - this will limit output!
                    # Set it anyway but warn through the error system
                    try:
                        self._set_ovp(ovp)
                        time.sleep(0.1)
                        # Note: EA will limit actual output voltage to ~OVP when enabled
                    except Exception as e:
                        raise SupplyBackendError(f"Failed to set final OVP to {ovp}V: {e}")
                elif abs(ovp - current_ovp) > 0.01:  # Different from what we set above
                    try:
                        self._set_ovp(ovp)
                        time.sleep(0.1)
                    except Exception as e:
                        raise SupplyBackendError(f"Failed to set final OVP to {ovp}V: {e}")
                    
            if ocp is not None:
                final_iset = current if current is not None else current_iset
                if ocp < final_iset:
                    # User wants OCP below setpoint - this will limit output!
                    try:
                        self._set_ocp(ocp)
                        time.sleep(0.1)
                        # Note: EA will limit actual current to ~OCP when enabled
                    except Exception as e:
                        raise SupplyBackendError(f"Failed to set final OCP to {ocp}A: {e}")
                elif abs(ocp - current_ocp) > 0.01:  # Different from what we set above
                    try:
                        self._set_ocp(ocp)
                        time.sleep(0.1)
                    except Exception as e:
                        raise SupplyBackendError(f"Failed to set final OCP to {ocp}A: {e}")
                
        finally:
            # Restore output state if we disabled it
            if needs_disable and was_enabled:
                try:
                    self._out_on()
                    time.sleep(0.1)
                except Exception:
                    # If restore fails, leave it disabled
                    pass

    # -------------------------- SupplyNet API --------------------------

    def voltage(self, value: float | None = None,
                ocp: float | None = None,
                ovp: float | None = None) -> None:
        # EA requires careful sequencing to avoid "Settings conflict" errors
        self._apply_safe_settings_sequence(voltage=value, current=None, ocp=ocp, ovp=ovp)
        
        if value is None:
            v = self._meas_v() if self._enabled() else self._get_vset()
            print(f"Voltage: {v if (not v or v.endswith('V')) else f'{v} V'}")

    def current(self, value: float | None = None,
                ocp: float | None = None,
                ovp: float | None = None) -> None:
        # EA requires careful sequencing to avoid "Settings conflict" errors  
        self._apply_safe_settings_sequence(voltage=None, current=value, ocp=ocp, ovp=ovp)
        
        if value is None:
            i = self._meas_i() if self._enabled() else self._get_iset()
            print(f"Current: {i if (not i or i.endswith('A')) else f'{i} A'}")

    def enable(self) -> None:
        # Idempotent: if the output is already on, do NOT toggle it off/on.
        # _clear_latched_events() writes OUTPut OFF as its first step, which
        # would cause a brief (~500ms) drop on a re-enable.
        if self._enabled():
            return

        # Clear any previous protection events before enabling
        self._clear_latched_events()

        # Enable output
        self._out_on()

        # EA supplies need more time to fully enable and settle
        # This prevents spurious protection trips immediately after enable
        time.sleep(0.3)

    def disable(self) -> None:
        # Only try to disable if actually enabled
        if self._enabled():
            self._out_off()
            time.sleep(0.2)

    def set_mode(self) -> None:
        """
        No-op for EA in 'supply' CLI. (EA’s PV/solar simulator mode is handled by `lager solar`.)
        """
        return

    def clear_ocp(self) -> None:
        self.clear_overcurrent_protection_trip()

    def clear_ovp(self) -> None:
        self.clear_overvoltage_protection_trip()

    def ocp(self, value: float | None = None) -> None:
        """Set or read over-current protection limit"""
        if value is not None:
            # Validate positive value
            if value < 0:
                raise SupplyBackendError(f"OCP limit must be positive, got {value}A")
            self._set_ocp(value)
            return

        # Read current OCP limit
        ocp_limit = self._get_ocp()
        print(f"{GREEN}OCP Limit: {ocp_limit}{RESET}")

    def ovp(self, value: float | None = None) -> None:
        """Set or read over-voltage protection limit"""
        if value is not None:
            # Validate positive value
            if value < 0:
                raise SupplyBackendError(f"OVP limit must be positive, got {value}V")

            # Check against current voltage setpoint
            current_vset_str = self._get_vset()
            try:
                current_vset = float(current_vset_str)
                if value < current_vset:
                    raise SupplyBackendError(f"OVP limit ({value}V) cannot be less than current voltage setpoint ({current_vset}V)")
            except (ValueError, TypeError):
                # If we can't parse voltage setpoint, skip the check
                pass

            self._set_ovp(value)
            return

        # Read current OVP limit
        ovp_limit = self._get_ovp()
        print(f"{GREEN}OVP Limit: {ovp_limit}{RESET}")

    def state(self) -> None:
        on = self._enabled()
        v_set = self._get_vset()
        i_set = self._get_iset()

        v = self._meas_v() if on else v_set
        i = self._meas_i() if on else i_set
        try:
            if on:
                p_meas = self._meas_p()
                if p_meas != "n/a":
                    p_str = p_meas if p_meas.endswith("W") else f"{p_meas} W"
                else:
                    # Calculate from voltage and current if direct measurement fails
                    p = float(v) * float(i) if v != "n/a" and i != "n/a" else 0.0
                    p_str = f"{p:.3f} W"
            else:
                p_str = "n/a"
        except Exception:
            p_str = "n/a"

        ocp_raw = self._safe_query("SOURce:CURRent:PROTection:LEVel?", "n/a")
        ocp = ocp_raw if (ocp_raw in ("", "n/a") or ocp_raw.endswith("A")) else f"{ocp_raw} A"

        ovp_raw = self._safe_query("SOURce:VOLTage:PROTection:LEVel?", "n/a")
        ovp = ovp_raw if (ovp_raw in ("", "n/a") or ovp_raw.endswith("V")) else f"{ovp_raw} V"

        ocp_trip = self.overcurrent_protection_is_tripped()
        ovp_trip = self.overvoltage_protection_is_tripped()

        print("Channel: 1")
        print(f"Enabled: {'ON' if on else 'OFF'}")
        print(f"Mode: {'CV' if on else 'OFF'}")
        print(f"Voltage: {v if (not v or v.endswith('V')) else f'{v} V'}")
        print(f"Current: {i if (not i or i.endswith('A')) else f'{i} A'}")
        print(f"Power: {p_str}")
        print(f"OCP Limit: {ocp}")
        print(f"    OCP Tripped: {'YES' if ocp_trip else 'NO'}")
        print(f"OVP Limit: {ovp}")
        print(f"    OVP Tripped: {'YES' if ovp_trip else 'NO'}")

    def get_full_state(self) -> None:
        """
        Get full state including measurements, setpoints, and limits.
        Format matches Rigol implementation for TUI compatibility.
        """
        # Check if output is enabled
        enabled = self._enabled()

        # Get setpoints
        v_set = self._get_vset()
        i_set = self._get_iset()

        # Get measurements if output is on, otherwise use setpoints
        if enabled:
            v = self._meas_v()
            i = self._meas_i()
        else:
            v = v_set
            i = i_set

        # Calculate power
        try:
            if enabled:
                p_meas = self._meas_p()
                if p_meas != "n/a":
                    # Remove unit if present for consistent formatting
                    p_str = p_meas.replace(" W", "") if p_meas.endswith(" W") else p_meas
                else:
                    p = float(v) * float(i) if v != "n/a" and i != "n/a" else 0.0
                    p_str = f"{p:.3f}"
            else:
                p_str = "0"
        except Exception:
            p_str = "0"

        # Get protection limits
        ocp_limit = self._safe_query("SOURce:CURRent:PROTection:LEVel?", "n/a")
        ovp_limit = self._safe_query("SOURce:VOLTage:PROTection:LEVel?", "n/a")

        # Check protection trip status
        ocp_tripped = self.overcurrent_protection_is_tripped()
        ovp_tripped = self.overvoltage_protection_is_tripped()

        # Mode determination (always CV for this supply, status badge already shows ON/OFF)
        mode = "CV"

        # Hardware limits for EA PSB models:
        # PSB 10060-60: 60V/60A max
        # PSB 10080-60: 80V/60A max
        # Query actual hardware limits from device
        try:
            # Query nominal voltage and current limits from device
            v_nom = self._safe_query("SOURce:VOLTage:NOMinal?", "60.0")
            i_nom = self._safe_query("SOURce:CURRent:NOMinal?", "60.0")
            v_max = float(v_nom)
            i_max = float(i_nom)
        except Exception:
            # Fallback to conservative 60V/60A if query fails
            v_max = 60.0
            i_max = 60.0

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

    # TUI-required methods
    def measure_voltage(self, channel=None) -> float:
        """Measure actual output voltage."""
        return self._safe_float(self._meas_v())

    def measure_current(self, channel=None) -> float:
        """Measure actual output current."""
        return self._safe_float(self._meas_i())

    def measure_power(self, channel=None) -> float:
        """Measure actual output power."""
        return self._safe_float(self._meas_p())

    def get_channel_voltage(self, source=None, channel=None) -> float:
        """Get voltage setpoint."""
        return self._safe_float(self._get_vset())

    def get_channel_current(self, source=None, channel=None) -> float:
        """Get current limit setpoint."""
        return self._safe_float(self._get_iset())

    def output_is_enabled(self, channel=None) -> bool:
        """Check if output is enabled."""
        return self._enabled()

    def get_output_mode(self, channel=None) -> str:
        """Get current output mode (CV or CC)."""
        mode_code = self._safe_query("SOUR:FUNC?", "0")
        # EA PSB returns different codes for CV/CC mode
        if "VOLT" in mode_code.upper() or mode_code == "0":
            return "CV"
        elif "CURR" in mode_code.upper() or mode_code == "1":
            return "CC"
        else:
            return "UNKNOWN"

    def get_channel_limits(self, channel=None) -> dict:
        """Get voltage and current limits for the channel."""
        # EA-PSB-9080-170: 80V, 170A
        # EA-PSB-9360-120: 360V, 120A
        # EA-PSB-9750-60: 750V, 60A
        # Try to detect model from IDN
        idn = self._safe_query("*IDN?", "EA PSB")
        if "9080" in idn:
            return {'voltage_max': 80.0, 'current_max': 170.0}
        elif "9360" in idn:
            return {'voltage_max': 360.0, 'current_max': 120.0}
        elif "9750" in idn:
            return {'voltage_max': 750.0, 'current_max': 60.0}
        else:
            # Default to 9360 if unknown
            return {'voltage_max': 360.0, 'current_max': 120.0}

    def get_overcurrent_protection_value(self, channel=None) -> float:
        """Get overcurrent protection limit."""
        return self._safe_float(self._get_ocp())

    def get_overvoltage_protection_value(self, channel=None) -> float:
        """Get overvoltage protection limit."""
        return self._safe_float(self._get_ovp())

    def overcurrent_protection_is_tripped(self, channel=None) -> bool:
        """Check if overcurrent protection is tripped."""
        return self._ocp_tripped()

    def overvoltage_protection_is_tripped(self, channel=None) -> bool:
        """Check if overvoltage protection is tripped."""
        return self._ovp_tripped()

    def __str__(self) -> str:
        return self._safe_query("*IDN?", "EA PSB")


def create_device(net_info):
    """
    Factory used by hardware_service.py:/invoke to instantiate this driver.

    Other supply drivers (rigol_dp800, keithley, keysight_e36000) already
    expose this factory. Without it, hardware_service falls back to a
    class-name lookup; explicit is cheaper than reasoning about the fallback.
    """
    return EA(instr=net_info["address"])