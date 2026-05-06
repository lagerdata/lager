# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Total Phase Aardvark I2C driver implementing the I2CBase interface.

Provides I2C communication via the Aardvark I2C/SPI Host Adapter
(TP2404141, USB VID:PID 0403:e0d0).

The Aardvark has fixed hardware I2C pins (SDA/SCL) and supports:
- Standard (100kHz) and Fast (400kHz) I2C modes
- Internal pull-ups on SDA and SCL
- Up to 65535 bytes per transaction
- Bus scan with free-bus recovery
"""
from __future__ import annotations

import atexit
import os
import sys
import time
from typing import List, Optional

from .i2c_base import I2CBase
from lager.exceptions import I2CBackendError

DEBUG = bool(os.environ.get("LAGER_I2C_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_I2C_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"I2C_DEBUG(Aardvark): {msg}\n")
        sys.stderr.flush()


# Maximum bytes per Aardvark I2C transaction
MAX_BYTES_PER_TRANSACTION = 65535

# Maximum retries for USB disconnect recovery
MAX_USB_RETRIES = 3

# Aardvark configuration constants
_AA_CONFIG_I2C_GPIO = 0x02  # Enable I2C + GPIO subsystems
_AA_I2C_NO_FLAGS = 0x00
_AA_I2C_NO_STOP = 0x04  # Don't send STOP (for repeated start)
_AA_I2C_PULLUP_BOTH = 0x03  # Pull-ups on both SDA and SCL
_AA_I2C_PULLUP_NONE = 0x00


class AardvarkI2C(I2CBase):
    """
    Total Phase Aardvark I2C implementation.

    Uses the Aardvark I2C/SPI Host Adapter's built-in I2C functionality.
    The Aardvark has fixed hardware pins so no pin configuration is needed.
    """

    def __init__(
        self,
        port: int = 0,
        serial: Optional[str] = None,
        frequency_hz: int = 100_000,
        pull_ups: bool = False,
        target_power: bool = False,
    ):
        self._port = port
        self._serial = serial
        self._handle = None
        self._frequency_hz = frequency_hz
        self._pull_ups = pull_ups
        self._target_power = target_power

        _debug(f"AardvarkI2C initialized: port={port}, serial={serial}, "
               f"freq={frequency_hz}Hz, pull_ups={pull_ups}, "
               f"target_power={target_power}")

    def _get_aa(self):
        """Lazy-import the aardvark_py module."""
        try:
            import aardvark_py
            return aardvark_py
        except ImportError:
            raise I2CBackendError(
                "aardvark_py library not available. "
                "Install with: pip install aardvark_py"
            )

    def _ensure_open(self) -> int:
        """
        Open the Aardvark device if not already open.

        Returns the device handle.
        """
        if self._handle is not None:
            return self._handle

        aa = self._get_aa()

        handle = aa.aa_open(self._port)
        if handle < 0:
            raise I2CBackendError(
                f"Failed to open Aardvark on port {self._port}: error {handle}"
            )

        # Configure as I2C master
        result = aa.aa_configure(handle, _AA_CONFIG_I2C_GPIO)
        if result < 0:
            aa.aa_close(handle)
            raise I2CBackendError(
                f"Failed to configure Aardvark as I2C master: error {result}"
            )

        self._handle = handle
        atexit.register(self._close)
        _debug(f"Aardvark opened on port {self._port}, handle={handle}")

        # Enable target power if requested (Aardvark supplies 5V to target)
        if self._target_power:
            tp_fn = getattr(aa, "aa_target_power", None)
            if tp_fn is not None:
                # aa_target_power(handle, AA_TARGET_POWER_BOTH)
                # AA_TARGET_POWER_NONE=0x00, AA_TARGET_POWER_BOTH=0x03
                result = tp_fn(handle, 0x03)
                if result < 0:
                    _debug(f"Target power enable returned error {result}")
                else:
                    _debug("Target power enabled (5V)")
            else:
                _debug("aa_target_power not available in aardvark_py")

        # Apply current I2C configuration
        self._apply_config()

        # Prime the I2C bus after initial configuration.
        # The Aardvark can return bus errors (status=1) on the first
        # I2C transaction after opening. Performing a dummy 0-byte
        # read followed by aa_i2c_free_bus() settles the bus.
        try:
            dummy = aa.array_u08(1)
            aa.aa_i2c_read(handle, 0x00, _AA_I2C_NO_FLAGS, dummy)
        except Exception:
            pass
        try:
            aa.aa_i2c_free_bus(handle)
        except Exception:
            pass

        return self._handle

    def _close(self):
        """Close the Aardvark device."""
        if self._handle is not None:
            try:
                aa = self._get_aa()
                aa.aa_close(self._handle)
                _debug(f"Aardvark closed, handle={self._handle}")
            except Exception as e:
                _debug(f"Error closing Aardvark: {e}")
            finally:
                self._handle = None

    def _apply_config(self):
        """Apply current I2C configuration to the open device."""
        if self._handle is None:
            return

        aa = self._get_aa()

        # Set bitrate (aardvark_py uses kHz)
        bitrate_khz = max(1, self._frequency_hz // 1000)
        actual_khz = aa.aa_i2c_bitrate(self._handle, bitrate_khz)
        if actual_khz < 0:
            raise I2CBackendError(
                f"Failed to set I2C bitrate: error {actual_khz}"
            )
        _debug(f"I2C bitrate: requested {bitrate_khz}kHz, "
               f"actual {actual_khz}kHz")

        # Set pull-ups
        pullup_mask = (
            _AA_I2C_PULLUP_BOTH if self._pull_ups
            else _AA_I2C_PULLUP_NONE
        )
        result = aa.aa_i2c_pullup(self._handle, pullup_mask)
        if result < 0:
            _debug(f"Pull-up set returned error {result}")
        _debug(f"I2C pull-ups: {'enabled' if self._pull_ups else 'disabled'}")

    def _reconnect(self) -> int:
        """
        Close and reopen the Aardvark device for USB disconnect recovery.

        Returns the new device handle.
        """
        _debug("Attempting reconnect after USB error...")
        self._handle = None  # Clear without closing (device may already be gone)
        return self._ensure_open()

    def config(
        self,
        frequency_hz: int = 100_000,
        pull_ups: Optional[bool] = None,
    ) -> None:
        """
        Configure I2C bus parameters.

        Args:
            frequency_hz: I2C clock frequency in Hz
            pull_ups: Enable/disable internal pull-ups on SDA and SCL
        """
        self._frequency_hz = frequency_hz
        if pull_ups is not None:
            self._pull_ups = pull_ups

        # Apply to hardware if device is open
        if self._handle is not None:
            self._apply_config()

        _debug(f"I2C configured: freq={frequency_hz}Hz, "
               f"pull_ups={self._pull_ups}")

    def scan(
        self,
        start_addr: int = 0x08,
        end_addr: int = 0x77,
    ) -> List[int]:
        """
        Scan I2C bus for connected devices.

        Attempts a 1-byte read from each address. Devices that ACK
        are reported as found. Calls aa_i2c_free_bus() after each
        probe to recover from stuck bus conditions.

        Args:
            start_addr: First 7-bit address to probe
            end_addr: Last 7-bit address to probe

        Returns:
            List of 7-bit addresses that responded with ACK
        """
        self._validate_address(start_addr)
        self._validate_address(end_addr)
        aa = self._get_aa()
        handle = self._ensure_open()
        found = []

        for addr in range(start_addr, end_addr + 1):
            try:
                rx_array = aa.array_u08(1)
                result = aa.aa_i2c_read(
                    handle, addr, _AA_I2C_NO_FLAGS, rx_array
                )

                # result is (count, data) tuple or just count
                # count > 0 means bytes were read (ACK from device)
                # count == 0 means NACK (no device at this address)
                # count < 0 means bus error
                if isinstance(result, tuple):
                    count = result[0]
                else:
                    count = result

                if count > 0:
                    found.append(addr)
                    _debug(f"Device found at 0x{addr:02x}")
            except Exception:
                pass

            # Free the bus after each probe to avoid stuck conditions
            try:
                aa.aa_i2c_free_bus(handle)
            except Exception:
                pass

        return found

    def read(
        self,
        address: int,
        num_bytes: int,
    ) -> List[int]:
        """
        Read bytes from an I2C device.

        Args:
            address: 7-bit I2C device address
            num_bytes: Number of bytes to read

        Returns:
            List of received bytes
        """
        self._validate_address(address)
        if num_bytes > MAX_BYTES_PER_TRANSACTION:
            raise I2CBackendError(
                f"Read size {num_bytes} exceeds maximum of "
                f"{MAX_BYTES_PER_TRANSACTION} bytes."
            )

        last_error = None
        for attempt in range(MAX_USB_RETRIES):
            try:
                aa = self._get_aa()
                handle = self._ensure_open()

                rx_array = aa.array_u08(num_bytes)
                result = aa.aa_i2c_read(
                    handle, address, _AA_I2C_NO_FLAGS, rx_array
                )

                if isinstance(result, tuple):
                    count, rx_data = result
                    if count < 0:
                        raise I2CBackendError(
                            f"I2C read from 0x{address:02x} failed: error {count}"
                        )
                else:
                    if result < 0:
                        raise I2CBackendError(
                            f"I2C read from 0x{address:02x} failed: error {result}"
                        )
                    rx_data = rx_array
                    count = result

                if count == 0 and num_bytes > 0:
                    raise I2CBackendError(
                        f"I2C read from 0x{address:02x}: no ACK received "
                        f"(device not responding)"
                    )

                rx_bytes = [rx_data[i] for i in range(num_bytes)]
                _debug(f"Read from 0x{address:02x}: {[hex(b) for b in rx_bytes]}")
                return rx_bytes
            except I2CBackendError:
                raise
            except Exception as e:
                last_error = e
                _debug(f"USB error on read attempt {attempt + 1}/{MAX_USB_RETRIES}: {e}")
                if attempt < MAX_USB_RETRIES - 1:
                    time.sleep(1.0 * (2 ** attempt))
                    self._reconnect()
                    continue
                raise I2CBackendError(
                    f"I2C read from 0x{address:02x} failed after "
                    f"{MAX_USB_RETRIES} attempts: {last_error}"
                )

    def write(
        self,
        address: int,
        data: List[int],
    ) -> None:
        """
        Write bytes to an I2C device.

        Args:
            address: 7-bit I2C device address
            data: List of bytes to write
        """
        self._validate_address(address)
        num_bytes = len(data)
        if num_bytes > MAX_BYTES_PER_TRANSACTION:
            raise I2CBackendError(
                f"Write size {num_bytes} exceeds maximum of "
                f"{MAX_BYTES_PER_TRANSACTION} bytes."
            )

        last_error = None
        for attempt in range(MAX_USB_RETRIES):
            try:
                aa = self._get_aa()
                handle = self._ensure_open()

                tx_array = aa.array_u08(num_bytes)
                for i, b in enumerate(data):
                    tx_array[i] = b

                # Use aa_i2c_write_read() with a 0-byte read instead of
                # aa_i2c_write(). The write-only API returns 0 for success
                # regardless of ACK/NACK, so NACKs go undetected.
                # write_read returns a 4-tuple with proper NACK detection.
                rx_dummy = aa.array_u08(0)
                result = aa.aa_i2c_write_read(
                    handle, address, _AA_I2C_NO_FLAGS, tx_array, rx_dummy
                )

                status = result[0]
                num_written = result[1]

                if status < 0:
                    raise I2CBackendError(
                        f"I2C write to 0x{address:02x} failed: error {status}"
                    )

                if num_written == 0 and num_bytes > 0:
                    raise I2CBackendError(
                        f"I2C write to 0x{address:02x}: no ACK received "
                        f"(device not responding)"
                    )

                _debug(f"Wrote to 0x{address:02x}: {[hex(b) for b in data]}")
                return
            except I2CBackendError:
                raise
            except Exception as e:
                last_error = e
                _debug(f"USB error on write attempt {attempt + 1}/{MAX_USB_RETRIES}: {e}")
                if attempt < MAX_USB_RETRIES - 1:
                    time.sleep(1.0 * (2 ** attempt))
                    self._reconnect()
                    continue
                raise I2CBackendError(
                    f"I2C write to 0x{address:02x} failed after "
                    f"{MAX_USB_RETRIES} attempts: {last_error}"
                )

    def write_read(
        self,
        address: int,
        data: List[int],
        num_bytes: int,
    ) -> List[int]:
        """
        Write then read in a single I2C transaction (repeated start).

        Uses aa_i2c_write_read() which handles the repeated start
        internally. Returns a 4-tuple: (status, num_written, rx_data, num_read).

        Args:
            address: 7-bit I2C device address
            data: List of bytes to write before reading
            num_bytes: Number of bytes to read after writing

        Returns:
            List of received bytes
        """
        self._validate_address(address)
        num_tx = len(data)
        if num_tx > MAX_BYTES_PER_TRANSACTION:
            raise I2CBackendError(
                f"Write size {num_tx} exceeds maximum of "
                f"{MAX_BYTES_PER_TRANSACTION} bytes."
            )
        if num_bytes > MAX_BYTES_PER_TRANSACTION:
            raise I2CBackendError(
                f"Read size {num_bytes} exceeds maximum of "
                f"{MAX_BYTES_PER_TRANSACTION} bytes."
            )

        last_error = None
        for attempt in range(MAX_USB_RETRIES):
            try:
                aa = self._get_aa()
                handle = self._ensure_open()

                tx_array = aa.array_u08(num_tx)
                for i, b in enumerate(data):
                    tx_array[i] = b

                rx_array = aa.array_u08(num_bytes)

                result = aa.aa_i2c_write_read(
                    handle, address, _AA_I2C_NO_FLAGS, tx_array, rx_array
                )

                # aa_i2c_write_read returns (status, num_written, rx_data, num_read)
                status = result[0]
                num_written = result[1]
                rx_data = result[2]
                num_read = result[3]

                if status < 0:
                    raise I2CBackendError(
                        f"I2C write_read to 0x{address:02x} failed: error {status}"
                    )

                if num_read == 0 and num_bytes > 0:
                    if num_written > 0:
                        raise I2CBackendError(
                            f"I2C write_read to 0x{address:02x}: write phase "
                            f"succeeded but read phase returned no data "
                            f"(bus may need recovery)"
                        )
                    raise I2CBackendError(
                        f"I2C write_read to 0x{address:02x}: no ACK received "
                        f"(device not responding)"
                    )

                _debug(f"Write/Read 0x{address:02x}: "
                       f"TX={[hex(b) for b in data]}, "
                       f"written={num_written}, read={num_read}, "
                       f"RX={[hex(rx_data[i]) for i in range(num_read)]}")

                rx_bytes = [rx_data[i] for i in range(num_bytes)]
                return rx_bytes
            except I2CBackendError:
                raise
            except Exception as e:
                last_error = e
                _debug(f"USB error on write_read attempt {attempt + 1}/{MAX_USB_RETRIES}: {e}")
                if attempt < MAX_USB_RETRIES - 1:
                    time.sleep(1.0 * (2 ** attempt))
                    self._reconnect()
                    continue
                raise I2CBackendError(
                    f"I2C write_read to 0x{address:02x} failed after "
                    f"{MAX_USB_RETRIES} attempts: {last_error}"
                )

    def __del__(self):
        """Clean up: close device on garbage collection."""
        self._close()
