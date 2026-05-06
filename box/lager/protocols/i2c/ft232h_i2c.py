# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
FTDI FT232H I2C driver implementing the I2CBase interface.

Provides I2C communication via the FT232H USB-to-MPSSE adapter
(VID:PID 0403:6014) using the pyftdi library.

The FT232H has fixed MPSSE pin assignments for I2C:
- AD0: SCL (clock)
- AD1 + AD2: SDA (data, directly bridged for open-drain)

Supports:
- Standard (100kHz), Fast (400kHz), and Fast-mode Plus (~1MHz) I2C
- Up to 64 KB per transaction (pyftdi limitation)
- Bus scan with NACK detection
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
        sys.stderr.write(f"I2C_DEBUG(FT232H): {msg}\n")
        sys.stderr.flush()


# Maximum retries for USB disconnect recovery
MAX_USB_RETRIES = 3


class FT232HI2C(I2CBase):
    """
    FTDI FT232H I2C implementation using pyftdi.

    Uses the FT232H's MPSSE engine for I2C communication.
    The FT232H has fixed hardware I2C pins so no pin configuration is needed.
    """

    def __init__(
        self,
        serial: Optional[str] = None,
        frequency_hz: int = 100_000,
    ):
        """
        Initialize FT232H I2C driver.

        Args:
            serial: USB serial number string (for multi-device setups).
                    If None, uses the first available FT232H.
            frequency_hz: I2C clock frequency in Hz (default 100kHz).
        """
        self._serial = serial
        self._frequency_hz = frequency_hz
        self._controller = None
        self._ports = {}  # Cache of I2cPort objects keyed by address

        _debug(f"FT232HI2C initialized: serial={serial}, freq={frequency_hz}Hz")

    @staticmethod
    def _get_pyftdi():
        """Lazy-import the pyftdi I2C module."""
        try:
            from pyftdi.i2c import I2cController, I2cNackError
            return I2cController, I2cNackError
        except ImportError:
            raise I2CBackendError(
                "pyftdi library not available. "
                "Install with: pip install pyftdi"
            )

    def _build_url(self) -> str:
        """Build the pyftdi device URL."""
        if self._serial:
            return f"ftdi://ftdi:232h:{self._serial}/1"
        return "ftdi://ftdi:232h/1"

    def _ensure_open(self):
        """
        Open the FT232H I2C controller if not already open.

        Returns the I2cController instance.
        """
        if self._controller is not None:
            return self._controller

        I2cController, _ = self._get_pyftdi()

        url = self._build_url()
        _debug(f"Opening FT232H at URL: {url}")

        try:
            ctrl = I2cController()
            ctrl.configure(url, frequency=self._frequency_hz)
        except Exception as exc:
            raise I2CBackendError(
                f"Failed to open FT232H I2C at {url}: {exc}"
            ) from exc

        self._controller = ctrl
        self._ports = {}
        atexit.register(self._close)
        _debug(f"FT232H I2C opened, freq={self._frequency_hz}Hz")

        return self._controller

    def _get_port(self, address: int):
        """
        Get or create an I2cPort for the given slave address.

        Args:
            address: 7-bit I2C slave address.

        Returns:
            I2cPort bound to the address.
        """
        if address not in self._ports:
            ctrl = self._ensure_open()
            self._ports[address] = ctrl.get_port(address)
        return self._ports[address]

    def _close(self):
        """Close the FT232H I2C controller."""
        if self._controller is not None:
            try:
                self._controller.close()
                _debug("FT232H I2C closed")
            except Exception as e:
                _debug(f"Error closing FT232H I2C: {e}")
            finally:
                self._controller = None
                self._ports = {}

    def _reconnect(self):
        """
        Close and reopen the FT232H for USB disconnect recovery.
        """
        _debug("Attempting reconnect after USB error...")
        self._controller = None
        self._ports = {}
        self._ensure_open()

    def config(
        self,
        frequency_hz: int = 100_000,
        pull_ups: Optional[bool] = None,
    ) -> None:
        """
        Configure I2C bus parameters.

        Args:
            frequency_hz: I2C clock frequency in Hz.
            pull_ups: Ignored - FT232H has no internal pull-ups.
                      External pull-ups are required on SDA and SCL.
        """
        if frequency_hz is not None and frequency_hz <= 0:
            raise I2CBackendError(
                f"Invalid I2C frequency: {frequency_hz}Hz. "
                f"Must be a positive value (e.g., 100000 for 100kHz)."
            )

        if pull_ups is not None:
            _debug("FT232H has no internal I2C pull-ups; "
                   "external pull-ups are required")

        self._frequency_hz = frequency_hz

        # Reconfigure if already open
        if self._controller is not None:
            self._close()
            self._ensure_open()

        _debug(f"I2C configured: freq={frequency_hz}Hz")

    def scan(
        self,
        start_addr: int = 0x08,
        end_addr: int = 0x77,
    ) -> List[int]:
        """
        Scan I2C bus for connected devices.

        Probes each address with a 0-byte read. Devices that ACK are
        reported as found; NACK raises I2cNackError which is caught.

        Args:
            start_addr: First 7-bit address to probe.
            end_addr: Last 7-bit address to probe.

        Returns:
            List of 7-bit addresses that responded with ACK.
        """
        self._validate_address(start_addr)
        self._validate_address(end_addr)
        _, I2cNackError = self._get_pyftdi()
        ctrl = self._ensure_open()
        found = []

        for addr in range(start_addr, end_addr + 1):
            try:
                port = ctrl.get_port(addr)
                port.read(0)
                found.append(addr)
                _debug(f"Device found at 0x{addr:02x}")
            except I2cNackError:
                pass
            except Exception as exc:
                _debug(f"Error probing 0x{addr:02x}: {exc}")

        return found

    def read(
        self,
        address: int,
        num_bytes: int,
    ) -> List[int]:
        """
        Read bytes from an I2C device.

        Args:
            address: 7-bit I2C device address.
            num_bytes: Number of bytes to read.

        Returns:
            List of received bytes.
        """
        self._validate_address(address)
        _, I2cNackError = self._get_pyftdi()

        last_error = None
        for attempt in range(MAX_USB_RETRIES):
            try:
                port = self._get_port(address)
                data = port.read(num_bytes)
                rx_bytes = list(data)
                _debug(f"Read from 0x{address:02x}: {[hex(b) for b in rx_bytes]}")
                return rx_bytes
            except I2cNackError:
                raise I2CBackendError(
                    f"I2C read from 0x{address:02x}: no ACK received "
                    f"(device not responding)"
                )
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
            address: 7-bit I2C device address.
            data: List of bytes to write.
        """
        self._validate_address(address)
        _, I2cNackError = self._get_pyftdi()

        last_error = None
        for attempt in range(MAX_USB_RETRIES):
            try:
                port = self._get_port(address)
                port.write(bytes(data))
                _debug(f"Wrote to 0x{address:02x}: {[hex(b) for b in data]}")
                return
            except I2cNackError:
                raise I2CBackendError(
                    f"I2C write to 0x{address:02x}: no ACK received "
                    f"(device not responding)"
                )
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

        Uses pyftdi's I2cPort.exchange() which performs a write followed
        by a repeated-start read in one MPSSE sequence.

        Args:
            address: 7-bit I2C device address.
            data: List of bytes to write before reading.
            num_bytes: Number of bytes to read after writing.

        Returns:
            List of received bytes.
        """
        self._validate_address(address)
        _, I2cNackError = self._get_pyftdi()

        last_error = None
        for attempt in range(MAX_USB_RETRIES):
            try:
                port = self._get_port(address)
                rx_data = port.exchange(bytes(data), num_bytes)
                rx_bytes = list(rx_data)
                _debug(f"Write/Read 0x{address:02x}: "
                       f"TX={[hex(b) for b in data]}, "
                       f"RX={[hex(b) for b in rx_bytes]}")
                return rx_bytes
            except I2cNackError:
                raise I2CBackendError(
                    f"I2C write_read to 0x{address:02x}: no ACK received "
                    f"(device not responding)"
                )
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
