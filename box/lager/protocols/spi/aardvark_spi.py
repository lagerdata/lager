# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Total Phase Aardvark SPI driver implementing the SPIBase interface.

Uses GPIO bit-bang SPI via the Aardvark I2C/SPI Host Adapter
(TP2404141, USB VID:PID 0403:e0d0).

The Aardvark's hardware SPI controller has a known defect where
aa_spi_write() clocks SCK but does not drive MOSI. This driver
bypasses the hardware SPI entirely, using GPIO mode to manually
toggle SCK, drive MOSI, read MISO, and optionally assert SS.

Supports:
- SPI modes 0-3
- MSB-first and LSB-first
- Up to 65535 bytes per transaction
- Configurable SS polarity and auto/manual CS modes
"""
from __future__ import annotations

import os
import sys
import time
from typing import List, Optional

from .spi_base import SPIBase
from lager.exceptions import SPIBackendError

DEBUG = bool(os.environ.get("LAGER_SPI_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_SPI_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"SPI_DEBUG(Aardvark): {msg}\n")
        sys.stderr.flush()


# Maximum bytes per Aardvark SPI transaction
MAX_BYTES_PER_TRANSACTION = 65535

# Maximum retries for USB disconnect recovery
MAX_USB_RETRIES = 3

# GPIO pin bitmasks for bit-bang SPI
_GPIO_SCK  = 0x01  # Pin 1 (SCL/SCK)
_GPIO_MOSI = 0x02  # Pin 3 (SDA/MOSI)
_GPIO_MISO = 0x04  # Pin 5 (MISO)
_GPIO_SS   = 0x20  # Pin 9 (SS)


class AardvarkSPI(SPIBase):
    """
    Total Phase Aardvark SPI implementation using GPIO bit-bang.

    Bypasses the Aardvark's built-in SPI controller (which has a MOSI
    defect) and instead uses GPIO mode to manually clock data in and out.
    """

    def __init__(
        self,
        port: int = 0,
        serial: Optional[str] = None,
        mode: int = 0,
        bit_order: str = "msb",
        frequency_hz: int = 1_000_000,
        word_size: int = 8,
        cs_active: str = "low",
        target_power: bool = False,
        cs_mode: str = "auto",
    ):
        """
        Initialize Aardvark SPI driver.

        Args:
            port: Aardvark port number (default 0 for first device)
            serial: Optional serial number string for multi-device setups
            mode: SPI mode (0-3)
            bit_order: "msb" or "lsb"
            frequency_hz: Clock frequency in Hz (ignored for bit-bang; each
                          GPIO USB round-trip is ~0.3ms)
            word_size: Bits per word (8, 16, or 32)
            cs_active: "low" or "high"
            target_power: Enable Aardvark's 5V target power output
            cs_mode: "auto" (hardware SS pin 9) or "manual" (user-managed GPIO)
        """
        self._port = port
        self._serial = serial
        self._handle = None
        self._target_power = target_power

        # Configuration parameters
        self._mode = mode
        self._bit_order = bit_order.lower()
        self._frequency_hz = frequency_hz
        self._word_size = word_size
        self._cs_active = cs_active.lower()
        self._cs_mode = cs_mode.lower()

        # Validate parameters
        if self._mode not in (0, 1, 2, 3):
            raise SPIBackendError(f"Invalid SPI mode {mode}. Must be 0, 1, 2, or 3.")
        if self._bit_order not in ("msb", "lsb"):
            raise SPIBackendError(f"Invalid bit order '{bit_order}'. Must be 'msb' or 'lsb'.")
        if self._word_size not in (8, 16, 32):
            raise SPIBackendError(f"Invalid word size {word_size}. Must be 8, 16, or 32.")
        if self._cs_active not in ("low", "high"):
            raise SPIBackendError(f"Invalid CS active '{cs_active}'. Must be 'low' or 'high'.")
        if self._cs_mode not in ("auto", "manual"):
            raise SPIBackendError(f"Invalid CS mode '{cs_mode}'. Must be 'auto' or 'manual'.")

        _debug(f"AardvarkSPI initialized: port={port}, serial={serial}, mode={mode}, "
               f"freq={frequency_hz}Hz, word_size={word_size}, "
               f"bit_order={bit_order}, cs_active={cs_active}, cs_mode={cs_mode}")

    def _get_aa(self):
        """Lazy-import the aardvark_py module."""
        try:
            import aardvark_py
            return aardvark_py
        except ImportError:
            raise SPIBackendError(
                "aardvark_py library not available. "
                "Install with: pip install aardvark_py"
            )

    def _idle_gpio_state(self) -> int:
        """Return the GPIO bitmask for the idle (no-transfer) state.

        SCK matches CPOL (LOW for modes 0/1, HIGH for modes 2/3).
        SS is deasserted (HIGH for active-low, LOW for active-high).
        MOSI is LOW.
        """
        state = 0
        # CPOL: modes 2 and 3 idle with clock HIGH
        if self._mode in (2, 3):
            state |= _GPIO_SCK
        # SS deasserted
        if self._cs_active == "low":
            state |= _GPIO_SS  # HIGH = deasserted for active-low
        # (for active-high, SS LOW = deasserted, so no bit set)
        return state

    def _ensure_open(self) -> int:
        """
        Open the Aardvark device in GPIO-only mode if not already open.

        Configures pin directions and sets the idle GPIO state.
        Returns the device handle.
        """
        if self._handle is not None:
            return self._handle

        aa = self._get_aa()

        port = self._port
        handle = aa.aa_open(port)
        if handle < 0:
            raise SPIBackendError(
                f"Failed to open Aardvark on port {port}: error {handle}"
            )

        # Configure as GPIO-only (bypass hardware SPI controller)
        result = aa.aa_configure(handle, aa.AA_CONFIG_GPIO_ONLY)
        if result < 0:
            aa.aa_close(handle)
            raise SPIBackendError(
                f"Failed to configure Aardvark in GPIO mode: error {result}"
            )

        # Preload the output register with idle state BEFORE setting
        # direction.  This minimises transient glitches on SCK/MOSI/SS
        # when the pins transition from input to output -- they take
        # the preloaded value immediately instead of defaulting to LOW.
        # Critical when an external CS is held low across processes.
        idle = self._idle_gpio_state()
        aa.aa_gpio_set(handle, idle)

        # Set pin directions: SCK, MOSI, SS as outputs; MISO as input
        outputs = _GPIO_SCK | _GPIO_MOSI | _GPIO_SS
        aa.aa_gpio_direction(handle, outputs)

        self._handle = handle

        # Confirm idle state (direction change is now glitch-free)
        aa.aa_gpio_set(handle, idle)

        _debug(f"Aardvark opened in GPIO mode on port {port}, handle={handle}")

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

        return self._handle

    def _close(self):
        """Close the Aardvark device.

        Sets GPIO to idle state before closing so that SCK is at the
        correct CPOL level when the pins transition to high-impedance.
        This prevents spurious clock edges if an external CS is held
        low across processes.
        """
        if self._handle is not None:
            try:
                aa = self._get_aa()
                # Drive idle state before releasing -- keeps SCK at CPOL
                # level so the transition to high-impedance is benign.
                try:
                    aa.aa_gpio_set(self._handle, self._idle_gpio_state())
                except Exception:
                    pass  # best-effort; device may already be gone
                aa.aa_close(self._handle)
                _debug(f"Aardvark closed, handle={self._handle}")
            except Exception as e:
                _debug(f"Error closing Aardvark: {e}")
            finally:
                self._handle = None

    def _apply_config(self):
        """Update GPIO idle state for current CPOL/CS setting."""
        if self._handle is None:
            return
        aa = self._get_aa()
        aa.aa_gpio_set(self._handle, self._idle_gpio_state())
        _debug(f"GPIO idle state updated for mode={self._mode}, cs_active={self._cs_active}")

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
        mode: int = None,
        bit_order: str = None,
        frequency_hz: int = None,
        word_size: int = None,
        cs_active: str = None,
        cs_mode: str = None,
    ) -> None:
        """
        Configure SPI parameters.

        Only explicitly-provided parameters are updated; omitted parameters
        retain their current values.

        Args:
            mode: SPI mode (0-3)
            bit_order: "msb" or "lsb"
            frequency_hz: Clock frequency in Hz (ignored for bit-bang)
            word_size: Bits per word (8, 16, or 32)
            cs_active: "low" or "high"
            cs_mode: "auto" (hardware SS) or "manual" (user-managed GPIO)
        """
        if mode is not None:
            if mode not in (0, 1, 2, 3):
                raise SPIBackendError(f"Invalid SPI mode {mode}. Must be 0, 1, 2, or 3.")
            self._mode = mode
        if bit_order is not None:
            if bit_order.lower() not in ("msb", "lsb"):
                raise SPIBackendError(f"Invalid bit order '{bit_order}'. Must be 'msb' or 'lsb'.")
            self._bit_order = bit_order.lower()
        if frequency_hz is not None:
            self._frequency_hz = frequency_hz
        if word_size is not None:
            if word_size not in (8, 16, 32):
                raise SPIBackendError(f"Invalid word size {word_size}. Must be 8, 16, or 32.")
            self._word_size = word_size
        if cs_active is not None:
            if cs_active.lower() not in ("low", "high"):
                raise SPIBackendError(f"Invalid CS active '{cs_active}'. Must be 'low' or 'high'.")
            self._cs_active = cs_active.lower()
        if cs_mode is not None:
            if cs_mode.lower() not in ("auto", "manual"):
                raise SPIBackendError(f"Invalid CS mode '{cs_mode}'. Must be 'auto' or 'manual'.")
            self._cs_mode = cs_mode.lower()

        # Update GPIO idle state if device is open
        if self._handle is not None:
            self._apply_config()

        _debug(f"SPI configured: mode={self._mode}, freq={self._frequency_hz}Hz, "
               f"word_size={self._word_size}, bit_order={self._bit_order}, "
               f"cs_active={self._cs_active}, cs_mode={self._cs_mode}")

    def _words_to_bytes(self, words: List[int]) -> List[int]:
        """
        Convert words to bytes based on word_size and bit_order.

        Bit order is handled in software for all word sizes since GPIO
        bit-bang always transmits MSB-first at the byte level.
        """
        max_value = (1 << self._word_size) - 1
        for w in words:
            if w > max_value:
                raise SPIBackendError(
                    f"Data value 0x{w:X} exceeds {self._word_size}-bit word size "
                    f"(max 0x{max_value:X}). Use commas to separate into "
                    f"{self._word_size}-bit values, or set --word-size to match "
                    f"your data."
                )

        if self._word_size == 8:
            if self._bit_order == "lsb":
                return [self.reverse_bits(w & 0xFF, 8) for w in words]
            return [w & 0xFF for w in words]

        bytes_per_word = self._word_size // 8
        result = []

        for word in words:
            if self._bit_order == "lsb":
                word = self.reverse_bits(word, self._word_size)

            # Split word into bytes (MSB first for transmission)
            for i in range(bytes_per_word - 1, -1, -1):
                result.append((word >> (i * 8)) & 0xFF)

        return result

    def _bytes_to_words(self, data_bytes: List[int]) -> List[int]:
        """
        Convert received bytes back to words based on word_size and bit_order.

        Bit order is handled in software for all word sizes.
        """
        if self._word_size == 8:
            if self._bit_order == "lsb":
                return [self.reverse_bits(b, 8) for b in data_bytes]
            return list(data_bytes)

        bytes_per_word = self._word_size // 8
        result = []

        for i in range(0, len(data_bytes), bytes_per_word):
            word = 0
            for j in range(bytes_per_word):
                if i + j < len(data_bytes):
                    word = (word << 8) | data_bytes[i + j]
            if self._bit_order == "lsb":
                word = self.reverse_bits(word, self._word_size)
            result.append(word)

        return result

    def _execute_transaction(
        self,
        tx_bytes: List[int],
        keep_cs: bool = False,
    ) -> List[int]:
        """
        Execute a GPIO bit-bang SPI transaction with retry logic.

        Clocks each bit manually via aa_gpio_set/aa_gpio_get. Each USB
        round-trip is ~0.3ms, which exceeds any SPI setup/hold time
        requirement, so no time.sleep() is needed.

        All four SPI modes are supported:
        - CPHA=0: data setup on idle edge, sample on first clock edge
        - CPHA=1: data setup on first clock edge, sample on second clock edge

        Args:
            tx_bytes: Bytes to transmit (MSB-first after word conversion)
            keep_cs: If True, keep CS asserted after transfer

        Returns:
            List of received bytes
        """
        num_bytes = len(tx_bytes)
        if num_bytes > MAX_BYTES_PER_TRANSACTION:
            raise SPIBackendError(
                f"Transaction size {num_bytes} exceeds maximum of "
                f"{MAX_BYTES_PER_TRANSACTION} bytes. Split into multiple transactions."
            )

        if keep_cs and self._cs_mode == "auto":
            sys.stderr.write(
                "Warning: Aardvark hardware does not support keep_cs in auto mode. "
                "Use cs_mode=manual with a GPIO net for manual CS control.\n"
            )
            sys.stderr.flush()

        last_error = None
        for attempt in range(MAX_USB_RETRIES):
            try:
                aa = self._get_aa()
                handle = self._ensure_open()

                _debug(f"TX bytes ({num_bytes}): "
                       f"{[hex(b) for b in tx_bytes[:32]]}"
                       f"{'...' if num_bytes > 32 else ''}")

                cpol = 1 if self._mode in (2, 3) else 0
                cpha = 1 if self._mode in (1, 3) else 0

                # Clock pin values for idle vs active states
                clk_idle = _GPIO_SCK if cpol else 0
                clk_active = 0 if cpol else _GPIO_SCK

                # Determine SS pin value during transfer
                if self._cs_active == "low":
                    ss_asserted = 0           # SS LOW = asserted
                    ss_deasserted = _GPIO_SS  # SS HIGH = deasserted
                else:
                    ss_asserted = _GPIO_SS    # SS HIGH = asserted
                    ss_deasserted = 0         # SS LOW = deasserted

                if self._cs_mode == "auto":
                    ss_val = ss_asserted
                else:
                    # Manual mode: keep SS deasserted (user controls CS externally)
                    ss_val = ss_deasserted

                # Assert CS before transfer (auto mode only)
                if self._cs_mode == "auto":
                    aa.aa_gpio_set(handle, clk_idle | ss_val)

                rx_bytes = []

                for tx_byte in tx_bytes:
                    rx_byte = 0

                    for bit in range(7, -1, -1):
                        mosi_val = _GPIO_MOSI if (tx_byte >> bit) & 1 else 0

                        if cpha == 0:
                            # CPHA=0: setup MOSI while clock idle,
                            #         sample MISO on first (active) edge

                            # Setup: drive MOSI, clock stays idle
                            aa.aa_gpio_set(handle, clk_idle | mosi_val | ss_val)

                            # First edge (idle -> active): sample MISO
                            aa.aa_gpio_set(handle, clk_active | mosi_val | ss_val)
                            pins = aa.aa_gpio_get(handle)
                            if pins & _GPIO_MISO:
                                rx_byte |= (1 << bit)

                            # Second edge (active -> idle)
                            aa.aa_gpio_set(handle, clk_idle | mosi_val | ss_val)
                        else:
                            # CPHA=1: setup MOSI on first edge,
                            #         sample MISO on second (idle) edge

                            # First edge (idle -> active): drive MOSI
                            aa.aa_gpio_set(handle, clk_active | mosi_val | ss_val)

                            # Second edge (active -> idle): sample MISO
                            aa.aa_gpio_set(handle, clk_idle | mosi_val | ss_val)
                            pins = aa.aa_gpio_get(handle)
                            if pins & _GPIO_MISO:
                                rx_byte |= (1 << bit)

                    rx_bytes.append(rx_byte)

                # Deassert CS after transfer (auto mode, unless keep_cs)
                if self._cs_mode == "auto" and not keep_cs:
                    aa.aa_gpio_set(handle, self._idle_gpio_state())

                _debug(f"RX bytes ({num_bytes}): "
                       f"{[hex(b) for b in rx_bytes[:32]]}"
                       f"{'...' if num_bytes > 32 else ''}")

                return rx_bytes
            except SPIBackendError:
                raise
            except Exception as e:
                last_error = e
                _debug(f"USB error on SPI attempt {attempt + 1}/{MAX_USB_RETRIES}: {e}")
                if attempt < MAX_USB_RETRIES - 1:
                    time.sleep(1.0 * (2 ** attempt))
                    self._reconnect()
                    continue
                raise SPIBackendError(
                    f"SPI transaction failed after "
                    f"{MAX_USB_RETRIES} attempts: {last_error}"
                )

    def read(
        self,
        n_words: int,
        fill: int = 0xFF,
        keep_cs: bool = False,
    ) -> List[int]:
        """
        Read data from SPI slave.

        Args:
            n_words: Number of words to read
            fill: Fill byte/word to send while reading
            keep_cs: If True, keep CS asserted after transfer

        Returns:
            List of received words
        """
        bytes_per_word = self._word_size // 8
        total_bytes = n_words * bytes_per_word
        if total_bytes > MAX_BYTES_PER_TRANSACTION:
            max_words = MAX_BYTES_PER_TRANSACTION // bytes_per_word
            raise SPIBackendError(
                f"Transaction size {n_words} words ({total_bytes} bytes) exceeds maximum of "
                f"{MAX_BYTES_PER_TRANSACTION} bytes. Use at most {max_words} words with "
                f"{self._word_size}-bit word size, or split into multiple transactions."
            )

        tx_words = [fill] * n_words
        return self.read_write(tx_words, keep_cs=keep_cs)

    def read_write(
        self,
        data: List[int],
        keep_cs: bool = False,
    ) -> List[int]:
        """
        Perform simultaneous read/write SPI transfer.

        Args:
            data: List of words to transmit
            keep_cs: If True, keep CS asserted after transfer

        Returns:
            List of received words
        """
        if not data:
            return []

        tx_bytes = self._words_to_bytes(data)

        if len(tx_bytes) > MAX_BYTES_PER_TRANSACTION:
            bytes_per_word = self._word_size // 8
            max_words = MAX_BYTES_PER_TRANSACTION // bytes_per_word
            raise SPIBackendError(
                f"Transaction size {len(data)} words ({len(tx_bytes)} bytes) exceeds maximum of "
                f"{MAX_BYTES_PER_TRANSACTION} bytes. Use at most {max_words} words with "
                f"{self._word_size}-bit word size, or split into multiple transactions."
            )

        rx_bytes = self._execute_transaction(tx_bytes, keep_cs=keep_cs)

        return self._bytes_to_words(rx_bytes)

    def __del__(self):
        """Clean up: close device on garbage collection."""
        self._close()
