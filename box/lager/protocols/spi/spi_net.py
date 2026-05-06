# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import List, Dict, Any, Optional

from lager.exceptions import SPIBackendError


class SPINet:
    """
    Represents an SPI net configuration.

    Provides clean object-oriented API for SPI communication from Python scripts.
    Wraps the dispatcher functions to provide instance methods that don't require
    passing the netname on every call.

    Attributes:
        name: Net name from saved_nets.json
        params: SPI configuration parameters from net config
    """

    def __init__(self, name: str, net_config: dict):
        """
        Initialize SPI net wrapper.

        Args:
            name: Net name
            net_config: Net configuration dict from saved_nets.json
        """
        self.name = name
        self._config = net_config
        self.params = net_config.get("params", {})

        # Cache the driver instance (lazy loaded)
        self._driver = None

    def _get_driver(self, overrides: Optional[Dict[str, Any]] = None):
        """
        Get or create the SPI driver instance.

        Args:
            overrides: Optional configuration overrides

        Returns:
            LabJackSPI driver instance
        """
        from .dispatcher import _resolve_net_and_driver
        return _resolve_net_and_driver(self.name, overrides)

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
        retain their previously-stored values.

        Args:
            mode: SPI mode (0-3)
                - Mode 0: CPOL=0, CPHA=0 (clock idle low, sample on rising edge)
                - Mode 1: CPOL=0, CPHA=1 (clock idle low, sample on falling edge)
                - Mode 2: CPOL=1, CPHA=0 (clock idle high, sample on falling edge)
                - Mode 3: CPOL=1, CPHA=1 (clock idle high, sample on rising edge)
            bit_order: "msb" (most significant bit first) or "lsb" (least significant bit first)
            frequency_hz: Clock frequency in Hz (14 Hz to 800 kHz on LabJack T7)
            word_size: Bits per word (8, 16, or 32)
            cs_active: Chip select active level - "low" or "high"
            cs_mode: CS assertion mode - "auto" (hardware SS) or "manual" (user-managed GPIO)

        Example:
            spi.config(mode=0, frequency_hz=1_000_000)
            spi.config(mode=3, bit_order="lsb", word_size=16)
            spi.config(cs_mode="manual")  # User manages CS via separate GPIO
        """
        overrides = {}
        if mode is not None:
            overrides["mode"] = mode
        if bit_order is not None:
            overrides["bit_order"] = bit_order
        if frequency_hz is not None:
            overrides["frequency_hz"] = frequency_hz
        if word_size is not None:
            overrides["word_size"] = word_size
        if cs_active is not None:
            overrides["cs_active"] = cs_active
        if cs_mode is not None:
            overrides["cs_mode"] = cs_mode

        drv = self._get_driver(overrides if overrides else None)
        # Configuration is applied through overrides in _get_driver

    def read(
        self,
        n_words: int,
        fill: int = 0xFF,
        keep_cs: bool = False,
        output_format: str = "list",
    ) -> List[int]:
        """
        Read data from SPI slave.

        Sends fill bytes while reading response data.

        Args:
            n_words: Number of words to read
            fill: Fill byte/word to send while reading (default 0xFF)
            keep_cs: If True, keep CS asserted after transfer for multi-part transactions
            output_format: Output format - "list" (default), "hex", "bytes", or "json"

        Returns:
            List of received words (integers)

        Example:
            # Read 4 bytes from SPI device
            data = spi.read(n_words=4)
            print(data)  # [0x00, 0x01, 0x02, 0x03]

            # Read with custom fill byte
            data = spi.read(n_words=4, fill=0x00)
        """
        drv = self._get_driver()
        result = drv.read(n_words, fill=fill, keep_cs=keep_cs)

        if output_format == "list":
            return result
        return self._format_output(result, output_format)

    def read_write(
        self,
        data: List[int],
        keep_cs: bool = False,
        output_format: str = "list",
    ) -> List[int]:
        """
        Perform simultaneous read/write SPI transfer (full duplex).

        Sends data while simultaneously receiving response.

        Args:
            data: List of words to transmit
            keep_cs: If True, keep CS asserted after transfer
            output_format: Output format - "list" (default), "hex", "bytes", or "json"

        Returns:
            List of received words (same length as transmitted data)

        Example:
            # Send JEDEC Read ID command (0x9F) and read 3 response bytes
            response = spi.read_write([0x9F, 0x00, 0x00, 0x00])
            manufacturer_id = response[1]
            device_id = (response[2] << 8) | response[3]
        """
        drv = self._get_driver()
        result = drv.read_write(data, keep_cs=keep_cs)

        if output_format == "list":
            return result
        return self._format_output(result, output_format)

    def transfer(
        self,
        n_words: int,
        data: Optional[List[int]] = None,
        fill: int = 0xFF,
        keep_cs: bool = False,
        output_format: str = "list",
    ) -> List[int]:
        """
        Perform SPI transfer with automatic padding/truncation.

        This is the main transfer function that handles variable-length data:
        - If data is shorter than n_words, pad with fill value
        - If data is longer than n_words, truncate

        Args:
            n_words: Number of words to transfer
            data: Optional list of words to transmit (will be padded/truncated)
            fill: Fill byte/word for padding (default 0xFF)
            keep_cs: If True, keep CS asserted after transfer
            output_format: Output format - "list" (default), "hex", "bytes", or "json"

        Returns:
            List of received words

        Example:
            # Send 1-byte command, read 3 response bytes (4 total)
            response = spi.transfer(n_words=4, data=[0x9F])
            # data [0x9F] is padded to [0x9F, 0xFF, 0xFF, 0xFF]
        """
        # Prepare data with padding/truncation
        if data is None:
            data = []

        if len(data) < n_words:
            data = data + [fill] * (n_words - len(data))
        elif len(data) > n_words:
            data = data[:n_words]

        drv = self._get_driver()
        result = drv.read_write(data, keep_cs=keep_cs)

        if output_format == "list":
            return result
        return self._format_output(result, output_format)

    def write(
        self,
        data: List[int],
        keep_cs: bool = False,
    ) -> None:
        """
        Write data to SPI slave (discards response).

        This is a convenience method for write-only operations where you
        don't need the response data.

        Args:
            data: List of words to transmit
            keep_cs: If True, keep CS asserted after transfer

        Example:
            # Send Write Enable command to SPI flash
            spi.write([0x06])

            # Send Page Program command with address and data
            spi.write([0x02, 0x00, 0x00, 0x00, 0xDE, 0xAD, 0xBE, 0xEF])
        """
        drv = self._get_driver()
        drv.read_write(data, keep_cs=keep_cs)
        # Discard response

    def _format_output(self, data: List[int], fmt: str) -> Any:
        """
        Format output data according to the requested format.

        Args:
            data: List of words to format
            fmt: Output format - "hex", "bytes", or "json"

        Returns:
            Formatted output (string or dict depending on format)
        """
        import json

        word_size = self.params.get("word_size", 8)

        if fmt == "json":
            return {"data": data}
        elif fmt == "bytes":
            return " ".join(str(w) for w in data)
        else:  # hex
            if word_size == 8:
                return " ".join(f"{w:02x}" for w in data)
            elif word_size == 16:
                return " ".join(f"{w:04x}" for w in data)
            else:
                return " ".join(f"{w:08x}" for w in data)

    def get_config(self) -> dict:
        """Get the raw net configuration dict."""
        return self._config.copy()

    def __str__(self):
        return f'<SPINet name="{self.name}">'

    def __repr__(self):
        return str(self)
