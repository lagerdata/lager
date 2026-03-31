# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Unified driver for Keysight E36xxx series power supplies.

Supports:
- E36200 series: E36233A (dual-output, 30V/20A per channel)
- E36300 series: E36311A, E36312A, E36313A (triple-output)

This module consolidates keysight_e36200.py and keysight_e36300.py into a single
driver since they share identical SCPI commands and only differ in:
- Number of channels (2 for E36200, 3 for E36300)
- OCP/OVP enable commands (explicit for E36200, auto-enabled for E36300)
- Channel limits (vary by model)
"""

import re
import logging
import time

try:
    import pyvisa
except (ImportError, ModuleNotFoundError):
    pyvisa = None

# Try to import USB libraries for kernel driver detachment
try:
    import usb.core
    import usb.util
    USB_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    USB_AVAILABLE = False

from lager.instrument_wrappers.instrument_wrap import InstrumentWrap, InstrumentError
from lager.instrument_wrappers.keysight_defines import (
    InstrumentChannel,
    StatusCondition,
)
from .supply_net import SupplyNet, LibraryMissingError, DeviceNotFoundError, SupplyBackendError

logger = logging.getLogger(__name__)

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

# Conservative default current limit for safety (amps)
LAGER_CURRENT_LIMIT = 1

# Global cache for PyVISA resources to share connections across channels
# Key: VISA address string, Value: (PyVISA resource object, ResourceManager)
# Multiple channels on the same device share the same USB connection
# The ResourceManager is stored alongside to prevent GC from invalidating sessions
_resource_cache = {}


def clear_resource_cache():
    """
    Clear the PyVISA resource cache and close all cached connections.

    This is useful for resetting USB connections or recovering from errors.
    Call this if you need to force reconnection to all devices.
    """
    global _resource_cache
    for addr, (resource, _rm) in _resource_cache.items():
        try:
            resource.close()
            logger.info(f"Closed cached resource: {addr}")
        except Exception as e:
            logger.warning(f"Error closing resource {addr}: {e}")
    _resource_cache.clear()
    logger.info("Resource cache cleared")


def _detach_kernel_driver_from_usb(visa_address):
    """
    Detach kernel driver (usbtmc) from USB device before PyVISA opens it.

    This prevents "Resource busy" errors when the Linux kernel's usbtmc driver
    has already claimed the device.

    Args:
        visa_address: VISA USB address (e.g., "USB0::0x2A8D::0x1102::MY59001048::INSTR")

    Returns:
        True if kernel driver was detached, False if not needed or failed
    """
    if not USB_AVAILABLE:
        logger.debug("USB libraries not available, skipping kernel driver detachment")
        return False

    # Parse USB address to extract VID/PID/Serial
    # Format: USB[board]::vendor::product::serial[::interface]::INSTR
    try:
        parts = visa_address.split("::")
        if len(parts) < 4 or not parts[0].startswith("USB"):
            logger.debug(f"Not a USB VISA address: {visa_address}")
            return False

        # Extract vendor ID (VID) and product ID (PID)
        vid = int(parts[1], 16)  # Convert from hex string
        pid = int(parts[2], 16)
        serial = parts[3] if len(parts) > 3 else None

        logger.debug(f"Parsed USB address: VID=0x{vid:04x}, PID=0x{pid:04x}, Serial={serial}")

        # Find USB device
        if serial:
            dev = usb.core.find(idVendor=vid, idProduct=pid, serial_number=serial)
        else:
            dev = usb.core.find(idVendor=vid, idProduct=pid)

        if dev is None:
            logger.warning(f"USB device not found: VID=0x{vid:04x}, PID=0x{pid:04x}")
            return False

        # Detach kernel driver from all interfaces
        detached = False
        for cfg in dev:
            for intf in cfg:
                if_num = intf.bInterfaceNumber
                try:
                    if dev.is_kernel_driver_active(if_num):
                        logger.info(f"Detaching kernel driver from interface {if_num}")
                        dev.detach_kernel_driver(if_num)
                        detached = True
                except usb.core.USBError as e:
                    logger.warning(f"Could not detach kernel driver from interface {if_num}: {e}")
                except NotImplementedError:
                    # Some backends don't support kernel driver operations
                    logger.debug("Kernel driver detachment not supported by USB backend")
                    pass

        if detached:
            logger.info("Successfully detached kernel driver from USB device")

        return detached

    except Exception as e:
        logger.warning(f"Error while detaching kernel driver: {e}")
        return False


class KeysightE36000(SupplyNet):
    """
    Unified driver for Keysight E36xxx series power supplies.

    Supports E36200 series (E36233A) and E36300 series (E36311A, E36312A, E36313A).
    Model-specific behavior is handled via _is_e36200_series() detection.
    """

    # E36200 has 2 channels, E36300 has 3 channels
    # We allow all possible channels here and validate based on detected model
    ALLOWED_CHANNELS = {InstrumentChannel.CH1, InstrumentChannel.CH2, InstrumentChannel.CH3}

    def __init__(self, instr=None, address=None, channel: int = 1, reset: bool = False, **_):
        """
        Driver for Keysight E36xxx series power supplies.

        Accepts either an open pyvisa resource or a VISA address string.
        `channel` specifies the output channel (1-3) to control.
        If `reset` is True, performs a *RST on connect.
        """
        raw = None
        addr = None

        # Determine address vs raw resource
        if instr is not None:
            if isinstance(instr, str):
                addr = instr
            else:
                raw = instr
        if raw is None and addr is None and address:
            addr = address
        if raw is None and addr is None:
            raise SupplyBackendError("KeysightE36000 requires a VISA address or an open VISA resource.")

        # Open VISA resource if only address provided
        if raw is None:
            if pyvisa is None:
                raise LibraryMissingError("PyVISA library is not installed.")

            # Check cache first - multiple channels share the same USB connection
            if addr in _resource_cache:
                cached_raw, _cached_rm = _resource_cache[addr]
                # Validate the cached resource is still alive
                try:
                    cached_raw.query("*IDN?")
                    raw = cached_raw
                    logger.debug(f"Reusing cached resource for {addr}")
                except Exception:
                    logger.warning(f"Stale cached resource for {addr}, removing and reconnecting")
                    try:
                        cached_raw.close()
                    except Exception:
                        pass
                    del _resource_cache[addr]
                    # raw remains None, fall through to create fresh connection

            if raw is None:
                try:
                    # Detach kernel driver first to prevent "Resource busy" errors
                    _detach_kernel_driver_from_usb(addr)

                    rm = pyvisa.ResourceManager()
                    raw = rm.open_resource(addr)
                    try:
                        raw.read_termination = "\n"
                        raw.write_termination = "\n"
                        raw.timeout = 5000
                    except Exception:
                        pass

                    # Cache the resource and RM for reuse by other channels
                    # Storing RM prevents GC from invalidating the session handle
                    _resource_cache[addr] = (raw, rm)
                    logger.debug(f"Cached new resource for {addr}")

                except Exception as e:
                    raise DeviceNotFoundError(f"Could not open instrument at {addr}: {e}")

        # Wrap the resource for SCPI I/O
        try:
            self.instr = InstrumentWrap(raw)
        except Exception:
            self.instr = raw

        # Set channel (int and as InstrumentChannel enum)
        self.channel = int(channel)
        try:
            self.chan = InstrumentChannel.from_numeric(self.channel)
        except Exception as exc:
            raise SupplyBackendError(f"Unsupported channel {channel}") from exc

        # Detect model from IDN before optionally resetting
        self._detect_model()

        # Optionally reset instrument
        if reset:
            try:
                self.reset_instrument()
            except Exception:
                pass

        # Verify instrument identity
        self.check_instrument()

        # Safe default: output off.
        # Keep existing protection settings unless explicitly resetting.
        try:
            self.disable_output(self.chan)
        except Exception:
            pass
        if reset:
            try:
                self.set_overcurrent_protection(LAGER_CURRENT_LIMIT, self.chan)
                self.enable_overcurrent_protection(self.chan)
            except Exception:
                pass

    def _detect_model(self):
        """Detect model from IDN string and set internal model identifier."""
        try:
            idn = self._query("*IDN?")
            # Parse "Keysight Technologies,E36233A,..." -> "E36233A"
            parts = idn.split(',')
            if len(parts) >= 2:
                self._model = parts[1].strip().upper()
            else:
                self._model = ""
        except Exception:
            self._model = ""

    def _is_e36200_series(self) -> bool:
        """
        Returns True for E36200 series (E36233A), False for E36300 series.

        E36200 series uses explicit OCP/OVP state commands.
        E36300 series auto-enables protection when threshold is set.
        """
        return "E362" in self._model

    def _write(self, cmd: str) -> None:
        """Low-level write to the instrument (no return value)."""
        self.instr.write(cmd)

    def _query(self, cmd: str) -> str:
        """Low-level query to the instrument (returns response string)."""
        return self.instr.query(cmd).strip()

    def _safe_query(self, cmd: str, default: str = "n/a") -> str:
        """Query the instrument, returning a default value if an error occurs."""
        try:
            return self._query(cmd)
        except Exception:
            return default

    def check_instrument(self) -> None:
        """Check that the connected instrument is a supported Keysight E36xxx model."""
        last_error = None
        # Retry IDN query up to 3 times with delay (handles USB/VISA timing issues)
        for attempt in range(3):
            try:
                idn = self._query("*IDN?")
                # Accept E36200 series (E36233A) or E36300 series (E3631xA)
                if re.match(r"Keysight Technologies,E36(2|3)\d\dA,", idn):
                    return  # Success
                else:
                    raise SupplyBackendError(f"Unknown device identification:\n{idn}")
            except SupplyBackendError:
                raise  # Re-raise identification mismatch
            except Exception as e:
                last_error = e
                logger.warning(f"IDN query attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    time.sleep(0.5)  # Wait before retry
        # All retries failed
        raise SupplyBackendError(f"Failed to query device identity after 3 attempts: {last_error}")

    def reset_instrument(self) -> None:
        """
        Perform a device reset and clear status/protection.
        Note: *RST does not clear the error queue, so *CLS is sent afterward.
        """
        try:
            self.clear_protection_errors()
        except Exception:
            pass
        self._write("*RST")
        self._write("*CLS")

    # -------------------- Low-level SCPI command implementations --------------------

    def clear_protection_errors(self, chanlist=None) -> None:
        """
        Clear any latched OVP/OCP protection events for the specified channel(s).
        If chanlist is None, clears protection on all channels.
        """
        if chanlist is not None:
            if isinstance(chanlist, (list, tuple)):
                chan_str = ",".join(str(ch) for ch in chanlist)
            else:
                chan_str = str(chanlist)
            self.instr.write(f":OUTPut:PROTection:CLEar (@{chan_str})", check_errors=False)
        else:
            self.instr.write(":OUTPut:PROTection:CLEar", check_errors=False)

    def get_identification(self) -> str:
        """Return the instrument identification string."""
        return self._query("*IDN?")

    def clear_errors(self) -> None:
        """Clear the instrument's error queue."""
        self._write("*CLS")

    def get_next_error(self) -> str:
        """Retrieve the next message from the error queue (FIFO)."""
        return self._query(":SYSTem:ERRor?")

    def get_status_register(self) -> str:
        """Query the Status Byte (STB) register."""
        return self._query("*STB?")

    def get_event_register(self) -> str:
        """Query the Standard Event Status Register (ESR)."""
        return self._query("*ESR?")

    def get_condition_register(self) -> str:
        """Query the Questionable Condition register."""
        return self._query("STATus:QUEStionable:CONDition?")

    def get_output_condition(self, channel: InstrumentChannel):
        """
        Query whether the specified channel is in CC, CV, off/unregulated, or fault condition.
        Returns a StatusCondition enum.
        """
        n = channel.to_numeric()
        resp = self._query(f"STATus:QUEStionable:INSTrument:ISUMmary{n}:CONDITION?")
        return StatusCondition.from_cmd(resp.strip())

    # -------------------- Overvoltage Protection (OVP) --------------------

    def get_overvoltage_protection(self, chanlist=None):
        """
        Get the OVP threshold (volts) for the specified channel(s).
        Returns a float if one channel, or a list of floats for multiple channels.
        """
        if chanlist is not None:
            # Use numeric channel list (e.g. (@2)); str(InstrumentChannel.CH2) is "CH2" and is invalid SCPI here.
            chan_str = self._chanlist_to_str(chanlist)
            response = self._query(f"SOURce:VOLTage:PROTection? (@{chan_str})")
            if isinstance(chanlist, (list, tuple)):
                return [float(val) for val in response.split(",")]
            return float(response)
        else:
            response = self._query("SOURce:VOLTage:PROTection?")
            return float(response)

    def set_overvoltage_protection(self, volts, chanlist=None) -> None:
        """Set the OVP threshold (volts) for the specified channel(s)."""
        # Validate: OVP must be positive
        if volts < 0:
            raise SupplyBackendError(f"OVP limit must be positive, got {volts}V")

        chan = chanlist if chanlist is not None else self.chan
        try:
            if chanlist is not None:
                chan_str = self._chanlist_to_str(chanlist)
                self._write(f"SOURce:VOLTage:PROTection {volts}, (@{chan_str})")
            else:
                self._write(f"SOURce:VOLTage:PROTection {volts}")
        except InstrumentError as e:
            # Error -222 is "Data out of range" - provide a friendly message
            if len(e.args) >= 1 and e.args[0] == -222:
                limits = self.get_channel_limits(chan)
                v_max = limits.get('voltage_max', 25.0)
                raise SupplyBackendError(
                    f"OVP value {volts}V is out of range. "
                    f"Valid range is approximately 1.5V to {v_max * 1.1:.1f}V for this channel."
                ) from None
            raise

    def enable_overvoltage_protection(self, chanlist=None, channel=None) -> None:
        """
        Enable OVP for the specified channel(s).

        E36200 series requires explicit STATe ON command.
        E36300 series auto-enables OVP when threshold is set (no-op).
        """
        if self._is_e36200_series():
            # Support both chanlist and channel parameters for TUI compatibility
            ch = chanlist if chanlist is not None else channel
            if ch is not None:
                chan_str = self._chanlist_to_str(ch)
                self._write(f"SOURce:VOLTage:PROTection:STATe ON, (@{chan_str})")
            else:
                self._write("SOURce:VOLTage:PROTection:STATe ON")
        # else: E36300 auto-enables OVP when level is set - no-op

    def disable_overvoltage_protection(self, chanlist=None) -> None:
        """Disable OVP for the specified channel(s). Only applicable to E36200 series."""
        if self._is_e36200_series():
            if chanlist is not None:
                chan_str = self._chanlist_to_str(chanlist)
                self._write(f"SOURce:VOLTage:PROTection:STATe OFF, (@{chan_str})")
            else:
                self._write("SOURce:VOLTage:PROTection:STATe OFF")
        # else: E36300 doesn't support disabling OVP

    def is_overvoltage_protection_enabled(self, chanlist=None) -> bool | list[bool]:
        """
        Check if OVP is enabled for the specified channel(s).
        Returns a bool for single channel or list of bools for multiple channels.

        E36200 series: query state explicitly.
        E36300 series: always returns True (auto-enabled).
        """
        if self._is_e36200_series():
            if chanlist is not None:
                chan_str = self._chanlist_to_str(chanlist)
                response = self._query(f"SOURce:VOLTage:PROTection:STATe? (@{chan_str})")
                if isinstance(chanlist, (list, tuple)):
                    return [bool(int(val)) for val in response.split(",")]
                else:
                    return bool(int(response))
            else:
                response = self._query("SOURce:VOLTage:PROTection:STATe?")
                return bool(int(response))
        else:
            # E36300 series always has OVP enabled
            if chanlist is not None and isinstance(chanlist, (list, tuple)):
                return [True] * len(chanlist)
            return True

    def clear_overvoltage_protection(self, chanlist=None) -> None:
        """Clear an OVP event for the specified channel(s)."""
        if chanlist is not None:
            chan_str = self._chanlist_to_str(chanlist)
            self._write(f"SOURce:VOLTage:PROTection:CLEar (@{chan_str})")
        else:
            self._write("SOURce:VOLTage:PROTection:CLEar")

    def is_overvoltage_tripped(self) -> bool:
        """Return True if an OVP condition has tripped (latched), otherwise False."""
        try:
            return bool(int(self._query("SOURce:VOLTage:PROTection:TRIPped?")))
        except Exception:
            return False

    # -------------------- Overcurrent Protection (OCP) --------------------

    def get_overcurrent_protection(self, chanlist=None):
        """
        Get the OCP threshold (amps) for the specified channel(s).
        Returns a float or list of floats.

        Note: On E36300 series, OCP threshold equals the current limit setting.
        """
        if self._is_e36200_series():
            if chanlist is not None:
                self._guard_chanlist(chanlist)
                chan_str = self._chanlist_to_str(chanlist)
                response = self._query(f"SOURce:CURRent:PROTection? (@{chan_str})")
                if isinstance(chanlist, (list, tuple)):
                    return [float(val) for val in response.split(",")]
                else:
                    return float(response)
            else:
                response = self._query("SOURce:CURRent:PROTection?")
                return float(response)
        else:
            # E36300: OCP threshold equals current limit
            return self.get_current(chanlist=chanlist)

    def set_overcurrent_protection(self, current, chanlist=None) -> None:
        """Set the OCP threshold (amps) for the specified channel(s)."""
        # Validate: OCP must be positive
        if current < 0:
            raise SupplyBackendError(f"OCP limit must be positive, got {current}A")

        chan = chanlist if chanlist is not None else self.chan
        try:
            if self._is_e36200_series():
                if chanlist is not None:
                    self._guard_chanlist(chanlist)
                    chan_str = self._chanlist_to_str(chanlist)
                    self._write(f"SOURce:CURRent:PROTection {current}, (@{chan_str})")
                else:
                    self._write(f"SOURce:CURRent:PROTection {current}")
            else:
                # E36300: setting current limit effectively sets OCP threshold
                self.set_current(current, chanlist=chanlist)
        except InstrumentError as e:
            # Error -222 is "Data out of range" - provide a friendly message
            if len(e.args) >= 1 and e.args[0] == -222:
                limits = self.get_channel_limits(chan)
                i_max = limits.get('current_max', 1.0)
                raise SupplyBackendError(
                    f"OCP value {current}A is out of range. "
                    f"Valid range is approximately 0.001A to {i_max:.3f}A for this channel."
                ) from None
            raise

    def enable_overcurrent_protection(self, chanlist=None, channel=None) -> None:
        """
        Enable OCP for the specified channel(s).

        E36200 series requires explicit STATe ON command.
        E36300 series auto-enables OCP when current limit is set (no-op).
        """
        if self._is_e36200_series():
            # Support both chanlist and channel parameters for TUI compatibility
            ch = chanlist if chanlist is not None else channel
            if ch is not None:
                chan_str = self._chanlist_to_str(ch)
                self._write(f"SOURce:CURRent:PROTection:STATe ON, (@{chan_str})")
            else:
                self._write("SOURce:CURRent:PROTection:STATe ON")
        # else: E36300 auto-enables OCP when limit is set - no-op

    def disable_overcurrent_protection(self, chanlist=None) -> None:
        """Disable OCP for the specified channel(s). Only applicable to E36200 series."""
        if self._is_e36200_series():
            if chanlist is not None:
                chan_str = self._chanlist_to_str(chanlist)
                self._write(f"SOURce:CURRent:PROTection:STATe OFF, (@{chan_str})")
            else:
                self._write("SOURce:CURRent:PROTection:STATe OFF")
        # else: E36300 doesn't support disabling OCP

    def is_overcurrent_protection_enabled(self, chanlist=None) -> bool | list[bool]:
        """
        Check if OCP is enabled for the specified channel(s).
        Returns a bool for single channel or list of bools for multiple channels.

        E36200 series: query state explicitly.
        E36300 series: always returns True (auto-enabled).
        """
        if self._is_e36200_series():
            if chanlist is not None:
                chan_str = self._chanlist_to_str(chanlist)
                response = self._query(f"SOURce:CURRent:PROTection:STATe? (@{chan_str})")
                if isinstance(chanlist, (list, tuple)):
                    return [bool(int(val)) for val in response.split(",")]
                else:
                    return bool(int(response))
            else:
                response = self._query("SOURce:CURRent:PROTection:STATe?")
                return bool(int(response))
        else:
            # E36300 series always has OCP enabled
            if chanlist is not None and isinstance(chanlist, (list, tuple)):
                return [True] * len(chanlist)
            return True

    def clear_overcurrent_protection(self, chanlist=None) -> None:
        """Clear an OCP event for the specified channel(s)."""
        if chanlist is not None:
            chan_str = self._chanlist_to_str(chanlist)
            self._write(f"SOURce:CURRent:PROTection:CLEar (@{chan_str})")
        else:
            self._write("SOURce:CURRent:PROTection:CLEar")

    def is_overcurrent_tripped(self) -> bool:
        """Return True if an OCP condition has tripped (latched), otherwise False."""
        try:
            return bool(int(self._query("SOURce:CURRent:PROTection:TRIPped?")))
        except Exception:
            return False

    # -------------------- Voltage/Current Setpoints --------------------

    def set_voltage(self, voltage, chanlist=None) -> None:
        """Set the output voltage (volts) for the specified channel(s)."""
        if chanlist is not None:
            chan_str = self._chanlist_to_str(chanlist)
            self._write(f"SOURce:VOLTage {voltage}, (@{chan_str})")
        else:
            self._write(f"SOURce:VOLTage {voltage}")

    def get_voltage(self, chanlist=None):
        """
        Get the programmed output voltage (volts) for the specified channel(s).
        Returns a float or list of floats.
        """
        if chanlist is not None:
            chan_str = self._chanlist_to_str(chanlist)
            response = self._query(f"SOURce:VOLTage? (@{chan_str})")
            if isinstance(chanlist, (list, tuple)):
                return [float(val) for val in response.split(",")]
            else:
                return float(response)
        else:
            response = self._query("SOURce:VOLTage?")
            return float(response)

    def set_current(self, current, chanlist=None) -> None:
        """Set the output current limit (amps) for the specified channel(s)."""
        if chanlist is not None:
            chan_str = self._chanlist_to_str(chanlist)
            self._write(f"SOURce:CURRent {current}, (@{chan_str})")
        else:
            self._write(f"SOURce:CURRent {current}")

    def get_current(self, chanlist=None):
        """
        Get the programmed output current (amps) for the specified channel(s).
        Returns a float or list of floats.
        """
        if chanlist is not None:
            chan_str = self._chanlist_to_str(chanlist)
            response = self._query(f"SOURce:CURRent? (@{chan_str})")
            if isinstance(chanlist, (list, tuple)):
                return [float(val) for val in response.split(",")]
            else:
                return float(response)
        else:
            response = self._query("SOURce:CURRent?")
            return float(response)

    # -------------------- Measurements --------------------

    def _fix_negative_zero(self, value):
        """Convert tiny negative values to zero to avoid displaying '-0.000'."""
        if isinstance(value, (int, float)) and -0.0005 < value < 0:
            return 0.0
        return value

    def measure_current(self, chanlist=None):
        """
        Measure the actual output current (amps) for the specified channel(s).
        Returns a float or list of floats.
        """
        if chanlist is not None:
            chan_str = self._chanlist_to_str(chanlist)
            response = self._query(f"MEASure:CURRent? (@{chan_str})")
            if isinstance(chanlist, (list, tuple)):
                return [self._fix_negative_zero(float(val)) for val in response.split(",")]
            else:
                return self._fix_negative_zero(float(response))
        else:
            response = self._query("MEASure:CURRent?")
            return self._fix_negative_zero(float(response))

    def measure_voltage(self, chanlist=None):
        """
        Measure the actual output voltage (volts) for the specified channel(s).
        Returns a float or list of floats.
        """
        if chanlist is not None:
            chan_str = self._chanlist_to_str(chanlist)
            response = self._query(f"MEASure:VOLTage? (@{chan_str})")
            if isinstance(chanlist, (list, tuple)):
                return [self._fix_negative_zero(float(val)) for val in response.split(",")]
            else:
                return self._fix_negative_zero(float(response))
        else:
            response = self._query("MEASure:VOLTage?")
            return self._fix_negative_zero(float(response))

    # -------------------- Output On/Off Control --------------------

    def enable_output(self, chanlist=None) -> None:
        """Turn on the output for the specified channel(s)."""
        if chanlist is not None:
            chan_str = self._chanlist_to_str(chanlist)
            self._write(f"OUTPut:STATe ON, (@{chan_str})")
        else:
            self._write("OUTPut:STATe ON")

    def disable_output(self, chanlist=None) -> None:
        """Turn off the output for the specified channel(s)."""
        if chanlist is not None:
            chan_str = self._chanlist_to_str(chanlist)
            self._write(f"OUTPut:STATe OFF, (@{chan_str})")
        else:
            self._write("OUTPut:STATe OFF")

    def output_is_enabled(self, channel=None) -> bool:
        """Return True if the given channel's output is ON, else False."""
        if channel is None:
            channel = self.chan
        # Convert channel to numeric if needed
        if isinstance(channel, InstrumentChannel):
            ch_num = channel.to_numeric()
        else:
            ch_num = int(channel)
        resp = self._query(f"OUTPut:STATe? (@{ch_num})").strip()
        return resp == "1" or resp.upper() == "ON"

    def get_instrument_channel(self) -> InstrumentChannel:
        """Get the currently selected output channel as an InstrumentChannel enum."""
        return InstrumentChannel.from_cmd(self._query("INSTrument:SELect?"))

    def set_instrument_channel_numeric(self, channel: InstrumentChannel) -> None:
        """Select the output channel by numeric ID (1-3)."""
        self._write(f"INSTrument:NSELect {channel.to_numeric()}")

    # -------------------- Helper Methods --------------------

    def _chanlist_to_str(self, chanlist) -> str:
        """
        Convert channel identifier(s) to comma-separated numeric string for SCPI.
        Accepts InstrumentChannel, int, str (like "CH1"), or list/tuple of these.
        """
        def ch_to_str(ch):
            if isinstance(ch, InstrumentChannel):
                return str(ch.to_numeric())
            if isinstance(ch, str) and ch.upper().startswith("CH"):
                return str(InstrumentChannel.from_cmd(ch).to_numeric())
            return str(ch)
        if isinstance(chanlist, (list, tuple)):
            return ",".join(ch_to_str(ch) for ch in chanlist)
        else:
            return ch_to_str(chanlist)

    def _guard_channel(self, channel) -> None:
        """Validate that the channel is one of the allowed channels for this model."""
        # Convert integer to InstrumentChannel if needed
        if isinstance(channel, int):
            try:
                channel = InstrumentChannel.from_numeric(channel)
            except Exception:
                raise SupplyBackendError(f"{channel} is not a valid channel for E36xxx series.")
        if channel not in self.ALLOWED_CHANNELS:
            raise SupplyBackendError(f"{channel} is not a valid channel for E36xxx series.")

        # Additional check for E36200 series (only 2 channels)
        if self._is_e36200_series() and channel == InstrumentChannel.CH3:
            raise SupplyBackendError(f"Channel 3 is not available on E36200 series (dual-output).")

    def _guard_chanlist(self, chanlist) -> None:
        """Validate a channel or list of channels."""
        if isinstance(chanlist, (list, tuple)):
            for ch in chanlist:
                self._guard_channel(ch)
        else:
            self._guard_channel(chanlist)

    # -------------------- SupplyNet API implementation --------------------

    def voltage(self, value: float | None = None, ocp: float | None = None, ovp: float | None = None) -> None:
        """
        Set or read the output voltage for this channel.
        If `value` is provided, sets the voltage (and optional OCP/OVP limits).
        If `value` is None, reads and prints the present voltage.
        """
        # Set operations (setpoint first, then protections)
        if value is not None:
            self.set_voltage(value, self.chan)
        if ocp is not None:
            self.set_overcurrent_protection(ocp, self.chan)
            self.enable_overcurrent_protection(self.chan)
        if ovp is not None:
            self.set_overvoltage_protection(ovp, self.chan)
            self.enable_overvoltage_protection(self.chan)
        # Read operation
        if value is None:
            if self.output_is_enabled(self.chan):
                try:
                    v = self.measure_voltage(self.chan)
                except Exception:
                    v = self.get_voltage(self.chan)
            else:
                v = self.get_voltage(self.chan)
            v_str = "n/a"
            if v is not None:
                v_str = str(v)
                if v_str.lower() == "nan" or v_str == "":
                    v_str = "n/a"
                elif not v_str.endswith("V"):
                    v_str += " V"
            print(f"Voltage: {v_str}")

    def current(self, value: float | None = None, ocp: float | None = None, ovp: float | None = None) -> None:
        """
        Set or read the output current for this channel.
        If `value` is provided, sets the current (and optional OCP/OVP limits).
        If `value` is None, reads and prints the present current.
        """
        if value is not None:
            self.set_current(value, self.chan)
        if ocp is not None:
            self.set_overcurrent_protection(ocp, self.chan)
            self.enable_overcurrent_protection(self.chan)
        if ovp is not None:
            self.set_overvoltage_protection(ovp, self.chan)
            self.enable_overvoltage_protection(self.chan)
        if value is None:
            if self.output_is_enabled(self.chan):
                try:
                    i = self.measure_current(self.chan)
                except Exception:
                    i = self.get_current(self.chan)
            else:
                i = self.get_current(self.chan)
            i_str = "n/a"
            if i is not None:
                i_str = str(i)
                if i_str.lower() == "nan" or i_str == "":
                    i_str = "n/a"
                elif not i_str.endswith("A"):
                    i_str += " A"
            print(f"Current: {i_str}")

    def enable(self) -> None:
        """Enable (turn on) the output for this channel."""
        self.enable_output(self.chan)

    def disable(self) -> None:
        """Disable (turn off) the output for this channel."""
        self.disable_output(self.chan)

    def set_mode(self) -> None:
        """(No-op) Keysight E36xxx series is always in power supply mode."""
        return  # This instrument has no alternate modes.

    def state(self) -> None:
        """
        Print a summary of the supply state for this channel, including
        setpoints, measurements, and protection status.
        """
        try:
            enabled = self.output_is_enabled(self.chan)
        except Exception:
            enabled = False
        # Get setpoints
        try:
            v_set = self.get_voltage(self.chan)
        except Exception:
            v_set = "n/a"
        try:
            i_set = self.get_current(self.chan)
        except Exception:
            i_set = "n/a"
        # Get measurements if output is on
        if enabled:
            try:
                v = self.measure_voltage(self.chan)
            except Exception:
                v = v_set
            try:
                i = self.measure_current(self.chan)
            except Exception:
                i = i_set
        else:
            v = v_set
            i = i_set
        # Compute power (if numeric values)
        try:
            p = float(v) * float(i)
            p_str = f"{p:.6g}"
        except Exception:
            p_str = "n/a"
        # Get protection limits
        try:
            ocp_lim = self.get_overcurrent_protection(self.chan)
        except Exception:
            ocp_lim = "n/a"
        try:
            ovp_lim = self.get_overvoltage_protection(self.chan)
        except Exception:
            ovp_lim = "n/a"
        ocp_str = str(ocp_lim) if ocp_lim not in (None, "n/a") else "n/a"
        ovp_str = str(ovp_lim) if ovp_lim not in (None, "n/a") else "n/a"
        if ocp_str and ocp_str not in ("", "n/a") and not ocp_str.endswith("A"):
            ocp_str += " A"
        if ovp_str and ovp_str not in ("", "n/a") and not ovp_str.endswith("V"):
            ovp_str += " V"
        # Check protection status
        ocp_tripped = self.is_overcurrent_tripped()
        ovp_tripped = self.is_overvoltage_tripped()
        try:
            ocp_enabled = self.is_overcurrent_protection_enabled(self.chan)
        except Exception:
            ocp_enabled = True  # assume enabled if query fails
        try:
            ovp_enabled = self.is_overvoltage_protection_enabled(self.chan)
        except Exception:
            ovp_enabled = True
        if not ocp_enabled:
            ocp_str = "OFF"
        if not ovp_enabled:
            ovp_str = "OFF"
        # Determine operating mode (CV/CC) if enabled
        if enabled:
            try:
                cond = self.get_output_condition(InstrumentChannel.from_numeric(self.channel))
                if cond == StatusCondition.CONSTANT_VOLTAGE:
                    mode_str = "CV"
                elif cond == StatusCondition.CONSTANT_CURRENT:
                    mode_str = "CC"
                elif cond == StatusCondition.HARDWARE_FAILURE:
                    mode_str = "FAIL"
                else:
                    mode_str = "UNREG"
            except Exception:
                mode_str = "CV"
        else:
            mode_str = "OFF"
        # Format voltage and current for display
        v_disp = str(v) if v not in (None, "") else "n/a"
        i_disp = str(i) if i not in (None, "") else "n/a"
        if v_disp not in ("n/a", "") and not v_disp.endswith("V"):
            v_disp += " V"
        if i_disp not in ("n/a", "") and not i_disp.endswith("A"):
            i_disp += " A"
        # Print state
        print(f"Channel: {self.channel}")
        print(f"Enabled: {'ON' if enabled else 'OFF'}")
        print(f"Mode: {mode_str}")
        print(f"Voltage: {v_disp}")
        print(f"Current: {i_disp}")
        print(f"Power: {p_str}")
        print(f"OCP Limit: {ocp_str}")
        print(f"    OCP Tripped: {'YES' if ocp_tripped else 'NO'}")
        print(f"OVP Limit: {ovp_str}")
        print(f"    OVP Tripped: {'YES' if ovp_tripped else 'NO'}")

    def get_full_state(self) -> None:
        """
        Get full state including measurements, setpoints, and limits.
        Format matches Rigol implementation for TUI compatibility.
        """
        # Check if output is enabled
        try:
            enabled = self.output_is_enabled(self.chan)
        except Exception:
            enabled = False

        # Get setpoints
        try:
            v_set = self.get_voltage(self.chan)
        except Exception:
            v_set = "0.0"
        try:
            i_set = self.get_current(self.chan)
        except Exception:
            i_set = "0.0"

        # Get measurements if output is on, otherwise use setpoints
        if enabled:
            try:
                v = self.measure_voltage(self.chan)
            except Exception:
                v = v_set
            try:
                i = self.measure_current(self.chan)
            except Exception:
                i = i_set
        else:
            v = v_set
            i = i_set

        # Calculate power
        try:
            p = float(v) * float(i)
            p_str = f"{p:.6g}"
        except Exception:
            p_str = "0"

        # Get protection limits
        try:
            ocp_limit = self.get_overcurrent_protection(self.chan)
        except Exception:
            ocp_limit = "n/a"
        try:
            ovp_limit = self.get_overvoltage_protection(self.chan)
        except Exception:
            ovp_limit = "n/a"

        # Check protection trip status
        ocp_tripped = self.is_overcurrent_tripped()
        ovp_tripped = self.is_overvoltage_tripped()

        # Determine mode (CV/CC) if enabled
        if enabled:
            try:
                cond = self.get_output_condition(InstrumentChannel.from_numeric(self.channel))
                if cond == StatusCondition.CONSTANT_VOLTAGE:
                    mode = "CV"
                elif cond == StatusCondition.CONSTANT_CURRENT:
                    mode = "CC"
                elif cond == StatusCondition.HARDWARE_FAILURE:
                    mode = "FAIL"
                else:
                    mode = "UNREG"
            except Exception:
                mode = "CV"
        else:
            # When disabled, default to CV mode (status badge already shows OFF)
            mode = "CV"

        # Get hardware limits
        limits = self.get_channel_limits(self.channel)
        v_max = limits.get('voltage_max', 30.0)
        i_max = limits.get('current_max', 20.0)

        # Print formatted output matching Rigol format for TUI parsing
        print(f"{GREEN}Channel: {self.channel}{RESET}")
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

    def clear_ocp(self) -> None:
        """Clear an OCP trip (over-current protection) on this channel."""
        self.clear_overcurrent_protection(self.chan)

    def clear_ovp(self) -> None:
        """Clear an OVP trip (over-voltage protection) on this channel."""
        self.clear_overvoltage_protection(self.chan)

    def ocp(self, value: float | None = None) -> None:
        """Set or read over-current protection limit."""
        if value is not None:
            # Validation is now in set_overcurrent_protection()
            self.set_overcurrent_protection(value, self.chan)
            return

        # Read current OCP limit
        ocp_limit = self.get_overcurrent_protection(self.chan)
        print(f"{GREEN}OCP Limit: {ocp_limit}{RESET}")

    def ovp(self, value: float | None = None) -> None:
        """Set or read over-voltage protection limit."""
        if value is not None:
            # Validation is now in set_overvoltage_protection()
            self.set_overvoltage_protection(value, self.chan)
            return

        # Read current OVP limit
        ovp_limit = self.get_overvoltage_protection(self.chan)
        print(f"{GREEN}OVP Limit: {ovp_limit}{RESET}")

    # -------------------- TUI-required methods --------------------

    def measure_power(self, channel=None) -> float:
        """Measure actual output power (V * I)."""
        chan = channel if channel is not None else self.chan
        v = self.measure_voltage(chan)
        i = self.measure_current(chan)
        return v * i

    def get_channel_voltage(self, source=None, channel=None) -> float:
        """Get voltage setpoint."""
        chan = channel if channel is not None else (source if source is not None else self.chan)
        return self.get_voltage(chan)

    def get_channel_current(self, source=None, channel=None) -> float:
        """Get current limit setpoint."""
        chan = channel if channel is not None else (source if source is not None else self.chan)
        return self.get_current(chan)

    def get_output_mode(self, channel=None) -> str:
        """Get output mode (CV or CC)."""
        chan = channel if channel is not None else self.chan
        if isinstance(chan, int):
            chan = InstrumentChannel.from_numeric(chan)
        try:
            cond = self.get_output_condition(chan)
            if cond == StatusCondition.CONSTANT_VOLTAGE:
                return "CV"
            elif cond == StatusCondition.CONSTANT_CURRENT:
                return "CC"
        except Exception:
            pass
        return "CV"  # Default

    def get_channel_limits(self, channel=None) -> dict:
        """
        Get hardware voltage and current limits for this channel.

        Returns model-specific limits based on detected model.
        """
        chan = channel if channel is not None else self.chan
        # Convert to numeric if it's an InstrumentChannel
        if hasattr(chan, 'to_numeric'):
            chan_num = chan.to_numeric()
        else:
            chan_num = int(chan)

        # E36200 series (E36233A): Both channels are 30V/20A (200W each, 400W total)
        if self._is_e36200_series():
            return {'voltage_max': 30.0, 'current_max': 20.0}

        # E36300 series - limits vary by model and channel
        if "E36312A" in self._model:
            # E36312A: CH1=6V/5A, CH2=25V/1A, CH3=25V/1A
            if chan_num == 1:
                return {'voltage_max': 6.0, 'current_max': 5.0}
            else:  # CH2 and CH3
                return {'voltage_max': 25.0, 'current_max': 1.0}
        elif "E36313A" in self._model:
            # E36313A: CH1=6V/10A, CH2=25V/2A, CH3=25V/2A (negative)
            if chan_num == 1:
                return {'voltage_max': 6.0, 'current_max': 10.0}
            else:  # CH2 and CH3
                return {'voltage_max': 25.0, 'current_max': 2.0}
        elif "E36311A" in self._model:
            # E36311A: CH1=6V/5A, CH2=25V/1A, CH3=25V/1A
            if chan_num == 1:
                return {'voltage_max': 6.0, 'current_max': 5.0}
            else:  # CH2 and CH3
                return {'voltage_max': 25.0, 'current_max': 1.0}

        # Default fallback for unknown E36300 models
        if chan_num == 1:
            return {'voltage_max': 6.0, 'current_max': 5.0}
        else:
            return {'voltage_max': 25.0, 'current_max': 1.0}

    def get_overcurrent_protection_value(self, channel=None) -> float:
        """Get OCP limit."""
        chan = channel if channel is not None else self.chan
        return self.get_overcurrent_protection(chan)

    def get_overvoltage_protection_value(self, channel=None) -> float:
        """Get OVP limit."""
        chan = channel if channel is not None else self.chan
        return self.get_overvoltage_protection(chan)

    def overcurrent_protection_is_tripped(self, channel=None) -> bool:
        """Check if OCP is tripped (TUI-compatible wrapper)."""
        return self.is_overcurrent_tripped()

    def overvoltage_protection_is_tripped(self, channel=None) -> bool:
        """Check if OVP is tripped (TUI-compatible wrapper)."""
        return self.is_overvoltage_tripped()

    def set_overcurrent_protection_value(self, value, channel=None):
        """Set OCP threshold value (TUI-compatible wrapper)."""
        chan = channel if channel is not None else self.chan
        self.set_overcurrent_protection(value, chan)

    def enable_overcurrent_protection_channel(self, channel=None):
        """Enable OCP for channel (TUI-compatible wrapper)."""
        chan = channel if channel is not None else self.chan
        self.enable_overcurrent_protection(chan)

    def set_overvoltage_protection_value(self, value, channel=None):
        """Set OVP threshold value (TUI-compatible wrapper)."""
        chan = channel if channel is not None else self.chan
        self.set_overvoltage_protection(value, chan)

    def enable_overvoltage_protection_channel(self, channel=None):
        """Enable OVP for channel (TUI-compatible wrapper)."""
        chan = channel if channel is not None else self.chan
        self.enable_overvoltage_protection(chan)

    def clear_overcurrent_protection_trip(self, channel=None):
        """Clear OCP trip (TUI-compatible wrapper)."""
        chan = channel if channel is not None else self.chan
        self.clear_overcurrent_protection(chan)

    def clear_overvoltage_protection_trip(self, channel=None):
        """Clear OVP trip (TUI-compatible wrapper)."""
        chan = channel if channel is not None else self.chan
        self.clear_overvoltage_protection(chan)

    def __str__(self) -> str:
        """Return the identification string of the instrument."""
        try:
            return self.get_identification().strip()
        except Exception:
            return f"Keysight {self._model or 'E36xxx'}"

    def close(self) -> None:
        """Close the VISA connection and release resources."""
        if hasattr(self, 'instr') and self.instr is not None:
            try:
                underlying = self.instr.instr if hasattr(self.instr, 'instr') else self.instr
                # Remove from resource cache before closing to prevent dangling entries
                addrs_to_remove = [
                    addr for addr, (res, _rm) in _resource_cache.items()
                    if res is underlying
                ]
                for addr in addrs_to_remove:
                    del _resource_cache[addr]
                # Close the resource
                if hasattr(underlying, 'close'):
                    underlying.close()
            except Exception:
                pass
            finally:
                self.instr = None

    def __del__(self) -> None:
        """Cleanup when instance is garbage collected."""
        self.close()


def create_device(net_info):
    """Factory function for hardware_service.

    Extracts the required parameters from net_info dict and creates a KeysightE36000 instance.
    This allows hardware_service to instantiate the device without knowing the constructor signature.
    """
    address = net_info.get('address')
    channel = net_info.get('channel') or net_info.get('pin') or 1
    return KeysightE36000(address=address, channel=int(channel))
