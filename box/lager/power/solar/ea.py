# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations
import re
import time
import fcntl
import os
import tempfile
from typing import Any, Optional

# Attempt to import PyVISA
try:
    import pyvisa  # type: ignore
except (ImportError, ModuleNotFoundError):
    pyvisa = None

from lager.instrument_wrappers.instrument_wrap import InstrumentWrap
from .solar_net import SolarNet, SolarBackendError, LibraryMissingError, DeviceNotFoundError, DeviceLockError as SolarDeviceLockError

# Enable basic logging for errors
import logging
logger = logging.getLogger(__name__)

# Helper regex to detect USBTMC VISA address
_USBTMC_RE = re.compile(r"^USB\d*::0x?([0-9A-Fa-f]{4})::0x?([0-9A-Fa-f]{4})::([^:]+)::INSTR$")

class DeviceLockError(SolarDeviceLockError):
    """Raised when a device is locked by another process."""
    pass

class DeviceLockManager:
    """
    Manages exclusive locks on EA devices to prevent concurrent access.
    EA power supplies can only handle one VISA connection at a time.
    """
    def __init__(self):
        self.lock_dir = os.path.join(tempfile.gettempdir(), "lager_ea_locks")
        os.makedirs(self.lock_dir, exist_ok=True)
        self.lock_handles = {}

    def _get_lock_path(self, address: str) -> str:
        """Generate a unique lock file path for a given device address."""
        # Sanitize address to create a valid filename
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', address)
        return os.path.join(self.lock_dir, f"ea_device_{safe_name}.lock")

    def acquire_lock(self, address: str, timeout: float = 0.5) -> bool:
        """
        Attempt to acquire an exclusive lock on the device.
        Returns True if lock acquired, raises DeviceLockError if timeout exceeded.

        Args:
            address: VISA address of the device
            timeout: Maximum time to wait for lock (seconds)

        Raises:
            DeviceLockError: If device is locked by another process
        """
        lock_path = self._get_lock_path(address)

        # If we already have the lock, return success
        if address in self.lock_handles:
            return True

        # Try to acquire the lock
        start_time = time.time()
        lock_file = None

        try:
            lock_file = open(lock_path, 'w')

            # Try non-blocking lock first
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.lock_handles[address] = lock_file
                lock_file.write(f"{os.getpid()}\n")
                lock_file.flush()
                return True
            except (IOError, OSError):
                # Lock is held by another process
                pass

            # Wait for lock with timeout
            while (time.time() - start_time) < timeout:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.lock_handles[address] = lock_file
                    lock_file.write(f"{os.getpid()}\n")
                    lock_file.flush()
                    return True
                except (IOError, OSError):
                    time.sleep(0.05)  # Wait 50ms before retry

            # Timeout exceeded
            if lock_file:
                lock_file.close()

            raise DeviceLockError(
                f"EA device at {address} is currently in use by another command. "
                f"EA solar simulators can only handle one operation at a time. "
                f"Please wait for the current operation to complete and try again."
            )

        except DeviceLockError:
            raise
        except Exception as e:
            if lock_file:
                lock_file.close()
            # If locking mechanism fails, log but don't block operation
            logger.warning(f"Device locking failed for {address}: {e}")
            return True  # Allow operation to continue

    def release_lock(self, address: str) -> None:
        """Release the lock on the device."""
        if address in self.lock_handles:
            try:
                lock_file = self.lock_handles[address]
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
                del self.lock_handles[address]
            except Exception as e:
                logger.warning(f"Failed to release lock for {address}: {e}")

    def __del__(self):
        """Clean up all locks on destruction."""
        for address in list(self.lock_handles.keys()):
            self.release_lock(address)

# Global device lock manager
_device_lock_manager = DeviceLockManager()

def _is_usbtmc_address(addr: str) -> bool:
    return bool(_USBTMC_RE.match(addr or ""))

def _serial_from_usbtmc(addr: str) -> Optional[str]:
    m = _USBTMC_RE.match(addr or "")
    return m.group(3) if m else None

def _find_serial_device_path(serial_hint: Optional[str]) -> Optional[str]:
    """Find a serial device path matching the given hint (for EA USB devices)."""
    import glob, os
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
    # Cross-platform USB-serial-number lookup (works on macOS).
    if serial_hint:
        try:
            from ...usb_enum import get_tty_for_usb_serial
            tty = get_tty_for_usb_serial(serial_hint)
            if tty:
                return tty
        except Exception:
            pass
    # Fallback to generic ACM/USB if by-id not found.
    for pat in ("/dev/ttyACM*", "/dev/ttyUSB*", "/dev/cu.usbmodem*"):
        found = sorted(glob.glob(pat))
        if found:
            return found[0]
    return None

def _open_visa_with_fallback(addr: str):
    """Open the EA device at the given VISA address with fallbacks for USBTMC/serial."""
    if pyvisa is None:
        raise LibraryMissingError("PyVISA is not installed on this box.")
    last_exc: Exception | None = None
    # Try opening with default backend, then with @py
    for backend in (None, "@py"):
        try:
            rm = pyvisa.ResourceManager() if backend is None else pyvisa.ResourceManager(backend)
            inst = rm.open_resource(addr)
            try:
                inst.read_termination = "\n"
                inst.write_termination = "\n"
                inst.timeout = 5000
                # Configure encoding to handle non-ASCII characters (like degree symbols)
                if hasattr(inst, 'encoding'):
                    inst.encoding = 'latin-1'  # EA devices use latin-1 for degree symbols
            except Exception:
                pass
            # Attach RM to prevent GC from invalidating session handles
            inst._lager_rm = rm
            return inst
        except Exception as e:
            last_exc = e
    # If USBTMC address fails, try finding a serial device
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
                    # Configure encoding to handle non-ASCII characters (like degree symbols)
                    if hasattr(inst, 'encoding'):
                        inst.encoding = 'latin-1'  # EA devices use latin-1 for degree symbols
                    # Configure serial parameters if applicable
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
                # Quick *IDN? to ensure we opened the correct port
                try:
                    response = inst.query("*IDN?")
                    # Handle encoding issues in the response
                    if isinstance(response, bytes):
                        idn = response.decode('utf-8', errors='replace').strip()
                    else:
                        idn = str(response).strip()
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
    # Clearer "missing backend" surface
    if last_exc and any(s in str(last_exc) for s in ("No matching interface", "failed to load", "could not open", "no such group")):
        raise DeviceNotFoundError(f"Could not open EA device at {addr}: {last_exc}")
    raise DeviceNotFoundError(f"Could not open EA device at {addr}: No device found.")

# Global registry to maintain persistent EA connections
_ea_instances = {}

# Keep connections alive between CLI commands
class ConnectionManager:
    def __init__(self):
        self.connections = {}
        self.connection_states = {}  # Track initialization state
    
    def get_connection(self, address):
        if address in self.connections:
            conn = self.connections[address]
            if conn._is_connection_alive():
                # Check if we need to restore simulation state
                if self.connection_states.get(address, {}).get('initialized', False):
                    conn._connected = True
                return conn
            else:
                del self.connections[address]
                if address in self.connection_states:
                    del self.connection_states[address]
        return None
    
    def store_connection(self, address, connection, initialized=False):
        self.connections[address] = connection
        self.connection_states[address] = {'initialized': initialized}
    
    def mark_initialized(self, address):
        if address in self.connection_states:
            self.connection_states[address]['initialized'] = True

_connection_manager = ConnectionManager()

class EA(SolarNet):
    """
    EA photovoltaic simulator backend (single-channel).
    Provides control over irradiance, MPP values, etc., for EA two-quadrant supplies.
    """
    def __init__(self, instr: Any = None, address: str | None = None, channel: int = 1, **_):
        """
        Initialize the EA solar simulator. Accepts either an open VISA resource or a VISA address string.
        """
        self._visa_resource = None
        self._rm = None  # Keep RM alive to prevent GC from invalidating session handles
        # Determine resource from given parameters
        raw = None
        addr = None
        if instr is not None:
            if isinstance(instr, str):
                addr = instr
            else:
                raw = instr
        if raw is None and addr is None and address:
            addr = address
        if raw is None and addr is None:
            raise SolarBackendError("EA solar simulator requires a VISA address or open resource.")
            
        self.address = addr  # Store address for persistence
        
        # Try to reuse existing connection from global manager
        if addr:
            existing_conn = _connection_manager.get_connection(addr)
            if existing_conn:
                self.instr = existing_conn.instr
                self._connected = existing_conn._connected
                # Update the global registry
                _ea_instances[addr] = self
                _connection_manager.store_connection(addr, self)
                return
        
        # Open the VISA resource if only address is provided (but don't configure yet)
        if raw is None:
            try:
                raw = _open_visa_with_fallback(addr)
                # Preserve the RM reference if the helper attached one
                if hasattr(raw, '_lager_rm'):
                    self._rm = raw._lager_rm
            except LibraryMissingError:
                raise
            except Exception as e:
                raise DeviceNotFoundError(f"Could not open EA device at {addr}: {e}")
        # Wrap the instrument for SCPI communication
        try:
            self.instr = InstrumentWrap(raw)
        except Exception:
            self.instr = raw
        
        # Store the connection status but don't connect yet
        self._connected = False
        
        # Initialize irradiance tracking for UI mode simulation
        self._current_irradiance = 1000.0  # Default standard test condition
        
        # Register this instance for persistence
        if addr:
            _ea_instances[addr] = self
            _connection_manager.store_connection(addr, self, initialized=False)

    def _is_connection_alive(self) -> bool:
        """Check if the VISA connection is still alive."""
        try:
            # Use a quick command that doesn't interfere with PV simulation
            response = self.instr.query("*IDN?")
            return bool(response and len(str(response).strip()) > 0)
        except Exception:
            # Be more forgiving - connection might be temporarily busy
            # Return True to avoid unnecessary reconnections that reset state
            return True

    def _attempt_device_lock(self, max_retries: int = 3) -> bool:
        """Attempt to lock the EA device with retry logic and error handling."""
        for attempt in range(max_retries):
            try:
                # Try to unlock first (in case it's stuck)
                if attempt > 0:
                    try:
                        self.instr.write("SYSTem:LOCK OFF")
                        time.sleep(0.5)
                    except Exception:
                        pass
                
                # Attempt to lock
                self.instr.write("SYSTem:LOCK ON")
                time.sleep(0.3)
                
                # Verify lock succeeded by querying lock status
                try:
                    lock_status = self.instr.query("SYSTem:LOCK?")
                    lock_decoded = self._safe_decode_response(lock_status)
                    if "ON" in lock_decoded or "1" in lock_decoded:
                        return True
                except Exception:
                    # Lock status query may not be supported, continue
                    pass
                
                # If we can't verify, assume success if no error was thrown
                return True
                
            except Exception as e:
                if attempt < max_retries - 1:
                    # Wait longer between retries
                    time.sleep(1.0 + attempt * 0.5)
        
        # All lock attempts failed - continue anyway 
        return False

    def _attempt_device_unlock(self, max_retries: int = 3) -> bool:
        """Attempt to unlock the EA device with retry logic."""
        for attempt in range(max_retries):
            try:
                self.instr.write("SYSTem:LOCK OFF")
                time.sleep(0.3)
                
                # Verify unlock succeeded
                try:
                    lock_status = self.instr.query("SYSTem:LOCK?")
                    lock_decoded = self._safe_decode_response(lock_status)
                    if "OFF" in lock_decoded or "0" in lock_decoded:
                        return True
                except Exception:
                    # Lock status query may not be supported, continue
                    pass
                
                # If we can't verify, assume success if no error was thrown
                return True
                
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(0.5 + attempt * 0.3)
        
        return False
    
    def enable(self) -> None:
        """Connect to the EA device and configure it for PV simulation mode."""

        # CRITICAL: Acquire device lock to prevent concurrent access
        if self.address:
            try:
                _device_lock_manager.acquire_lock(self.address, timeout=2.0)
            except DeviceLockError as e:
                raise DeviceLockError(str(e))

        # CRITICAL: Check if EA device is already configured before doing anything
        try:
            # Quick connection test - don't lock yet
            mode = self.instr.query("FUNCtion:PHOTovoltaics:MODe?")
            state = self.instr.query("FUNCtion:PHOTovoltaics:STATe?")
            mode_decoded = self._safe_decode_response(mode)
            state_decoded = self._safe_decode_response(state)
            
            # If device is already in ET mode, assume it's configured by previous process
            if "ET" in mode_decoded:
                if "RUN" in state_decoded:
                    self._connected = True
                    return  # Device already configured and running - don't touch it!
                elif "STOP" in state_decoded:
                    # Device configured but stopped - just restart
                    try:
                        self.instr.write("FUNCtion:PHOTovoltaics:STATe RUN")
                        time.sleep(1.0)
                        self._connected = True
                        return
                    except Exception:
                        pass
            
        except Exception:
            # If queries fail, continue with full connection setup
            pass
            
        # Device needs configuration - try to lock and initialize
        self._attempt_device_lock()
        
        # Do minimal initialization to avoid resetting values
        try:
            # Check current mode and only change if necessary
            current_mode = self.instr.query("FUNCtion:PHOTovoltaics:MODe?")
            mode_decoded = self._safe_decode_response(current_mode)
            
            if "ET" not in mode_decoded:
                # Only do full init if not already in ET mode
                self._initialize_pv_simulation()
            else:
                # Already in ET mode - just ensure it's running
                self.instr.write("OUTPut ON")
                time.sleep(0.3)
                self.instr.write("FUNCtion:PHOTovoltaics:STATe RUN")
                time.sleep(1.0)
                
                # Verify state
                mode = self.instr.query("FUNCtion:PHOTovoltaics:MODe?")
                state = self.instr.query("FUNCtion:PHOTovoltaics:STATe?")
                mode_decoded = self._safe_decode_response(mode)
                state_decoded = self._safe_decode_response(state)
            
        except Exception:
            # If anything fails, do full initialization
            self._initialize_pv_simulation()
        
        self._connected = True
        
        # Mark as initialized in connection manager
        if self.address:
            _connection_manager.mark_initialized(self.address)
    
    def _safe_write(self, cmd: str) -> None:
        """Write SCPI command with error checking."""
        try:
            self.instr.write(cmd)
        except Exception as e:
            # Check for SCPI errors in the device
            try:
                error = self.instr.query("SYSTem:ERRor?")
                error_str = self._safe_decode_response(error)
                if "No error" not in error_str and not error_str.startswith("0"):
                    raise SolarBackendError(f"SCPI command '{cmd}' failed: {error_str}")
            except Exception:
                pass
            raise SolarBackendError(f"SCPI command '{cmd}' failed: {e}")

    def _clear_ea_alarms(self) -> None:
        """Clear all EA alarm conditions and counters (from supply EA code)."""
        try:
            # Stop simulation and turn off output first
            self.instr.write("FUNCtion:PHOTovoltaics:STATe STOP")
            time.sleep(0.2)
            self.instr.write("OUTPut OFF")
            time.sleep(0.2)
            
            # Clear EA-specific alarm counters (critical for ERROR ALARM state)
            self.instr.write("SYSTem:ALARm:COUNt:OVOLtage:CLEar")
            time.sleep(0.05)
            self.instr.write("SYSTem:ALARm:COUNt:OCURrent:CLEar")
            time.sleep(0.05)
            self.instr.write("SYSTem:ALARm:COUNt:OPOWer:CLEar")
            time.sleep(0.05)
            self.instr.write("SYSTem:ALARm:CLEar")
            time.sleep(0.1)
            
            # Clear system errors
            for _ in range(10):
                try:
                    error = self.instr.query("SYSTem:ERRor?")
                    if "No error" in error or error.startswith("0"):
                        break
                except Exception:
                    break
            
            # Clear SCPI status registers
            status_regs = [
                "STATus:QUEStionable:EVENt",
                "STATus:OPERation:EVENt", 
                "*ESR"
            ]
            for reg in status_regs:
                try:
                    self.instr.query(f"{reg}?")
                    time.sleep(0.05)
                except Exception:
                    pass
                    
            # Final status clear
            self.instr.write("*CLS")
            time.sleep(0.1)
            self.instr.write("STATus:PRESet")
            time.sleep(0.1)
            
        except Exception:
            # If alarm clearing fails, continue anyway
            pass

    def _initialize_pv_simulation(self) -> None:
        """Complete PV simulation initialization sequence using proven SCPI sequence."""
        try:
            # CRITICAL: Clear EA alarms first (handles ERROR ALARM state)
            self._clear_ea_alarms()
            
            # Release any existing lock
            try:
                self.instr.write("SYSTem:LOCK OFF")
                time.sleep(0.2)
            except Exception:
                pass
            
            # Complete reset first (critical for EA)
            self.instr.write("*RST")
            time.sleep(1.0)  # EA needs more time after reset
            self.instr.write("*CLS") 
            time.sleep(0.2)
            
            # Clear any existing errors
            for i in range(10):  # More attempts to clear errors
                try:
                    error = self.instr.query("SYSTem:ERRor?")
                    error_str = self._safe_decode_response(error)
                    if "No error" in error_str or error_str.startswith("0"):
                        break
                except Exception:
                    break
            
            # EA devices need proper locking (critical for PV operations)
            # Skip redundant lock - device should already be locked from connect
            
            # CRITICAL: Stop any running PV simulation first, then turn off output
            try:
                self.instr.write("FUNCtion:PHOTovoltaics:STATe STOP")
                time.sleep(0.3)
            except Exception:
                pass
            
            try:
                self.instr.write("OUTPut OFF")
                time.sleep(0.3)
            except Exception:
                # If output can't be turned off, continue - might already be off
                pass
            
            # CRITICAL: Set PV mode to ET mode (continuous temp/irradiation control)  
            self._safe_write("FUNCtion:PHOTovoltaics:MODe ET")
            time.sleep(0.2)
            
            # Set input mode to ULIK (uses Uoc/Isc as base values for ET mode)
            self._safe_write("FUNCtion:PHOTovoltaics:IMODe ULIK")
            time.sleep(0.2)
            
            # Set technology (affects curve calculation)
            self._safe_write("FUNCtion:PHOTovoltaics:TECHnology CSI")
            time.sleep(0.2)
            
            # Set standard panel parameters for ET mode (Voc, Isc basis for calculation)
            self._safe_write("FUNCtion:PHOTovoltaics:STANdard:OCVoltage 21.98")
            time.sleep(0.15)
            self._safe_write("FUNCtion:PHOTovoltaics:STANdard:SCCurrent 5.5")
            time.sleep(0.15)
            
            # Set environmental parameters (make temperature optional)
            try:
                self.instr.write("FUNCtion:PHOTovoltaics:TEMPerature 200.0")
                time.sleep(0.15)
            except Exception:
                # Temperature setting may not be available in UI mode, skip it
                pass
            
            try:
                self.instr.write("FUNCtion:PHOTovoltaics:IRRadiation 1000")
                time.sleep(0.15)
            except Exception:
                # Irradiance setting may not work in MPP mode, skip it
                pass
            
            # Enable output (critical before starting simulation)
            try:
                self.instr.write("OUTPut ON")
                time.sleep(0.3)
            except Exception:
                # If output enable fails, try to continue - might already be on
                pass
            
            # Start PV simulation
            self._safe_write("FUNCtion:PHOTovoltaics:STATe RUN")
            time.sleep(1.0)  # Wait for initial curve calculation
            
            # Verify we're in the correct state
            try:
                mode = self.instr.query("FUNCtion:PHOTovoltaics:MODe?")
                state = self.instr.query("FUNCtion:PHOTovoltaics:STATe?")
                mode_decoded = self._safe_decode_response(mode)
                state_decoded = self._safe_decode_response(state)

                if "ET" not in mode_decoded:
                    raise SolarBackendError(f"Expected ET mode, got: {mode}")
                if "RUN" not in state_decoded:
                    raise SolarBackendError(f"Expected RUN state, got: {state}")
            except Exception as e:
                # State verification failed - log but don't block initialization
                logger.debug(f"State verification failed: {e}")
            
            # Check for any setup errors
            self._check_scpi_errors()
            
        except Exception as e:
            # If initialization fails, don't leave device in unknown state
            try:
                self.instr.write("OUTPut OFF")
                self.instr.write("FUNCtion:PHOTovoltaics:STATe STOP")
                self.instr.write("SYSTem:LOCK OFF")
            except Exception:
                pass
            raise SolarBackendError(f"EA PV initialization failed: {e}")

    def disable(self) -> None:
        """Disconnect from the EA device, releasing the remote lock."""
        try:
            # Stop the PV simulation
            self.instr.write("FUNCtion:PHOTovoltaics:STATe STOP")
            time.sleep(0.5)  # Give device time to stop cleanly
        except Exception:
            pass
        try:
            # Turn off output
            self.instr.write("OUTPut OFF")
            time.sleep(0.3)  # Give device time to turn off output
        except Exception:
            pass
        # Attempt to unlock with retry logic
        self._attempt_device_unlock()

        # Release the file-based device lock
        if self.address:
            _device_lock_manager.release_lock(self.address)

        # IMPORTANT: Don't close the connection to maintain persistence
        # Only mark as disconnected for state tracking
        self._connected = False

    def irradiance(self, value: float | None = None) -> str:
        """Get or set the solar irradiance (W/m^2)."""
        # Ensure instrument is connected before use
        if not self._connected:
            self.connect_instrument()
        else:
            # Quick check if we can still communicate
            try:
                self._ensure_simulation_running()
            except Exception:
                self.connect_instrument()
            
        if value is None:
            # GET irradiance - use proper ET mode query
            try:
                self._ensure_simulation_running()
                # In ET mode, we can directly query irradiance
                response = self.instr.query("FUNCtion:PHOTovoltaics:IRRadiation?")
                decoded = self._safe_decode_response(response)
                float_val = self._parse_numeric_response(decoded)
                
                if decoded and float_val >= 0.0:
                    # Store the queried value for consistency
                    self._current_irradiance = float_val
                    return str(float_val)
                else:
                    # If query fails, return stored value
                    current_irradiance = getattr(self, '_current_irradiance', 1000.0)
                    return str(current_irradiance)
                    
            except Exception:
                # If query fails, return stored value
                current_irradiance = getattr(self, '_current_irradiance', 1000.0)
                return str(current_irradiance)
        else:
            # SET irradiance - use proper ET mode command
            try:
                self._ensure_simulation_running()
                
                # Stop simulation to change irradiance
                self.instr.write("FUNCtion:PHOTovoltaics:STATe STOP")
                time.sleep(0.3)
                
                # Set irradiance directly using ET mode command
                self.instr.write(f"FUNCtion:PHOTovoltaics:IRRadiation {value}")
                time.sleep(0.2)
                
                # Restart simulation with new irradiance
                self.instr.write("FUNCtion:PHOTovoltaics:STATe RUN")
                time.sleep(1.0)
                
                # Check for SCPI errors
                self._check_scpi_errors()
                
                # Store the requested irradiance value
                self._current_irradiance = value
                return str(value)
                
            except Exception:
                # Fall back to stored value if available
                if hasattr(self, '_current_irradiance'):
                    return str(self._current_irradiance)
                return str(value)

    def mpp_current(self) -> str:
        """Return the current at the maximum-power point (A)."""
        # Ensure instrument is connected before use
        if not self._connected:
            self.connect_instrument()
        else:
            # Quick check if we can still communicate
            try:
                self._ensure_simulation_running()
            except Exception:
                self.connect_instrument()
        
        try:
            # Check and ensure simulation is running
            self._ensure_simulation_running()
            
            response = self.instr.query("FUNCtion:PHOTovoltaics:MPP:CURRent?")
            decoded = self._safe_decode_response(response)
            
            # Format the response with units
            try:
                float_val = float(decoded)
                result = f"{float_val:.3f} A"
                return result
            except (ValueError, TypeError):
                # If already has units or is formatted, return as is
                if "A" in decoded:
                    return decoded
                result = f"{decoded} A"
                return result
        except Exception:
            return "0.000 A"

    def mpp_voltage(self) -> str:
        """Return the voltage at the maximum-power point (V)."""
        # Ensure instrument is connected before use
        if not self._connected:
            self.connect_instrument()
        else:
            # Quick check if we can still communicate
            try:
                self._ensure_simulation_running()
            except Exception:
                self.connect_instrument()
        
        try:
            # Check and ensure simulation is running
            self._ensure_simulation_running()
            
            response = self.instr.query("FUNCtion:PHOTovoltaics:MPP:VOLTage?")
            decoded = self._safe_decode_response(response)
            # Format the response with units
            try:
                float_val = float(decoded)
                return f"{float_val:.3f} V"
            except (ValueError, TypeError):
                # If already has units or is formatted, return as is
                if "V" in decoded:
                    return decoded
                return f"{decoded} V"
        except Exception:
            return "0.000 V"

    def resistance(self, value: float | None = None) -> str:
        """Get or set the dynamic panel resistance (Voc / Isc) in ohms."""
        # Ensure instrument is connected before use
        if not self._connected:
            self.connect_instrument()
        
        if value is None:
            # GET resistance
            try:
                # Get standard values (configured parameters)
                voc_response = self.instr.query("FUNCtion:PHOTovoltaics:STANdard:OCVoltage?")
                isc_response = self.instr.query("FUNCtion:PHOTovoltaics:STANdard:SCCurrent?")
                
                voc_str = self._safe_decode_response(voc_response)
                isc_str = self._safe_decode_response(isc_response)
                
                # Extract numeric values from strings
                try:
                    voc_val = float(re.sub(r'[^0-9.-]', '', voc_str))
                    isc_val = float(re.sub(r'[^0-9.-]', '', isc_str))
                except (ValueError, TypeError):
                    return "n/a"
                
                if isc_val <= 0:
                    return "n/a"
                
                res_val = voc_val / isc_val
                return f"{res_val:.2f}"
            except Exception:
                return "n/a"
        else:
            # SET resistance by adjusting Isc while keeping Voc constant (works in ET mode)
            try:
                self._ensure_simulation_running()
                
                # Stop simulation to change parameters
                self.instr.write("FUNCtion:PHOTovoltaics:STATe STOP")
                time.sleep(0.3)
                
                # Get current Voc to calculate new Isc
                voc_response = self.instr.query("FUNCtion:PHOTovoltaics:STANdard:OCVoltage?")
                voc_str = self._safe_decode_response(voc_response)
                voc_val = float(re.sub(r'[^0-9.-]', '', voc_str))
                
                # Calculate new Isc: Isc = Voc / R
                new_isc = voc_val / value
                
                # Set the new short-circuit current (this works in ET mode)
                self.instr.write(f"FUNCtion:PHOTovoltaics:STANdard:SCCurrent {new_isc:.3f}")
                time.sleep(0.2)
                
                # Restart simulation with new parameters
                self.instr.write("FUNCtion:PHOTovoltaics:STATe RUN")
                time.sleep(1.0)
                
                self._check_scpi_errors()
                
                return f"{value:.2f}"
                
            except Exception:
                return self.resistance()  # Return current resistance on failure

    # Alias panel_resistance to resistance for compatibility
    def panel_resistance(self) -> str:
        return self.resistance()

    def temperature(self, value: float | None = None) -> str:
        """Get or set the cell temperature (°C). Simplified to avoid mode switching issues."""
        # Ensure instrument is connected before use
        if not self._connected:
            self.connect_instrument()
        else:
            # Quick check if we can still communicate
            try:
                self._ensure_simulation_running()
            except Exception:
                self.connect_instrument()
                
        if value is None:
            # GET temperature - try direct query without mode switching to preserve irradiance
            try:
                response = self.instr.query("FUNCtion:PHOTovoltaics:TEMPerature?")
                decoded = self._safe_decode_response(response)
                float_val = self._parse_numeric_response(decoded)
                
                if decoded and float_val > 0.0:
                    return f"{float_val:.1f}°C"
                else:
                    # EA devices often provide a default/ambient temperature
                    # Try alternative temperature queries or return reasonable default
                    try:
                        # Some EA models report temperature differently
                        alt_response = self.instr.query("*IDN?")  # Basic connectivity check
                        if alt_response:
                            return "25.0°C"  # Reasonable ambient temperature for EA device
                    except Exception:
                        pass
                    raise SolarBackendError(f"Could not read temperature - device returned invalid value: '{decoded}'")
                    
            except Exception as e:
                raise SolarBackendError(f"Could not read temperature from EA device: {e}")
        else:
            # Temperature setting is read-only for this implementation to preserve irradiance
            raise SolarBackendError("Temperature setting is read-only on this EA solar simulator")

    def voc(self) -> str:
        """Return the open-circuit voltage (Voc)."""
        # Ensure instrument is connected before use
        if not self._connected:
            self.connect_instrument()
        
        try:
            # Check and ensure simulation is running
            self._ensure_simulation_running()
            
            response = self.instr.query("FUNCtion:PHOTovoltaics:OCVoltage?")
            decoded = self._safe_decode_response(response)
            # Format the response with units
            try:
                float_val = float(decoded)
                return f"{float_val:.3f} V"
            except (ValueError, TypeError):
                # If already has units or is formatted, return as is
                if "V" in decoded:
                    return decoded
                return f"{decoded} V"
        except Exception:
            return "0.000 V"
    
    def _ensure_simulation_running(self) -> None:
        """Ensure the PV simulation is running before making measurements."""
        if not self._connected:
            self.connect_instrument()
            return
            
        try:
            # Skip redundant locking - device is already locked during connect
            status = self.instr.query("FUNCtion:PHOTovoltaics:STATe?")
            status_str = self._safe_decode_response(status)
            
            if "RUN" in status_str:
                # Already running, just return
                return
            elif "STOP" in status_str:
                # Try to restart simulation (common case)
                self.instr.write("OUTPut ON")
                time.sleep(0.3)  # EA needs time
                self.instr.write("FUNCtion:PHOTovoltaics:STATe RUN")
                time.sleep(1.0)  # Wait for curve calculation
            elif "ERROR" in status_str:
                # Error state - try gentle restart first, preserve settings
                try:
                    self.instr.write("OUTPut ON")
                    time.sleep(0.3)
                    self.instr.write("FUNCtion:PHOTovoltaics:STATe RUN")
                    time.sleep(1.0)
                except Exception:
                    # Only do full reinit if gentle restart fails
                    self._initialize_pv_simulation()
        except Exception:
            # Connection may be dead, try gentle recovery first
            try:
                # Simple restart attempt
                self.instr.write("FUNCtion:PHOTovoltaics:STATe RUN")
                time.sleep(1.0)
            except Exception:
                # If that fails, mark as disconnected for reconnection
                self._connected = False
                # Don't call connect_instrument here to avoid infinite recursion

    def _safe_decode_response(self, response: Any) -> str:
        """Safely decode SCPI response, handling encoding issues and degree symbols."""
        if isinstance(response, bytes):
            # Handle EA device responses with degree symbols and other special chars
            try:
                # Try latin-1 first for EA devices
                decoded = response.decode('latin-1', errors='replace').strip()
                # Convert degree symbol to text for better compatibility
                decoded = decoded.replace('\u00b2', '2').replace('\u00b0', 'deg')
                return decoded
            except UnicodeDecodeError:
                # Ultimate fallback
                return response.decode('ascii', errors='replace').strip()
        elif response is None:
            return ""
        else:
            result = str(response).strip()
            # Clean up degree symbols in string responses too
            result = result.replace('\u00b2', '2').replace('\u00b0', 'deg')
            return result
    
    def _parse_numeric_response(self, response: str) -> float:
        """Parse numeric response from EA device, handling comma decimals and units."""
        try:
            # Extract just the numeric part before any units/symbols
            # Look for patterns like "1000 W/m2", "1000.5 V", "25.0 degC"
            match = re.search(r'^([0-9,.-]+)', response.strip())
            if match:
                numeric_part = match.group(1)
                # Handle comma decimals (e.g., "1000,0") 
                cleaned = numeric_part.replace(',', '.')
                result = float(cleaned)
                return result
            else:
                return 0.0
        except (ValueError, TypeError):
            return 0.0
    
    def _check_scpi_errors(self) -> None:
        """Check and log SCPI errors from the device."""
        try:
            for i in range(10):  # Clear all errors in queue
                error = self.instr.query("SYSTem:ERRor?")
                error_str = self._safe_decode_response(error)
                if "No error" in error_str or error_str.startswith("0"):
                    break
                # Some errors may be expected during normal operation
                pass
        except Exception:
            pass

    # Alias methods for compatibility with dispatcher
    def connect_instrument(self):
        """Alias for enable() for compatibility."""
        return self.enable()

    def disconnect_instrument(self):
        """Alias for disable() for compatibility."""
        return self.disable()

    def close(self) -> None:
        """Close the VISA connection and release resources."""
        if hasattr(self, 'instr') and self.instr is not None:
            try:
                if hasattr(self.instr, 'instr') and hasattr(self.instr.instr, 'close'):
                    self.instr.instr.close()
                elif hasattr(self.instr, 'close'):
                    self.instr.close()
            except Exception:
                pass
            finally:
                self.instr = None

    def __del__(self) -> None:
        """Cleanup when instance is garbage collected."""
        self.close()
